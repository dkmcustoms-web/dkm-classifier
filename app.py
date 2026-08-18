import streamlit as st
import json
import base64
import io
import uuid
from datetime import datetime
from PIL import Image
from anthropic import Anthropic, APIStatusError
from utils.sheets import (log_to_sheets, get_pending_reviews, get_all_history,
                           save_senior_review, lookup_verified)
from utils.prompts import (PROMPT1, PROMPT2, PROMPT3, PROMPT_FOLLOWUP,
                           PROMPT_SPLIT, PROMPT_DOC_LINES, PROMPT_CODE_COMPARE)
from utils import audit, pricing
from utils import db as neon

st.set_page_config(page_title="DKM Classifier", page_icon="🔍", layout="wide")

# ── Config ────────────────────────────────────────────────────────────────────
# Model-ID's: claude-opus-5 (sterkst in redeneren), claude-sonnet-5 (balans),
# claude-haiku-4-5-20251001 (snel/goedkoop). Sonnet 4 is uitgefaseerd -> 404.
MODEL      = "claude-sonnet-5"
MAX_TOKENS = 4000

st.markdown("""
<style>
    [data-testid="stAppViewContainer"] { background-color: #1a1a1a; }
    [data-testid="stSidebar"] { background-color: #111111; }
    .stButton > button {
        background-color: #D94F2B; color: white; border: none;
        border-radius: 6px; font-weight: 600; padding: 0.5rem 2rem;
    }
    .stButton > button:hover { background-color: #b83e21; }
    .verdict-validated { background:#1a3d1a; border:1px solid #4a9e4a;
        border-radius:8px; padding:1rem 1.25rem; margin-top:1rem; }
    .verdict-partial { background:#3d2e0a; border:1px solid #c8880a;
        border-radius:8px; padding:1rem 1.25rem; margin-top:1rem; }
    .verdict-invalid { background:#3d0f0f; border:1px solid #c84a4a;
        border-radius:8px; padding:1rem 1.25rem; margin-top:1rem; }
    .verdict-verified { background:#0a2a3d; border:1px solid #0a7abf;
        border-radius:8px; padding:1rem 1.25rem; margin-top:1rem; }
    .cn-code { font-size:2rem; font-weight:700; color:#D94F2B; font-family:monospace; }
    .tree-box { background:#1e1e1e; border:1px solid #333; border-radius:8px;
        padding:1.2rem 1.5rem; margin-top:1rem; font-family:monospace; font-size:0.82rem;
        line-height:1.8; color:#ccc; white-space:pre-wrap; }
    .review-card { background:#1e1e1e; border:1px solid #333; border-radius:8px;
        padding:1rem 1.25rem; margin-bottom:1rem; }
    .followup-box { background:#1a2a1a; border:1px solid #2a5a2a;
        border-radius:8px; padding:1.2rem 1.5rem; margin-top:1rem; }
    .badge-pending  { background:#3d2e0a; color:#f0a030; border-radius:4px;
        padding:2px 8px; font-size:0.75rem; font-weight:600; }
    .badge-verified { background:#0a2a3d; color:#4ab0f0; border-radius:4px;
        padding:2px 8px; font-size:0.75rem; font-weight:600; }
    .badge-rejected { background:#3d0f0f; color:#f04a4a; border-radius:4px;
        padding:2px 8px; font-size:0.75rem; font-weight:600; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
for key, default in [
    ("username",""), ("history",[]), ("page","classify"),
    ("followup_active", False),
    ("followup_questions", []),
    ("followup_context", {}),
    ("multi_stage", "input"),
    ("multi_items", []),
    ("multi_results", []),
    ("multi_shared", ""),
    ("multi_batch", ""),
    ("multi_doc_meta", {}),
    ("audit_stage", "input"),
    ("audit_items", []),
    ("audit_totals", {}),
    ("audit_meta", {}),
    ("audit_invoice", {}),
    ("audit_findings", []),
    ("audit_opinions", []),
    ("audit_batch", ""),
    ("audit_value", {}),
    ("usage_events", []),
    ("last_run_events", []),
    ("schema_ready", False),
    ("multi_cost", {}),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ── storage backends ──────────────────────────────────────────────────────────

def _secret(name, default=""):
    try:
        return st.secrets[name]
    except Exception:
        return default


def neon_dsn():
    return str(_secret("NEON_DATABASE_URL", "") or "").strip()


def sheets_configured():
    return bool(_secret("GOOGLE_SHEETS_ID", "")) and bool(_secret("GOOGLE_SERVICE_ACCOUNT", ""))


def ensure_schema():
    """Create the Neon tables once per session."""
    dsn = neon_dsn()
    if not dsn or st.session_state.schema_ready:
        return bool(dsn)
    try:
        neon.init_schema(dsn)
        st.session_state.schema_ready = True
        return True
    except Exception as e:
        st.warning(f"Neon niet bereikbaar: {type(e).__name__}: {e}")
        return False


def usd_per_eur():
    try:
        return float(_secret("USD_PER_EUR", 0) or 0)
    except Exception:
        return 0.0


def price_overrides():
    try:
        return dict(st.secrets["MODEL_PRICING"])
    except Exception:
        return {}


# ── usage / cost tracking ─────────────────────────────────────────────────────

def record_usage(model, usage, step="", row_id="", batch_id=""):
    tokens = pricing.usage_to_dict(usage)
    cost, known = pricing.cost_usd(model, tokens, price_overrides())
    event = {
        "user": st.session_state.username, "page": st.session_state.page,
        "step": step, "model": model, "row_id": row_id, "batch_id": batch_id,
        "cost_usd": cost, "rate_known": known, **tokens,
    }
    st.session_state.usage_events.append(event)
    st.session_state.last_run_events.append(event)
    return event


def take_run_events():
    """Return and clear the events collected since the last checkpoint."""
    events = list(st.session_state.last_run_events)
    st.session_state.last_run_events = []
    return events


def cost_caption(events, label="This run"):
    s = pricing.summarize_events(events)
    eur = pricing.fmt_eur(s["cost_usd"], usd_per_eur())
    parts = [f"{label}: <strong>{pricing.fmt_usd(s['cost_usd'])}</strong>"]
    if eur:
        parts.append(f"({eur})")
    parts.append(f"· {s['calls']} calls · {s['input_tokens']:,} in / {s['output_tokens']:,} out tokens")
    if any(not e.get("rate_known", True) for e in events or []):
        parts.append("· ⚠ unknown rate for at least one model, estimate only")
    return ("<span style='color:#888;font-size:0.8rem;'>" + " ".join(parts) + "</span>")


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    col_logo, col_ttl = st.columns([1,3])
    with col_logo:
        try:
            st.image("assets/dkm_logo.png", width=50)
        except Exception:
            st.markdown("**DKM**")
    with col_ttl:
        st.markdown("### DKM Classifier")
    st.divider()
    st.markdown("### Navigation")
    if st.button("🔍  Classify product",   use_container_width=True):
        st.session_state.page = "classify"
        st.session_state.followup_active = False
    if st.button("📦  Classify multi",      use_container_width=True):
        st.session_state.page = "multi"
        st.session_state.followup_active = False
    if st.button("🧾  Dossier audit",       use_container_width=True):
        st.session_state.page = "audit"
        st.session_state.followup_active = False
    if st.button("📋  Senior review",       use_container_width=True):
        st.session_state.page = "review"
    if st.button("📊  History & analytics", use_container_width=True):
        st.session_state.page = "history"
    st.divider()
    st.markdown("### User")
    username = st.text_input("Name / initials", value=st.session_state.username, placeholder="e.g. LVD")
    st.session_state.username = username
    st.divider()
    st.markdown("### AI cost")
    _sess = pricing.summarize_events(st.session_state.usage_events)
    _eur  = pricing.fmt_eur(_sess["cost_usd"], usd_per_eur())
    st.markdown(
        f"<div style='font-size:1.35rem;font-weight:700;color:#D94F2B;font-family:monospace;'>"
        f"{pricing.fmt_usd(_sess['cost_usd'])}</div>"
        + (f"<div style='color:#888;font-size:0.78rem;'>{_eur}</div>" if _eur else "")
        + f"<div style='color:#888;font-size:0.75rem;margin-top:2px;'>this session · "
          f"{_sess['calls']} calls · {_sess['input_tokens']+_sess['output_tokens']:,} tokens</div>",
        unsafe_allow_html=True)
    if _sess["by_step"]:
        rows = "".join(
            f"<div style='display:flex;justify-content:space-between;font-size:0.74rem;"
            f"color:#999;padding:1px 0;'><span>{k}</span>"
            f"<span style='font-family:monospace;'>{pricing.fmt_usd(v['cost_usd'])}</span></div>"
            for k, v in sorted(_sess["by_step"].items(), key=lambda kv: -kv[1]["cost_usd"]))
        with st.expander("Breakdown"):
            st.markdown(rows, unsafe_allow_html=True)
    st.markdown(f"<div style='color:#666;font-size:0.7rem;margin-top:4px;'>"
                f"{MODEL} · rates checked {pricing.PRICING_VERIFIED} · estimate</div>",
                unsafe_allow_html=True)

    st.divider()
    st.markdown("### Storage")
    _store = []
    if neon_dsn():
        _store.append("Neon")
    if sheets_configured():
        _store.append("Sheets")
    st.markdown(
        f"<span style='color:{'#4a9e4a' if _store else '#c84a4a'};font-size:0.78rem;'>"
        f"{' + '.join(_store) if _store else 'not configured'}</span>",
        unsafe_allow_html=True)

    st.divider()
    st.markdown("### Session history")
    if st.session_state.history:
        for entry in reversed(st.session_state.history[-10:]):
            ts      = entry.get("timestamp","")
            code    = entry.get("cn_code","—")
            outcome = entry.get("outcome","—")
            color   = "#4a9e4a" if "VALIDATED" in outcome and "NOT" not in outcome else (
                      "#c8880a" if "PARTIAL" in outcome else "#c84a4a")
            st.markdown(
                f"<div style='font-size:0.78rem;padding:4px 0;border-bottom:1px solid #333;'>"
                f"<span style='color:#888'>{ts}</span><br>"
                f"<span style='font-family:monospace;font-weight:600'>{code}</span> "
                f"<span style='color:{color};font-size:0.72rem'>{outcome}</span></div>",
                unsafe_allow_html=True)
    else:
        st.markdown("<span style='color:#555;font-size:0.8rem'>No searches yet</span>", unsafe_allow_html=True)

# ── Helpers ───────────────────────────────────────────────────────────────────
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def build_file_block(f):
    """Return a valid Anthropic content block for an uploaded file.

    - PDF  -> document block (NOT an image block: the API rejects
              application/pdf inside an image source).
    - jfif / bmp / heic / unknown mime -> converted to JPEG.
    - oversized images -> downscaled below the ~5 MB / 8000 px API limits.
    """
    f.seek(0)
    raw  = f.read()
    mime = (f.type or "").lower()
    name = (f.name or "").lower()

    # PDF -> document block
    if mime == "application/pdf" or name.endswith(".pdf"):
        return {"type": "document",
                "source": {"type": "base64",
                           "media_type": "application/pdf",
                           "data": base64.b64encode(raw).decode()}}

    # Unsupported / unknown image mime -> convert to JPEG
    if mime not in ALLOWED_IMAGE_MIMES:
        img = Image.open(io.BytesIO(raw))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        raw, mime = buf.getvalue(), "image/jpeg"

    # Too large -> downscale
    if len(raw) > 4_500_000:
        img = Image.open(io.BytesIO(raw))
        img.thumbnail((1568, 1568))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        raw, mime = buf.getvalue(), "image/jpeg"

    return {"type": "image",
            "source": {"type": "base64", "media_type": mime,
                       "data": base64.b64encode(raw).decode()}}


def extract_json(text: str):
    """Return the LAST complete top-level JSON object in `text`.

    Scans with brace matching (string-aware) instead of rfind('{'), because
    rfind lands on the innermost brace and therefore fails on any JSON that
    contains nested objects — e.g. the line_items list of PROMPT_SPLIT.
    """
    if not text:
        return None

    found = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, in_str, esc, end = 0, False, False, -1
        for j in range(i, n):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end == -1:
            break
        try:
            found.append(json.loads(text[i:end + 1]))
        except Exception:
            pass
        i = end + 1

    if found:
        return found[-1]

    # Fallback: original behaviour, for objects without nesting
    idx = text.rfind("{")
    if idx == -1:
        return None
    try:
        return json.loads(text[idx:])
    except Exception:
        return None

def call_claude(system: str, user_content, step: str = "", row_id: str = "",
                batch_id: str = "") -> str:
    client = Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    if isinstance(user_content, str):
        user_content = [{"type": "text", "text": user_content}]
    if not user_content:
        raise ValueError("Geen input voor de API (geen tekst en geen bestanden).")
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user_content}]
        )
    except APIStatusError as e:
        # Streamlit redacts the exception text, so surface the real cause here.
        st.error(f"Anthropic API {e.status_code}: {getattr(e, 'message', '')}")
        try:
            st.code(str(e.response.text)[:2000], language="json")
        except Exception:
            pass
        raise
    record_usage(MODEL, getattr(resp, "usage", None), step=step,
                 row_id=row_id, batch_id=batch_id)
    return "".join(b.text for b in resp.content if hasattr(b, "text"))

def needs_followup(json2: dict) -> bool:
    """Follow-up only when NO code could be determined at all."""
    if not json2:
        return True
    if not json2.get("cn_code","").strip():
        return True
    if json2.get("confidence","").upper() == "LOW" and not json2.get("cn_code","").strip():
        return True
    return False

def has_soft_warnings(json2: dict, json3: dict) -> list:
    """Return missing details when a code was found but info is incomplete."""
    if not json2 or not json2.get("cn_code","").strip():
        return []
    missing = []
    if json2.get("confidence","").upper() in ["LOW", "MEDIUM"]:
        missing += json2.get("warnings", [])
    missing += (json3 or {}).get("missing_data", [])
    return missing[:5]

def verdict_html(outcome, code, taric, manual, issues, verified_by=None, cn_desc="", taric_desc=""):
    if verified_by:
        css, icon, label = "verdict-verified", "✓✓", f"Verified by previous inquiry ({verified_by})"
    elif "NOT VALIDATED" in outcome:
        css, icon, label = "verdict-invalid",   "✗",  "Not validated"
    elif "PARTIAL" in outcome:
        css, icon, label = "verdict-partial",   "~",  "Partially validated"
    else:
        css, icon, label = "verdict-validated", "✓",  "Validated"
    code_str = code or "—"
    if taric and taric != code:
        code_str += f" / {taric}"
    desc_str = ""
    if cn_desc:
        sub = taric_desc if taric_desc and taric_desc != cn_desc else ""
        desc_str = f"<span style='font-size:0.95rem;color:#ccc;margin-left:16px;vertical-align:middle;'>{cn_desc}"
        if sub:
            desc_str += f" <span style='color:#888;font-size:0.82rem;'>· {sub}</span>"
        desc_str += "</span>"
    issues_str = ("<br><small style='color:#aaa'>Issues: " + "; ".join(issues) + "</small>") if issues else ""
    manual_str = "" if verified_by else ("<br><small style='color:#f0a030'>⚠ Manual review recommended</small>" if manual else "")
    return f"""<div class='{css}'>
        <div style='font-size:0.8rem;font-weight:600;letter-spacing:0.06em;
                    text-transform:uppercase;margin-bottom:0.5rem;'>{icon} {label}</div>
        <div style='display:flex;align-items:center;flex-wrap:wrap;gap:8px;'>
            <div class='cn-code'>{code_str}</div>{desc_str}
        </div>{manual_str}{issues_str}</div>"""

def build_decision_tree(description, specs, json1, json2, json3, raw2,
                        followup_qa=None) -> str:
    lines = ["CLASSIFICATION DECISION TREE", "=" * 60]
    lines.append("\n▸ INPUT")
    lines.append(f"  Description : {(description or '—')[:120]}")
    if specs:
        lines.append(f"  Specs       : {specs[:120]}")
    if followup_qa:
        lines.append("\n▸ FOLLOW-UP ANSWERS")
        for q, a in followup_qa.items():
            lines.append(f"  Q: {q[:80]}")
            lines.append(f"  A: {a[:120]}")
    lines.append("\n▸ STEP 1 — FEATURE EXTRACTION")
    if json1:
        lines += [
            f"  Product     : {json1.get('product_identification','—')}",
            f"  Materials   : {', '.join(json1.get('materials') or []) or '—'}",
            f"  Function    : {json1.get('function','—')}",
            f"  Form        : {json1.get('form','—')}",
            f"  Category    : {json1.get('category_hint','—')}",
            f"  Is part     : {json1.get('is_part',False)}",
            f"  Is set      : {json1.get('is_set',False)}",
            f"  Data quality: {json1.get('data_quality','—')}",
        ]
        if json1.get('missing_information'):
            lines.append(f"  ⚠ Missing   : {', '.join(json1['missing_information'])}")
        if json1.get('ambiguities'):
            lines.append(f"  ⚠ Ambiguous : {', '.join(json1['ambiguities'])}")
    else:
        lines.append("  ⚠ Could not parse structured extraction")
    lines.append("\n▸ STEP 2 — CLASSIFICATION REASONING")
    if json2:
        candidates = json2.get('candidate_headings') or []
        if candidates:
            lines.append(f"  Candidates  : {', '.join(str(c) for c in candidates)}")
        keywords = ["STEP 3","STEP 4","STEP 5","STEP 6","STEP 7","STEP 8",
                    "GIR","legal note","heading","subheading","chapter","section"]
        captured = []
        for rl in raw2.splitlines():
            rs = rl.strip()
            if rs and any(kw.lower() in rs.lower() for kw in keywords):
                captured.append("  │ " + rs[:120])
            if len(captured) >= 20:
                break
        if captured:
            lines.append("  Reasoning excerpts:")
            lines.extend(captured)
        lines += [
            f"  → CN code   : {json2.get('cn_code','—')}",
            f"  → TARIC code: {json2.get('taric_code','—')}",
            f"  Confidence  : {json2.get('confidence','—')}",
        ]
        for w in (json2.get('warnings') or []):
            lines.append(f"  ⚠ Warning   : {w}")
        lines.append(f"  Manual review: {'YES' if json2.get('manual_review_recommended') else 'no'}")
    else:
        lines.append("  ⚠ Could not parse classification JSON")
    lines.append("\n▸ STEP 3 — VALIDATION")
    if json3:
        outcome = json3.get('validation_outcome','—')
        symbol  = "✓" if "VALIDATED" in outcome and "NOT" not in outcome else ("~" if "PARTIAL" in outcome else "✗")
        lines += [
            f"  Outcome     : {symbol} {outcome}",
            f"  Final code  : {json3.get('validated_code','—')}",
        ]
        for iss in (json3.get('issues') or []):
            lines.append(f"  ✗ Issue     : {iss}")
        for m in (json3.get('missing_data') or []):
            lines.append(f"  ⚠ Missing   : {m}")
        lines.append(f"  Manual review: {'YES' if json3.get('manual_review_recommended') else 'no'}")
    else:
        lines.append("  ⚠ Could not parse validation JSON")
    lines += ["", "=" * 60, f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]
    return "\n".join(lines)

def get_secrets():
    return st.secrets["GOOGLE_SHEETS_ID"], st.secrets["GOOGLE_SERVICE_ACCOUNT"]

def run_pipeline(description, specs, img_file, inv_file, extra_context=""):
    """Run the full 3-step pipeline. Returns (raw1,json1,raw2,json2,raw3,json3)."""
    # STEP 1
    user_content = []
    for f in [img_file, inv_file]:
        if f:
            try:
                user_content.append(build_file_block(f))
            except Exception as e:
                st.warning(f"Bestand '{getattr(f,'name','?')}' kon niet worden verwerkt: "
                           f"{type(e).__name__}: {e}")
    txt = ""
    if description:
        txt += f"Product description / invoice text:\n{description}\n\n"
    if specs:
        txt += f"Technical specifications:\n{specs}\n\n"
    if extra_context:
        txt += f"Additional information provided by the user:\n{extra_context}"
    if txt:
        user_content.append({"type":"text","text":txt.strip()})

    if not user_content:
        st.error("Geen bruikbare input: geen tekst en geen leesbaar bestand.")
        st.stop()

    raw1  = call_claude(PROMPT1, user_content, step="step1_extraction")
    json1 = extract_json(raw1)

    # STEP 2
    step2_input = "Structured product data from feature extractor:\n\n" + (
        json.dumps(json1, indent=2) if json1 else raw1)
    raw2  = call_claude(PROMPT2, step2_input, step="step2_classification")
    json2 = extract_json(raw2)

    # STEP 3
    step3_input = (
        f"Product data:\n{json.dumps(json1, indent=2) if json1 else description}\n\n"
        f"Proposed classification:\n{json.dumps(json2, indent=2) if json2 else raw2}\n\n"
        f"Full reasoning:\n{raw2}"
    )
    raw3  = call_claude(PROMPT3, step3_input, step="step3_validation")
    json3 = extract_json(raw3)

    return raw1, json1, raw2, json2, raw3, json3

def save_result(description, specs, img_file, inv_file, json1, json2, json3,
                raw1, raw2, raw3, decision_tree, followup_qa=None,
                row_id_override=None, desc_prefix="", quiet=False,
                batch_id="", source="classify", declared_code="", agreement=""):
    """Log result to Google Sheets.

    row_id_override / desc_prefix are used by the multi-product page so that all
    items of one document share a traceable batch reference, without requiring a
    new column in the existing History sheet.
    """
    outcome = json3.get("validation_outcome","UNKNOWN") if json3 else "UNKNOWN"
    code    = (json3 or {}).get("validated_code","") or (json2 or {}).get("cn_code","")
    taric   = (json3 or {}).get("taric_code","")     or (json2 or {}).get("taric_code","")
    manual  = bool((json3 or {}).get("manual_review_recommended") or
                   (json2 or {}).get("manual_review_recommended"))
    issues  = (json3 or {}).get("issues",[])
    row_id  = row_id_override or str(uuid.uuid4())[:8]

    followup_str = ""
    if followup_qa:
        followup_str = " | ".join(f"Q: {q} → A: {a}" for q,a in followup_qa.items())

    row = {
        "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user":           st.session_state.username,
        "description":    (desc_prefix + (description or ""))[:200],
        "specs":          specs[:200] if specs else "",
        "has_image":      "yes" if img_file else "no",
        "has_invoice":    "yes" if inv_file else "no",
        "product_id":     (json1 or {}).get("product_identification",""),
        "category":       (json1 or {}).get("category_hint",""),
        "data_quality":   (json1 or {}).get("data_quality",""),
        "cn_code":        (json2 or {}).get("cn_code",""),
        "taric_code":     (json2 or {}).get("taric_code",""),
        "confidence":     (json2 or {}).get("confidence",""),
        "outcome":        outcome,
        "validated_code": code,
        "manual_review":  "yes" if manual else "no",
        "issues":         "; ".join(issues),
        "decision_tree":  decision_tree,
        "raw_step1":      json.dumps(json1) if json1 else raw1[:500],
        "raw_step2":      raw2[:500],
        "raw_step3":      raw3[:500],
        "senior_reviewed":"no",
        "row_id":         row_id,
        "followup_qa":    followup_str,
    }
    events = take_run_events()
    for e in events:
        e["row_id"] = e.get("row_id") or row_id
        e["batch_id"] = e.get("batch_id") or batch_id
    usage = pricing.summarize_events(events)
    row["cost_usd"]      = usage["cost_usd"]
    row["input_tokens"]  = usage["input_tokens"]
    row["output_tokens"] = usage["output_tokens"]
    row["batch_id"]      = batch_id
    row["source"]        = source
    row["declared_code"] = declared_code
    row["agreement"]     = agreement

    saved = []
    if neon_dsn() and ensure_schema():
        try:
            neon.log_classification(neon_dsn(), row)
            neon.log_usage_events(neon_dsn(), events)
            saved.append("Neon")
        except Exception as e:
            st.warning(f"Neon logging failed: {type(e).__name__}: {e}")
    if sheets_configured():
        try:
            sid, sac = get_secrets()
            log_to_sheets(row, sid, sac)
            saved.append("Sheets")
        except Exception as e:
            import traceback
            st.warning(f"Sheets logging failed: {type(e).__name__}: {e}")
            st.code(traceback.format_exc(), language="text")
    if not saved:
        st.warning("Niet opgeslagen: geen Neon-connectiestring en geen Sheets-configuratie.")
    elif not quiet:
        st.success("✓ Saved to " + " + ".join(saved))
        st.markdown(cost_caption(events), unsafe_allow_html=True)

    st.session_state.history.append({
        "timestamp": datetime.now().strftime("%H:%M"),
        "cn_code":   code,
        "outcome":   outcome,
    })
    return outcome, code, taric, manual, issues

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: CLASSIFY
# ═══════════════════════════════════════════════════════════════════════════════
if st.session_state.page == "classify":
    st.markdown("## CN/TARIC Classification Tool")
    st.markdown("<span style='color:#888;font-size:0.85rem;'>Powered by DKM Classification Engine · 3-step AI pipeline</span>",
                unsafe_allow_html=True)
    st.divider()

    # ── FOLLOW-UP FORM (shown instead of input when active) ───────────────────
    if st.session_state.followup_active:
        ctx = st.session_state.followup_context
        st.markdown("### ℹ️ Additional information needed")
        st.markdown(
            f"<div class='followup-box'>"
            f"<span style='color:#4a9e4a;font-weight:600;'>The AI identified candidate headings: "
            f"<code>{', '.join(ctx.get('candidates',[]))}</code></span><br>"
            f"<span style='color:#aaa;font-size:0.88rem;'>To determine the correct code, please answer the questions below.</span>"
            f"</div>",
            unsafe_allow_html=True
        )
        st.divider()

        questions = st.session_state.followup_questions
        answers = {}
        for i, q in enumerate(questions):
            answers[q] = st.text_input(f"**{q}**", key=f"fq_{i}", placeholder="Type your answer here...")

        col1, col2 = st.columns(2)
        with col1:
            retry_btn = st.button("🔄  Classify with this information", use_container_width=True)
        with col2:
            cancel_btn = st.button("✕  Cancel and start over", use_container_width=True)

        if cancel_btn:
            st.session_state.followup_active   = False
            st.session_state.followup_questions = []
            st.session_state.followup_context  = {}
            st.rerun()

        if retry_btn:
            unanswered = [q for q, a in answers.items() if not a.strip()]
            if unanswered:
                st.warning("Please answer all questions before continuing.")
            else:
                ctx = st.session_state.followup_context
                extra = "\n".join(f"- {q}: {a}" for q, a in answers.items())

                st.divider()
                st.markdown("### Pipeline results — retry with additional information")

                with st.status("**Step 1** — Feature extraction (retry)…", expanded=True) as s1:
                    raw1, json1, raw2, json2, raw3, json3 = run_pipeline(
                        ctx["description"], ctx["specs"],
                        ctx.get("img_file"), ctx.get("inv_file"),
                        extra_context=extra
                    )
                    if json1:
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Product",      json1.get("product_identification","—")[:40])
                        c2.metric("Category",     json1.get("category_hint","—"))
                        c3.metric("Data quality", json1.get("data_quality","—"))
                        with st.expander("Full extraction JSON"):
                            st.json(json1)
                    s1.update(label="**Step 1** — Feature extraction ✓", state="complete")

                with st.status("**Step 2** — CN/TARIC classification (retry)…", expanded=True) as s2:
                    if json2:
                        c1, c2, c3 = st.columns(3)
                        c1.metric("CN code",    json2.get("cn_code","—"))
                        c2.metric("TARIC code", json2.get("taric_code","—"))
                        c3.metric("Confidence", json2.get("confidence","—"))
                        if json2.get("warnings"):
                            st.warning("Warnings: " + "; ".join(json2["warnings"]))
                        with st.expander("Full classification reasoning"):
                            st.markdown(raw2)
                    s2.update(label="**Step 2** — CN/TARIC classification ✓", state="complete")

                with st.status("**Step 3** — Validation (retry)…", expanded=True) as s3:
                    with st.expander("Full validation reasoning"):
                        st.markdown(raw3)
                    s3.update(label="**Step 3** — Validation ✓", state="complete")

                # Verdict
                outcome = json3.get("validation_outcome","UNKNOWN") if json3 else "UNKNOWN"
                code    = (json3 or {}).get("validated_code","") or (json2 or {}).get("cn_code","")
                taric   = (json3 or {}).get("taric_code","")     or (json2 or {}).get("taric_code","")
                manual  = bool((json3 or {}).get("manual_review_recommended") or
                               (json2 or {}).get("manual_review_recommended"))
                issues  = (json3 or {}).get("issues",[])
                cn_desc    = (json2 or {}).get("cn_description","")
                taric_desc = (json2 or {}).get("taric_description","")

                st.markdown(verdict_html(outcome, code, taric, manual, issues,
                                         cn_desc=cn_desc, taric_desc=taric_desc),
                            unsafe_allow_html=True)

                # MEDIUM confidence after retry: same improve option as normal flow
                if (json2 or {}).get("confidence","").upper() == "MEDIUM" and not needs_followup(json2):
                    soft_warnings = has_soft_warnings(json2, json3)
                    if soft_warnings:
                        missing_html = "".join(f"<li>{w}</li>" for w in soft_warnings)
                        st.markdown(
                            f"<div style='background:#1a2a3a;border:1px solid #2a6a8a;border-radius:8px;"
                            f"padding:1rem 1.25rem;margin-top:0.5rem;'>"
                            f"<span style='color:#4ab0f0;font-weight:600;font-size:0.85rem;'>"
                            f"ℹ️ Code found — for a more precise result, consider adding:</span>"
                            f"<ul style='color:#aaa;font-size:0.83rem;margin:0.5rem 0 0 1.2rem;'>"
                            f"{missing_html}</ul></div>",
                            unsafe_allow_html=True
                        )
                    col_imp, col_skip = st.columns([1, 3])
                    with col_imp:
                        improve_retry_btn = st.button(
                            "🔧  Improve result with more info",
                            use_container_width=True, key="improve_retry_btn"
                        )
                    with col_skip:
                        st.markdown(
                            "<span style='color:#888;font-size:0.83rem;'>"
                            "Result is usable as-is — accept or send to senior review.</span>",
                            unsafe_allow_html=True
                        )
                    if improve_retry_btn:
                        parts = [
                            "Product description: " + str(ctx["description"]),
                            "Step 1 extraction:\n" + (json.dumps(json1, indent=2) if json1 else str(raw1)),
                            "Step 2 warnings:\n" + json.dumps((json2 or {}).get("warnings", []), indent=2),
                            "Missing information:\n" + json.dumps((json1 or {}).get("missing_information", []), indent=2),
                            "Current code: " + str((json2 or {}).get("cn_code","")) + " (MEDIUM confidence)",
                            "Candidate headings: " + json.dumps((json2 or {}).get("candidate_headings", [])),
                        ]
                        fq_input = "\n\n".join(parts)
                        with st.spinner("Generating targeted questions..."):
                            raw_fq = call_claude(PROMPT_FOLLOWUP, fq_input, step="followup")
                        fq_json = extract_json(raw_fq)
                        questions = fq_json.get("questions", []) if fq_json else []
                        if not questions:
                            questions = [
                                line.lstrip("0123456789.-) ").strip()
                                for line in raw_fq.splitlines()
                                if line.strip() and line.strip()[0].isdigit()
                            ][:6]
                        if questions:
                            st.session_state.followup_active    = True
                            st.session_state.followup_questions = questions
                            st.session_state.followup_context   = {
                                "description": ctx["description"],
                                "specs":       ctx["specs"],
                                "img_file":    ctx.get("img_file"),
                                "inv_file":    ctx.get("inv_file"),
                                "candidates":  [str(c) for c in (json2 or {}).get("candidate_headings",[])],
                            }
                            st.rerun()

                decision_tree = build_decision_tree(
                    ctx["description"], ctx["specs"], json1, json2, json3, raw2,
                    followup_qa=answers
                )
                with st.expander("📋  Decision tree / audit trail", expanded=False):
                    st.markdown(f"<div class='tree-box'>{decision_tree}</div>",
                                unsafe_allow_html=True)

                save_result(ctx["description"], ctx["specs"],
                            ctx.get("img_file"), ctx.get("inv_file"),
                            json1, json2, json3, raw1, raw2, raw3,
                            decision_tree, followup_qa=answers)

                # ── Still insufficient after retry? ──────────────────────────
                if needs_followup(json2):
                    missing_items = (json3 or {}).get("missing_data", []) or (json1 or {}).get("missing_information", [])
                    known_info = [f"{q.split(chr(8212))[0].split(chr(45))[0].strip()}: {a}"
                                  for q, a in answers.items() if a.strip().lower() not in ["no clue","unknown","?","","n/a"]]
                    candidates = ctx.get("candidates", [])

                    # Build pre-filled description suggestion
                    orig_desc = ctx["description"].strip().rstrip(chr(10))
                    known_str = ", ".join(known_info) if known_info else ""
                    suggested = orig_desc
                    if known_str:
                        suggested += f" ({known_str})"

                    st.markdown("---")
                    st.markdown(
                        f"""<div style='background:#2a1a0a;border:1px solid #c8880a;border-radius:8px;padding:1.2rem 1.5rem;margin-top:1rem;'>
                        <div style='color:#f0a030;font-weight:600;font-size:0.9rem;letter-spacing:0.05em;text-transform:uppercase;margin-bottom:0.75rem;'>
                        ⚠ Still insufficient information to determine the commodity code</div>
                        <div style='color:#ccc;font-size:0.88rem;margin-bottom:0.75rem;'>
                        The candidate headings identified are: <code style='color:#D94F2B;'>{', '.join(candidates)}</code><br>
                        To classify correctly, the following information is still needed:</div>
                        <ul style='color:#aaa;font-size:0.85rem;margin:0 0 1rem 1.2rem;'>""" +
                        "".join(f"<li>{m}</li>" for m in missing_items[:6]) +
                        f"""</ul>
                        <div style='color:#ccc;font-size:0.88rem;margin-bottom:0.5rem;'>
                        ✏️ <strong>Please start a new search with this pre-filled description — add the missing details:</strong></div>
                        </div>""",
                        unsafe_allow_html=True
                    )
                    st.code(suggested, language="")
                    st.info("Copy the description above, add the missing details, and paste it into a new search.")

                st.session_state.followup_active   = False
                st.session_state.followup_questions = []
                st.session_state.followup_context  = {}

    else:
        # ── NORMAL INPUT FORM ─────────────────────────────────────────────────
        st.markdown("### Product information")
        col1, col2 = st.columns(2)
        with col1:
            description = st.text_area("Product description / invoice description", height=120,
                placeholder="e.g. Hydraulic pump for tractors, cast iron housing, max 250 bar, 45 l/min flow rate...")
            specs = st.text_area("Technical specifications (optional)", height=80,
                placeholder="Material composition, power, dimensions, standards...")
        with col2:
            img_file = st.file_uploader("Product image (optional)", type=["jpg","jpeg","jfif","png","webp"])
            inv_file = st.file_uploader("Invoice document / image (optional)", type=["jpg","jpeg","jfif","png","webp","pdf"])
            if img_file:
                st.image(img_file, caption="Product image", width=300)

        run_btn = st.button("🔍  Classify product", use_container_width=True)

        if run_btn:
            if not description and not img_file and not inv_file:
                st.warning("Please provide at least a product description or upload an image.")
                st.stop()
            if not st.session_state.username.strip():
                st.warning("Please enter your name or initials in the sidebar first.")
                st.stop()

            # Check verified lookup
            verified_match = None
            if description:
                try:
                    if neon_dsn() and ensure_schema():
                        from utils.sheets import _make_fingerprint
                        verified_match = neon.lookup_verified(
                            neon_dsn(), _make_fingerprint(description))
                    else:
                        sid, sac = get_secrets()
                        verified_match = lookup_verified(description, sid, sac)
                except Exception:
                    pass

            st.divider()
            st.markdown("### Pipeline results")

            with st.status("**Step 1** — Feature extraction…", expanded=True) as s1:
                raw1, json1, raw2, json2, raw3, json3 = run_pipeline(
                    description, specs, img_file, inv_file)
                if json1:
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Product",      json1.get("product_identification","—")[:40])
                    c2.metric("Category",     json1.get("category_hint","—"))
                    c3.metric("Data quality", json1.get("data_quality","—"))
                    with st.expander("Full extraction JSON"):
                        st.json(json1)
                else:
                    st.text(raw1)
                s1.update(label="**Step 1** — Feature extraction ✓", state="complete")

            with st.status("**Step 2** — CN/TARIC classification…", expanded=True) as s2:
                if json2:
                    c1, c2, c3 = st.columns(3)
                    c1.metric("CN code",    json2.get("cn_code","—"))
                    c2.metric("TARIC code", json2.get("taric_code","—"))
                    c3.metric("Confidence", json2.get("confidence","—"))
                    if json2.get("warnings"):
                        st.warning("Warnings: " + "; ".join(json2["warnings"]))
                    with st.expander("Full classification reasoning"):
                        st.markdown(raw2)
                else:
                    st.text(raw2)
                s2.update(label="**Step 2** — CN/TARIC classification ✓", state="complete")

            with st.status("**Step 3** — Validation…", expanded=True) as s3:
                with st.expander("Full validation reasoning"):
                    st.markdown(raw3)
                s3.update(label="**Step 3** — Validation ✓", state="complete")

            # ── Check if follow-up needed ──────────────────────────────────────
            if needs_followup(json2):
                st.info("ℹ️  Insufficient information to determine a reliable code. Generating targeted questions...")

                followup_input = (
                    f"Product description: {description}\n\n"
                    f"Step 1 extraction:\n{json.dumps(json1, indent=2) if json1 else raw1}\n\n"
                    f"Step 2 warnings:\n{json.dumps(json2.get('warnings',[]) if json2 else [], indent=2)}\n\n"
                    f"Missing information:\n{json.dumps((json1 or {}).get('missing_information',[]), indent=2)}\n\n"
                    f"Candidate headings found: {json.dumps((json2 or {}).get('candidate_headings',[]))}"
                )
                raw_questions = call_claude(PROMPT_FOLLOWUP, followup_input, step="followup")
                fq_json = extract_json(raw_questions)
                questions = fq_json.get("questions", []) if fq_json else []

                if not questions:
                    # Fallback: parse numbered list
                    questions = [
                        line.lstrip("0123456789.-) ").strip()
                        for line in raw_questions.splitlines()
                        if line.strip() and line.strip()[0].isdigit()
                    ][:6]

                if questions:
                    candidates = (json2 or {}).get("candidate_headings", [])
                    st.session_state.followup_active    = True
                    st.session_state.followup_questions = questions
                    st.session_state.followup_context   = {
                        "description": description,
                        "specs":       specs,
                        "img_file":    img_file,
                        "inv_file":    inv_file,
                        "candidates":  [str(c) for c in candidates],
                    }
                    st.rerun()
                else:
                    st.warning("Could not generate follow-up questions. Showing result as-is.")

            # ── Normal verdict (no follow-up needed) ──────────────────────────
            else:
                outcome = json3.get("validation_outcome","UNKNOWN") if json3 else "UNKNOWN"
                code    = (json3 or {}).get("validated_code","") or (json2 or {}).get("cn_code","")
                taric   = (json3 or {}).get("taric_code","")     or (json2 or {}).get("taric_code","")
                manual  = bool((json3 or {}).get("manual_review_recommended") or
                               (json2 or {}).get("manual_review_recommended"))
                issues  = (json3 or {}).get("issues",[])
                cn_desc    = (json2 or {}).get("cn_description","")
                taric_desc = (json2 or {}).get("taric_description","")

                verified_by = None
                if verified_match and verified_match.get("cn_code") == code:
                    verified_by = f"{verified_match.get('senior_user','senior')} on {verified_match.get('senior_timestamp','')[:10]}"

                st.markdown(verdict_html(outcome, code, taric, manual, issues,
                                         verified_by, cn_desc, taric_desc),
                            unsafe_allow_html=True)

                # ── Soft info box: code found but MEDIUM confidence ────────
                soft_warnings = has_soft_warnings(json2, json3)
                if soft_warnings:
                    missing_html = "".join(f"<li>{w}</li>" for w in soft_warnings)
                    st.markdown(
                        f"<div style='background:#1a2a3a;border:1px solid #2a6a8a;border-radius:8px;"
                        f"padding:1rem 1.25rem;margin-top:0.5rem;'>"
                        f"<span style='color:#4ab0f0;font-weight:600;font-size:0.85rem;'>"
                        f"ℹ️ Code found — for a more precise classification, consider adding:</span>"
                        f"<ul style='color:#aaa;font-size:0.83rem;margin:0.5rem 0 0 1.2rem;'>"
                        f"{missing_html}</ul></div>",
                        unsafe_allow_html=True
                    )

                # ── Optional improve button for MEDIUM confidence ──────────────
                if (json2 or {}).get("confidence","").upper() == "MEDIUM" and manual:
                    col_a, col_b = st.columns([2, 3])
                    with col_a:
                        improve_btn = st.button(
                            "💡  Improve this result with more details",
                            use_container_width=True
                        )
                    with col_b:
                        st.markdown(
                            "<span style='color:#888;font-size:0.82rem;line-height:2.4;'>"
                            "Confidence is MEDIUM. Adding more product details may yield a more precise code.</span>",
                            unsafe_allow_html=True
                        )
                    if improve_btn:
                        parts = [
                            "Product description: " + str(description),
                            "Step 1 extraction:\n" + (json.dumps(json1, indent=2) if json1 else str(raw1)),
                            "Step 2 warnings:\n" + json.dumps((json2 or {}).get("warnings", []), indent=2),
                            "Missing information:\n" + json.dumps((json1 or {}).get("missing_information", []), indent=2),
                            "Current code: " + str((json2 or {}).get("cn_code","")) + " (MEDIUM confidence)",
                            "Candidate headings: " + json.dumps((json2 or {}).get("candidate_headings", [])),
                        ]
                        fq_input = "\n\n".join(parts)
                        raw_fq = call_claude(PROMPT_FOLLOWUP, fq_input, step="followup")
                        fq_json = extract_json(raw_fq)
                        questions = fq_json.get("questions", []) if fq_json else []
                        if not questions:
                            questions = [
                                line.lstrip("0123456789.-) ").strip()
                                for line in raw_fq.splitlines()
                                if line.strip() and line.strip()[0].isdigit()
                            ][:6]
                        if questions:
                            st.session_state.followup_active    = True
                            st.session_state.followup_questions = questions
                            st.session_state.followup_context   = {
                                "description": description,
                                "specs":       specs,
                                "img_file":    img_file,
                                "inv_file":    inv_file,
                                "candidates":  [str(c) for c in (json2 or {}).get("candidate_headings",[])],
                            }
                            st.rerun()
                        else:
                            st.info("Could not generate improvement questions. Please add more details manually.")

                decision_tree = build_decision_tree(
                    description, specs, json1, json2, json3, raw2)
                with st.expander("📋  Decision tree / audit trail", expanded=False):
                    st.markdown(f"<div class='tree-box'>{decision_tree}</div>",
                                unsafe_allow_html=True)

                save_result(description, specs, img_file, inv_file,
                            json1, json2, json3, raw1, raw2, raw3, decision_tree)

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: CLASSIFY MULTI  (documents with several goods)
# ═══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "multi":
    st.markdown("## Multi-product classification")
    st.markdown("<span style='color:#888;font-size:0.85rem;'>Split an invoice or packing list into its line items and classify each one separately</span>",
                unsafe_allow_html=True)
    st.divider()

    if not st.session_state.username.strip():
        st.warning("Please enter your name or initials in the sidebar first.")
        st.stop()

    def _reset_multi():
        st.session_state.multi_stage    = "input"
        st.session_state.multi_items    = []
        st.session_state.multi_results  = []
        st.session_state.multi_shared   = ""
        st.session_state.multi_batch    = ""
        st.session_state.multi_doc_meta = {}

    # ── STAGE 1: upload and split ─────────────────────────────────────────────
    if st.session_state.multi_stage == "input":
        col1, col2 = st.columns(2)
        with col1:
            doc_file = st.file_uploader(
                "Invoice / packing list (PDF or image)",
                type=["pdf","jpg","jpeg","jfif","png","webp"], key="multi_doc")
            shared_extra = st.text_input(
                "Shared context (optional)",
                placeholder="e.g. all items are spare parts for ventilation systems")
        with col2:
            pasted = st.text_area(
                "Or paste the invoice lines here", height=180,
                placeholder="1  Klembeugel RVS 100mm      50 pcs\n2  Flexibele slang PVC 3m     20 pcs\n3  Ventilatorblad aluminium    5 pcs")

        if st.button("📄  Analyse document", use_container_width=True):
            content = []
            if doc_file:
                try:
                    content.append(build_file_block(doc_file))
                except Exception as e:
                    st.error(f"Bestand kon niet worden verwerkt: {type(e).__name__}: {e}")
                    st.stop()
            if pasted and pasted.strip():
                content.append({"type":"text","text":"Document text:\n" + pasted.strip()})
            if not content:
                st.warning("Upload a document or paste the invoice lines first.")
                st.stop()

            with st.spinner("Reading the document and splitting it into line items..."):
                raw_split = call_claude(PROMPT_SPLIT, content, step="split")
            split = extract_json(raw_split)

            if not split:
                st.error("Could not read the document structure.")
                with st.expander("Raw output"):
                    st.text(raw_split[:3000])
                st.stop()

            items = split.get("line_items") or []
            if not items:
                st.warning("No goods found on this document.")
                for w in split.get("warnings", []):
                    st.markdown(f"- {w}")
                st.stop()

            st.session_state.multi_items = [{
                "classify":    True,
                "line_ref":    str(it.get("line_ref","") or ""),
                "description": str(it.get("description","") or ""),
                "article_number": str(it.get("article_number","") or ""),
                "quantity":    str(it.get("quantity","") or ""),
                "specs":       str(it.get("specs","") or ""),
                "notes":       str(it.get("notes","") or ""),
            } for it in items]
            shared = split.get("shared_context","") or ""
            if shared_extra.strip():
                shared = (shared + " " + shared_extra.strip()).strip()
            st.session_state.multi_shared   = shared
            st.session_state.multi_batch    = str(uuid.uuid4())[:8]
            st.session_state.multi_doc_meta = {
                "document_type":  split.get("document_type",""),
                "excluded_lines": split.get("excluded_lines",[]),
                "warnings":       split.get("warnings",[]),
                "has_file":       bool(doc_file),
            }
            st.session_state.multi_stage = "confirm"
            st.rerun()

    # ── STAGE 2: confirm the split ────────────────────────────────────────────
    elif st.session_state.multi_stage == "confirm":
        meta  = st.session_state.multi_doc_meta
        items = st.session_state.multi_items

        st.markdown(f"### {len(items)} line item(s) found "
                    f"<span style='color:#888;font-size:0.8rem;'>· {meta.get('document_type','document')} "
                    f"· batch {st.session_state.multi_batch}</span>", unsafe_allow_html=True)

        if meta.get("warnings"):
            st.warning("Split warnings: " + "; ".join(meta["warnings"]))
        if meta.get("excluded_lines"):
            st.markdown(
                "<span style='color:#888;font-size:0.8rem;'>Excluded as non-goods: "
                + ", ".join(str(x) for x in meta["excluded_lines"]) + "</span>",
                unsafe_allow_html=True)
        if st.session_state.multi_shared:
            st.markdown(f"<div class='followup-box'><span style='color:#4a9e4a;font-weight:600;'>"
                        f"Shared context</span><br><span style='color:#ccc;font-size:0.88rem;'>"
                        f"{st.session_state.multi_shared}</span></div>", unsafe_allow_html=True)

        st.markdown("<span style='color:#888;font-size:0.83rem;'>"
                    "Check the descriptions before classifying — you can edit them, add rows or "
                    "untick items you want to skip.</span>", unsafe_allow_html=True)

        edited = st.data_editor(
            items, num_rows="dynamic", use_container_width=True, key="multi_editor",
            column_config={
                "classify":       st.column_config.CheckboxColumn("✓", width="small"),
                "line_ref":       st.column_config.TextColumn("Line", width="small"),
                "description":    st.column_config.TextColumn("Description", width="large"),
                "article_number": st.column_config.TextColumn("Art. no.", width="small"),
                "quantity":       st.column_config.TextColumn("Qty", width="small"),
                "specs":          st.column_config.TextColumn("Specs", width="medium"),
                "notes":          st.column_config.TextColumn("Note", width="medium"),
            })

        selected = [r for r in edited
                    if r.get("classify") and str(r.get("description","")).strip()]

        est = len(selected) * 3
        col_go, col_back = st.columns([2,1])
        with col_go:
            go = st.button(f"🔍  Classify {len(selected)} item(s)", use_container_width=True)
        with col_back:
            if st.button("✕  Start over", use_container_width=True):
                _reset_multi()
                st.rerun()
        st.markdown(f"<span style='color:#888;font-size:0.8rem;'>"
                    f"≈ {est} API calls — allow roughly {max(1, est//4)}–{max(2, est//2)} "
                    f"minutes for this batch.</span>", unsafe_allow_html=True)

        if go:
            if not selected:
                st.warning("Select at least one item.")
                st.stop()
            st.session_state.multi_items   = edited
            st.session_state.multi_results = []
            batch = st.session_state.multi_batch
            total = len(selected)
            bar   = st.progress(0.0, text="Starting...")

            for n, item in enumerate(selected, start=1):
                desc = str(item.get("description","")).strip()
                bar.progress((n-1)/total, text=f"Item {n} of {total} — {desc[:60]}")

                specs_parts = [str(item.get("specs","") or "").strip(),
                               (f"Quantity: {item['quantity']}" if item.get("quantity") else ""),
                               (f"Article number: {item['article_number']}" if item.get("article_number") else "")]
                specs = "\n".join(p for p in specs_parts if p)

                extra = st.session_state.multi_shared
                if item.get("notes"):
                    extra = (extra + "\nObservation on this line: " + str(item["notes"])).strip()

                try:
                    raw1, json1, raw2, json2, raw3, json3 = run_pipeline(
                        desc, specs, None, None, extra_context=extra)
                except Exception as e:
                    st.session_state.multi_results.append({
                        "line_ref": item.get("line_ref",""), "description": desc,
                        "error": f"{type(e).__name__}: {e}",
                    })
                    continue

                tree = build_decision_tree(desc, specs, json1, json2, json3, raw2)
                row_id = f"{batch}-{n:02d}"
                save_result(desc, specs, None, None, json1, json2, json3,
                            raw1, raw2, raw3, tree,
                            row_id_override=row_id,
                            desc_prefix=f"[{batch} {n}/{total}] ",
                            quiet=True, batch_id=batch, source="multi")

                st.session_state.multi_results.append({
                    "row_id":     row_id,
                    "line_ref":   item.get("line_ref",""),
                    "description": desc,
                    "quantity":   item.get("quantity",""),
                    "cn_code":    (json2 or {}).get("cn_code",""),
                    "taric_code": (json3 or {}).get("taric_code","") or (json2 or {}).get("taric_code",""),
                    "cn_description": (json2 or {}).get("cn_description",""),
                    "confidence": (json2 or {}).get("confidence",""),
                    "outcome":    (json3 or {}).get("validation_outcome","UNKNOWN"),
                    "manual":     bool((json3 or {}).get("manual_review_recommended") or
                                       (json2 or {}).get("manual_review_recommended")),
                    "issues":     (json3 or {}).get("issues",[]),
                    "warnings":   (json2 or {}).get("warnings",[]),
                    "missing":    (json1 or {}).get("missing_information",[]),
                    "tree":       tree,
                    "raw2":       raw2,
                    "raw3":       raw3,
                })
                bar.progress(n/total, text=f"Item {n} of {total} done")

            bar.empty()
            st.session_state.multi_cost = pricing.summarize_events(
                [e for e in st.session_state.usage_events if e.get("batch_id") == batch])
            st.session_state.multi_stage = "results"
            st.rerun()

    # ── STAGE 3: results ──────────────────────────────────────────────────────
    elif st.session_state.multi_stage == "results":
        results = st.session_state.multi_results
        batch   = st.session_state.multi_batch
        ok      = [r for r in results if not r.get("error")]

        st.markdown(f"### Batch {batch} — {len(results)} item(s)")
        _bc = st.session_state.get("multi_cost") or {}
        if _bc:
            _e = pricing.fmt_eur(_bc.get("cost_usd"), usd_per_eur())
            st.markdown(f"<span style='color:#888;font-size:0.82rem;'>AI cost for this batch: "
                        f"<strong>{pricing.fmt_usd(_bc.get('cost_usd'))}</strong>"
                        + (f" ({_e})" if _e else "")
                        + f" · {_bc.get('calls',0)} calls · "
                          f"{pricing.fmt_usd((_bc.get('cost_usd') or 0)/max(1,len(results)))} per line"
                          f"</span>", unsafe_allow_html=True)

        n_val  = len([r for r in ok if "VALIDATED" in r["outcome"] and "NOT" not in r["outcome"]])
        n_part = len([r for r in ok if "PARTIAL" in r["outcome"]])
        n_bad  = len([r for r in ok if "NOT VALIDATED" in r["outcome"]])
        n_att  = len([r for r in ok if r["manual"] or not r["cn_code"]]) + \
                 len([r for r in results if r.get("error")])

        c1,c2,c3,c4 = st.columns(4)
        c1.metric("Validated", n_val)
        c2.metric("Partial",   n_part)
        c3.metric("Not validated", n_bad)
        c4.metric("Needs attention", n_att)
        st.divider()

        table = [{
            "Line":  r.get("line_ref","") or "—",
            "Description": r["description"][:60],
            "Qty":   r.get("quantity","") or "—",
            "CN":    r.get("cn_code","") or "—",
            "TARIC": r.get("taric_code","") or "—",
            "Conf.": r.get("confidence","") or "—",
            "Outcome": r.get("error") and "ERROR" or r.get("outcome","—"),
            "Review": "yes" if r.get("manual") else "no",
        } for r in results]
        st.dataframe(table, use_container_width=True)

        csv_lines = ["line_ref;description;quantity;cn_code;taric_code;confidence;outcome;manual_review;row_id"]
        for r in results:
            csv_lines.append(";".join([
                str(r.get("line_ref","")), '"' + r["description"].replace('"',"'") + '"',
                str(r.get("quantity","")), str(r.get("cn_code","")), str(r.get("taric_code","")),
                str(r.get("confidence","")), str(r.get("error") and "ERROR" or r.get("outcome","")),
                "yes" if r.get("manual") else "no", str(r.get("row_id","")),
            ]))
        st.download_button("⬇  Download batch as CSV", "\n".join(csv_lines),
                           file_name=f"dkm_batch_{batch}.csv", mime="text/csv")

        st.divider()
        st.markdown("### Per item")
        for r in results:
            if r.get("error"):
                st.error(f"Line {r.get('line_ref','?')} — {r['description'][:60]}: {r['error']}")
                continue
            st.markdown(verdict_html(r["outcome"], r["cn_code"], r["taric_code"],
                                     r["manual"], r["issues"],
                                     cn_desc=r.get("cn_description","")),
                        unsafe_allow_html=True)
            st.markdown(f"<span style='color:#888;font-size:0.82rem;'>"
                        f"Line {r.get('line_ref','—')} · {r['description'][:90]} · "
                        f"confidence {r.get('confidence','—')} · id {r['row_id']}</span>",
                        unsafe_allow_html=True)
            if not r["cn_code"]:
                miss = (r.get("missing") or [])[:4]
                st.warning("No code could be determined. Missing: "
                           + ("; ".join(miss) if miss else "insufficient description")
                           + " — classify this item on the single-product page to get targeted questions.")
            elif r.get("warnings"):
                st.markdown("<span style='color:#4ab0f0;font-size:0.82rem;'>ℹ️ "
                            + "; ".join(r["warnings"][:3]) + "</span>", unsafe_allow_html=True)
            with st.expander(f"📋  Decision tree — {r['row_id']}"):
                st.markdown(f"<div class='tree-box'>{r['tree']}</div>", unsafe_allow_html=True)
            with st.expander(f"Full reasoning — {r['row_id']}"):
                st.markdown(r["raw2"])
                st.divider()
                st.markdown(r["raw3"])
            st.divider()

        if st.button("📄  New batch", use_container_width=True):
            _reset_multi()
            st.rerun()

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: DOSSIER AUDIT  (verify a customer's preparation file)
# ═══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "audit":
    st.markdown("## Dossier audit")
    st.markdown("<span style='color:#888;font-size:0.85rem;'>Check a preparation file against the commercial invoice: "
                "arithmetic, declared goods codes, and DKM's own classification opinion</span>",
                unsafe_allow_html=True)
    st.divider()

    if not st.session_state.username.strip():
        st.warning("Please enter your name or initials in the sidebar first.")
        st.stop()

    def _reset_audit():
        for k, v in [("audit_stage","input"), ("audit_items",[]), ("audit_totals",{}),
                     ("audit_meta",{}), ("audit_invoice",{}), ("audit_findings",[]),
                     ("audit_opinions",[]), ("audit_batch",""), ("audit_value",{})]:
            st.session_state[k] = v

    # ── STAGE 1: upload ───────────────────────────────────────────────────────
    if st.session_state.audit_stage == "input":
        st.markdown("<span style='color:#888;font-size:0.83rem;'>Provide at least one source. "
                    "Give two and they are checked against each other; give one and only that "
                    "document is verified. Spreadsheets are read directly; PDFs, images and "
                    "pasted text are read by the document reader.</span>",
                    unsafe_allow_html=True)
        col1, col2 = st.columns(2)
        with col1:
            src_a = st.file_uploader(
                "Source A — preparation file or declaration (xlsx / csv / PDF / image)",
                type=["xlsx","xlsm","csv","tsv","pdf","jpg","jpeg","jfif","png","webp"],
                key="audit_a")
            src_b = st.file_uploader(
                "Source B — commercial invoice or second document (optional)",
                type=["xlsx","xlsm","csv","tsv","pdf","jpg","jpeg","jfif","png","webp"],
                key="audit_b")
        with col2:
            pasted = st.text_area(
                "Or paste the lines here", height=150,
                placeholder="BISSAP           1212999590   14 colis   274,4 kg   600   164640\nKAOLIN           2507002000   41 colis   803,6 kg   125   100450")
            ctx_txt = st.text_input(
                "Shipment context (optional)",
                placeholder="e.g. origin Côte d'Ivoire, 40RF reefer, sea freight Abidjan–Antwerp")

        st.markdown("#### Value calculation")
        v1, v2, v3, v4 = st.columns(4)
        currency  = v1.text_input("Currency", value="", placeholder="XOF / EUR / USD")
        rate      = v2.text_input("Rate (1 EUR = ...)", value="", placeholder="blank = fixed parity")
        incoterm  = v3.text_input("Incoterm", value="", placeholder="FOB / CIF / EXW")
        freight   = v4.text_input("Freight (EUR)", value="", placeholder="2450")
        i1, i2, _ = st.columns([1,1,2])
        insurance = i1.text_input("Insurance (EUR)", value="")
        other_add = i2.text_input("Other additions (EUR)", value="")

        if st.button("📥  Read dossier", use_container_width=True):
            if not src_a and not src_b and not (pasted and pasted.strip()):
                st.warning("Provide a file or paste the lines first.")
                st.stop()

            def _read_source(f=None, text=None, label=""):
                """Return (items, totals, header) for one source, or None."""
                if f is not None and audit.is_spreadsheet(f.name):
                    f.seek(0)
                    items, totals, colmap, notes = audit.parse_prep_file(f.read(), f.name)
                    if not items:
                        st.warning(f"{label}: geen regels herkend in het bestand."
                                   + (" " + "; ".join(notes) if notes else ""))
                        return None
                    return items, totals, {"source": f.name, "reader": "spreadsheet"}
                content = []
                if f is not None:
                    try:
                        content.append(build_file_block(f))
                    except Exception as e:
                        st.error(f"{label}: bestand kon niet worden verwerkt: "
                                 f"{type(e).__name__}: {e}")
                        return None
                if text:
                    content.append({"type":"text","text":"Document text:\n" + text.strip()})
                if not content:
                    return None
                with st.spinner(f"Reading {label.lower()}..."):
                    raw = call_claude(PROMPT_DOC_LINES, content, step="doc_reader")
                doc = extract_json(raw) or {}
                items = audit.items_from_doc_lines(doc.get("line_items"))
                if not items:
                    st.warning(f"{label}: geen goederenregels herkend.")
                    for w in doc.get("warnings", []):
                        st.markdown(f"- {w}")
                    return None
                header = {k: doc.get(k, "") for k in
                          ("document_type","document_number","document_date","currency",
                           "incoterm","country_of_origin","origin_statement","seller","buyer")}
                header.update({"source": getattr(f, "name", "pasted text"), "reader": "AI"})
                header["excluded_lines"] = doc.get("excluded_lines", [])
                header["warnings"] = doc.get("warnings", [])
                return items, audit.totals_from_stated(doc.get("stated_totals")), header

            sources = []
            for f, txt, label in ((src_a, None, "Source A"),
                                  (src_b, None, "Source B"),
                                  (None, pasted, "Pasted text")):
                if f is None and not (txt and txt.strip()):
                    continue
                got = _read_source(f, txt, label)
                if got:
                    sources.append({"label": label, "items": got[0],
                                    "totals": got[1], "header": got[2]})
            if not sources:
                st.error("Geen bruikbare regels gevonden in de aangeleverde bronnen.")
                st.stop()

            # The source carrying goods codes is the one being audited; a second
            # source becomes the cross-check. With equal coverage, the first wins.
            sources.sort(key=lambda s: audit.code_coverage(s["items"]), reverse=True)
            declared, cross = sources[0], (sources[1] if len(sources) > 1 else None)

            header = declared["header"]
            cross_header = cross["header"] if cross else {}
            st.session_state.audit_items   = declared["items"]
            st.session_state.audit_totals  = declared["totals"]
            st.session_state.audit_invoice = {
                **cross_header,
                "line_items": audit.items_to_doc_lines(cross["items"]) if cross else [],
                "stated_totals": (cross["totals"].get("grand") if cross else {}) or {},
            } if cross else {}
            st.session_state.audit_batch   = str(uuid.uuid4())[:8]
            st.session_state.audit_meta    = {
                "context": ctx_txt,
                "currency": (currency or header.get("currency","")
                             or cross_header.get("currency","") or "").strip(),
                "rate": rate.strip(),
                "incoterm": (incoterm or header.get("incoterm","")
                             or cross_header.get("incoterm","") or "").strip(),
                "freight": freight.strip(), "insurance": insurance.strip(),
                "other": other_add.strip(),
                "prep_name": header.get("source",""),
                "reader": header.get("reader",""),
                "cross_name": cross_header.get("source","") if cross else "",
                "origin": header.get("country_of_origin","") or cross_header.get("country_of_origin",""),
                "origin_statement": header.get("origin_statement","") or cross_header.get("origin_statement",""),
                "doc_number": header.get("document_number","") or cross_header.get("document_number",""),
                "single_source": cross is None,
                "code_coverage": audit.code_coverage(declared["items"]),
            }
            st.session_state.audit_stage = "review"
            st.rerun()

    # ── STAGE 2: deterministic checks (no API cost) ───────────────────────────
    elif st.session_state.audit_stage in ("review", "opinion"):
        items   = st.session_state.audit_items
        totals  = st.session_state.audit_totals
        invoice = st.session_state.audit_invoice or {}
        meta    = st.session_state.audit_meta
        batch   = st.session_state.audit_batch

        findings, basis, scores = audit.run_all_checks(
            items, totals, invoice.get("line_items") or [])
        st.session_state.audit_findings = findings
        summary = audit.summarize(findings)

        st.markdown(f"### Dossier {batch} "
                    f"<span style='color:#888;font-size:0.8rem;'>· {meta.get('prep_name','')} "
                    f"· {len(items)} regels</span>", unsafe_allow_html=True)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Lines", len(items))
        c2.metric("Errors", summary["errors"])
        c3.metric("Warnings", summary["warnings"])
        c4.metric("Notes", summary["infos"])

        src_line = f"Audited: {meta.get('prep_name','—')} ({meta.get('reader','')})"
        if meta.get("cross_name"):
            src_line += f" · cross-checked against {meta['cross_name']}"
        for extra in (meta.get("doc_number"), meta.get("currency"),
                      meta.get("incoterm"), meta.get("origin")):
            if extra:
                src_line += f" · {extra}"
        st.markdown(f"<span style='color:#888;font-size:0.82rem;'>{src_line}</span>",
                    unsafe_allow_html=True)
        if meta.get("origin_statement"):
            st.markdown(f"<span style='color:#888;font-size:0.8rem;'>Origin statement: "
                        f"{meta['origin_statement'][:200]}</span>", unsafe_allow_html=True)

        if meta.get("single_source"):
            st.info("Only one source was provided, so figures could not be compared between "
                    "documents. Internal consistency (line amounts, totals, weights, code "
                    "format) is still fully checked.")
        if meta.get("reader") == "AI":
            st.markdown("<span style='color:#888;font-size:0.8rem;'>The audited lines were read "
                        "from a document by the AI reader. Spot-check the figures below against "
                        "the original before relying on them.</span>", unsafe_allow_html=True)
        if not meta.get("code_coverage"):
            st.warning("No goods codes were found on the audited source. The arithmetic checks "
                       "still apply; the code comparison will show DKM's own classification only.")

        st.divider()

        # ── value calculation ─────────────────────────────────────────────────
        stated_total = ((totals.get("grand") or {}).get("amount")
                        or sum(i.get("amount") or 0 for i in items))
        cv, steps, vwarn = audit.customs_value(
            stated_total, meta.get("currency"), audit.to_num(meta.get("rate")),
            meta.get("incoterm"), audit.to_num(meta.get("freight")) or 0,
            audit.to_num(meta.get("insurance")) or 0, audit.to_num(meta.get("other")) or 0)
        st.session_state.audit_value = {"value": cv, "steps": steps, "warnings": vwarn}

        st.markdown("### Customs value")
        if cv is None:
            st.warning("; ".join(vwarn) or "Niet berekend.")
        else:
            rows = "".join(
                f"<tr><td style='padding:2px 14px 2px 0;color:#aaa;'>{k}</td>"
                f"<td style='font-family:monospace;color:#eee;'>{v}</td></tr>" for k, v in steps)
            st.markdown(f"<div class='tree-box'><table>{rows}</table></div>",
                        unsafe_allow_html=True)
            for w in vwarn:
                st.warning(w)

        st.divider()

        # ── findings ──────────────────────────────────────────────────────────
        st.markdown("### Checks")
        st.markdown(f"<span style='color:#888;font-size:0.82rem;'>Line amounts were verified "
                    f"against <strong>{ {'net':'net weight','gross':'gross weight','packages':'package count'}.get(basis,'—') }</strong> "
                    f"× unit price ({scores.get(basis,0)} of {len(items)} lines match this basis). "
                    f"All arithmetic is computed by the application, not by the AI.</span>",
                    unsafe_allow_html=True)

        if not findings:
            st.success("✓ No arithmetic or code-format problems found.")
        for sev, css, icon in [("error","verdict-invalid","✗"),
                               ("warning","verdict-partial","~"),
                               ("info","verdict-verified","ℹ")]:
            group = [f for f in findings if f["severity"] == sev]
            if not group:
                continue
            body = "".join(
                f"<div style='margin-bottom:6px;'>{icon} "
                f"<strong>{(f['line'] or 'dossier')}</strong> — {f['message']}"
                + (f"<br><span style='color:#888;font-size:0.8rem;margin-left:16px;'>{f['detail']}</span>"
                   if f.get('detail') else "") + "</div>"
                for f in group)
            st.markdown(f"<div class='{css}'>{body}</div>", unsafe_allow_html=True)

        st.divider()

        # ── declared lines ────────────────────────────────────────────────────
        st.markdown("### Declared lines")
        st.dataframe([{
            "Product": i["product"][:44], "Declared code": i["hs_code"],
            "Colis": i["packages"], "Gross": i["gross"], "Net": i["net"],
            "Unit": i["price"], "Amount": i["amount"],
        } for i in items], use_container_width=True)

        # ── own opinion ───────────────────────────────────────────────────────
        st.divider()
        st.markdown("### DKM classification opinion")

        if st.session_state.audit_stage == "review":
            est = len(items) * 4
            col_go, col_reset = st.columns([2,1])
            with col_go:
                run_op = st.button(f"⚖️  Classify all {len(items)} lines independently",
                                   use_container_width=True)
            with col_reset:
                if st.button("✕  New dossier", use_container_width=True):
                    _reset_audit(); st.rerun()
            st.markdown(f"<span style='color:#888;font-size:0.8rem;'>Runs the full 3-step pipeline "
                        f"per line and compares the result with the declared code. "
                        f"≈ {est} API calls.</span>", unsafe_allow_html=True)

            if run_op:
                st.session_state.audit_opinions = []
                total = len(items)
                bar = st.progress(0.0, text="Starting...")
                for n, it in enumerate(items, start=1):
                    bar.progress((n-1)/total, text=f"Line {n} of {total} — {it['product'][:50]}")
                    specs = " · ".join(x for x in [
                        f"{it['packages']:g} packages" if it.get("packages") else "",
                        f"gross {it['gross']:g} kg" if it.get("gross") else "",
                        f"net {it['net']:g} kg" if it.get("net") else "",
                        f"unit price {it['price']:g}" if it.get("price") else "",
                    ] if x)
                    extra = meta.get("context","")
                    if invoice.get("country_of_origin"):
                        extra += f"\nCountry of origin: {invoice['country_of_origin']}"
                    try:
                        raw1, json1, raw2, json2, raw3, json3 = run_pipeline(
                            it["product"], specs, None, None, extra_context=extra.strip())
                    except Exception as e:
                        st.session_state.audit_opinions.append(
                            {"product": it["product"], "declared": it["hs_code"],
                             "error": f"{type(e).__name__}: {e}"})
                        continue

                    own_taric = ((json3 or {}).get("taric_code","")
                                 or (json2 or {}).get("taric_code",""))
                    own_cn    = (json2 or {}).get("cn_code","")
                    level     = audit.compare_codes(it["hs_code"], own_taric or own_cn)

                    compare = {}
                    if it["hs_code"] and level != "identical" and own_cn:
                        cmp_input = (
                            f"Product data:\n{json.dumps(json1, indent=2) if json1 else it['product']}\n\n"
                            f"Declared code in preparation file: {it['hs_code']}\n\n"
                            f"DKM engine code: CN {own_cn} / TARIC {own_taric}\n"
                            f"Engine confidence: {(json2 or {}).get('confidence','')}\n\n"
                            f"Engine reasoning:\n{raw2[:4000]}")
                        try:
                            compare = extract_json(call_claude(PROMPT_CODE_COMPARE, cmp_input,
                                                              step="code_compare")) or {}
                        except Exception:
                            compare = {}

                    tree = build_decision_tree(it["product"], specs, json1, json2, json3, raw2)
                    row_id = f"AUD{batch}-{n:02d}"
                    issues = list((json3 or {}).get("issues", []))
                    issues.append(f"Declared {it['hs_code']} vs DKM {own_taric or own_cn} "
                                  f"({audit.AGREEMENT_LABELS.get(level,('?',''))[0]})")
                    save_result(it["product"], specs, None, None, json1, json2,
                                {**(json3 or {}), "issues": issues},
                                raw1, raw2, raw3, tree,
                                row_id_override=row_id,
                                desc_prefix=f"[AUDIT {batch} {n}/{total}] ", quiet=True,
                                batch_id=batch, source="audit",
                                declared_code=it["hs_code"],
                                agreement=audit.AGREEMENT_LABELS.get(level,("?",""))[0])

                    st.session_state.audit_opinions.append({
                        "row_id": row_id, "product": it["product"], "declared": it["hs_code"],
                        "own_cn": own_cn, "own_taric": own_taric,
                        "own_desc": (json2 or {}).get("cn_description",""),
                        "confidence": (json2 or {}).get("confidence",""),
                        "outcome": (json3 or {}).get("validation_outcome",""),
                        "level": level, "compare": compare, "tree": tree, "raw2": raw2,
                    })
                    bar.progress(n/total, text=f"Line {n} of {total} done")
                bar.empty()
                st.session_state.audit_stage = "opinion"
                st.rerun()

        else:
            ops = st.session_state.audit_opinions
            ok  = [o for o in ops if not o.get("error")]
            agree = len([o for o in ok if o["level"] == "identical"])
            minor = len([o for o in ok if o["level"] in ("taric","subheading")])
            major = len([o for o in ok if o["level"] in ("heading","chapter","different")])
            _ac = pricing.summarize_events(
                [e for e in st.session_state.usage_events if e.get("batch_id") == batch])
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Identical", agree)
            m2.metric("Minor difference", minor)
            m3.metric("Substantive difference", major)
            m4.metric("AI cost", pricing.fmt_usd(_ac["cost_usd"]))

            st.dataframe([{
                "Product": o["product"][:40],
                "Declared": o.get("declared",""),
                "DKM": o.get("own_taric") or o.get("own_cn",""),
                "Agreement": audit.AGREEMENT_LABELS.get(o.get("level","unknown"))[0],
                "Conf.": o.get("confidence",""),
                "Prefers": (o.get("compare") or {}).get("preferred",""),
                "Risk": (o.get("compare") or {}).get("risk",""),
            } for o in ops], use_container_width=True)

            csv_rows = ["product;declared_code;dkm_cn;dkm_taric;agreement;confidence;preferred;risk;recommended;row_id"]
            for o in ops:
                c = o.get("compare") or {}
                csv_rows.append(";".join([
                    '"' + o["product"].replace('"',"'") + '"', str(o.get("declared","")),
                    str(o.get("own_cn","")), str(o.get("own_taric","")),
                    audit.AGREEMENT_LABELS.get(o.get("level","unknown"))[0],
                    str(o.get("confidence","")), str(c.get("preferred","")),
                    str(c.get("risk","")), str(c.get("recommended_code","")),
                    str(o.get("row_id",""))]))
            fnd = ["", "severity;code;line;message"]
            for f in st.session_state.audit_findings:
                fnd.append(";".join([f["severity"], f["code"], '"' + str(f["line"] or "") + '"',
                                     '"' + f["message"].replace('"',"'") + '"']))
            st.download_button("⬇  Download audit as CSV",
                               "\n".join(csv_rows + fnd),
                               file_name=f"dkm_audit_{batch}.csv", mime="text/csv")

            st.divider()
            for o in ops:
                if o.get("error"):
                    st.error(f"{o['product'][:50]}: {o['error']}")
                    continue
                lvl   = o.get("level","unknown")
                label, expl = audit.AGREEMENT_LABELS.get(lvl, ("?",""))
                sev   = audit.AGREEMENT_SEVERITY.get(lvl,"warning")
                css   = {"ok":"verdict-validated","warning":"verdict-partial",
                         "error":"verdict-invalid"}[sev]
                icon  = {"ok":"✓","warning":"~","error":"✗"}[sev]
                c = o.get("compare") or {}
                body = (f"<div style='font-size:0.8rem;font-weight:600;letter-spacing:0.06em;"
                        f"text-transform:uppercase;margin-bottom:0.5rem;'>{icon} {label}</div>"
                        f"<div style='color:#ddd;font-size:0.95rem;margin-bottom:6px;'>"
                        f"{o['product'][:70]}</div>"
                        f"<div style='display:flex;gap:26px;flex-wrap:wrap;'>"
                        f"<div><span style='color:#888;font-size:0.72rem;'>DECLARED</span><br>"
                        f"<span class='cn-code' style='font-size:1.25rem;'>{o.get('declared','—')}</span></div>"
                        f"<div><span style='color:#888;font-size:0.72rem;'>DKM ENGINE</span><br>"
                        f"<span class='cn-code' style='font-size:1.25rem;'>{o.get('own_taric') or o.get('own_cn','—')}</span></div>"
                        f"</div>")
                if o.get("own_desc"):
                    body += (f"<div style='color:#aaa;font-size:0.83rem;margin-top:6px;'>"
                             f"{o['own_desc']}</div>")
                if c.get("reasoning"):
                    body += (f"<div style='color:#ccc;font-size:0.86rem;margin-top:8px;'>"
                             f"<strong>Opinion ({c.get('preferred','')}"
                             + (f", risk {c['risk']}" if c.get("risk") else "") + ")</strong>: "
                             f"{c['reasoning']}</div>")
                if c.get("question_for_client"):
                    body += (f"<div style='color:#f0a030;font-size:0.83rem;margin-top:6px;'>"
                             f"→ Ask the client: {c['question_for_client']}</div>")
                st.markdown(f"<div class='{css}'>{body}</div>", unsafe_allow_html=True)
                with st.expander(f"📋  Decision tree — {o.get('row_id','')}"):
                    st.markdown(f"<div class='tree-box'>{o['tree']}</div>", unsafe_allow_html=True)
                with st.expander(f"Full reasoning — {o.get('row_id','')}"):
                    st.markdown(o["raw2"])

            st.divider()
            if st.button("✕  New dossier", use_container_width=True):
                _reset_audit(); st.rerun()

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: SENIOR REVIEW
# ═══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "review":
    st.markdown("## Senior Review")
    st.markdown("<span style='color:#888;font-size:0.85rem;'>Review and confirm or reject AI classifications</span>",
                unsafe_allow_html=True)
    st.divider()

    if not st.session_state.username.strip():
        st.warning("Please enter your name or initials in the sidebar first.")
        st.stop()

    try:
        if neon_dsn() and ensure_schema():
            pending = neon.get_pending_reviews(neon_dsn())
        else:
            sid, sac = get_secrets()
            pending  = get_pending_reviews(sid, sac)
    except Exception as e:
        st.error(f"Could not load reviews: {e}")
        st.stop()

    if not pending:
        st.success("✓ All classifications have been reviewed.")
        st.stop()

    st.markdown(f"**{len(pending)} classification(s) pending review**")
    st.divider()

    for idx, rec in enumerate(pending):
        row_id  = rec.get("row_id","?")
        ts      = rec.get("timestamp","")
        user    = rec.get("user","")
        desc    = rec.get("description","")
        cn      = rec.get("cn_code","—")
        taric   = rec.get("taric_code","—")
        conf    = rec.get("confidence","—")
        outcome = rec.get("outcome","—")
        tree    = rec.get("decision_tree","")
        issues  = rec.get("issues","")
        fqa     = rec.get("followup_qa","")

        with st.container():
            st.markdown("<div class='review-card'>", unsafe_allow_html=True)
            c1, c2, c3, c4 = st.columns([3,2,2,2])
            c1.markdown(
                f"**{desc[:80]}{'...' if len(desc)>80 else ''}**  \n"
                f"<span style='color:#888;font-size:0.78rem'>{ts} · by {user}</span>",
                unsafe_allow_html=True)
            c2.metric("CN code",    cn)
            c3.metric("TARIC code", taric)
            c4.metric("Confidence", conf)

            if issues:
                st.warning(f"Issues: {issues}")
            if fqa:
                with st.expander("💬 Follow-up Q&A used"):
                    for pair in fqa.split(" | "):
                        st.markdown(f"- {pair}")

            with st.expander(f"📋 Decision tree — {row_id}"):
                st.markdown(f"<div class='tree-box'>{tree}</div>", unsafe_allow_html=True)

            col_v, col_c = st.columns([1,3])
            with col_v:
                verdict = st.selectbox("Verdict",
                    ["CONFIRMED","REJECTED","NEEDS_MORE_INFO"],
                    key=f"verdict_{idx}_{row_id}")
            with col_c:
                comment = st.text_input(
                    "Comment (optional — shown on future matches)",
                    key=f"comment_{idx}_{row_id}",
                    placeholder="e.g. Confirmed after checking TARIC chapter note 3(b)")

            if st.button("✔  Submit review", key=f"submit_{idx}_{row_id}"):
                try:
                    if neon_dsn() and ensure_schema():
                        from utils.sheets import _make_fingerprint
                        neon.save_senior_review(
                            neon_dsn(), row_id=row_id, verdict=verdict, comment=comment,
                            senior_user=st.session_state.username,
                            cn_code=cn, taric_code=taric, description=desc,
                            fingerprint=_make_fingerprint(desc))
                    if sheets_configured():
                        sid, sac = get_secrets()
                        save_senior_review(
                            row_id=row_id, verdict=verdict, comment=comment,
                            senior_user=st.session_state.username,
                            cn_code=cn, taric_code=taric, description=desc,
                            spreadsheet_id=sid, service_account_info=sac,
                        )
                    st.success(f"✓ Review saved — {verdict}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not save review: {e}")

            st.markdown("</div>", unsafe_allow_html=True)
            st.divider()

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: HISTORY & ANALYTICS
# ═══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "history":
    st.markdown("## History & Analytics")
    st.divider()

    try:
        if neon_dsn() and ensure_schema():
            all_rows = neon.get_all_history(neon_dsn())
        else:
            sid, sac = get_secrets()
            all_rows = get_all_history(sid, sac)
    except Exception as e:
        st.error(f"Could not load history: {e}")
        st.stop()

    if not all_rows:
        st.info("No classifications logged yet.")
        st.stop()

    import pandas as pd
    df = pd.DataFrame(all_rows)

    total     = len(df)
    validated = len(df[df["outcome"].str.contains("VALIDATED",na=False) & ~df["outcome"].str.contains("NOT",na=False)])
    partial   = len(df[df["outcome"].str.contains("PARTIAL",na=False)])
    rejected  = len(df[df["outcome"].str.contains("NOT VALIDATED",na=False)])
    reviewed  = len(df[df["senior_reviewed"].str.lower() == "yes"]) if "senior_reviewed" in df.columns else 0

    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Total",        total)
    c2.metric("Validated",    validated)
    c3.metric("Partial",      partial)
    c4.metric("Rejected",     rejected)
    c5.metric("Sr. reviewed", reviewed)

    st.divider()
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        filter_outcome = st.multiselect("Filter by outcome",
            ["VALIDATED","PARTIALLY VALIDATED","NOT VALIDATED"],
            default=["VALIDATED","PARTIALLY VALIDATED","NOT VALIDATED"])
    with col_f2:
        filter_review = st.selectbox("Filter by review status",
            ["All","Pending review","Reviewed"])

    filtered = df[df["outcome"].isin(filter_outcome)] if filter_outcome else df
    if filter_review == "Pending review":
        filtered = filtered[filtered.get("senior_reviewed","no").str.lower() != "yes"]
    elif filter_review == "Reviewed":
        filtered = filtered[filtered.get("senior_reviewed","no").str.lower() == "yes"]

    display_cols = ["timestamp","user","description","cn_code","taric_code",
                    "confidence","outcome","cost_usd","senior_reviewed","senior_verdict","senior_user"]
    available = [c for c in display_cols if c in filtered.columns]
    st.dataframe(filtered[available], use_container_width=True, height=400)

    # ── AI cost ───────────────────────────────────────────────────────────────
    st.divider()
    st.markdown("## AI cost")

    if "cost_usd" in df.columns:
        spent = float(pd.to_numeric(df["cost_usd"], errors="coerce").fillna(0).sum())
        k1, k2, k3 = st.columns(3)
        k1.metric("Logged classifications", total)
        k2.metric("Total cost", pricing.fmt_usd(spent))
        k3.metric("Average per line", pricing.fmt_usd(spent / total if total else 0))

    if not neon_dsn():
        st.info("Detailed cost analytics (per day, per user, per pipeline step) require the "
                "Neon backend. Add NEON_DATABASE_URL to your secrets to enable it.")
    else:
        days = st.selectbox("Period", [7, 30, 90, 365], index=1,
                            format_func=lambda d: f"last {d} days")
        try:
            u = neon.usage_summary(neon_dsn(), days)
        except Exception as e:
            st.warning(f"Kon verbruiksgegevens niet ophalen: {type(e).__name__}: {e}")
            u = None
        if u:
            t = u["totals"]
            eur = pricing.fmt_eur(t.get("cost", 0), usd_per_eur())
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("API calls", f"{int(t.get('calls',0)):,}")
            c2.metric("Cost", pricing.fmt_usd(t.get("cost", 0)), eur or None)
            c3.metric("Input tokens", f"{int(t.get('input_tokens',0)):,}")
            c4.metric("Output tokens", f"{int(t.get('output_tokens',0)):,}")

            if u["by_day"]:
                dfd = pd.DataFrame(u["by_day"])
                dfd["cost"] = dfd["cost"].astype(float)
                st.markdown("##### Cost per day")
                st.bar_chart(dfd.set_index("day")["cost"], height=200)

            cc1, cc2 = st.columns(2)
            with cc1:
                st.markdown("##### Per user")
                st.dataframe([{"User": r["app_user"] or "—", "Calls": int(r["calls"]),
                               "Cost": pricing.fmt_usd(r["cost"])} for r in u["by_user"]],
                             use_container_width=True)
            with cc2:
                st.markdown("##### Per pipeline step")
                st.dataframe([{"Model": r["model"], "Step": r["step"],
                               "Calls": int(r["calls"]), "Cost": pricing.fmt_usd(r["cost"])}
                              for r in u["by_model"]], use_container_width=True)

            try:
                dossiers = neon.cost_per_dossier(neon_dsn())
            except Exception:
                dossiers = []
            if dossiers:
                st.markdown("##### Cost per dossier")
                st.dataframe([{"Batch": d["batch_id"], "Started": d["started"],
                               "Lines": int(d["lines"]), "Cost": pricing.fmt_usd(d["cost"]),
                               "Per line": pricing.fmt_usd(float(d["cost"])/max(1,int(d["lines"])))}
                              for d in dossiers], use_container_width=True)

    st.markdown(f"<span style='color:#666;font-size:0.76rem;'>Costs are estimates computed from "
                f"token counts returned by the API, priced with the table in utils/pricing.py "
                f"(rates checked {pricing.PRICING_VERIFIED}). The authoritative figure is the "
                f"Anthropic Console usage dashboard.</span>", unsafe_allow_html=True)
