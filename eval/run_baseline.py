"""Draai de huidige 3-staps pipeline op een testset en log elk resultaat.

    python eval/run_baseline.py --testset eval/testsets/realistic_300.jsonl
    python eval/run_baseline.py --testset eval/testsets/realistic_300.jsonl --model claude-opus-5

Gebruikt exact dezelfde prompts als de app (utils/prompts.py) en standaard
hetzelfde model als app.py, zodat de meting weergeeft wat er vandaag in
productie gebeurt. Met --model meet je "zelfde prompts, ander model".

De run is hervatbaar: bestaande resultaten in het uitvoerbestand worden
overgeslagen, dus onderbreken en opnieuw starten is veilig.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import Anthropic  # noqa: E402

from utils.prompts import PROMPT1, PROMPT2, PROMPT3  # noqa: E402

# Zelfde model als app.py:116 — verander dit niet zonder ook de app te wijzigen.
PRODUCTION_MODEL = "claude-sonnet-4-20250514"

# Modellen die adaptief denken en output_config.effort ondersteunen.
MODERN = ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
          "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5")

_print_lock = threading.Lock()


def api_key() -> str:
    if key := os.environ.get("ANTHROPIC_API_KEY"):
        return key
    secrets = Path(__file__).resolve().parent.parent / ".streamlit" / "secrets.toml"
    if secrets.exists():
        with secrets.open("rb") as fh:
            if key := tomllib.load(fh).get("ANTHROPIC_API_KEY"):
                return key
    raise SystemExit(
        "Geen API-sleutel gevonden. Zet ANTHROPIC_API_KEY in de omgeving "
        "of in .streamlit/secrets.toml"
    )


def extract_json(text: str):
    """Zelfde tolerante JSON-extractie als app.py:102 — inclusief zijn zwakte:
    hij pakt de laatste '{' en faalt op geneste objecten aan het eind."""
    idx = text.rfind("{")
    if idx == -1:
        return None
    try:
        return json.loads(text[idx:])
    except Exception:
        return None


class Runner:
    def __init__(self, model: str, max_tokens: int, effort: str | None):
        self.client = Anthropic(api_key=api_key(), max_retries=5, timeout=300.0)
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.modern = any(model.startswith(m) for m in MODERN)
        self.usage = {"input": 0, "output": 0}
        self._usage_lock = threading.Lock()

    def call(self, system: str, user: str) -> str:
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": [{"type": "text", "text": user}]}],
        )
        if self.modern:
            kwargs["thinking"] = {"type": "adaptive"}
            if self.effort:
                kwargs["output_config"] = {"effort": self.effort}
        resp = self.client.messages.create(**kwargs)
        with self._usage_lock:
            self.usage["input"] += resp.usage.input_tokens
            self.usage["output"] += resp.usage.output_tokens
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    def classify(self, description: str) -> dict:
        """De drie stappen uit app.py:249 (run_pipeline), tekst-only."""
        t0 = time.time()
        raw1 = self.call(PROMPT1, f"Product description / invoice text:\n{description}")
        json1 = extract_json(raw1)

        step2 = "Structured product data from feature extractor:\n\n" + (
            json.dumps(json1, indent=2) if json1 else raw1)
        raw2 = self.call(PROMPT2, step2)
        json2 = extract_json(raw2)

        step3 = (
            f"Product data:\n{json.dumps(json1, indent=2) if json1 else description}\n\n"
            f"Proposed classification:\n{json.dumps(json2, indent=2) if json2 else raw2}\n\n"
            f"Full reasoning:\n{raw2}"
        )
        raw3 = self.call(PROMPT3, step3)
        json3 = extract_json(raw3)

        return {
            "json1": json1, "json2": json2, "json3": json3,
            "raw2_tail": raw2[-1500:], "raw3_tail": raw3[-1500:],
            "seconds": round(time.time() - t0, 1),
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="Baseline-meting van de classificatiepipeline")
    ap.add_argument("--testset", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--model", default=PRODUCTION_MODEL)
    ap.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-tokens", type=int, default=None,
                    help="standaard 2000 (zoals app.py), 8000 voor modellen met denken")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    cases = [json.loads(line) for line in args.testset.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        cases = cases[: args.limit]

    tag = args.model.replace("claude-", "").replace("-", "")
    out = args.out or Path(__file__).parent / "runs" / f"{args.testset.stem}__{tag}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    done: set[str] = set()
    failed = 0
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            # Mislukte gevallen tellen niet als klaar — die willen we opnieuw.
            if record.get("error"):
                failed += 1
            else:
                done.add(record["id"])
        print(f"Hervat: {len(done)} geslaagd, {failed} mislukt (worden opnieuw gedraaid)")

    todo = [c for c in cases if c["id"] not in done]
    if not todo:
        print("Niets te doen — alle voorbeelden zijn al gedraaid.")
        return 0

    modern = any(args.model.startswith(m) for m in MODERN)
    max_tokens = args.max_tokens or (8000 if modern else 2000)
    runner = Runner(args.model, max_tokens, args.effort)

    # Preflight: één goedkope call, zodat een kapotte sleutel of een verkeerde
    # modelnaam meteen zichtbaar is en niet pas na 900 mislukte aanroepen.
    try:
        runner.client.messages.create(
            model=args.model, max_tokens=16,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Preflight mislukt ({type(exc).__name__}): {exc}")

    print(f"Model {args.model} | {len(todo)} voorbeelden | {args.workers} parallel "
          f"| max_tokens={max_tokens}" + (f" | effort={args.effort}" if args.effort else ""))
    started = time.time()
    fh = out.open("a", encoding="utf-8")
    write_lock = threading.Lock()
    completed = 0

    def work(case: dict) -> dict:
        try:
            result = runner.classify(case["description"])
            error = None
        except Exception as exc:  # noqa: BLE001 — één kapot geval mag de run niet stoppen
            result, error = {}, f"{type(exc).__name__}: {exc}"
        return {
            "id": case["id"],
            "bti_reference": case["bti_reference"],
            "language": case["language"],
            "expected_cn8": case["expected_cn8"],
            "model": args.model,
            "error": error,
            **result,
        }

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, c) for c in todo]
        for future in as_completed(futures):
            record = future.result()
            with write_lock:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                completed += 1
            with _print_lock:
                got = ((record.get("json2") or {}).get("cn_code") or "?")[:8]
                mark = "!" if record.get("error") else ("+" if got == record["expected_cn8"] else ".")
                print(f"  [{completed}/{len(todo)}] {mark} {record['id']} "
                      f"verwacht {record['expected_cn8']} kreeg {got}", flush=True)

    fh.close()
    elapsed = time.time() - started
    print(f"\nKlaar in {elapsed/60:.1f} min -> {out}")
    print(f"Tokens: {runner.usage['input']:,} in / {runner.usage['output']:,} uit")
    print(f"Scoren:  python eval/score.py --run {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
