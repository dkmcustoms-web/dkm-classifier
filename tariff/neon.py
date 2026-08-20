"""BTI-corpus in Neon (serverless Postgres).

Waarom Postgres en niet het lokale SQLite-bestand: Streamlit Cloud heeft geen
plek voor een index van gigabytes, maar wel een netwerkverbinding. De index
staat één keer centraal, de dagelijkse delta werkt hem bij, en elke gebruiker
van de app bevraagt dezelfde actuele set.

Deze module gebruikt dezelfde verbindingsopzet als utils/db.py: psycopg3,
de gepoolde Neon-endpoint, DSN als eerste argument.

Zoeken gebeurt met de ingebouwde full-text search van Postgres, per taal
geconfigureerd — 61% van de geldige BTI's is Duitstalig, dus Duitse stemming
scheelt echt. De kolom voor semantische embeddings ligt klaar maar wordt nog
niet gevuld; dat is fase 3.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None
    dict_row = None


# Postgres levert deze tekstzoekconfiguraties standaard mee. Talen die er niet
# in staan (cs, pl, sk, sl, hr, bg, lt, lv, et, mt, ga) vallen terug op
# 'simple': geen stemming, wel tokenisatie en kleine letters.
TS_CONFIG = {
    "de": "german",   "fr": "french",     "nl": "dutch",      "en": "english",
    "es": "spanish",  "it": "italian",    "pt": "portuguese", "sv": "swedish",
    "da": "danish",   "fi": "finnish",    "no": "norwegian",  "ro": "romanian",
    "hu": "hungarian", "el": "greek",     "tr": "turkish",
}
FALLBACK_CONFIG = "simple"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bti (
    bti_reference       TEXT PRIMARY KEY,
    issuing_country     TEXT,
    start_date          DATE,
    end_date            DATE,
    date_of_issue       DATE,
    nomenclature_code   TEXT,
    cn8                 TEXT,
    hs6                 TEXT,
    heading             TEXT,
    chapter             TEXT,
    status              TEXT,
    invalidation_reason TEXT,
    language            TEXT,
    ts_config           TEXT,
    place_of_issue      TEXT,
    description         TEXT,
    justification       TEXT,
    keywords            TEXT,
    search_vector       TSVECTOR,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bti_search  ON bti USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS idx_bti_cn8     ON bti (cn8);
CREATE INDEX IF NOT EXISTS idx_bti_hs6     ON bti (hs6);
CREATE INDEX IF NOT EXISTS idx_bti_chapter ON bti (chapter);
CREATE INDEX IF NOT EXISTS idx_bti_status  ON bti (status);
CREATE INDEX IF NOT EXISTS idx_bti_lang    ON bti (language);

-- Samenvatting per CN8 over het VOLLEDIGE archief, dus inclusief de 922k
-- verlopen BTI's. Die passen niet integraal in Neon, maar hun bewijswaarde
-- ("ook 14 verlopen BTI's uit 6 lidstaten kwamen op deze code uit") past hier
-- wel in, voor een paar MB.
CREATE TABLE IF NOT EXISTS bti_code_stats (
    cn8          TEXT NOT NULL,
    status       TEXT NOT NULL,
    n            INTEGER NOT NULL,
    countries    INTEGER NOT NULL,
    country_list TEXT,
    first_year   INTEGER,
    last_year    INTEGER,
    PRIMARY KEY (cn8, status)
);

-- Bijhouden wat er geladen is, zodat een delta-run weet waar hij staat.
CREATE TABLE IF NOT EXISTS bti_load_log (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT,
    kind        TEXT,
    rows_loaded INTEGER,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

COLUMNS = [
    "bti_reference", "issuing_country", "start_date", "end_date", "date_of_issue",
    "nomenclature_code", "cn8", "hs6", "heading", "chapter", "status",
    "invalidation_reason", "language", "ts_config", "place_of_issue",
    "description", "justification", "keywords",
]

# De tsvector wordt in SQL berekend: keywords wegen het zwaarst (A), dan de
# omschrijving (B), dan de motivering (C).
_UPSERT = f"""
INSERT INTO bti ({', '.join(COLUMNS)}, search_vector, updated_at)
VALUES (
    {', '.join('%s' for _ in COLUMNS)},
    setweight(to_tsvector(%s::regconfig, coalesce(%s, '')), 'A') ||
    setweight(to_tsvector(%s::regconfig, coalesce(%s, '')), 'B') ||
    setweight(to_tsvector(%s::regconfig, coalesce(%s, '')), 'C'),
    now()
)
ON CONFLICT (bti_reference) DO UPDATE SET
    {', '.join(f'{c} = EXCLUDED.{c}' for c in COLUMNS if c != 'bti_reference')},
    search_vector = EXCLUDED.search_vector,
    updated_at    = now()
"""


def available() -> bool:
    return psycopg is not None


def connect(dsn: str):
    """Verbind met Neon.

    prepare_threshold=None zet de automatische prepared statements van psycopg3
    uit. Neon's gepoolde endpoint is PgBouncer in transaction mode: die kan een
    volgende query naar een andere serververbinding sturen, waar het prepared
    statement niet bestaat. Zonder deze instelling faalt een bulk-load daarop.
    """
    if psycopg is None:
        raise RuntimeError("psycopg ontbreekt. Voeg 'psycopg[binary]' toe aan requirements.txt.")
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=15,
                           prepare_threshold=None)


def init_schema(dsn: str) -> None:
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        conn.commit()


def installed_configs(conn) -> set[str]:
    """Welke tekstzoekconfiguraties heeft deze Postgres echt? Ontbrekende
    configuraties zouden de load laten crashen op een onbegrijpelijke fout."""
    with conn.cursor() as cur:
        cur.execute("SELECT cfgname FROM pg_ts_config")
        return {r["cfgname"] for r in cur.fetchall()}


def config_for(language: str | None, available_configs: set[str]) -> str:
    cfg = TS_CONFIG.get((language or "").lower(), FALLBACK_CONFIG)
    return cfg if cfg in available_configs else FALLBACK_CONFIG


def storage_report(dsn: str) -> dict:
    """Hoeveel plek neemt dit in? Bepaalt welk Neon-plan nodig is."""
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM bti")
        rows = cur.fetchone()["n"]
        cur.execute("""
            SELECT pg_size_pretty(pg_total_relation_size('bti'))  AS bti_total,
                   pg_size_pretty(pg_relation_size('bti'))        AS bti_table,
                   pg_size_pretty(pg_indexes_size('bti'))         AS bti_indexes,
                   pg_size_pretty(pg_database_size(current_database())) AS database
        """)
        sizes = dict(cur.fetchone())
    return {"rows": rows, **sizes}


# ── Laden vanuit de lokale SQLite-index ───────────────────────────────────────

def _sqlite_rows(sqlite_path: Path, *, valid_only: bool, batch: int) -> Iterator[list[sqlite3.Row]]:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM bti"
    if valid_only:
        sql += " WHERE status = 'VALID'"
    cur = conn.execute(sql)
    while chunk := cur.fetchmany(batch):
        yield chunk
    conn.close()


def load_from_sqlite(
    dsn: str,
    sqlite_path: Path,
    *,
    valid_only: bool = True,
    batch: int = 2_000,
    progress=None,
) -> int:
    """Kopieer de lokale SQLite-index naar Neon. Idempotent: een tweede run
    werkt bestaande records bij in plaats van te verdubbelen."""
    total = 0
    with connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
        configs = installed_configs(conn)

        for chunk in _sqlite_rows(sqlite_path, valid_only=valid_only, batch=batch):
            payload = []
            for row in chunk:
                cfg = config_for(row["language"], configs)
                payload.append((
                    row["bti_reference"], row["issuing_country"], row["start_date"],
                    row["end_date"], row["date_of_issue"], row["nomenclature_code"],
                    row["cn8"], row["hs6"], row["heading"], row["chapter"],
                    row["status"], row["invalidation_reason"], row["language"], cfg,
                    row["place_of_issue"], row["description"], row["justification"],
                    row["keywords"],
                    # argumenten voor de drie to_tsvector-aanroepen
                    cfg, row["keywords"], cfg, row["description"], cfg, row["justification"],
                ))
            with conn.cursor() as cur:
                cur.executemany(_UPSERT, payload)
            conn.commit()
            total += len(payload)
            if progress:
                progress(total)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bti_load_log (source, kind, rows_loaded) VALUES (%s, %s, %s)",
                (str(sqlite_path), "valid_only" if valid_only else "full", total))
        conn.commit()
    return total


# ── Zoeken ────────────────────────────────────────────────────────────────────

def search(
    dsn: str,
    query: str,
    *,
    language: str | None = None,
    limit: int = 20,
    valid_only: bool = True,
    chapter: str | None = None,
) -> list[dict]:
    """Full-text zoeken over keywords + omschrijving + motivering.

    De vraag wordt gesteld in de configuratie van de opgegeven taal; de index
    is per record in de eigen taal opgebouwd. Dat werkt binnen een taal goed en
    over talen heen matig — daarvoor komen in fase 3 embeddings bij.
    """
    with connect(dsn) as conn:
        cfg = config_for(language, installed_configs(conn))
        sql = [
            "SELECT bti_reference, issuing_country, language, cn8, hs6, chapter,",
            "       status, start_date, end_date, description, justification, keywords,",
            "       ts_rank(search_vector, q) AS rank",
            "FROM bti, websearch_to_tsquery(%s::regconfig, %s) AS q",
            "WHERE search_vector @@ q",
        ]
        params: list = [cfg, query]
        if valid_only:
            sql.append("AND status = 'VALID'")
        if chapter:
            sql.append("AND chapter = %s")
            params.append(chapter)
        sql.append("ORDER BY rank DESC LIMIT %s")
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(" ".join(sql), params)
            return [dict(r) for r in cur.fetchall()]


def load_code_stats(dsn: str, sqlite_path: Path, *, progress=None) -> int:
    """Vul bti_code_stats uit het VOLLEDIGE lokale archief.

    Zo telt het verlopen materiaal mee in de onderbouwing zonder dat de 922k
    verlopen records zelf naar Neon moeten. Kost een paar MB in plaats van 2 GB.
    """
    conn_lite = sqlite3.connect(sqlite_path)
    rows = conn_lite.execute("""
        SELECT cn8, status, COUNT(*) AS n,
               COUNT(DISTINCT issuing_country) AS countries,
               GROUP_CONCAT(DISTINCT issuing_country) AS country_list,
               MIN(substr(start_date, 1, 4)) AS first_year,
               MAX(substr(start_date, 1, 4)) AS last_year
        FROM bti
        WHERE cn8 IS NOT NULL AND status IS NOT NULL
        GROUP BY cn8, status
    """).fetchall()
    conn_lite.close()

    payload = [
        (cn8, status, n, countries, country_list,
         int(first_year) if first_year and first_year.isdigit() else None,
         int(last_year) if last_year and last_year.isdigit() else None)
        for cn8, status, n, countries, country_list, first_year, last_year in rows
    ]

    with connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute("TRUNCATE bti_code_stats")
            cur.executemany(
                "INSERT INTO bti_code_stats "
                "(cn8, status, n, countries, country_list, first_year, last_year) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                payload)
        conn.commit()
    if progress:
        progress(len(payload))
    return len(payload)


def code_evidence(dsn: str, cn8: str) -> dict:
    """Bewijsmateriaal achter één CN8-code: hoeveel BTI's, uit welke lidstaten,
    geldig én verlopen. Dit voedt de bewijsgebaseerde confidence uit fase 3."""
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, n, countries, country_list, first_year, last_year "
            "FROM bti_code_stats WHERE cn8 = %s", (cn8,))
        stats = {r["status"]: dict(r) for r in cur.fetchall()}

        # De geldige BTI's staan voluit in Neon, dus daarvan halen we ook
        # concrete voorbeelden op om aan de gebruiker te tonen.
        cur.execute(
            "SELECT bti_reference, issuing_country, language, end_date, "
            "       left(description, 300) AS snippet "
            "FROM bti WHERE cn8 = %s AND status = 'VALID' "
            "ORDER BY end_date DESC LIMIT 5", (cn8,))
        examples = [dict(r) for r in cur.fetchall()]

    return {"stats": stats, "examples": examples}
