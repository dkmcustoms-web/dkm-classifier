"""Bouw een testset uit de EBTI-data om de classificatietool objectief te scoren.

Elke geldige BTI is een gelabeld voorbeeld: de douane-omschrijving van de goederen
is de input, de toegekende CN-code is het juiste antwoord.

    python eval/build_testset.py --n 300 --set realistic
    python eval/build_testset.py --n 300 --set broad

Twee sets, want ze meten iets anders:
  realistic — NL/FR/EN BTI's, dicht bij wat DKM zelf binnenkrijgt
  broad     — alle talen, gespreid over hoofdstukken, breedste dekking

Belangrijke beperking: een BTI-omschrijving is geschreven door een douanebeambte
die de indeling al kende, en is dus preciezer dan een doorsnee factuurregel.
De score is daarom een bovengrens, geen voorspelling van praktijkprestatie.
Voor vergelijking tussen versies van de tool is dat prima — dat is wat we willen.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tariff import ebti  # noqa: E402

REALISTIC_LANGUAGES = ("nl", "fr", "en")

# Omschrijvingen die naar een bijlage of foto verwijzen zijn onbruikbaar:
# de bijlage zit niet in de export.
ATTACHMENT_MARKERS = re.compile(
    r"\b(siehe\s+anlage|voir\s+annexe|see\s+annex|zie\s+bijlage|vedi\s+allegato|"
    r"ver\s+anexo|foto\s+siehe)\b",
    re.IGNORECASE,
)

# Codes in de omschrijving zouden het antwoord weggeven.
CODE_LEAK = re.compile(r"\b\d{4}[\s.]?\d{2}[\s.]?\d{2,4}\b")


def usable(row) -> bool:
    desc = row["description"] or ""
    if len(desc) < 200 or len(desc) > 6000:
        return False
    if not row["cn8"] or len(row["cn8"]) != 8:
        return False
    if ATTACHMENT_MARKERS.search(desc):
        return False
    if CODE_LEAK.search(desc):
        return False
    return True


def sample_stratified(rows: list, n: int, key, rng: random.Random) -> list:
    """Spreid de trekking over de sleutel (hoofdstuk of taal) in plaats van
    de natuurlijke verdeling te volgen — anders is 60% van de testset Duits."""
    buckets: dict[str, list] = defaultdict(list)
    for row in rows:
        buckets[key(row)].append(row)
    for bucket in buckets.values():
        rng.shuffle(bucket)

    picked, order = [], sorted(buckets)
    while len(picked) < n and any(buckets[k] for k in order):
        for k in order:
            if buckets[k]:
                picked.append(buckets[k].pop())
                if len(picked) == n:
                    break
    rng.shuffle(picked)
    return picked


def main() -> int:
    ap = argparse.ArgumentParser(description="Testset bouwen uit EBTI")
    ap.add_argument("--db", type=Path, default=ebti.DEFAULT_DB)
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--set", choices=["realistic", "broad"], default="realistic")
    ap.add_argument("--seed", type=int, default=20260820)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    out = args.out or Path(__file__).parent / "testsets" / f"{args.set}_{args.n}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    conn = ebti.connect(args.db)
    sql = "SELECT * FROM bti WHERE status='VALID' AND description IS NOT NULL"
    params: list = []
    if args.set == "realistic":
        sql += f" AND language IN ({','.join('?' * len(REALISTIC_LANGUAGES))})"
        params += list(REALISTIC_LANGUAGES)

    pool = [r for r in conn.execute(sql, params) if usable(r)]
    print(f"Bruikbare geldige BTI's in de pool: {len(pool)}")
    if len(pool) < args.n:
        print(f"  let op: pool kleiner dan gevraagd ({args.n}), neem alles")

    key = (lambda r: r["language"] or "?") if args.set == "realistic" else (lambda r: r["chapter"] or "?")
    picked = sample_stratified(pool, min(args.n, len(pool)), key, rng)

    with out.open("w", encoding="utf-8") as fh:
        for i, row in enumerate(picked, 1):
            fh.write(json.dumps({
                "id": f"{args.set}-{i:04d}",
                "bti_reference": row["bti_reference"],
                "country": row["issuing_country"],
                "language": row["language"],
                "description": row["description"],
                "expected_cn8": row["cn8"],
                "expected_hs6": row["hs6"],
                "expected_heading": row["heading"],
                "expected_chapter": row["chapter"],
                "expected_full_code": row["nomenclature_code"],
                "justification": row["justification"],
                "keywords": row["keywords"],
                "end_date": row["end_date"],
            }, ensure_ascii=False) + "\n")

    by_lang: dict[str, int] = defaultdict(int)
    by_chapter: dict[str, int] = defaultdict(int)
    for row in picked:
        by_lang[row["language"] or "?"] += 1
        by_chapter[row["chapter"] or "?"] += 1

    print(f"\nGeschreven: {out}  ({len(picked)} voorbeelden)")
    print("  talen:      ", dict(sorted(by_lang.items(), key=lambda kv: -kv[1])))
    print(f"  hoofdstukken: {len(by_chapter)} verschillende")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
