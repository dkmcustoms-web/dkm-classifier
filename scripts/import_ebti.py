"""Importeer de EBTI-export in een lokale SQLite-database.

Volledige export (eenmalig, ~1,04M records):
    python scripts/import_ebti.py --source "C:/Users/Luc/Downloads/EBTI"
    python scripts/import_ebti.py --source "C:/Users/Luc/Downloads/DDS2-EBTI_Full.zip"

Dagelijkse delta (voegt nieuwe BTI's toe en zet ingetrokken op INVALID):
    python scripts/import_ebti.py --delta "C:/Users/Luc/Downloads/DDS2-EBTI_20260720_044113.zip"
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tariff import ebti  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="EBTI-export importeren")
    ap.add_argument("--source", type=Path, help="map, zip of csv met de volledige export")
    ap.add_argument("--delta", type=Path, help="delta-zip van DDS2")
    ap.add_argument("--db", type=Path, default=ebti.DEFAULT_DB)
    ap.add_argument("--no-fts", action="store_true", help="sla de FTS-herbouw over")
    args = ap.parse_args()

    source = args.delta or args.source
    if not source:
        ap.error("geef --source of --delta op")
    if not source.exists():
        ap.error(f"bestaat niet: {source}")

    kind = "delta" if args.delta else "full"
    started = time.time()
    conn = ebti.connect(args.db, fast=True)

    def progress(name: str, seen: int, new: int) -> None:
        print(f"  {name:<24} {seen:>8} rijen  ({new:+} nieuw)  {time.time()-started:6.1f}s", flush=True)

    print(f"Importeren ({kind}) uit {source}")
    ebti.import_source(conn, source, kind=kind, progress=progress)

    if not args.no_fts:
        print("Zoekindex herbouwen...", flush=True)
        ebti.rebuild_fts(conn)

    print("\nDatabase:", args.db)
    for key, value in ebti.stats(conn).items():
        print(f"  {key:<16} {value}")
    print(f"\nKlaar in {time.time()-started:.1f}s")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
