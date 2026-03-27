"""
core/sheets.py — Utilitaire Google Sheets pour la tenue de livres.

Utilise le Service Account Google (GOOGLE_SA_JSON_B64) pour accéder aux Sheets.
Fonctions principales :
  - sheets_ensure_header(spreadsheet_id) → crée les en-têtes si la feuille est vide
  - sheets_append(spreadsheet_id, values) → ajoute une ligne

Structure des colonnes (tenue de livres factures reçues) :
  Date reçue | Fournisseur | N° facture | Montant | Date facture | Échéance | Statut | Sujet email
"""
import logging
from core.auth import get_service_account_creds

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADER_ROW = [
    "Date reçue",
    "Fournisseur",
    "N° facture",
    "Montant",
    "Date facture",
    "Échéance",
    "Statut",
    "Sujet email",
]


def _get_service():
    from googleapiclient.discovery import build
    creds = get_service_account_creds(SCOPES)
    return build("sheets", "v4", credentials=creds)


def sheets_ensure_header(spreadsheet_id: str, sheet_name: str = "Factures") -> None:
    """
    Vérifie si la première ligne contient les en-têtes.
    Si la feuille est vide, insère les en-têtes automatiquement.
    """
    try:
        svc = _get_service()
        result = svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A1:H1",
        ).execute()
        existing = result.get("values", [])
        if not existing:
            svc.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{sheet_name}!A1",
                valueInputOption="RAW",
                body={"values": [HEADER_ROW]},
            ).execute()
            log.info(f"sheets: en-têtes créés dans {spreadsheet_id}")
    except Exception as e:
        log.warning(f"sheets.ensure_header error: {e}")


def sheets_append(spreadsheet_id: str, values: list, sheet_name: str = "Factures") -> None:
    """
    Ajoute une ligne à la fin du Google Sheet.

    Args:
        spreadsheet_id: ID du sheet (dans l'URL Google Sheets)
        values: Liste de valeurs pour les colonnes [date_recue, fournisseur, no_facture, montant, ...]
        sheet_name: Nom de l'onglet (défaut: "Factures")
    """
    try:
        svc = _get_service()
        svc.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()
        log.info(f"sheets: ligne ajoutée dans {spreadsheet_id}")
    except Exception as e:
        log.error(f"sheets.append error: {e}")
        raise
