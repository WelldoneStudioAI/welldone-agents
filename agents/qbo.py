"""
agents/qbo.py — Agent QuickBooks Online.

Capacités :
  - create         → créer une facture (détecte si le client existe, sinon demande l'email)
  - create_client  → créer un nouveau client QBO
  - send           → envoyer une facture par email via QBO
  - list           → lister les factures impayées

Auth : OAuth 2.0 via refresh token (géré par core.auth.get_qbo_access_token).
API : QuickBooks Online REST v3 (https://developer.intuit.com/app/developer/qbo/docs)
"""
import logging
import requests
from agents._base import BaseAgent
from core.auth import get_qbo_access_token
from config import QBO_REALM_ID, QBO_BASE_URL, QBO_SERVICE_ITEM_ID

log = logging.getLogger(__name__)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_qbo_access_token()}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }


def _base() -> str:
    return f"{QBO_BASE_URL}/{QBO_REALM_ID}"


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
        if rows:
            return rows[0]
        return None
    except Exception as e:
        log.error(f"qbo._find_customer error: {e}")
        return None


class QBOAgent(BaseAgent):
    name        = "qbo"
    description = "Facturation QuickBooks Online — créer clients, créer et envoyer des factures"

    @property
    def commands(self):
        return {
            "create":        self.create,
            "create_client": self.create_client,
            "send":          self.send,
            "list":          self.list_invoices,
        }

    async def create(self, context: dict | None = None) -> str:
        """
        Crée une facture dans QBO.

        context:
          client (str)         ← nom du client
          amount (float)       ← montant total
          description (str)    ← description de la ligne
          client_email (str)   ← [optionnel] email si le client n'existe pas encore
        """
        ctx = context or {}
        client_name  = ctx.get("client", "").strip()
        amount       = float(ctx.get("amount", 0))
        description  = ctx.get("description", "Services professionnels")
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
            # Re-chercher après création
            customer = _find_customer(client_name)

        # 3. Si toujours introuvable → demander l'email
        if not customer:
            return (
                f"❓ Le client *{client_name}* n'existe pas encore dans QuickBooks.\n"
                f"Envoie-moi son adresse email pour le créer et continuer la facturation.\n"
                f"_(Ex: abc@client.com)_"
            )

        customer_id   = customer["Id"]
        customer_name = customer.get("DisplayName", client_name)

        # 4. Créer la facture
        try:
            invoice_body = {
                "Line": [{
                    "Amount": amount,
                    "DetailType": "SalesItemLineDetail",
                    "Description": description,
                    "SalesItemLineDetail": {
                        "UnitPrice": amount,
                        "Qty": 1,
                        "ItemRef": {"value": QBO_SERVICE_ITEM_ID},
                    },
                }],
                "CustomerRef": {"value": customer_id},
            }
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
            inv_num = invoice.get("DocNumber", inv_id)
            balance = invoice.get("Balance", amount)

            return (
                f"✅ Facture *#{inv_num}* créée dans QuickBooks\n"
                f"👤 Client: {customer_name}\n"
                f"💰 Montant: ${balance:,.2f}\n"
                f"📋 Description: {description}\n"
                f"_(Utilise `/qbo send {inv_id}` pour l'envoyer par email)_"
            )
        except Exception as e:
            log.error(f"qbo.create invoice error: {e}")
            return f"❌ Erreur création facture QBO: {e}"

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
                "DisplayName":       display_name,
                "PrimaryEmailAddr":  {"Address": email},
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
                f"📧 Email: {email}\n"
                f"🆔 ID QBO: {cid}"
            )
        except Exception as e:
            log.error(f"qbo.create_client error: {e}")
            return f"❌ Erreur création client QBO: {e}"

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
                return f"❌ Erreur recherche facture: {e}"

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
            return f"❌ Erreur envoi facture QBO: {e}"

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
                from datetime import date
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
                lines.append(f"🔸 *#{num}* — {client}\n   💰 ${balance:,.2f} · Échéance: {due_date}")

            return "\n".join(lines)

        except Exception as e:
            log.error(f"qbo.list error: {e}")
            return f"❌ Erreur liste factures QBO: {e}"


agent = QBOAgent()
