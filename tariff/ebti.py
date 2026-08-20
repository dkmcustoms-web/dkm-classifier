"""EBTI data layer — load the DG TAXUD DDS2 export into local SQLite.

The DDS2 portal publishes two things:
  * DDS2-EBTI_Full.zip           — full snapshot, one CSV per issue year
  * DDS2-EBTI_<timestamp>.zip    — daily delta, one CSV with changed records

Both use the same 15 columns, so the same importer handles them. Records are
keyed on BTI_REFERENCE and upserted, which means a delta transparently adds new
BTIs and flips withdrawn ones to STATUS=INVALID.

Source quirks handled here:
  * NOMENCLATURE_CODE is right-padded with '*' to 22 chars  -> stripped
  * dates are DD/MM/YYYY in the full export, ISO in the delta -> normalised
  * the column name "DATE_OF _ISSUE" contains a stray space  -> kept as-is
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

csv.field_size_limit(10_000_000)

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "ebti.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bti (
    id                         INTEGER PRIMARY KEY,
    bti_reference              TEXT NOT NULL UNIQUE,
    issuing_country            TEXT,
    start_date                 TEXT,
    end_date                   TEXT,
    date_of_issue              TEXT,
    nomenclature_code          TEXT,
    cn8                        TEXT,
    hs6                        TEXT,
    heading                    TEXT,
    chapter                    TEXT,
    status                     TEXT,
    invalidation_reason        TEXT,
    invalidation_justification TEXT,
    language                   TEXT,
    place_of_issue             TEXT,
    name_and_address           TEXT,
    description                TEXT,
    justification              TEXT,
    keywords                   TEXT,
    source_file                TEXT,
    updated_at                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_bti_cn8      ON bti(cn8);
CREATE INDEX IF NOT EXISTS idx_bti_hs6      ON bti(hs6);
CREATE INDEX IF NOT EXISTS idx_bti_chapter  ON bti(chapter);
CREATE INDEX IF NOT EXISTS idx_bti_status   ON bti(status);
CREATE INDEX IF NOT EXISTS idx_bti_lang     ON bti(language);
CREATE INDEX IF NOT EXISTS idx_bti_end      ON bti(end_date);

CREATE VIRTUAL TABLE IF NOT EXISTS bti_fts USING fts5(
    description,
    keywords,
    justification,
    content='bti',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS import_log (
    id          INTEGER PRIMARY KEY,
    source      TEXT,
    kind        TEXT,
    rows_seen   INTEGER,
    rows_new    INTEGER,
    imported_at TEXT
);
"""

_UPSERT = """
INSERT INTO bti (
    bti_reference, issuing_country, start_date, end_date, date_of_issue,
    nomenclature_code, cn8, hs6, heading, chapter, status,
    invalidation_reason, invalidation_justification, language,
    place_of_issue, name_and_address, description, justification,
    keywords, source_file, updated_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(bti_reference) DO UPDATE SET
    issuing_country            = excluded.issuing_country,
    start_date                 = excluded.start_date,
    end_date                   = excluded.end_date,
    date_of_issue              = excluded.date_of_issue,
    nomenclature_code          = excluded.nomenclature_code,
    cn8                        = excluded.cn8,
    hs6                        = excluded.hs6,
    heading                    = excluded.heading,
    chapter                    = excluded.chapter,
    status                     = excluded.status,
    invalidation_reason        = excluded.invalidation_reason,
    invalidation_justification = excluded.invalidation_justification,
    language                   = excluded.language,
    place_of_issue             = excluded.place_of_issue,
    name_and_address           = excluded.name_and_address,
    description                = excluded.description,
    justification              = excluded.justification,
    keywords                   = excluded.keywords,
    source_file                = excluded.source_file,
    updated_at                 = excluded.updated_at
"""


# ── Normalisation ─────────────────────────────────────────────────────────────

_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y")


def parse_date(value: str | None) -> str | None:
    """Return an ISO date, or None. Accepts DD/MM/YYYY and ISO, with or without time."""
    v = (value or "").strip()
    if not v:
        return None
    v = v.split(" ")[0].split("T")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def split_code(raw: str | None) -> tuple[str | None, ...]:
    """'6307909899************' -> (full, cn8, hs6, heading, chapter)."""
    digits = re.sub(r"\D", "", (raw or ""))
    if len(digits) < 4:
        return (digits or None, None, None, None, None)
    return (
        digits,
        digits[:8] if len(digits) >= 8 else None,
        digits[:6] if len(digits) >= 6 else None,
        digits[:4],
        digits[:2],
    )


def _row_to_record(row: dict, source_file: str, now: str) -> tuple | None:
    ref = (row.get("BTI_REFERENCE") or "").strip()
    if not ref:
        return None
    full, cn8, hs6, heading, chapter = split_code(row.get("NOMENCLATURE_CODE"))
    return (
        ref,
        (row.get("ISSUING_COUNTRY") or "").strip().upper() or None,
        parse_date(row.get("START_DATE_OF_VALIDITY")),
        parse_date(row.get("END_DATE_OF_VALIDITY")),
        # The stray space in "DATE_OF _ISSUE" is in the source export.
        parse_date(row.get("DATE_OF _ISSUE") or row.get("DATE_OF_ISSUE")),
        full, cn8, hs6, heading, chapter,
        (row.get("STATUS") or "").strip().upper() or None,
        (row.get("INVALIDATION_REASON") or "").strip() or None,
        (row.get("INVALIDATION_JUSTIFICATION") or "").strip() or None,
        (row.get("LANGUAGE") or "").strip().lower() or None,
        (row.get("PLACE_OF_ISSUE") or "").strip() or None,
        (row.get("NAME_AND_ADDRESS") or "").strip() or None,
        (row.get("DESCRIPTION_OF_GOODS") or "").strip() or None,
        (row.get("CLASSIFICATION_JUSTIFICATION") or "").strip() or None,
        (row.get("KEYWORDS") or "").strip() or None,
        source_file,
        now,
    )


# ── Reading sources ───────────────────────────────────────────────────────────

def iter_csv_sources(source: Path) -> Iterator[tuple[str, Iterable[str]]]:
    """Yield (name, line-iterator) for every EBTI CSV in a file, zip or directory."""
    if source.is_dir():
        for path in sorted(source.glob("*.csv")):
            with path.open(encoding="utf-8-sig", newline="") as fh:
                yield path.name, fh
    elif source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as zf:
            for name in sorted(n for n in zf.namelist() if n.lower().endswith(".csv")):
                with zf.open(name) as raw:
                    yield name, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    elif source.suffix.lower() == ".csv":
        with source.open(encoding="utf-8-sig", newline="") as fh:
            yield source.name, fh
    else:
        raise ValueError(f"Onbekend brontype: {source}")


# ── Database ──────────────────────────────────────────────────────────────────

def connect(db_path: Path = DEFAULT_DB, *, fast: bool = False) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    if fast:
        # Import-only: durability is irrelevant, the source files are the truth.
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA cache_size=-200000")
    return conn


def rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO bti_fts(bti_fts) VALUES('rebuild')")
    conn.commit()


def import_source(
    conn: sqlite3.Connection,
    source: Path,
    *,
    kind: str = "full",
    batch_size: int = 5_000,
    progress=None,
) -> dict:
    """Import a full export, a delta zip or a single CSV. Returns per-file counts."""
    now = datetime.now().isoformat(timespec="seconds")
    counts: dict[str, int] = {}

    for name, lines in iter_csv_sources(Path(source)):
        before = conn.execute("SELECT COUNT(*) FROM bti").fetchone()[0]
        seen = 0
        batch: list[tuple] = []
        for row in csv.DictReader(lines):
            record = _row_to_record(row, name, now)
            if record is None:
                continue
            batch.append(record)
            seen += 1
            if len(batch) >= batch_size:
                conn.executemany(_UPSERT, batch)
                batch.clear()
        if batch:
            conn.executemany(_UPSERT, batch)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM bti").fetchone()[0]
        counts[name] = seen
        conn.execute(
            "INSERT INTO import_log (source, kind, rows_seen, rows_new, imported_at)"
            " VALUES (?,?,?,?,?)",
            (name, kind, seen, after - before, now),
        )
        conn.commit()
        if progress:
            progress(name, seen, after - before)

    return counts


def stats(conn: sqlite3.Connection) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "records": q("SELECT COUNT(*) FROM bti"),
        "valid": q("SELECT COUNT(*) FROM bti WHERE status='VALID'"),
        "with_cn8": q("SELECT COUNT(*) FROM bti WHERE cn8 IS NOT NULL"),
        "chapters": q("SELECT COUNT(DISTINCT chapter) FROM bti WHERE chapter IS NOT NULL"),
        "languages": q("SELECT COUNT(DISTINCT language) FROM bti"),
        "newest_end_date": q("SELECT MAX(end_date) FROM bti"),
    }


# ── Search ────────────────────────────────────────────────────────────────────

def fts_escape(query: str) -> str:
    """Turn free text into a safe FTS5 OR-query of quoted terms."""
    terms = [t for t in re.findall(r"\w{3,}", query, flags=re.UNICODE)]
    return " OR ".join(f'"{t}"' for t in terms)


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    valid_only: bool = False,
    languages: list[str] | None = None,
    chapter: str | None = None,
) -> list[sqlite3.Row]:
    """Lexical search over description + keywords + justification.

    This is the fase-1 retrieval: fast and precise on shared vocabulary, but blind
    across languages. Semantic retrieval comes on top of it in fase 3.
    """
    match = fts_escape(query)
    if not match:
        return []
    sql = [
        "SELECT b.*, bm25(bti_fts, 1.0, 2.0, 0.5) AS score",
        "FROM bti_fts JOIN bti b ON b.id = bti_fts.rowid",
        "WHERE bti_fts MATCH ?",
    ]
    params: list = [match]
    if valid_only:
        sql.append("AND b.status = 'VALID'")
    if languages:
        sql.append(f"AND b.language IN ({','.join('?' * len(languages))})")
        params += [l.lower() for l in languages]
    if chapter:
        sql.append("AND b.chapter = ?")
        params.append(chapter)
    sql.append("ORDER BY score LIMIT ?")
    params.append(limit)
    return conn.execute(" ".join(sql), params).fetchall()
