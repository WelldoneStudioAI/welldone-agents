"""
agents/qbo.py — Agent QuickBooks Online.

Capacités :
  - create         → prévisualisation + confirmation avant création
  - create_client  → créer un nouveau client QBO
  - send           → envoyer une facture par email via QBO
  - list           → lister les factures impayées

Règles Welldone Studio :
  - TPS 5 % + TVQ 9,975 % calculées et affichées avant création
  - Numérotation WS-AAAAMMJJ-### générée automatiquement
  - Aucune facture créée sans prévisualisation et confirmation
  - Signature : Service Externe Comptabilité Welldone Studio

Auth : OAuth 2.0 via refresh token (géré par core.auth.get_qbo_access_token).
API : QuickBooks Online REST v3 (https://developer.intuit.com/app/developer/qbo/docs)
"""
import logging
import requests
from datetime import date
from agents._base import BaseAgent
from core.auth import get_qbo_access_token
from config import (
    QBO_REALM_ID, QBO_BASE_URL, QBO_SERVICE_ITEM_ID,
    QBO_BILLING_SIGNATURE, GMAIL_BILLING_FROM,
)

log = logging.getLogger(__name__)

# ── État pending (module-level, partagé avec bot/telegram.py) ─────────────────
# Structure: user_id → {customer_id, customer_name, email, service, amount,
#                        tps, tvq, total, inv_num, tax_code_id}
_pending_invoices: dict[int, dict] = {}


def store_pending(user_id: int, data: dict) -> None:
    _pending_invoices[user_id] = data


def get_pending(user_id: int) -> dict | None:
    return _pending_invoices.get(user_id)


def clear_pending(user_id: int) -> None:
    _pending_invoices.pop(user_id, None)


# ── Helpers HTTP ──────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_qbo_access_token()}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }


def _base() -> str:
    return f"{QBO_BASE_URL}/{QBO_REALM_ID}"


# ── Helpers QBO ───────────────────────────────────────────────────────────────

def _find_customer(name: str) -> dict | None:
    """Cherche un client par nom dans QBO. Retourne le Customer dict ou None."""
    try:
        escaped = name.replace("'", "\\'")
        resp = requests.get(
            f"{_base()}/query",
            headers=_headers(),
            params={"query": f"SELECT * FROM Customer WHERE DisplayName LIKE '%{escaped}%'",
                    "minorversion": "65"},
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json().get("QueryResponse", {}).get("Customer", [])
        return rows[0] if rows else None
    except Exception as e:
        log.error(f"qbo._find_customer error: {e}")
        return None


def _next_invoice_number() -> str:
    """Génère WS-AAAAMMJJ-### en cherchant le dernier numéro du jour dans QBO."""
    today  = date.today().strftime("%Y%m%d")
    prefix = f"WS-{today}-"
    try:
        resp = requests.get(
            f"{_base()}/query",
            headers=_headers(),
            params={"query": f"SELECT DocNumber FROM Invoice WHERE DocNumber LIKE '{prefix}%' ORDERBY DocNumber DESC MAXRESULTS 1",
                    "minorversion": "65"},
            timeout=10,
        )
        resp.raise_for_status()
        invoices = resp.json().get("QueryResponse", {}).get("Invoice", [])
        if invoices:
            last_num = invoices[0].get("DocNumber", f"{prefix}000")
            try:
                seq = int(last_num.split("-")[-1]) + 1
            except (ValueError, IndexError):
                seq = 1
        else:
            seq = 1
        return f"{prefix}{seq:03d}"
    except Exception as e:
        log.error(f"qbo._next_invoice_number error: {e}")
        # Fallback sécurisé si QBO inaccessible
        return f"{prefix}001"


def _get_qbo_tax_code() -> str | None:
    """Cherche le code taxe TPS+TVQ dans QBO. Retourne l'Id ou None."""
    try:
        resp = requests.get(
            f"{_base()}/query",
            headers=_headers(),
            params={"query": "SELECT * FROM TaxCode", "minorversion": "65"},
            timeout=10,
        )
        resp.raise_for_status()
        codes = resp.json().get("QueryResponse", {}).get("TaxCode", [])
        # Chercher un TaxCode qui ressemble à TPS+TVQ (GST/QST, Canada)
        for code in codes:
            name = code.get("Name", "").upper()
            if any(k in name for k in ("GST", "QST", "TPS", "TVQ", "HST", "TAX")):
                log.info(f"qbo: TaxCode trouvé → {code['Name']} (Id={code['Id']})")
                return code["Id"]
        # Sinon, prendre le premier code non-exempt
        for code in codes:
            if not code.get("Taxable") is False and code.get("Name", "") not in ("NON", "EXEMPT", "Out of scope"):
                log.info(f"qbo: TaxCode fallback → {code['Name']} (Id={code['Id']})")
                return code["Id"]
        return None
    except Exception as e:
        log.error(f"qbo._get_qbo_tax_code error: {e}")
        return None


# ── Preview ───────────────────────────────────────────────────────────────────

def _build_invoice_preview(
    customer_name: str,
    email: str,
    service: str,
    amount: float,
    inv_num: str,
    tax_code_id: str | None,
) -> dict:
    """Calcule TPS/TVQ et retourne le dict complet de prévisualisation."""
    tps   = round(amount * 0.05, 2)
    tvq   = round(amount * 0.09975, 2)
    total = round(amount + tps + tvq, 2)
    return {
        "customer_name": customer_name,
        "email":         email,
        "service":       service,
        "amount":        amount,
        "tps":           tps,
        "tvq":           tvq,
        "total":         total,
        "inv_num":       inv_num,
        "tax_code_id":   tax_code_id,
        "signature":     QBO_BILLING_SIGNATURE,
        "from_email":    GMAIL_BILLING_FROM,
    }


def _format_preview(data: dict) -> str:
    """Formate la prévisualisation en texte Telegram Markdown standard."""
    return (
        f"📋 *Prévisualisation de la facture*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Client : {data['customer_name']}\n"
        f"📧 Email : {data['email']}\n"
        f"📋 Service : {data['service']}\n"
        f"💵 Montant HT : {data['amount']:,.2f} $\n"
        f"🏦 TPS (5%) : {data['tps']:,.2f} $\n"
        f"🏦 TVQ (9,975%) : {data['tvq']:,.2f} $\n"
        f"💰 *Total TTC : {data['total']:,.2f} $*\n"
        f"📅 Paiement : Dû à réception\n"
        f"📝 N° facture : {data['inv_num']}\n"
        f"✉️ Signature : {data['signature']}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


# ── Création effective (appelée depuis telegram.py après confirmation) ─────────

async def execute_create(user_id: int) -> str:
    """Crée la facture en brouillon dans QBO (sans l'envoyer au client)."""
    data = get_pending(user_id)
    if not data:
        return "❌ Aucune facture en attente. Recommence la commande."

    try:
        invoice_body = _build_invoice_body(data)
        resp = requests.post(
            f"{_base()}/invoice",
            headers=_headers(),
            params={"minorversion": "65"},
            json=invoice_body,
            timeout=10,
        )
        resp.raise_for_status()
        invoice = resp.json().get("Invoice", {})
        inv_id  = invoice.get("Id")
        inv_num = invoice.get("DocNumber", data["inv_num"])
        balance = invoice.get("Balance", data["total"])
        clear_pending(user_id)

        return (
            f"✅ Facture *{inv_num}* créée dans QuickBooks\n"
            f"👤 Client : {data['customer_name']}\n"
            f"💰 Total TTC : ${balance:,.2f}\n"
            f"📋 Service : {data['service']}\n"
            f"📝 Statut : Brouillon — non envoyée\n"
            f"_(Utilise `/qbo send {inv_id}` pour l'envoyer)_"
        )
    except Exception as e:
        log.error(f"qbo.execute_create error: {e}")
        return f"❌ Erreur création facture QBO : {e}"


async def execute_send_direct(user_id: int) -> str:
    """Crée la facture ET l'envoie immédiatement au client via QBO."""
    data = get_pending(user_id)
    if not data:
        return "❌ Aucune facture en attente. Recommence la commande."

    try:
        invoice_body = _build_invoice_body(data)
        resp = requests.post(
            f"{_base()}/invoice",
            headers=_headers(),
            params={"minorversion": "65"},
            json=invoice_body,
            timeout=10,
        )
        resp.raise_for_status()
        invoice = resp.json().get("Invoice", {})
        inv_id  = invoice.get("Id")
        inv_num = invoice.get("DocNumber", data["inv_num"])
        balance = invoice.get("Balance", data["total"])

        # Envoyer par email via QBO
        send_resp = requests.post(
            f"{_base()}/invoice/{inv_id}/send",
            headers=_headers(),
            params={"minorversion": "65"},
            timeout=10,
        )
        send_resp.raise_for_status()
        clear_pending(user_id)

        return (
            f"✅ Facture *{inv_num}* créée et envoyée\n"
            f"👤 Client : {data['customer_name']}\n"
            f"📧 Envoyée à : {data['email']}\n"
            f"💰 Total TTC : ${balance:,.2f}\n"
            f"📋 Service : {data['service']}"
        )
    except Exception as e:
        log.error(f"qbo.execute_send_direct error: {e}")
        return f"❌ Erreur création/envoi facture QBO : {e}"


def _build_invoice_body(data: dict) -> dict:
    """Construit le corps de facture QBO avec taxes et numérotation."""
    body: dict = {
        "DocNumber":   data["inv_num"],
        "CustomerRef": {"value": data["customer_id"]},
        "CurrencyRef": {"value": "CAD"},
        "Line": [{
            "Amount":      data["amount"],
            "DetailType":  "SalesItemLineDetail",
            "Description": data["service"],
            "SalesItemLineDetail": {
                "UnitPrice":  data["amount"],
                "Qty":        1,
                "ItemRef":    {"value": QBO_SERVICE_ITEM_ID},
                "TaxCodeRef": {"value": "TAX"},
            },
        }],
    }

    # Ajouter la taxe si un TaxCode a été trouvé
    if data.get("tax_code_id"):
        body["TxnTaxDetail"] = {
            "TxnTaxCodeRef": {"value": data["tax_code_id"]}
        }

    # SalesTermRef "1" = Due on Receipt (peut varier selon la config QBO)
    try:
        body["SalesTermRef"] = {"value": "1"}
    except Exception:
        pass  # Omettre si erreur

    return body


# ── Agent ─────────────────────────────────────────────────────────────────────

class QBOAgent(BaseAgent):
    name        = "qbo"
    description = "Facturation QuickBooks Online — créer clients, créer et envoyer des factures"

    # Signal retourné à telegram.py pour déclencher le flux preview
    NEEDS_CONFIRMATION = "__QBO_NEEDS_CONFIRMATION__"
    NEEDS_SERVICE      = "__QBO_NEEDS_SERVICE__"

    @property
    def commands(self):
        return {
            "create":        self.create,
            "create_client": self.create_client,
            "send":          self.send,
            "list":          self.list_invoices,
        }

    async def create(self, context: dict | None = None, user_id: int = 0) -> str:
        """
        Prévisualise une facture et stocke l'état pending.

        context:
          client (str)         ← nom du client
          amount (float)       ← montant HT
          description (str)    ← service ("?" pour afficher les pills)
          client_email (str)   ← [optionnel] email si le client n'existe pas encore
        """
        ctx          = context or {}
        client_name  = ctx.get("client", "").strip()
        amount       = float(ctx.get("amount", 0))
        description  = ctx.get("description", "?").strip()
        client_email = ctx.get("client_email", "").strip()

        if not client_name:
            return "❌ Paramètre 'client' manquant."
        if amount <= 0:
            return "❌ Paramètre 'amount' manquant ou invalide."
        if not QBO_REALM_ID:
            return "❌ QBO_REALM_ID non défini dans Railway."

        # 1. Chercher le client
        customer = _find_customer(client_name)

        # 2. Si introuvable et email fourni → créer le client
        if not customer and client_email:
            result = await self.create_client({
                "display_name": client_name,
                "email":        client_email,
            })
            if result.startswith("❌"):
                return result
            customer = _find_customer(client_name)

        # 3. Si toujours introuvable → demander l'email
        if not customer:
            return (
                f"❓ Le client *{client_name}* n'existe pas encore dans QuickBooks.\n"
                f"Envoie-moi son adresse email pour le créer et continuer.\n"
                f"_(Ex: abc@client.com)_"
            )

        customer_id    = customer["Id"]
        customer_name  = customer.get("DisplayName", client_name)
        customer_email = customer.get("PrimaryEmailAddr", {}).get("Address", "—")

        # 4. Si service non précisé → signal pour afficher les pills
        if description == "?" or not description:
            # Stocker infos partielles pour compléter après sélection service
            store_pending(user_id, {
                "customer_id":   customer_id,
                "customer_name": customer_name,
                "email":         customer_email,
                "service":       None,
                "amount":        amount,
                "tps":           None,
                "tvq":           None,
                "total":         None,
                "inv_num":       None,
                "tax_code_id":   None,
            })
            return self.NEEDS_SERVICE

        # 5. Générer numéro de facture + code taxe
        inv_num     = _next_invoice_number()
        tax_code_id = _get_qbo_tax_code()

        # 6. Construire preview
        preview_data = _build_invoice_preview(
            customer_name=customer_name,
            email=customer_email,
            service=description,
            amount=amount,
            inv_num=inv_num,
            tax_code_id=tax_code_id,
        )
        preview_data["customer_id"] = customer_id

        if tax_code_id is None:
            preview_data["_tax_warning"] = True

        # 7. Stocker pending
        store_pending(user_id, preview_data)

        return self.NEEDS_CONFIRMATION

    async def create_client(self, context: dict | None = None) -> str:
        """
        Crée un nouveau client dans QBO.

        context:
          display_name (str)  ← nom complet du client (obligatoire)
          email (str)         ← email (obligatoire)
          phone (str)         ← [optionnel]
          address (str)       ← [optionnel]
        """
        ctx          = context or {}
        display_name = ctx.get("display_name", "").strip()
        email        = ctx.get("email", "").strip()
        phone        = ctx.get("phone", "").strip()
        address      = ctx.get("address", "").strip()

        if not display_name:
            return "❌ Paramètre 'display_name' manquant (nom du client)."
        if not email:
            return "❌ Paramètre 'email' manquant."
        if not QBO_REALM_ID:
            return "❌ QBO_REALM_ID non défini dans Railway."

        try:
            customer_body: dict = {
                "DisplayName":      display_name,
                "PrimaryEmailAddr": {"Address": email},
            }
            if phone:
                customer_body["PrimaryPhone"] = {"FreeFormNumber": phone}
            if address:
                customer_body["BillAddr"] = {"Line1": address}

            resp = requests.post(
                f"{_base()}/customer",
                headers=_headers(),
                params={"minorversion": "65"},
                json=customer_body,
                timeout=10,
            )
            resp.raise_for_status()
            customer = resp.json().get("Customer", {})
            cid  = customer.get("Id")
            name = customer.get("DisplayName", display_name)

            return (
                f"✅ Client *{name}* créé dans QuickBooks\n"
                f"📧 Email : {email}\n"
                f"🆔 ID QBO : {cid}"
            )
        except Exception as e:
            log.error(f"qbo.create_client error: {e}")
            return f"❌ Erreur création client QBO : {e}"

    async def send(self, context: dict | None = None) -> str:
        """
        Envoie une facture QBO par email (via QBO, pas Gmail).

        context:
          invoice_id (str)  ← ID QBO de la facture (prioritaire)
          invoice_num (str) ← numéro de facture (si invoice_id non dispo)
        """
        ctx        = context or {}
        invoice_id = str(ctx.get("invoice_id", "")).strip()
        inv_num    = str(ctx.get("invoice_num", "")).strip()

        if not QBO_REALM_ID:
            return "❌ QBO_REALM_ID non défini dans Railway."

        # Si on a seulement le numéro de facture, trouver l'ID
        if not invoice_id and inv_num:
            try:
                resp = requests.get(
                    f"{_base()}/query",
                    headers=_headers(),
                    params={"query": f"SELECT * FROM Invoice WHERE DocNumber = '{inv_num}'",
                            "minorversion": "65"},
                    timeout=10,
                )
                resp.raise_for_status()
                invoices = resp.json().get("QueryResponse", {}).get("Invoice", [])
                if not invoices:
                    return f"❌ Facture #{inv_num} introuvable dans QBO."
                invoice_id = invoices[0]["Id"]
            except Exception as e:
                return f"❌ Erreur recherche facture : {e}"

        if not invoice_id:
            return "❌ Paramètre 'invoice_id' ou 'invoice_num' manquant."

        try:
            resp = requests.post(
                f"{_base()}/invoice/{invoice_id}/send",
                headers=_headers(),
                params={"minorversion": "65"},
                timeout=10,
            )
            resp.raise_for_status()
            return f"✅ Facture #{invoice_id} envoyée par email via QuickBooks."
        except Exception as e:
            log.error(f"qbo.send error: {e}")
            return f"❌ Erreur envoi facture QBO : {e}"

    async def list_invoices(self, context: dict | None = None) -> str:
        """
        Liste les factures QBO.

        context:
          status (str)  ← "unpaid" (défaut) | "overdue" | "all"
          limit (int)   ← nombre max de factures (défaut: 10)
        """
        ctx    = context or {}
        status = ctx.get("status", "unpaid")
        limit  = int(ctx.get("limit", 10))

        if not QBO_REALM_ID:
            return "❌ QBO_REALM_ID non défini dans Railway."

        try:
            if status == "all":
                query = f"SELECT * FROM Invoice ORDERBY TxnDate DESC MAXRESULTS {limit}"
            elif status == "overdue":
                today = date.today().isoformat()
                query = f"SELECT * FROM Invoice WHERE Balance > '0' AND DueDate < '{today}' ORDERBY DueDate ASC MAXRESULTS {limit}"
            else:  # unpaid (défaut)
                query = f"SELECT * FROM Invoice WHERE Balance > '0' ORDERBY TxnDate DESC MAXRESULTS {limit}"

            resp = requests.get(
                f"{_base()}/query",
                headers=_headers(),
                params={"query": query, "minorversion": "65"},
                timeout=10,
            )
            resp.raise_for_status()
            invoices = resp.json().get("QueryResponse", {}).get("Invoice", [])

            if not invoices:
                label = {"unpaid": "impayées", "overdue": "en retard", "all": "récentes"}.get(status, "")
                return f"📭 Aucune facture {label} dans QuickBooks."

            label = {"unpaid": "impayées", "overdue": "en retard ⚠️", "all": "récentes"}.get(status, "")
            lines = [f"📄 *Factures {label} ({len(invoices)}) :*\n"]

            for inv in invoices:
                num      = inv.get("DocNumber", inv.get("Id"))
                client   = inv.get("CustomerRef", {}).get("name", "Inconnu")
                balance  = inv.get("Balance", 0)
                due_date = inv.get("DueDate", "—")
                lines.append(f"🔸 *#{num}* — {client}\n   💰 ${balance:,.2f} · Échéance : {due_date}")

            return "\n".join(lines)

        except Exception as e:
            log.error(f"qbo.list error: {e}")
            return f"❌ Erreur liste factures QBO : {e}"


agent = QBOAgent()
