import json
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Main classification history
HEADERS_HISTORY = [
    "timestamp", "user", "description", "specs",
    "has_image", "has_invoice",
    "product_id", "category", "data_quality",
    "cn_code", "taric_code", "confidence",
    "outcome", "validated_code", "manual_review", "issues",
    "decision_tree",
    "raw_step1", "raw_step2", "raw_step3",
    "senior_reviewed", "senior_user", "senior_timestamp",
    "senior_verdict", "senior_comment", "row_id",
]

# Verified codes lookup table
HEADERS_VERIFIED = [
    "row_id", "product_fingerprint",
    "cn_code", "taric_code",
    "senior_user", "senior_timestamp", "senior_comment",
    "original_description",
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


def _get_or_create_sheet(ss, title, headers):
    try:
        sheet = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        sheet = ss.add_worksheet(title=title, rows=2000, cols=len(headers))
    if sheet.row_count == 0 or sheet.cell(1, 1).value != headers[0]:
        sheet.insert_row(headers, index=1)
    return sheet


def _open_ss(spreadsheet_id: str, service_account_info):
    info = _normalize_service_account(service_account_info)
    gc  = _get_client(info)
    return gc.open_by_key(spreadsheet_id)


def log_to_sheets(row: dict, spreadsheet_id: str, service_account_info):
    ss    = _open_ss(spreadsheet_id, service_account_info)
    sheet = _get_or_create_sheet(ss, "History", HEADERS_HISTORY)
    values = [str(row.get(h, "")) for h in HEADERS_HISTORY]
    sheet.append_row(values, value_input_option="USER_ENTERED")


def get_pending_reviews(spreadsheet_id: str, service_account_info) -> list[dict]:
    """Return all rows in History that have not yet been senior-reviewed."""
    ss    = _open_ss(spreadsheet_id, service_account_info)
    sheet = _get_or_create_sheet(ss, "History", HEADERS_HISTORY)
    records = sheet.get_all_records(expected_headers=HEADERS_HISTORY)
    return [r for r in records if str(r.get("senior_reviewed","")).strip().lower() != "yes"]


def get_all_history(spreadsheet_id: str, service_account_info) -> list[dict]:
    ss    = _open_ss(spreadsheet_id, service_account_info)
    sheet = _get_or_create_sheet(ss, "History", HEADERS_HISTORY)
    return sheet.get_all_records(expected_headers=HEADERS_HISTORY)


def save_senior_review(row_id: str, verdict: str, comment: str,
                       senior_user: str, cn_code: str, taric_code: str,
                       description: str,
                       spreadsheet_id: str, service_account_info):
    """Write senior verdict back to History row and optionally add to Verified sheet."""
    ss      = _open_ss(spreadsheet_id, service_account_info)
    history = _get_or_create_sheet(ss, "History", HEADERS_HISTORY)
    records = history.get_all_records(expected_headers=HEADERS_HISTORY)
    now     = datetime.now_str()

    # Find and update the row in History
    for i, rec in enumerate(records):
        if str(rec.get("row_id","")) == str(row_id):
            data_row = i + 2  # +1 for header, +1 for 1-indexed
            col = lambda name: HEADERS_HISTORY.index(name) + 1
            history.update_cell(data_row, col("senior_reviewed"),  "yes")
            history.update_cell(data_row, col("senior_user"),       senior_user)
            history.update_cell(data_row, col("senior_timestamp"),  now)
            history.update_cell(data_row, col("senior_verdict"),    verdict)
            history.update_cell(data_row, col("senior_comment"),    comment)
            break

    # If CONFIRMED → add to Verified lookup table
    if verdict == "CONFIRMED":
        verified = _get_or_create_sheet(ss, "Verified", HEADERS_VERIFIED)
        fingerprint = _make_fingerprint(description)
        verified.append_row([
            row_id, fingerprint, cn_code, taric_code,
            senior_user, now, comment, description[:300]
        ], value_input_option="USER_ENTERED")


def lookup_verified(description: str, spreadsheet_id: str, service_account_info) -> dict | None:
    """Check if a similar product was already verified by a senior."""
    ss       = _open_ss(spreadsheet_id, service_account_info)
    verified = _get_or_create_sheet(ss, "Verified", HEADERS_VERIFIED)
    records  = verified.get_all_records(expected_headers=HEADERS_VERIFIED)
    fingerprint = _make_fingerprint(description)
    for rec in records:
        if rec.get("product_fingerprint","") == fingerprint:
            return rec
    return None


def _make_fingerprint(description: str) -> str:
    """Simple normalized fingerprint for description matching."""
    import re
    text = description.lower().strip()
    text = re.sub(r'[^a-z0-9 ]', ' ', text)
    words = sorted(set(text.split()))
    return " ".join(words[:30])


class datetime:
    @staticmethod
    def now_str():
        import datetime as _dt
        return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
