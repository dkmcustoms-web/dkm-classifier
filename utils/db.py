"""Neon (serverless Postgres) storage for the DKM Classifier.

Mirrors the function surface of utils/sheets.py so the application can use
either backend, or both. Every function takes the connection string as its
first argument; when no connection string is configured the caller skips this
module entirely and falls back to Google Sheets.

Connection string goes in .streamlit/secrets.toml:

    NEON_DATABASE_URL = "postgresql://user:pass@ep-xxx-pooler.eu-central-1.aws.neon.tech/dbname?sslmode=require"

Use the POOLED endpoint (the host containing "-pooler"). Streamlit opens a new
connection per interaction, and Neon's direct endpoint runs out of connections
quickly under that pattern.
"""

import json
from datetime import datetime, timezone

try:
    import psycopg
    from psycopg.rows import dict_row
    DRIVER = "psycopg3"
except ImportError:  # pragma: no cover - fallback for psycopg2-only environments
    psycopg = None
    dict_row = None
    DRIVER = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS classifications (
    row_id            TEXT PRIMARY KEY,
    batch_id          TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    app_timestamp     TEXT,
    app_user          TEXT,
    source            TEXT,
    description       TEXT,
    specs             TEXT,
    has_image         BOOLEAN,
    has_invoice       BOOLEAN,
    product_id        TEXT,
    category          TEXT,
    data_quality      TEXT,
    cn_code           TEXT,
    taric_code        TEXT,
    confidence        TEXT,
    outcome           TEXT,
    validated_code    TEXT,
    declared_code     TEXT,
    agreement         TEXT,
    manual_review     BOOLEAN,
    issues            TEXT,
    decision_tree     TEXT,
    raw_step1         TEXT,
    raw_step2         TEXT,
    raw_step3         TEXT,
    followup_qa       TEXT,
    cost_usd          NUMERIC(12,6) DEFAULT 0,
    input_tokens      INTEGER DEFAULT 0,
    output_tokens     INTEGER DEFAULT 0,
    senior_reviewed   BOOLEAN DEFAULT FALSE,
    senior_user       TEXT,
    senior_timestamp  TIMESTAMPTZ,
    senior_verdict    TEXT,
    senior_comment    TEXT
);

CREATE INDEX IF NOT EXISTS idx_cls_created  ON classifications (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cls_pending  ON classifications (senior_reviewed);
CREATE INDEX IF NOT EXISTS idx_cls_batch    ON classifications (batch_id);

CREATE TABLE IF NOT EXISTS verified_codes (
    id                  BIGSERIAL PRIMARY KEY,
    row_id              TEXT,
    product_fingerprint TEXT NOT NULL,
    cn_code             TEXT,
    taric_code          TEXT,
    senior_user         TEXT,
    senior_timestamp    TIMESTAMPTZ DEFAULT now(),
    senior_comment      TEXT,
    original_description TEXT
);

CREATE INDEX IF NOT EXISTS idx_ver_fp ON verified_codes (product_fingerprint);

CREATE TABLE IF NOT EXISTS usage_events (
    id             BIGSERIAL PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    app_user       TEXT,
    row_id         TEXT,
    batch_id       TEXT,
    page           TEXT,
    step           TEXT,
    model          TEXT,
    input_tokens   INTEGER DEFAULT 0,
    output_tokens  INTEGER DEFAULT 0,
    cache_read     INTEGER DEFAULT 0,
    cache_write    INTEGER DEFAULT 0,
    cost_usd       NUMERIC(12,6) DEFAULT 0,
    rate_known     BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_user    ON usage_events (app_user);
CREATE INDEX IF NOT EXISTS idx_usage_row     ON usage_events (row_id);
"""


def available() -> bool:
    return psycopg is not None


def _connect(dsn: str):
    if psycopg is None:
        raise RuntimeError(
            "psycopg is not installed. Add 'psycopg[binary]' to requirements.txt.")
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)


def _yes(value) -> bool:
    return str(value).strip().lower() in {"yes", "true", "1", "ja", "y"}


def init_schema(dsn: str):
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        conn.commit()


def ping(dsn: str) -> str:
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT version() AS v")
        return (cur.fetchone() or {}).get("v", "")


# ── writes ────────────────────────────────────────────────────────────────────

COLUMNS = [
    "row_id", "batch_id", "app_timestamp", "app_user", "source", "description",
    "specs", "has_image", "has_invoice", "product_id", "category", "data_quality",
    "cn_code", "taric_code", "confidence", "outcome", "validated_code",
    "declared_code", "agreement", "manual_review", "issues", "decision_tree",
    "raw_step1", "raw_step2", "raw_step3", "followup_qa",
    "cost_usd", "input_tokens", "output_tokens", "senior_reviewed",
]


def row_to_record(row: dict) -> dict:
    """Map an app-level row (the Sheets shape) onto the table columns."""
    return {
        "row_id":         str(row.get("row_id") or ""),
        "batch_id":       str(row.get("batch_id") or "") or None,
        "app_timestamp":  str(row.get("timestamp") or ""),
        "app_user":       str(row.get("user") or ""),
        "source":         str(row.get("source") or "classify"),
        "description":    str(row.get("description") or ""),
        "specs":          str(row.get("specs") or ""),
        "has_image":      _yes(row.get("has_image")),
        "has_invoice":    _yes(row.get("has_invoice")),
        "product_id":     str(row.get("product_id") or ""),
        "category":       str(row.get("category") or ""),
        "data_quality":   str(row.get("data_quality") or ""),
        "cn_code":        str(row.get("cn_code") or ""),
        "taric_code":     str(row.get("taric_code") or ""),
        "confidence":     str(row.get("confidence") or ""),
        "outcome":        str(row.get("outcome") or ""),
        "validated_code": str(row.get("validated_code") or ""),
        "declared_code":  str(row.get("declared_code") or ""),
        "agreement":      str(row.get("agreement") or ""),
        "manual_review":  _yes(row.get("manual_review")),
        "issues":         str(row.get("issues") or ""),
        "decision_tree":  str(row.get("decision_tree") or ""),
        "raw_step1":      str(row.get("raw_step1") or ""),
        "raw_step2":      str(row.get("raw_step2") or ""),
        "raw_step3":      str(row.get("raw_step3") or ""),
        "followup_qa":    str(row.get("followup_qa") or ""),
        "cost_usd":       float(row.get("cost_usd") or 0),
        "input_tokens":   int(row.get("input_tokens") or 0),
        "output_tokens":  int(row.get("output_tokens") or 0),
        "senior_reviewed": _yes(row.get("senior_reviewed")),
    }


def log_classification(dsn: str, row: dict):
    rec = row_to_record(row)
    cols = ", ".join(COLUMNS)
    ph   = ", ".join(f"%({c})s" for c in COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "row_id")
    sql = (f"INSERT INTO classifications ({cols}) VALUES ({ph}) "
           f"ON CONFLICT (row_id) DO UPDATE SET {updates}")
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, rec)
        conn.commit()


def log_usage_events(dsn: str, events: list):
    if not events:
        return
    sql = ("INSERT INTO usage_events (app_user, row_id, batch_id, page, step, model, "
           "input_tokens, output_tokens, cache_read, cache_write, cost_usd, rate_known) "
           "VALUES (%(app_user)s, %(row_id)s, %(batch_id)s, %(page)s, %(step)s, %(model)s, "
           "%(input_tokens)s, %(output_tokens)s, %(cache_read)s, %(cache_write)s, "
           "%(cost_usd)s, %(rate_known)s)")
    payload = [{
        "app_user":      str(e.get("user") or ""),
        "row_id":        str(e.get("row_id") or "") or None,
        "batch_id":      str(e.get("batch_id") or "") or None,
        "page":          str(e.get("page") or ""),
        "step":          str(e.get("step") or ""),
        "model":         str(e.get("model") or ""),
        "input_tokens":  int(e.get("input_tokens") or 0),
        "output_tokens": int(e.get("output_tokens") or 0),
        "cache_read":    int(e.get("cache_read") or 0),
        "cache_write":   int(e.get("cache_write") or 0),
        "cost_usd":      float(e.get("cost_usd") or 0),
        "rate_known":    bool(e.get("rate_known", True)),
    } for e in events]
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.executemany(sql, payload)
        conn.commit()


def save_senior_review(dsn: str, row_id: str, verdict: str, comment: str,
                       senior_user: str, cn_code: str, taric_code: str,
                       description: str, fingerprint: str):
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE classifications SET senior_reviewed = TRUE, senior_user = %s, "
            "senior_timestamp = %s, senior_verdict = %s, senior_comment = %s "
            "WHERE row_id = %s",
            (senior_user, datetime.now(timezone.utc), verdict, comment, str(row_id)))
        if verdict == "CONFIRMED":
            cur.execute(
                "INSERT INTO verified_codes (row_id, product_fingerprint, cn_code, "
                "taric_code, senior_user, senior_comment, original_description) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (str(row_id), fingerprint, cn_code, taric_code, senior_user,
                 comment, (description or "")[:300]))
        conn.commit()


# ── reads ─────────────────────────────────────────────────────────────────────

def _to_app_rows(records):
    """Present DB rows in the same shape the pages already expect."""
    out = []
    for r in records:
        d = dict(r)
        d["timestamp"] = d.pop("app_timestamp", "") or (
            d["created_at"].strftime("%Y-%m-%d %H:%M:%S") if d.get("created_at") else "")
        d["user"] = d.pop("app_user", "")
        d["senior_reviewed"] = "yes" if d.get("senior_reviewed") else "no"
        d["manual_review"]   = "yes" if d.get("manual_review") else "no"
        d["cost_usd"] = float(d.get("cost_usd") or 0)
        if d.get("senior_timestamp"):
            d["senior_timestamp"] = d["senior_timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        else:
            d["senior_timestamp"] = ""
        d.pop("created_at", None)
        out.append(d)
    return out


def get_pending_reviews(dsn: str, limit: int = 200) -> list:
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM classifications WHERE senior_reviewed = FALSE "
                    "ORDER BY created_at DESC LIMIT %s", (limit,))
        return _to_app_rows(cur.fetchall())


def get_all_history(dsn: str, limit: int = 2000) -> list:
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM classifications ORDER BY created_at DESC LIMIT %s",
                    (limit,))
        return _to_app_rows(cur.fetchall())


def lookup_verified(dsn: str, fingerprint: str):
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM verified_codes WHERE product_fingerprint = %s "
                    "ORDER BY senior_timestamp DESC LIMIT 1", (fingerprint,))
        rec = cur.fetchone()
        if not rec:
            return None
        rec = dict(rec)
        if rec.get("senior_timestamp"):
            rec["senior_timestamp"] = rec["senior_timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        return rec


def usage_summary(dsn: str, days: int = 30) -> dict:
    """Totals, plus breakdowns by day, user and model, over the last N days."""
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS calls, coalesce(sum(cost_usd),0) AS cost, "
            "coalesce(sum(input_tokens),0) AS input_tokens, "
            "coalesce(sum(output_tokens),0) AS output_tokens "
            "FROM usage_events WHERE created_at > now() - make_interval(days => %s)",
            (days,))
        totals = dict(cur.fetchone() or {})

        cur.execute(
            "SELECT date_trunc('day', created_at)::date AS day, count(*) AS calls, "
            "coalesce(sum(cost_usd),0) AS cost FROM usage_events "
            "WHERE created_at > now() - make_interval(days => %s) "
            "GROUP BY 1 ORDER BY 1", (days,))
        by_day = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT app_user, count(*) AS calls, coalesce(sum(cost_usd),0) AS cost "
            "FROM usage_events WHERE created_at > now() - make_interval(days => %s) "
            "GROUP BY 1 ORDER BY cost DESC", (days,))
        by_user = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT model, step, count(*) AS calls, coalesce(sum(cost_usd),0) AS cost "
            "FROM usage_events WHERE created_at > now() - make_interval(days => %s) "
            "GROUP BY 1,2 ORDER BY cost DESC", (days,))
        by_model = [dict(r) for r in cur.fetchall()]

    return {"totals": totals, "by_day": by_day, "by_user": by_user, "by_model": by_model}


def cost_per_dossier(dsn: str, limit: int = 25) -> list:
    with _connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT batch_id, count(*) AS lines, coalesce(sum(cost_usd),0) AS cost, "
            "min(created_at) AS started FROM classifications "
            "WHERE batch_id IS NOT NULL GROUP BY 1 ORDER BY started DESC LIMIT %s",
            (limit,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            d["started"] = d["started"].strftime("%Y-%m-%d %H:%M") if d.get("started") else ""
            rows.append(d)
        return rows
