"""
agents/gmail.py — Agent Gmail.
Capacités: lire les emails non lus, envoyer un email, chercher un contact.
"""
import re, base64, logging
from email.message import EmailMessage
from agents._base import BaseAgent
from core.auth import get_google_service
from config import EMAIL_FROM_JP, EMAIL_FROM_BILL, EMAIL_BCC

log = logging.getLogger(__name__)


class GmailAgent(BaseAgent):
    name        = "gmail"
    description = "Lire et envoyer des emails via Gmail"

    @property
    def commands(self):
        return {
            "read":          self.read_unread,
            "send":          self.send,
            "search":        self.search_contact,
            "scan_invoices": self.scan_invoices,
        }

    async def read_unread(self, context: dict | None = None) -> str:
        max_results = (context or {}).get("max_results", 5)
        try:
            svc = get_google_service("gmail", "v1")
            results = svc.users().messages().list(
                userId="me", labelIds=["UNREAD"], maxResults=max_results
            ).execute()
            messages = results.get("messages", [])
            if not messages:
                return "📭 Aucun email non lu."

            lines = ["📧 *Emails non lus :*\n"]
            for msg in messages:
                data = svc.users().messages().get(
                    userId="me", id=msg["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"]
                ).execute()
                headers = {h["name"]: h["value"] for h in data["payload"]["headers"]}
                sujet  = headers.get("Subject", "Sans sujet")[:60]
                sender = headers.get("From", "Inconnu")[:40]
                date   = headers.get("Date", "")[:16]
                lines.append(f"🔸 *De:* {sender}\n🔹 *Sujet:* {sujet}\n📅 {date}\n")

            return "\n".join(lines)
        except Exception as e:
            log.error(f"gmail.read error: {e}")
            return f"❌ Erreur lecture emails: {e}"

    async def send(self, context: dict | None = None) -> str:
        """
        context attendu:
          to (str), subject (str), body (str),
          signature_type (str): "client" | "facturation"  [optionnel]
        """
        ctx = context or {}
        to      = ctx.get("to", "")
        subject = ctx.get("subject", "")
        body    = ctx.get("body", "")
        sig_type = ctx.get("signature_type", "client")

        if not all([to, subject, body]):
            return "❌ Paramètres manquants: to, subject, body requis"

        if sig_type == "facturation":
            signature = "\n\nCordialement,\nFacturation\nWelldone | Studio\n+1 514 835 3313"
            from_addr = EMAIL_FROM_BILL
        else:
            signature = "\n\nCordialement,\nJean-Philippe Roy Tanguay\nWelldone | Studio\n+1 514 835 3313"
            from_addr = EMAIL_FROM_JP

        try:
            svc = get_google_service("gmail", "v1")
            msg = EmailMessage()
            msg.set_content(body + signature)
            msg["To"]      = to
            msg["From"]    = from_addr
            msg["Subject"] = subject
            msg["Bcc"]     = EMAIL_BCC

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            svc.users().messages().send(userId="me", body={"raw": raw}).execute()
            log.info(f"gmail.send ok to={to}")
            return f"✅ Email envoyé à {to}\n📌 Sujet: {subject}"
        except Exception as e:
            log.error(f"gmail.send error: {e}")
            return f"❌ Erreur envoi email: {e}"

    async def search_contact(self, context: dict | None = None) -> str:
        """
        context: {"query": "nom ou email partiel"}
        Cherche dans Google Contacts + fallback Gmail.
        """
        query = (context or {}).get("query", "")
        if not query:
            return "❌ Paramètre 'query' manquant"

        results = []

        # 1. Google People API (contacts + otherContacts)
        try:
            people = get_google_service("people", "v1")
            r = people.people().searchContacts(
                query=query, readMask="names,emailAddresses"
            ).execute()
            for c in r.get("results", []):
                p    = c.get("person", {})
                name = p.get("names", [{}])[0].get("displayName", "")
                for e in p.get("emailAddresses", []):
                    results.append({"name": name, "email": e.get("value"), "source": "Contacts"})
        except Exception as e:
            log.warning(f"gmail.search_contact: People API failed (scope manquant ou API désactivée): {e}")

        # 2. Fallback Gmail
        if not results:
            try:
                gmail = get_google_service("gmail", "v1")
                msgs  = gmail.users().messages().list(
                    userId="me", q=f"from:{query} OR to:{query}", maxResults=10
                ).execute()
                seen = set()
                for msg in msgs.get("messages", []):
                    data = gmail.users().messages().get(
                        userId="me", id=msg["id"], format="metadata",
                        metadataHeaders=["From", "To"]
                    ).execute()
                    for h in data["payload"]["headers"]:
                        if h["name"] in ("From", "To") and query.lower() in h["value"].lower():
                            match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", h["value"])
                            if match and match.group(0) not in seen:
                                seen.add(match.group(0))
                                display = h["value"].split("<")[0].strip().strip('"') or match.group(0)
                                results.append({"name": display, "email": match.group(0), "source": "Gmail"})
            except Exception as e:
                log.warning(f"gmail.search_contact: Gmail fallback failed: {e}")

        if not results:
            return f"❌ Aucun contact trouvé pour « {query} »"

        # Dédupliquer
        seen = {}
        for r in results:
            if r["email"] not in seen:
                seen[r["email"]] = r
        unique = list(seen.values())

        lines = [f"🔍 *Contacts trouvés ({len(unique)}) :*\n"]
        for i, c in enumerate(unique[:5], 1):
            lines.append(f"{i}. *{c['name']}* — `{c['email']}` ({c['source']})")

        return "\n".join(lines)


    async def scan_invoices(self, context: dict | None = None) -> str:
        """
        Cherche les factures reçues dans Gmail et les classe dans Google Sheets.

        context: {"days": 7}  ← période de recherche (défaut: 7 jours)
        """
        import anthropic as _anthropic
        from core.sheets import sheets_ensure_header, sheets_append
        from config import SHEETS_LIVRES_ID, ANTHROPIC_API_KEY, CLAUDE_MODEL

        ctx  = context or {}
        days = ctx.get("days", 7)

        if not SHEETS_LIVRES_ID:
            return "❌ SHEETS_LIVRES_ID non défini dans Railway — crée un Google Sheet et ajoute son ID."

        try:
            svc = get_google_service("gmail", "v1")
            query = f"(facture OR invoice OR bill OR receipt) newer_than:{days}d"
            results = svc.users().messages().list(
                userId="me", q=query, maxResults=20
            ).execute()
            messages = results.get("messages", [])

            if not messages:
                return f"📭 Aucune facture trouvée dans les {days} derniers jours."

            # S'assurer que les en-têtes existent
            sheets_ensure_header(SHEETS_LIVRES_ID)

            ai = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            added = 0
            skipped = 0

            for msg in messages:
                try:
                    data = svc.users().messages().get(
                        userId="me", id=msg["id"], format="full"
                    ).execute()
                    headers = {h["name"]: h["value"] for h in data["payload"].get("headers", [])}

                    sender  = headers.get("From", "Inconnu")[:80]
                    subject = headers.get("Subject", "")[:100]
                    date    = headers.get("Date", "")[:30]

                    # Extraire le texte du corps
                    body_text = _extract_email_body(data["payload"])[:3000]

                    # Claude analyse le corps pour extraire les infos
                    extraction = ai.messages.create(
                        model=CLAUDE_MODEL,
                        max_tokens=300,
                        system="Extrait les informations de cette facture reçue. Réponds UNIQUEMENT en JSON: {\"fournisseur\": str, \"no_facture\": str, \"montant\": str, \"date_facture\": str, \"echeance\": str, \"est_facture\": bool}. Si ce n'est pas une facture, met est_facture: false.",
                        messages=[{"role": "user", "content": f"De: {sender}\nSujet: {subject}\nDate: {date}\n\n{body_text}"}],
                    )
                    raw = extraction.content[0].text.strip()
                    if raw.startswith("```"):
                        raw = raw.split("```")[1]
                        if raw.startswith("json"):
                            raw = raw[4:]

                    import json as _json
                    info = _json.loads(raw)

                    if not info.get("est_facture", False):
                        skipped += 1
                        continue

                    from datetime import datetime as _dt
                    row = [
                        _dt.now().strftime("%Y-%m-%d"),
                        info.get("fournisseur") or sender,
                        info.get("no_facture", ""),
                        info.get("montant", ""),
                        info.get("date_facture", ""),
                        info.get("echeance", ""),
                        "À payer",
                        subject,
                    ]
                    sheets_append(SHEETS_LIVRES_ID, row)
                    added += 1

                except Exception as e:
                    log.warning(f"gmail.scan_invoices: skip msg {msg['id']}: {e}")
                    skipped += 1
                    continue

            return (
                f"📊 Scan terminé ({days} jours) :\n"
                f"✅ {added} facture(s) ajoutée(s) au Google Sheet\n"
                f"⏭ {skipped} email(s) ignoré(s) (non-factures ou erreurs)"
            )

        except Exception as e:
            log.error(f"gmail.scan_invoices error: {e}")
            return f"❌ Erreur scan factures: {e}"


def _extract_email_body(payload: dict) -> str:
    """Extrait le texte brut d'un message Gmail (récursif sur les parts)."""
    import base64 as _b64
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return _b64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        result = _extract_email_body(part)
        if result:
            return result
    return ""


agent = GmailAgent()
