import json
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "timestamp", "user", "description", "specs",
    "has_image", "has_invoice",
    "product_id", "category", "data_quality",
    "cn_code", "taric_code", "confidence",
    "outcome", "validated_code", "manual_review", "issues",
    "raw_step1", "raw_step2", "raw_step3",
]


def _get_client(service_account_info: dict):
    """Build an authenticated gspread client from a service account dict."""
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return gspread.authorize(creds)


def _ensure_headers(sheet):
    """Add header row if the sheet is empty."""
    if sheet.row_count == 0 or sheet.cell(1, 1).value != "timestamp":
        sheet.insert_row(HEADERS, index=1)


def log_to_sheets(row: dict, spreadsheet_id: str, service_account_info):
    """
    Append one row to the 'History' worksheet of the given spreadsheet.

    Parameters
    ----------
    row : dict
        Keys must match HEADERS list above.
    spreadsheet_id : str
        The Google Sheets document ID (from the URL).
    service_account_info : dict | str
        Service account JSON as a dict, or a JSON string.
        In Streamlit this comes from st.secrets["GOOGLE_SERVICE_ACCOUNT"].
    """
    if isinstance(service_account_info, str):
        service_account_info = json.loads(service_account_info)

    gc = _get_client(service_account_info)
    ss = gc.open_by_key(spreadsheet_id)

    # Use or create the 'History' worksheet
    try:
        sheet = ss.worksheet("History")
    except gspread.WorksheetNotFound:
        sheet = ss.add_worksheet(title="History", rows=1000, cols=len(HEADERS))

    _ensure_headers(sheet)

    values = [str(row.get(h, "")) for h in HEADERS]
    sheet.append_row(values, value_input_option="USER_ENTERED")
