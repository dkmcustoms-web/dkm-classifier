import streamlit as st
import json
import base64
from datetime import datetime
from anthropic import Anthropic
from utils.sheets import log_to_sheets
from utils.prompts import PROMPT1, PROMPT2, PROMPT3

st.set_page_config(
    page_title="DKM Classifier",
    page_icon="🔍",
    layout="wide",
)

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
    .cn-code { font-size:2rem; font-weight:700; color:#D94F2B; font-family:monospace; }
    .tree-box { background:#1e1e1e; border:1px solid #333; border-radius:8px;
        padding:1.2rem 1.5rem; margin-top:1rem; font-family:monospace; font-size:0.82rem;
        line-height:1.8; color:#ccc; white-space:pre-wrap; }
    .tree-step { color:#D94F2B; font-weight:700; }
    .tree-ok   { color:#4a9e4a; }
    .tree-warn { color:#c8880a; }
    .tree-bad  { color:#c84a4a; }
    .tree-dim  { color:#666; }
</style>
""", unsafe_allow_html=True)

# ── Logo + title ──────────────────────────────────────────────────────────────
col_logo, col_title = st.columns([1, 6])
with col_logo:
    try:
        st.image("assets/dkm_logo.png", width=70)
    except Exception:
        st.markdown("**DKM**")
with col_title:
    st.markdown("## CN/TARIC Classification Tool")
    st.markdown("<span style='color:#888;font-size:0.85rem;'>Powered by DKM Classification Engine · 3-step AI pipeline</span>",
                unsafe_allow_html=True)

st.divider()

if "username" not in st.session_state:
    st.session_state.username = ""
if "history" not in st.session_state:
    st.session_state.history = []

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### User")
    username = st.text_input("Name / initials", value=st.session_state.username, placeholder="e.g. LVD")
    st.session_state.username = username
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

# ── Input form ────────────────────────────────────────────────────────────────
st.markdown("### Product information")
col1, col2 = st.columns(2)
with col1:
    description = st.text_area("Product description / invoice description", height=120,
        placeholder="e.g. Hydraulic pump for tractors, cast iron housing, max 250 bar, 45 l/min flow rate...")
    specs = st.text_area("Technical specifications (optional)", height=80,
        placeholder="Material composition, power, dimensions, standards...")
with col2:
    img_file = st.file_uploader("Product image (optional)", type=["jpg","jpeg","png","webp"])
    inv_file = st.file_uploader("Invoice document / image (optional)", type=["jpg","jpeg","png","webp","pdf"])
    if img_file:
        st.image(img_file, caption="Product image", use_container_width=True)

run_btn = st.button("🔍  Classify product", use_container_width=True)

# ── Helpers ───────────────────────────────────────────────────────────────────
def file_to_b64(f):
    return base64.b64encode(f.read()).decode("utf-8")

def extract_json(text: str):
    idx = text.rfind("{")
    if idx == -1:
        return None
    try:
        return json.loads(text[idx:])
    except Exception:
        return None

def call_claude(system: str, user_content) -> str:
    client = Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    if isinstance(user_content, str):
        user_content = [{"type": "text", "text": user_content}]
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user_content}]
    )
    return "".join(b.text for b in resp.content if hasattr(b, "text"))

def verdict_html(outcome, code, taric, manual, issues):
    if "NOT VALIDATED" in outcome:
        css, icon, label = "verdict-invalid", "✗", "Not validated"
    elif "PARTIAL" in outcome:
        css, icon, label = "verdict-partial", "~", "Partially validated"
    else:
        css, icon, label = "verdict-validated", "✓", "Validated"
    code_str = code or "—"
    if taric and taric != code:
        code_str += f" / {taric}"
    issues_str = ("<br><small style='color:#aaa'>Issues: " + "; ".join(issues) + "</small>") if issues else ""
    manual_str = "<br><small style='color:#f0a030'>⚠ Manual review recommended</small>" if manual else ""
    return f"""<div class='{css}'>
        <div style='font-size:0.8rem;font-weight:600;letter-spacing:0.06em;
                    text-transform:uppercase;margin-bottom:0.5rem;'>{icon} {label}</div>
        <div class='cn-code'>{code_str}</div>{manual_str}{issues_str}</div>"""

def build_decision_tree(description, specs, json1, json2, json3, raw2) -> str:
    """Build a readable decision tree / audit trail from all 3 pipeline steps."""
    lines = []
    lines.append("CLASSIFICATION DECISION TREE")
    lines.append("=" * 60)

    # ── INPUT ──
    lines.append("\n▸ INPUT")
    lines.append(f"  Description : {(description or '—')[:120]}")
    if specs:
        lines.append(f"  Specs       : {specs[:120]}")

    # ── STEP 1 ──
    lines.append("\n▸ STEP 1 — FEATURE EXTRACTION")
    if json1:
        lines.append(f"  Product     : {json1.get('product_identification','—')}")
        mats = json1.get('materials') or []
        lines.append(f"  Materials   : {', '.join(mats) if mats else '—'}")
        lines.append(f"  Function    : {json1.get('function','—')}")
        lines.append(f"  Form        : {json1.get('form','—')}")
        lines.append(f"  Category    : {json1.get('category_hint','—')}")
        lines.append(f"  Is part     : {json1.get('is_part', False)}")
        lines.append(f"  Is set      : {json1.get('is_set', False)}")
        lines.append(f"  Data quality: {json1.get('data_quality','—')}")
        missing = json1.get('missing_information') or []
        if missing:
            lines.append(f"  ⚠ Missing   : {', '.join(missing)}")
        ambig = json1.get('ambiguities') or []
        if ambig:
            lines.append(f"  ⚠ Ambiguous : {', '.join(ambig)}")
    else:
        lines.append("  ⚠ Could not parse structured extraction")

    # ── STEP 2 ──
    lines.append("\n▸ STEP 2 — CLASSIFICATION REASONING")
    if json2:
        candidates = json2.get('candidate_headings') or []
        if candidates:
            lines.append(f"  Candidates  : {', '.join(str(c) for c in candidates)}")

        # Extract GIR / legal notes section from raw reasoning if present
        reasoning_lines = raw2.splitlines()
        in_section = False
        captured = []
        keywords = ["STEP 3","STEP 4","STEP 5","STEP 6","STEP 7","STEP 8",
                    "GIR","legal note","heading","subheading","chapter","section"]
        for rl in reasoning_lines:
            rl_stripped = rl.strip()
            if any(kw.lower() in rl_stripped.lower() for kw in keywords):
                if rl_stripped:
                    captured.append("  │ " + rl_stripped[:120])
            if len(captured) >= 20:
                break
        if captured:
            lines.append("  Reasoning excerpts:")
            lines.extend(captured)

        lines.append(f"  → CN code   : {json2.get('cn_code','—')}")
        lines.append(f"  → TARIC code: {json2.get('taric_code','—')}")
        lines.append(f"  Confidence  : {json2.get('confidence','—')}")
        warnings = json2.get('warnings') or []
        for w in warnings:
            lines.append(f"  ⚠ Warning   : {w}")
        lines.append(f"  Manual review: {'YES' if json2.get('manual_review_recommended') else 'no'}")
    else:
        lines.append("  ⚠ Could not parse classification JSON")

    # ── STEP 3 ──
    lines.append("\n▸ STEP 3 — VALIDATION")
    if json3:
        outcome = json3.get('validation_outcome','—')
        symbol = "✓" if "VALIDATED" in outcome and "NOT" not in outcome else (
                 "~" if "PARTIAL" in outcome else "✗")
        lines.append(f"  Outcome     : {symbol} {outcome}")
        lines.append(f"  Final code  : {json3.get('validated_code','—')}")
        issues = json3.get('issues') or []
        for iss in issues:
            lines.append(f"  ✗ Issue     : {iss}")
        missing = json3.get('missing_data') or []
        for m in missing:
            lines.append(f"  ⚠ Missing   : {m}")
        lines.append(f"  Manual review: {'YES' if json3.get('manual_review_recommended') else 'no'}")
    else:
        lines.append("  ⚠ Could not parse validation JSON")

    lines.append("\n" + "=" * 60)
    lines.append(f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)

# ── Pipeline ──────────────────────────────────────────────────────────────────
if run_btn:
    if not description and not img_file and not inv_file:
        st.warning("Please provide at least a product description or upload an image.")
        st.stop()
    if not st.session_state.username.strip():
        st.warning("Please enter your name or initials in the sidebar first.")
        st.stop()

    st.divider()
    st.markdown("### Pipeline results")

    # ── STEP 1 ────────────────────────────────────────────────────────────────
    with st.status("**Step 1** — Feature extraction…", expanded=True) as s1:
        user_content = []
        for f in [img_file, inv_file]:
            if f:
                f.seek(0)
                b64  = file_to_b64(f)
                mime = f.type if f.type else "image/jpeg"
                user_content.append({"type":"image","source":{"type":"base64","media_type":mime,"data":b64}})
        txt = ""
        if description:
            txt += f"Product description / invoice text:\n{description}\n\n"
        if specs:
            txt += f"Technical specifications:\n{specs}"
        if txt:
            user_content.append({"type":"text","text":txt.strip()})

        raw1  = call_claude(PROMPT1, user_content)
        json1 = extract_json(raw1)

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

    # ── STEP 2 ────────────────────────────────────────────────────────────────
    with st.status("**Step 2** — CN/TARIC classification…", expanded=True) as s2:
        step2_input = "Structured product data from feature extractor:\n\n" + (
            json.dumps(json1, indent=2) if json1 else raw1)
        raw2  = call_claude(PROMPT2, step2_input)
        json2 = extract_json(raw2)

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

    # ── STEP 3 ────────────────────────────────────────────────────────────────
    with st.status("**Step 3** — Validation…", expanded=True) as s3:
        step3_input = (
            f"Product data:\n{json.dumps(json1, indent=2) if json1 else description}\n\n"
            f"Proposed classification:\n{json.dumps(json2, indent=2) if json2 else raw2}\n\n"
            f"Full reasoning:\n{raw2}"
        )
        raw3  = call_claude(PROMPT3, step3_input)
        json3 = extract_json(raw3)

        with st.expander("Full validation reasoning"):
            st.markdown(raw3)
        s3.update(label="**Step 3** — Validation ✓", state="complete")

    # ── FINAL VERDICT ─────────────────────────────────────────────────────────
    outcome = json3.get("validation_outcome","UNKNOWN") if json3 else "UNKNOWN"
    code    = (json3 or {}).get("validated_code","") or (json2 or {}).get("cn_code","")
    taric   = (json3 or {}).get("taric_code","")     or (json2 or {}).get("taric_code","")
    manual  = bool((json3 or {}).get("manual_review_recommended") or
                   (json2 or {}).get("manual_review_recommended"))
    issues  = (json3 or {}).get("issues",[])

    st.markdown(verdict_html(outcome, code, taric, manual, issues), unsafe_allow_html=True)

    # ── DECISION TREE ─────────────────────────────────────────────────────────
    decision_tree = build_decision_tree(description, specs, json1, json2, json3, raw2)

    with st.expander("📋  Decision tree / audit trail", expanded=False):
        st.markdown(f"<div class='tree-box'>{decision_tree}</div>", unsafe_allow_html=True)

    # ── LOG TO GOOGLE SHEETS ──────────────────────────────────────────────────
    row = {
        "timestamp":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user":            st.session_state.username,
        "description":     description[:200] if description else "",
        "specs":           specs[:200] if specs else "",
        "has_image":       "yes" if img_file else "no",
        "has_invoice":     "yes" if inv_file else "no",
        "product_id":      (json1 or {}).get("product_identification",""),
        "category":        (json1 or {}).get("category_hint",""),
        "data_quality":    (json1 or {}).get("data_quality",""),
        "cn_code":         (json2 or {}).get("cn_code",""),
        "taric_code":      (json2 or {}).get("taric_code",""),
        "confidence":      (json2 or {}).get("confidence",""),
        "outcome":         outcome,
        "validated_code":  code,
        "manual_review":   "yes" if manual else "no",
        "issues":          "; ".join(issues),
        "decision_tree":   decision_tree,
        "raw_step1":       json.dumps(json1) if json1 else raw1[:500],
        "raw_step2":       raw2[:500],
        "raw_step3":       raw3[:500],
    }

    try:
        log_to_sheets(row, st.secrets["GOOGLE_SHEETS_ID"],
                      st.secrets["GOOGLE_SERVICE_ACCOUNT"])
        st.success("✓ Saved to Google Sheets")
    except Exception as e:
        import traceback
        st.warning(f"Sheets logging failed: {type(e).__name__}: {e}")
        st.code(traceback.format_exc(), language="text")

    st.session_state.history.append({
        "timestamp": datetime.now().strftime("%H:%M"),
        "cn_code":   code,
        "outcome":   outcome,
    })
