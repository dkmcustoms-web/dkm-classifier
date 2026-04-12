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
    "decision_tree",
    "raw_step1", "raw_step2", "raw_step3",
]


def _normalize_service_account(service_account_info) -> dict:
    if not isinstance(service_account_info, (dict, str)):
        service_account_info = dict(service_account_info)
    if isinstance(service_account_info, str):
        service_account_info = service_account_info.replace('\r', '')
        service_account_info = json.loads(service_account_info)
    if "private_key" in service_account_info:
        pk = service_account_info["private_key"]
        if "\\n" in pk:
            service_account_info["private_key"] = pk.replace("\\n", "\n")
    return service_account_info


def _get_client(service_account_info: dict):
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    return gspread.authorize(creds)


def _ensure_headers(sheet):
    if sheet.row_count == 0 or sheet.cell(1, 1).value != "timestamp":
        sheet.insert_row(HEADERS, index=1)


def log_to_sheets(row: dict, spreadsheet_id: str, service_account_info):
    service_account_info = _normalize_service_account(service_account_info)
    gc = _get_client(service_account_info)
    ss = gc.open_by_key(spreadsheet_id)
    try:
        sheet = ss.worksheet("History")
    except gspread.WorksheetNotFound:
        sheet = ss.add_worksheet(title="History", rows=1000, cols=len(HEADERS))
    _ensure_headers(sheet)
    values = [str(row.get(h, "")) for h in HEADERS]
    sheet.append_row(values, value_input_option="USER_ENTERED")
