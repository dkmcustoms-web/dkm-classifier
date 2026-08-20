"""Scoor een run tegen de verwachte CN-codes uit de testset.

    python eval/score.py --run eval/runs/realistic_300__sonnet420250514.jsonl
    python eval/score.py --run A.jsonl --run B.jsonl        # naast elkaar

Naast de trefzekerheid meet dit twee dingen die direct over het gemelde probleem
gaan ("twijfelt dikwijls en is onzeker"):

  twijfelgraad — hoe vaak de tool zelf om handmatige controle vraagt
  kalibratie   — of HIGH confidence ook echt vaker klopt dan LOW

Een tool die bij HIGH even vaak fout zit als bij LOW geeft geen informatie met
zijn confidence; dan is het label ruis en moet het uit de interface.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

DIGITS = re.compile(r"\D")


def clean(code) -> str:
    return DIGITS.sub("", str(code or ""))


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def evaluate(records: list[dict]) -> dict:
    n = len(records)
    hits = Counter()
    conf_buckets: dict[str, list[bool]] = defaultdict(list)
    outcome_buckets: dict[str, list[bool]] = defaultdict(list)
    lang_buckets: dict[str, list[bool]] = defaultdict(list)
    confusion: Counter = Counter()
    manual_review = 0
    bad_digits = {"cn": 0, "taric": 0}
    parse_fail = 0
    errors = 0

    for rec in records:
        if rec.get("error"):
            errors += 1
            continue
        j2, j3 = rec.get("json2") or {}, rec.get("json3") or {}
        if not j2:
            parse_fail += 1

        expected = clean(rec["expected_cn8"])
        # De definitieve code van de validator gaat voor, net als in app.py:293.
        got = clean(j3.get("validated_code")) or clean(j2.get("cn_code"))
        taric = clean(j3.get("taric_code")) or clean(j2.get("taric_code"))

        correct8 = len(got) >= 8 and got[:8] == expected
        hits["cn8"] += correct8
        hits["hs6"] += len(got) >= 6 and got[:6] == expected[:6]
        hits["heading"] += len(got) >= 4 and got[:4] == expected[:4]
        hits["chapter"] += len(got) >= 2 and got[:2] == expected[:2]

        if len(got) != 8:
            bad_digits["cn"] += 1
        if len(taric) != 10:
            bad_digits["taric"] += 1

        conf = (j2.get("confidence") or "?").upper().strip()
        conf_buckets[conf].append(correct8)
        outcome = (j3.get("validation_outcome") or "?").upper().strip()
        outcome_buckets[outcome].append(correct8)
        lang_buckets[rec.get("language") or "?"].append(correct8)

        if j2.get("manual_review_recommended") or j3.get("manual_review_recommended"):
            manual_review += 1
        if not correct8 and len(got) >= 2:
            confusion[(expected[:2], got[:2])] += 1

    scored = n - errors
    return {
        "n": n, "scored": scored, "errors": errors, "parse_fail": parse_fail,
        "hits": hits, "bad_digits": bad_digits, "manual_review": manual_review,
        "conf": conf_buckets, "outcome": outcome_buckets, "lang": lang_buckets,
        "confusion": confusion,
    }


def pct(part: int, total: int) -> str:
    return f"{100*part/total:5.1f}%" if total else "    —"


def report(name: str, r: dict) -> None:
    s = r["scored"] or 1
    print(f"\n{'='*72}\n{name}   ({r['scored']} gescoord, {r['errors']} fouten)\n{'='*72}")

    print("\nTREFZEKERHEID")
    for label, key in [("CN8 exact  (8 cijfers)", "cn8"), ("HS6        (6 cijfers)", "hs6"),
                       ("Post       (4 cijfers)", "heading"), ("Hoofdstuk  (2 cijfers)", "chapter")]:
        print(f"  {label:<24} {pct(r['hits'][key], s)}   {r['hits'][key]}/{s}")

    print("\nBRUIKBAARHEID VAN DE UITVOER")
    print(f"  geen geldige JSON        {pct(r['parse_fail'], s)}")
    print(f"  CN geen 8 cijfers        {pct(r['bad_digits']['cn'], s)}")
    print(f"  TARIC geen 10 cijfers    {pct(r['bad_digits']['taric'], s)}")
    print(f"  vraagt handmatige review {pct(r['manual_review'], s)}   <- twijfelgraad")

    print("\nKALIBRATIE — klopt HIGH vaker dan LOW?")
    for level in ["HIGH", "MEDIUM", "LOW", "?"]:
        vals = r["conf"].get(level)
        if vals:
            print(f"  {level:<8} {len(vals):>4} gevallen   correct {pct(sum(vals), len(vals))}")

    print("\nVALIDATIE-UITSPRAAK vs werkelijkheid")
    for outcome, vals in sorted(r["outcome"].items(), key=lambda kv: -len(kv[1])):
        print(f"  {outcome:<22} {len(vals):>4} gevallen   correct {pct(sum(vals), len(vals))}")

    if len(r["lang"]) > 1:
        print("\nPER TAAL")
        for lang, vals in sorted(r["lang"].items(), key=lambda kv: -len(kv[1])):
            print(f"  {lang:<8} {len(vals):>4} gevallen   correct {pct(sum(vals), len(vals))}")

    if r["confusion"]:
        print("\nMEEST VOORKOMENDE HOOFDSTUKVERWISSELINGEN (verwacht -> gekregen)")
        for (exp, got), count in r["confusion"].most_common(8):
            print(f"  hfdst {exp} -> {got}   {count}x")


def main() -> int:
    ap = argparse.ArgumentParser(description="Scoor classificatie-runs")
    ap.add_argument("--run", type=Path, action="append", required=True)
    args = ap.parse_args()

    results = []
    for path in args.run:
        r = evaluate(load(path))
        report(path.stem, r)
        results.append((path.stem, r))

    if len(results) > 1:
        print(f"\n{'='*72}\nVERGELIJKING\n{'='*72}")
        print(f"  {'run':<40} {'CN8':>8} {'HS6':>8} {'post':>8} {'twijfel':>9}")
        for name, r in results:
            s = r["scored"] or 1
            print(f"  {name[:40]:<40} {pct(r['hits']['cn8'], s):>8} {pct(r['hits']['hs6'], s):>8} "
                  f"{pct(r['hits']['heading'], s):>8} {pct(r['manual_review'], s):>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
