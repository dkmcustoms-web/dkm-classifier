"""Deterministic checks for a customs preparation dossier.

Design rule: every number in this module is computed in Python, never by the
language model. Arithmetic that decides a customs value must be reproducible
and identical on every run — an LLM is the wrong tool for it. The model is
used only for reading documents and for forming a classification opinion.
"""

import io
import re
import csv
from datetime import datetime

# XOF and a few other currencies pegged to the euro at a fixed rate.
FIXED_EUR_RATES = {
    "XOF": 655.957,   # CFA franc BCEAO
    "XAF": 655.957,   # CFA franc BEAC
    "XPF": 119.3317,
}

COLUMN_SYNONYMS = {
    "product":  ["produits", "produit", "product", "omschrijving", "description",
                 "designation", "désignation", "goederen", "marchandise", "artikel"],
    "hs_code":  ["hs code", "hscode", "hs", "code", "gn code", "cn code", "taric",
                 "goederencode", "commodity code", "nomenclature"],
    "packages": ["colis", "nombre de colis", "colli", "packages", "aantal",
                 "aantal colli", "pieces", "pcs", "cartons"],
    "gross":    ["pb", "poids brut", "brutogewicht", "bruto", "gross", "gross weight",
                 "poids brut en kg"],
    "net":      ["pn", "poids net", "nettogewicht", "netto", "net", "net weight",
                 "poids net en kg"],
    "price":    ["p/u", "pu", "prix unitaire", "unit price", "eenheidsprijs",
                 "prijs per eenheid"],
    "amount":   ["montant", "amount", "bedrag", "prix total", "total price",
                 "totaal", "value", "waarde"],
}

TOTAL_MARKERS = ["totaal", "total", "eindtotaal", "grand total", "total général",
                 "totaal generaal", "somme"]


# ── helpers ───────────────────────────────────────────────────────────────────

def to_num(value):
    """Parse a number from a cell that may be text with , or . as decimal mark."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s or s in {"-", ".", ","}:
        return None
    if "," in s and "." in s:
        # last separator is the decimal mark
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # a single comma: decimal mark unless it groups thousands (1,234)
        if re.fullmatch(r"-?\d{1,3}(,\d{3})+", s):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def normalize_code(value):
    """Return only the digits of a goods code."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\D", "", str(value))


def is_total_row(cells):
    """A subtotal/total row: no product name, but a total marker somewhere."""
    text = " ".join(str(c).lower() for c in cells if c is not None)
    first = str(cells[0]).strip() if cells and cells[0] is not None else ""
    if first:
        return False
    return any(m in text for m in TOTAL_MARKERS)


def _match_header(cell):
    t = re.sub(r"\s+", " ", str(cell or "").strip().lower())
    if not t:
        return None
    for field, names in COLUMN_SYNONYMS.items():
        if t in names:
            return field
    for field, names in COLUMN_SYNONYMS.items():
        for n in names:
            if t.startswith(n) or n in t:
                return field
    return None


# ── parsing ───────────────────────────────────────────────────────────────────

def read_grid(raw: bytes, filename: str):
    """Return the file as a list of rows (lists of cell values)."""
    name = (filename or "").lower()
    if name.endswith((".csv", ".tsv", ".txt")):
        text = raw.decode("utf-8", errors="replace")
        sample = text[:4000]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
            delim = dialect.delimiter
        except Exception:
            delim = ";" if sample.count(";") > sample.count(",") else ","
        return [row for row in csv.reader(io.StringIO(text), delimiter=delim)]

    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    best, best_score = [], -1
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        score = sum(1 for r in rows if any(c is not None for c in r))
        if score > best_score:
            best, best_score = rows, score
    return best


def parse_prep_grid(grid):
    """Map a grid onto item rows plus the stated totals.

    Returns (items, totals, colmap, notes).
    """
    notes = []
    header_idx, colmap = None, {}
    for i, row in enumerate(grid[:25]):
        mapping, hits = {}, 0
        for j, cell in enumerate(row):
            field = _match_header(cell)
            if field and field not in mapping:
                mapping[field] = j
                hits += 1
        if hits >= 3 and "amount" in mapping or hits >= 4:
            header_idx, colmap = i, mapping
            break
    if header_idx is None:
        return [], {}, {}, ["Geen herkenbare kolomkoppen gevonden."]

    missing = [f for f in ("product", "amount") if f not in colmap]
    if missing:
        notes.append("Ontbrekende kolommen: " + ", ".join(missing))

    def cell(row, field):
        j = colmap.get(field)
        if j is None or j >= len(row):
            return None
        return row[j]

    items, totals = [], {}
    for row in grid[header_idx + 1:]:
        if not any(c is not None and str(c).strip() != "" for c in row):
            continue
        if is_total_row(row):
            label = " ".join(str(c) for c in row if c).lower()
            target = totals.setdefault(
                "grand" if "eind" in label or "grand" in label or "général" in label
                else "sub", {})
            if "grand" in totals or "eind" in label or "général" in label:
                totals["grand"] = {
                    "packages": to_num(cell(row, "packages")),
                    "gross":    to_num(cell(row, "gross")),
                    "net":      to_num(cell(row, "net")),
                    "amount":   to_num(cell(row, "amount")),
                }
            else:
                target.update({})
            continue
        product = str(cell(row, "product") or "").strip()
        if not product:
            continue
        items.append({
            "product":  product,
            "hs_code":  normalize_code(cell(row, "hs_code")),
            "packages": to_num(cell(row, "packages")),
            "gross":    to_num(cell(row, "gross")),
            "net":      to_num(cell(row, "net")),
            "price":    to_num(cell(row, "price")),
            "amount":   to_num(cell(row, "amount")),
        })
    return items, totals, colmap, notes


def parse_prep_file(raw: bytes, filename: str):
    return parse_prep_grid(read_grid(raw, filename))


# ── checks ────────────────────────────────────────────────────────────────────

def finding(sev, code, message, line=None, detail=""):
    return {"severity": sev, "code": code, "message": message,
            "line": line, "detail": detail}


def detect_amount_basis(items, tol=0.01):
    """Which quantity was the line amount computed on: net, gross or packages?"""
    scores = {}
    for basis in ("net", "gross", "packages"):
        hits = 0
        for it in items:
            q, p, a = it.get(basis), it.get("price"), it.get("amount")
            if q and p and a and abs(q * p - a) <= max(tol, abs(a) * 1e-6):
                hits += 1
        scores[basis] = hits
    basis = max(scores, key=scores.get)
    return (basis, scores) if scores[basis] else (None, scores)


def check_line_math(items, basis, tol=0.01):
    out = []
    if not basis:
        out.append(finding("warning", "NO_BASIS",
                  "Kon niet vaststellen waarop de regelbedragen zijn berekend "
                  "(netto, bruto of aantal colli). Bedragen niet automatisch gecontroleerd."))
        return out
    label = {"net": "nettogewicht", "gross": "brutogewicht", "packages": "aantal colli"}[basis]
    for it in items:
        q, p, a = it.get(basis), it.get("price"), it.get("amount")
        if q is None or p is None or a is None:
            out.append(finding("warning", "MATH_INCOMPLETE",
                      f"Bedrag niet controleerbaar: ontbrekende waarde.", it["product"]))
            continue
        calc = round(q * p, 2)
        if abs(calc - a) > max(tol, abs(a) * 1e-6):
            out.append(finding("error", "MATH_LINE",
                      f"Regelbedrag klopt niet: {label} {q:,.4g} × {p:,.4g} = {calc:,.2f}, "
                      f"opgegeven {a:,.2f} (verschil {a - calc:+,.2f}).", it["product"],
                      detail=f"impliciet {label} = {a / p:,.4f}" if p else ""))
    return out


def check_totals(items, totals, tol=0.01):
    out = []
    grand = (totals or {}).get("grand") or {}
    labels = {"packages": "aantal colli", "gross": "brutogewicht",
              "net": "nettogewicht", "amount": "totaalbedrag"}
    for field, label in labels.items():
        vals = [it.get(field) for it in items if it.get(field) is not None]
        if not vals:
            continue
        s = round(sum(vals), 4)
        stated = grand.get(field)
        if stated is None:
            continue
        if abs(s - stated) > max(tol, abs(stated) * 1e-6):
            out.append(finding("error", "MATH_TOTAL",
                      f"Opgeteld {label} ({s:,.2f}) wijkt af van het vermelde "
                      f"eindtotaal ({stated:,.2f}); verschil {stated - s:+,.2f}."))
    return out


def check_weights(items, tol=0.005):
    """Net > gross is impossible; a deviating tare ratio is worth a look."""
    out, ratios = [], []
    for it in items:
        g, n = it.get("gross"), it.get("net")
        if g and n:
            if n > g + 1e-9:
                out.append(finding("error", "WEIGHT_NET_GT_GROSS",
                          f"Nettogewicht ({n:,.2f}) is hoger dan brutogewicht ({g:,.2f}).",
                          it["product"]))
            else:
                ratios.append((it["product"], round(n / g, 6)))
    if len(ratios) >= 4:
        counts = {}
        for _, r in ratios:
            counts[r] = counts.get(r, 0) + 1
        modal, n_modal = max(counts.items(), key=lambda kv: kv[1])
        if n_modal >= max(3, int(0.6 * len(ratios))):
            for product, r in ratios:
                if abs(r - modal) > tol:
                    out.append(finding("warning", "WEIGHT_RATIO",
                              f"Verhouding netto/bruto ({r:.4f}) wijkt af van de "
                              f"verhouding op {n_modal} van de {len(ratios)} regels ({modal:.4f}).",
                              product))
    return out


def check_codes(items):
    out, seen = [], {}
    for it in items:
        code = it.get("hs_code") or ""
        p = it["product"]
        if not code:
            out.append(finding("error", "CODE_MISSING", "Geen goederencode opgegeven.", p))
            continue
        if len(code) != 10:
            sev = "error" if len(code) < 8 else "warning"
            out.append(finding(sev, "CODE_LENGTH",
                      f"Code heeft {len(code)} cijfers; voor invoer in de EU zijn er 10 nodig.", p))
        seen.setdefault(code, []).append(p)
    for code, prods in seen.items():
        if len(prods) > 1:
            out.append(finding("info", "CODE_SHARED",
                      f"Code {code} wordt gebruikt voor {len(prods)} verschillende "
                      f"omschrijvingen: {', '.join(prods)}. Controleer of dat terecht is."))
    return out


def check_against_invoice(items, invoice_lines, tol=0.01):
    """Compare the prepared lines with the lines read from the commercial invoice."""
    out = []
    if not invoice_lines:
        return out

    def norm(s):
        return re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower()).split()

    used = set()
    for it in items:
        words = set(norm(it["product"]))
        best, best_score = None, 0.0
        for k, inv in enumerate(invoice_lines):
            if k in used:
                continue
            iw = set(norm(inv.get("description")))
            if not iw or not words:
                continue
            score = len(words & iw) / len(words | iw)
            if score > best_score:
                best, best_score = k, score
        if best is None or best_score < 0.34:
            out.append(finding("warning", "INV_NO_MATCH",
                      "Geen overeenkomstige factuurregel gevonden.", it["product"]))
            continue
        used.add(best)
        inv = invoice_lines[best]
        for field, label in (("packages", "aantal colli"), ("gross", "brutogewicht"),
                             ("net", "nettogewicht"), ("price", "eenheidsprijs"),
                             ("amount", "bedrag")):
            a, b = it.get(field), to_num(inv.get(field))
            if a is None or b is None:
                continue
            if abs(a - b) > max(tol, abs(b) * 1e-6):
                out.append(finding("error", "INV_MISMATCH",
                          f"{label.capitalize()} wijkt af van de factuur: "
                          f"voorbereiding {a:,.2f} · factuur {b:,.2f} (verschil {a - b:+,.2f}).",
                          it["product"]))
    for k, inv in enumerate(invoice_lines):
        if k not in used:
            out.append(finding("warning", "INV_EXTRA",
                      f"Factuurregel komt niet voor in de voorbereiding: "
                      f"{str(inv.get('description'))[:70]}."))
    return out


# ── code comparison ───────────────────────────────────────────────────────────

AGREEMENT_LABELS = {
    "identical":  ("Identiek", "De opgegeven code komt volledig overeen."),
    "taric":      ("Zelfde CN8", "Gelijk tot 8 cijfers; de TARIC-onderverdeling verschilt."),
    "subheading": ("Zelfde post 6", "Gelijk tot 6 cijfers; de CN-onderverdeling verschilt."),
    "heading":    ("Zelfde post 4", "Gelijke post, andere onderverdeling."),
    "chapter":    ("Zelfde hoofdstuk", "Zelfde hoofdstuk, andere post."),
    "different":  ("Ander hoofdstuk", "Fundamenteel andere indeling."),
    "unknown":    ("Niet vergelijkbaar", "Een van beide codes ontbreekt."),
}


def compare_codes(declared, own):
    a, b = normalize_code(declared), normalize_code(own)
    if not a or not b:
        return "unknown"
    if a == b:
        return "identical"
    if a[:8] == b[:8]:
        return "taric"
    if a[:6] == b[:6]:
        return "subheading"
    if a[:4] == b[:4]:
        return "heading"
    if a[:2] == b[:2]:
        return "chapter"
    return "different"


AGREEMENT_SEVERITY = {
    "identical": "ok", "taric": "warning", "subheading": "warning",
    "heading": "error", "chapter": "error", "different": "error", "unknown": "warning",
}


# ── customs value ─────────────────────────────────────────────────────────────

def customs_value(total_amount, currency, rate, incoterm,
                  freight_eur=0.0, insurance_eur=0.0, other_eur=0.0):
    """Compute the customs value in EUR, plus the trail of how it was built."""
    steps, warnings = [], []
    cur = (currency or "EUR").upper()

    if cur == "EUR":
        base = float(total_amount or 0)
        steps.append(("Factuurbedrag", f"EUR {base:,.2f}"))
    else:
        if not rate:
            rate = FIXED_EUR_RATES.get(cur)
            if rate:
                warnings.append(f"Vaste pariteit {cur}/EUR {rate} gebruikt. "
                                "Controleer of dit de juiste koers is voor deze aangifte.")
        if not rate:
            return None, steps, ["Geen wisselkoers opgegeven; douanewaarde niet berekend."]
        base = float(total_amount or 0) / float(rate)
        steps.append(("Factuurbedrag", f"{cur} {float(total_amount or 0):,.2f}"))
        steps.append(("Wisselkoers", f"1 EUR = {float(rate):,.4f} {cur}"))
        steps.append(("Omgerekend", f"EUR {base:,.2f}"))

    inc = (incoterm or "").upper().strip()
    total = base
    if inc in {"EXW", "FCA", "FAS", "FOB"}:
        total += float(freight_eur or 0) + float(insurance_eur or 0) + float(other_eur or 0)
        if freight_eur:
            steps.append(("+ Vracht tot EU-grens", f"EUR {float(freight_eur):,.2f}"))
        else:
            warnings.append(f"Incoterm {inc}: vracht tot de EU-grens moet bij de "
                            "douanewaarde worden geteld, maar er is geen bedrag opgegeven.")
        if insurance_eur:
            steps.append(("+ Verzekering", f"EUR {float(insurance_eur):,.2f}"))
        if other_eur:
            steps.append(("+ Overige bijtellingen", f"EUR {float(other_eur):,.2f}"))
    elif inc in {"CIF", "CIP", "DAP", "DPU", "DDP"}:
        steps.append(("Incoterm", f"{inc} — vracht en verzekering zitten in de prijs"))
        if freight_eur:
            warnings.append(f"Incoterm {inc}: kosten ná binnenkomst in de EU mogen juist "
                            "worden afgetrokken, niet bijgeteld. Controleer de opgegeven vracht.")
    elif inc:
        warnings.append(f"Incoterm {inc} niet herkend; bijtellingen niet automatisch toegepast.")
    else:
        warnings.append("Geen incoterm opgegeven; bijtellingen niet automatisch toegepast.")

    steps.append(("Douanewaarde", f"EUR {total:,.2f}"))
    return round(total, 2), steps, warnings


def summarize(findings):
    return {
        "errors":   len([f for f in findings if f["severity"] == "error"]),
        "warnings": len([f for f in findings if f["severity"] == "warning"]),
        "infos":    len([f for f in findings if f["severity"] == "info"]),
    }


def run_all_checks(items, totals, invoice_lines=None):
    basis, scores = detect_amount_basis(items)
    findings = []
    findings += check_line_math(items, basis)
    findings += check_totals(items, totals)
    findings += check_weights(items)
    findings += check_codes(items)
    findings += check_against_invoice(items, invoice_lines or [])
    return findings, basis, scores
