"""
agents/zoho.py — Agent Zoho Books (facturation).
Capacités: lister les factures, envoyer une facture par email.
"""
import logging, requests
from agents._base import BaseAgent
from core.auth import get_zoho_access_token
from config import ZOHO_BASE_URL, ZOHO_ORG_ID

log = logging.getLogger(__name__)


class ZohoAgent(BaseAgent):
    name        = "zoho"
    description = "Gérer la facturation Zoho Books (factures, envois)"

    @property
    def commands(self):
        return {
            "list": self.list_invoices,
            "send": self.send_invoice,
        }

    def _headers(self) -> dict:
        return {
            "Authorization": f"Zoho-oauthtoken {get_zoho_access_token()}",
            "Content-Type":  "application/json",
        }

    async def list_invoices(self, context: dict | None = None) -> str:
        """
        context: {"search": "nom client ou numéro"} [optionnel]
        """
        ctx    = context or {}
        search = ctx.get("search", "")
        status = ctx.get("status", "")  # "unpaid", "overdue", etc.

        params = {"organization_id": ZOHO_ORG_ID, "per_page": 10, "sort_column": "date", "sort_order": "D"}
        if search:
            params["search_text"] = search
        if status:
            params["status"] = status

        try:
            resp = requests.get(f"{ZOHO_BASE_URL}/invoices", headers=self._headers(), params=params, timeout=10)
            resp.raise_for_status()
            invoices = resp.json().get("invoices", [])

            if not invoices:
                return f"📋 Aucune facture trouvée{' pour « ' + search + ' »' if search else ''}."

            lines = [f"📋 *Factures{' — ' + search if search else ''} :*\n"]
            for inv in invoices:
                num    = inv.get("invoice_number", "?")
                client = inv.get("customer_name", "?")
                total  = f"{float(inv.get('total', 0)):,.2f} {inv.get('currency_code', 'CAD')}"
                status = inv.get("status", "?")
                date   = inv.get("date", "")
                icon   = "🔴" if status in ("overdue",) else "🟡" if status == "sent" else "✅" if status == "paid" else "⚪"
                lines.append(f"{icon} *{num}* — {client}\n   {total} · {date} · {status}")

            return "\n".join(lines)
        except Exception as e:
            log.error(f"zoho.list error: {e}")
            return f"❌ Erreur liste factures: {e}"

    async def send_invoice(self, context: dict | None = None) -> str:
        """
        context attendu:
          invoice_id (str) OU search (str) pour trouver la facture
          to_email (str) [optionnel, utilise l'email du client par défaut]
        """
        ctx        = context or {}
        invoice_id = ctx.get("invoice_id", "")
        search     = ctx.get("search", "")
        to_email   = ctx.get("to_email", "")

        # Trouver la facture si pas d'ID direct
        if not invoice_id and search:
            try:
                resp = requests.get(
                    f"{ZOHO_BASE_URL}/invoices",
                    headers=self._headers(),
                    params={"organization_id": ZOHO_ORG_ID, "search_text": search, "per_page": 1},
                    timeout=10,
                )
                resp.raise_for_status()
                invoices = resp.json().get("invoices", [])
                if not invoices:
                    return f"❌ Aucune facture trouvée pour « {search} »"
                invoice_id = invoices[0]["invoice_id"]
                client     = invoices[0].get("customer_name", "?")
                num        = invoices[0].get("invoice_number", "?")
            except Exception as e:
                return f"❌ Erreur recherche facture: {e}"
        elif not invoice_id:
            return "❌ Paramètre 'invoice_id' ou 'search' requis"

        try:
            body = {"send_from_org_email_id": True, "to_mail_ids": [to_email] if to_email else []}
            resp = requests.post(
                f"{ZOHO_BASE_URL}/invoices/{invoice_id}/email",
                headers=self._headers(),
                params={"organization_id": ZOHO_ORG_ID},
                json=body,
                timeout=10,
            )
            resp.raise_for_status()
            log.info(f"zoho.send ok invoice_id={invoice_id}")
            name_info = f" ({client} — {num})" if search else ""
            return f"✅ Facture{name_info} envoyée par email{' à ' + to_email if to_email else ' au client'}."
        except Exception as e:
            log.error(f"zoho.send error: {e}")
            return f"❌ Erreur envoi facture: {e}"


agent = ZohoAgent()
