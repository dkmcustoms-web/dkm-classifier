"""Laad de BTI-index vanuit het lokale SQLite-bestand naar Neon.

    # eerst controleren of de verbinding werkt en hoeveel plek er is
    python scripts/load_neon.py --check

    # geldige BTI's laden (aanbevolen start, ~123k records)
    python scripts/load_neon.py

    # alles laden, inclusief verlopen BTI's (~1,04M records)
    python scripts/load_neon.py --include-expired

De verbindingsreeks komt uit NEON_DATABASE_URL in de omgeving of uit
.streamlit/secrets.toml. Gebruik de gepoolde endpoint (host bevat "-pooler").

Draaien is idempotent: bestaande records worden bijgewerkt, niet verdubbeld.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tariff import ebti, neon  # noqa: E402


def dsn_from_config() -> str:
    if url := os.environ.get("NEON_DATABASE_URL"):
        return url
    secrets = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"
    if secrets.exists():
        with secrets.open("rb") as fh:
            if url := tomllib.load(fh).get("NEON_DATABASE_URL"):
                return url
    raise SystemExit(
        "Geen NEON_DATABASE_URL gevonden.\n"
        "Zet hem in de omgeving of in .streamlit/secrets.toml "
        "(zie secrets.toml.example). Gebruik de gepoolde endpoint."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="BTI-index naar Neon laden")
    ap.add_argument("--sqlite", type=Path, default=ebti.DEFAULT_DB)
    ap.add_argument("--include-expired", action="store_true",
                    help="ook verlopen BTI's laden (veel meer opslag)")
    ap.add_argument("--check", action="store_true",
                    help="alleen verbinding en opslag rapporteren")
    ap.add_argument("--code-stats", action="store_true",
                    help="alleen de samenvatting per CN8 herberekenen "
                         "(telt het volledige archief mee, ook verlopen BTI's)")
    ap.add_argument("--batch", type=int, default=2000)
    args = ap.parse_args()

    if not neon.available():
        raise SystemExit("psycopg ontbreekt: pip install 'psycopg[binary]'")

    dsn = dsn_from_config()

    if args.check:
        with neon.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT version() AS v")
            print("Verbonden:", cur.fetchone()["v"].split(",")[0])
            configs = neon.installed_configs(conn)
        print("Tekstzoekconfiguraties:", len(configs), "beschikbaar")
        missing = {v for v in neon.TS_CONFIG.values()} - configs
        if missing:
            print("  ontbreekt (valt terug op 'simple'):", ", ".join(sorted(missing)))
        neon.init_schema(dsn)
        for key, value in neon.storage_report(dsn).items():
            print(f"  {key:<14} {value}")
        return 0

    if not args.sqlite.exists():
        raise SystemExit(f"Lokale index ontbreekt: {args.sqlite}\n"
                         f"Draai eerst: python scripts/import_ebti.py --source <EBTI-map>")

    if args.code_stats:
        started = time.time()
        n = neon.load_code_stats(dsn, args.sqlite)
        print(f"{n:,} CN8-samenvattingen geladen in {time.time()-started:.1f}s "
              f"(volledig archief, incl. verlopen BTI's)")
        return 0

    valid_only = not args.include_expired
    scope = "geldige BTI's" if valid_only else "alle BTI's (incl. verlopen)"
    print(f"Laden naar Neon: {scope}")
    started = time.time()

    def progress(total: int) -> None:
        if total % 20_000 == 0:
            rate = total / max(time.time() - started, 1)
            print(f"  {total:>9,} records   {rate:6.0f}/s", flush=True)

    total = neon.load_from_sqlite(dsn, args.sqlite, valid_only=valid_only,
                                  batch=args.batch, progress=progress)

    # De samenvatting per CN8 dekt altijd het volledige archief, ook wanneer
    # alleen de geldige BTI's naar Neon gaan.
    stats_rows = neon.load_code_stats(dsn, args.sqlite)
    print(f"  {stats_rows:,} CN8-samenvattingen (volledig archief)")

    print(f"\n{total:,} records geladen in {(time.time()-started)/60:.1f} min")
    for key, value in neon.storage_report(dsn).items():
        print(f"  {key:<14} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
