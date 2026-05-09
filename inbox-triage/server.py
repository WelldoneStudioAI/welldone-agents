#!/usr/bin/env python3
"""
Inbox Triage Dashboard — server local.

Lance via :   python3 inbox-triage/server.py
Ouvre :       http://localhost:5050

Endpoints :
  GET  /              → index.html
  GET  /api/inbox     → JSON des emails HOT non snoozed
  POST /api/draft     → {uid, sender, subject, body, mode, gmail_thread_id?, rfc822_id?}
                        → génère draft Gemini + crée draft Gmail, renvoie URL
  POST /api/snooze    → {uid} → marque comme traité (cache 14j, fichier local)
"""
import os
import sys
import json
import base64
import imaplib
import email
import email.policy
import re
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from email.mime.text import MIMEText
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import Lock
import time as _time
import traceback

# ── Setup ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Charger .env vers os.environ pour que agents.email lise les vars
ENV_FILE = ROOT / ".env"
with open(ENV_FILE) as f:
    for line in f:
        line = line.rstrip("\n")
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            os.environ[k] = v

# Clé Gemini
GEMINI_KEY_FILE = Path.home() / ".gemini_api_key"
GEMINI_KEY = GEMINI_KEY_FILE.read_text().strip() if GEMINI_KEY_FILE.exists() else ""

# Importer la logique HOT du code existant
from agents.email import (
    _is_hot_email, _decode, _connect_account,
    _gmail_oauth_token, _gmail_api,
    HST_IMAP_HOST, HST_IMAP_PORT, HST_EMAIL, HST_PASS,
    EmailAgent,
)

# Cache snooze : emails déjà traités → ne plus afficher
SNOOZE_FILE = Path.home() / ".welldone" / "dashboard_snoozed.json"

def _load_snoozed() -> dict:
    try:
        if SNOOZE_FILE.exists():
            data = json.loads(SNOOZE_FILE.read_text())
            cutoff = (datetime.now() - timedelta(days=14)).isoformat()
            return {k: v for k, v in data.items() if v >= cutoff}
    except Exception:
        pass
    return {}

def _save_snoozed(snoozed: dict) -> None:
    SNOOZE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNOOZE_FILE.write_text(json.dumps(snoozed, indent=2))


# ── Helpers d'extraction ──────────────────────────────────────────────────────
def _extract_body_text(msg) -> str:
    """Body text/plain en priorité, sinon premier text/html stripé."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    pass
        # Fallback HTML stripé
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                        return re.sub(r"<[^>]+>", " ", html)
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            pass
    return ""


def _email_summary(msg, uid: str, account: str, gmail_thread_id: str = None) -> dict:
    from_raw = _decode(msg.get("From", ""))
    subject = _decode(msg.get("Subject", "(sans sujet)"))
    date = msg.get("Date", "")
    rfc822_id = msg.get("Message-ID", "").strip()

    m_match = re.search(r"<([^>]+)>", from_raw)
    sender = m_match.group(1).lower() if m_match else from_raw.strip().lower()

    body = _extract_body_text(msg)
    snippet = re.sub(r"\s+", " ", body).strip()[:240]

    return {
        "uid": f"{account}:{uid}",
        "account": account,
        "rfc822_id": rfc822_id,
        "from_raw": from_raw,
        "sender": sender,
        "subject": subject,
        "snippet": snippet,
        "body": body[:6000],  # cap pour Gemini context
        "date": date,
        "gmail_thread_id": gmail_thread_id,
    }


def _scan_hostinger() -> list[dict]:
    """Scan IMAP Hostinger INBOX 7j → liste des HOT."""
    M = _connect_account(HST_IMAP_HOST, HST_IMAP_PORT, HST_EMAIL, HST_PASS)
    if M is None:
        return []
    try:
        M.select("INBOX", readonly=True)
        # Fenêtre 3 jours pour le dashboard (vs 7j pour auto_trier qui a un cache UID)
        since = (datetime.now() - timedelta(days=3)).strftime("%d-%b-%Y")
        typ, data = M.uid("search", None, f"SINCE {since}")
        uids = data[0].split() if data and data[0] else []
        # Cap : 100 derniers UIDs max pour rapidité dashboard
        uids = uids[-100:]

        results = []
        for uid_b in uids:
            uid_s = uid_b.decode() if isinstance(uid_b, bytes) else str(uid_b)
            try:
                typ, msg_data = M.uid("fetch", uid_s, "(RFC822)")
                if not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg = email.message_from_bytes(msg_data[0][1])

                from_raw = _decode(msg.get("From", ""))
                m_match = re.search(r"<([^>]+)>", from_raw)
                sender = m_match.group(1).lower() if m_match else from_raw.strip().lower()

                is_hot, reason = _is_hot_email(msg, sender, EmailAgent._LEAD_DOMAINS)
                if not is_hot:
                    continue

                summary = _email_summary(msg, uid_s, "Hostinger")
                summary["reason"] = reason
                results.append(summary)
            except Exception:
                continue
        return results
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _scan_gmail() -> list[dict]:
    """Scan Gmail INBOX 3j via API → liste des HOT (cap 50 messages pour rapidité dashboard)."""
    token = _gmail_oauth_token()
    if not token:
        return []

    list_resp = _gmail_api(
        "GET",
        "messages?q=" + urllib.parse.quote("in:inbox newer_than:3d") + "&maxResults=50",
        token,
    )
    if list_resp.get("_error"):
        return []
    messages = list_resp.get("messages", []) or []

    results = []
    for m in messages:
        msg_id = m["id"]
        thread_id = m.get("threadId")
        raw_resp = _gmail_api("GET", f"messages/{msg_id}?format=raw", token)
        if raw_resp.get("_error"):
            continue
        try:
            raw_bytes = base64.urlsafe_b64decode(raw_resp["raw"])
            msg = email.message_from_bytes(raw_bytes)
        except Exception:
            continue

        from_raw = _decode(msg.get("From", ""))
        m_match = re.search(r"<([^>]+)>", from_raw)
        sender = m_match.group(1).lower() if m_match else from_raw.strip().lower()

        is_hot, reason = _is_hot_email(msg, sender, EmailAgent._LEAD_DOMAINS)
        if not is_hot:
            continue

        summary = _email_summary(msg, msg_id, "Gmail", thread_id)
        summary["reason"] = reason
        results.append(summary)
    return results


# ── Gemini draft generation ───────────────────────────────────────────────────
DRAFT_PROMPTS = {
    "court": (
        "Réponds en 2-3 phrases, ton chaleureux mais professionnel, en français québécois. "
        "Sois direct, concret. Aucun jargon, aucune formule pompeuse type 'Je vous remercie de votre courriel'."
    ),
    "info": (
        "Demande UNE clarification précise nécessaire pour avancer (pas plusieurs questions). "
        "Court, ton chaleureux, en 2-3 phrases. Français québécois."
    ),
    "decline": (
        "Décline poliment mais clairement. Expose 1 raison brève (timing, scope, ou fit). "
        "Ouvre la porte pour le futur si pertinent. 2-3 phrases. Français québécois."
    ),
}


def _gemini_generate(email_data: dict, mode: str) -> str | None:
    """Appelle Gemini pour générer le corps de la réponse."""
    if not GEMINI_KEY:
        return None
    if mode not in DRAFT_PROMPTS:
        return None

    prompt = f"""Tu es Jean-Philippe (JP) Roy, fondateur de Welldone Studio à Québec.
Studio nomade de production visuelle stratégique : photographie architecturale,
immobilier luxe, branding, design pour PME au Québec. ~10 ans d'existence,
travaille en solo avec sous-traitants ponctuels. Ton sobre, direct, sans bullshit.

Email reçu :
De : {email_data['from_raw']}
Sujet : {email_data['subject']}

Corps de l'email reçu :
\"\"\"
{email_data['body']}
\"\"\"

Mode de réponse demandé : {DRAFT_PROMPTS[mode]}

CONSIGNES STRICTES :
- Retourne UNIQUEMENT le corps de la réponse (le texte qui ira dans le mail)
- PAS de salutation initiale ("Bonjour X,")
- PAS de signature finale ("JP", "Cordialement", etc.)
- PAS de markdown, pas de code, pas d'explications meta
- Texte plain, prêt à copier-coller
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 600},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.load(resp)
        if "candidates" in result and result["candidates"]:
            return result["candidates"][0]["content"]["parts"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        print(f"Gemini HTTP error {e.code}: {e.read().decode()[:300]}")
    except Exception as e:
        print(f"Gemini error: {e}")
    return None


def _create_gmail_draft(to_email: str, subject: str, body: str,
                        thread_id: str = None, in_reply_to: str = None) -> dict:
    """Crée un draft Gmail. Retourne {draft_id, url} ou {error}."""
    token = _gmail_oauth_token()
    if not token:
        return {"error": "OAuth Gmail indisponible"}

    msg = MIMEText(body, _charset="utf-8")
    msg["To"] = to_email
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body_payload = {"message": {"raw": raw}}
    if thread_id:
        body_payload["message"]["threadId"] = thread_id

    result = _gmail_api("POST", "drafts", token, body=body_payload)
    if result.get("_error"):
        return {"error": str(result.get("_body", ""))[:300]}
    draft_id = result.get("id")
    inner_msg_id = result.get("message", {}).get("id", "")
    return {
        "draft_id": draft_id,
        "gmail_url": f"https://mail.google.com/mail/u/0/#drafts?compose={inner_msg_id}",
    }


# ── Cache inbox (60s TTL) ─────────────────────────────────────────────────────
# Le scan Hostinger + Gmail prend 10-30s. On cache pour ne pas refaire le tour
# à chaque rafraîchissement de page. Refresh forcé via /api/inbox?force=1.
_CACHE: dict = {"emails": None, "ts": 0}
_CACHE_LOCK = Lock()
_CACHE_TTL_SEC = 60


def _get_inbox_cached(force: bool = False) -> list[dict]:
    """Renvoie la liste filtrée + sortée des HOT non snoozed, avec cache."""
    with _CACHE_LOCK:
        now = _time.time()
        if (not force) and _CACHE["emails"] is not None and (now - _CACHE["ts"] < _CACHE_TTL_SEC):
            # Cache encore frais — refiltre juste les snoozed (peut avoir changé)
            snoozed = _load_snoozed()
            return [e for e in _CACHE["emails"] if e["uid"] not in snoozed]

        # Scan complet
        emails = _scan_hostinger() + _scan_gmail()

        # Filtrer les self-sender (JP s'envoie à lui-même = bruit)
        SELF_SENDERS = {"awelldonestudio@gmail.com", "jptanguay@awelldone.com",
                        "jptanguay.awelldone@gmail.com", "hello@awelldone.com",
                        "hello@awelldone.studio", "hello@welldone.archi"}
        emails = [e for e in emails if e.get("sender", "").lower() not in SELF_SENDERS]

        # Dédoublonner par (subject_normalisé, sender) — garde le plus récent
        seen = {}
        for e in emails:
            key = ((e.get("subject", "") or "").strip().lower(), e.get("sender", ""))
            if key not in seen:
                seen[key] = e
        emails = list(seen.values())

        def date_key(e):
            try:
                return parsedate_to_datetime(e["date"]).replace(tzinfo=None)
            except Exception:
                return datetime.min

        emails.sort(key=date_key, reverse=True)
        _CACHE["emails"] = emails
        _CACHE["ts"] = now

    snoozed = _load_snoozed()
    return [e for e in emails if e["uid"] not in snoozed]


# ── HTTP server ──────────────────────────────────────────────────────────────
HTML_PATH = Path(__file__).parent / "index.html"


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                html = HTML_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"index.html not found")
        elif self.path.startswith("/api/inbox"):
            force = "force=1" in self.path
            try:
                emails = _get_inbox_cached(force=force)
                self._json(200, {"count": len(emails), "emails": emails, "cached_at": _CACHE.get("ts")})
            except Exception as e:
                self._json(500, {"error": str(e), "trace": traceback.format_exc()})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return

        if self.path == "/api/draft":
            try:
                # Re-fetch full body (le list endpoint l'a stripé)
                # Pour simplifier : data doit contenir le body complet (envoyé depuis le client)
                # Si pas de body, on en re-fetch un
                if not data.get("body"):
                    data["body"] = data.get("snippet", "")

                draft_text = _gemini_generate(data, data.get("mode", "court"))
                if not draft_text:
                    self._json(500, {"error": "Gemini n'a rien retourné"})
                    return

                draft = _create_gmail_draft(
                    to_email=data.get("sender", ""),
                    subject=data.get("subject", ""),
                    body=draft_text,
                    thread_id=data.get("gmail_thread_id"),
                    in_reply_to=data.get("rfc822_id"),
                )
                self._json(200, {"draft_text": draft_text, **draft})
            except Exception as e:
                self._json(500, {"error": str(e), "trace": traceback.format_exc()})
        elif self.path == "/api/snooze":
            uid = data.get("uid")
            if not uid:
                self._json(400, {"error": "uid required"})
                return
            snoozed = _load_snoozed()
            snoozed[uid] = datetime.now().isoformat()
            _save_snoozed(snoozed)
            self._json(200, {"ok": True})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # silent (pas de log par requête)
        pass


def main():
    port = 5050
    print()
    print("┌─────────────────────────────────────────┐")
    print("│   📬  Inbox Triage Dashboard            │")
    print("│                                         │")
    print(f"│   → http://localhost:{port}              │")
    print("│                                         │")
    print("│   Ctrl+C pour arrêter                   │")
    print("└─────────────────────────────────────────┘")
    print()

    # ThreadingHTTPServer : un thread par requête.
    # Sinon le scan IMAP/Gmail (lent) bloque toutes les autres requêtes (même /).
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Arrêt du serveur.")
        server.shutdown()


if __name__ == "__main__":
    main()
