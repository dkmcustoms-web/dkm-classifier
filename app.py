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

# ── Branding ──────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    [data-testid="stAppViewContainer"] { background-color: #1a1a1a; }
    [data-testid="stSidebar"] { background-color: #111111; }
    .stButton > button {
        background-color: #D94F2B;
        color: white;
        border: none;
        border-radius: 6px;
        font-weight: 600;
        padding: 0.5rem 2rem;
    }
    .stButton > button:hover { background-color: #b83e21; }
    .verdict-validated { background:#1a3d1a; border:1px solid #4a9e4a;
        border-radius:8px; padding:1rem 1.25rem; margin-top:1rem; }
    .verdict-partial   { background:#3d2e0a; border:1px solid #c8880a;
        border-radius:8px; padding:1rem 1.25rem; margin-top:1rem; }
    .verdict-invalid   { background:#3d0f0f; border:1px solid #c84a4a;
        border-radius:8px; padding:1rem 1.25rem; margin-top:1rem; }
    .step-header { font-size:0.8rem; font-weight:600; letter-spacing:0.08em;
        text-transform:uppercase; color:#888; margin-bottom:0.25rem; }
    .cn-code { font-size:2rem; font-weight:700; color:#D94F2B; font-family:monospace; }
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
    st.markdown("## CN/TARIC Classificatie Tool")
    st.markdown("<span style='color:#888;font-size:0.85rem;'>Powered by DKM Classification Engine · 3-staps AI pipeline</span>", unsafe_allow_html=True)

st.divider()

# ── Session state ─────────────────────────────────────────────────────────────
if "username" not in st.session_state:
    st.session_state.username = ""
if "history" not in st.session_state:
    st.session_state.history = []

# ── Sidebar: gebruiker + history ──────────────────────────────────────────────
with st.sidebar:
    st.markdown("### Gebruiker")
    username = st.text_input("Naam / initialen", value=st.session_state.username,
                              placeholder="bv. LVD")
    st.session_state.username = username

    st.divider()
    st.markdown("### Sessie history")
    if st.session_state.history:
        for i, entry in enumerate(reversed(st.session_state.history[-10:])):
            ts = entry.get("timestamp","")
            code = entry.get("cn_code","—")
            outcome = entry.get("outcome","—")
            color = "#4a9e4a" if "VALIDATED" in outcome and "NOT" not in outcome else (
                    "#c8880a" if "PARTIAL" in outcome else "#c84a4a")
            st.markdown(
                f"<div style='font-size:0.78rem;padding:4px 0;border-bottom:1px solid #333;'>"
                f"<span style='color:#888'>{ts}</span><br>"
                f"<span style='font-family:monospace;font-weight:600'>{code}</span> "
                f"<span style='color:{color};font-size:0.72rem'>{outcome}</span></div>",
                unsafe_allow_html=True
            )
    else:
        st.markdown("<span style='color:#555;font-size:0.8rem'>Nog geen zoekopdrachten</span>",
                    unsafe_allow_html=True)

# ── Input formulier ───────────────────────────────────────────────────────────
st.markdown("### Productinformatie")

col1, col2 = st.columns(2)
with col1:
    description = st.text_area(
        "Productomschrijving / factuuromschrijving",
        height=120,
        placeholder="Bv: Hydraulic pump for tractors, cast iron housing, max 250 bar, 45 l/min flow rate..."
    )
    specs = st.text_area(
        "Technische specificaties (optioneel)",
        height=80,
        placeholder="Materiaalsamenstelling, vermogen, afmetingen, normen..."
    )

with col2:
    img_file = st.file_uploader("Productafbeelding (optioneel)", type=["jpg","jpeg","png","webp"])
    inv_file = st.file_uploader("Factuurdocument / afbeelding (optioneel)", type=["jpg","jpeg","png","webp","pdf"])

    if img_file:
        st.image(img_file, caption="Productafbeelding", use_container_width=True)

run_btn = st.button("🔍  Classificeer product", use_container_width=True)

# ── Helpers ───────────────────────────────────────────────────────────────────
def file_to_b64(f):
    return base64.b64encode(f.read()).decode("utf-8")

def extract_json(text: str) -> dict | None:
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

def verdict_html(outcome: str, code: str, taric: str, manual: bool, issues: list) -> str:
    if "NOT VALIDATED" in outcome:
        css = "verdict-invalid"
        icon = "✗"
        label = "Niet gevalideerd"
    elif "PARTIAL" in outcome:
        css = "verdict-partial"
        icon = "~"
        label = "Gedeeltelijk gevalideerd"
    else:
        css = "verdict-validated"
        icon = "✓"
        label = "Gevalideerd"

    code_str = code or "—"
    if taric and taric != code:
        code_str += f" / {taric}"

    issues_str = ""
    if issues:
        issues_str = "<br><small style='color:#aaa'>Issues: " + "; ".join(issues) + "</small>"

    manual_str = "<br><small style='color:#f0a030'>⚠ Manuele review aanbevolen</small>" if manual else ""

    return f"""
    <div class='{css}'>
        <div style='font-size:0.8rem;font-weight:600;letter-spacing:0.06em;
                    text-transform:uppercase;margin-bottom:0.5rem;'>{icon} {label}</div>
        <div class='cn-code'>{code_str}</div>
        {manual_str}{issues_str}
    </div>
    """

# ── Pipeline ──────────────────────────────────────────────────────────────────
if run_btn:
    if not description and not img_file and not inv_file:
        st.warning("Geef minimaal een productomschrijving of upload een afbeelding.")
        st.stop()

    if not st.session_state.username.strip():
        st.warning("Vul eerst je naam/initialen in de sidebar in.")
        st.stop()

    st.divider()
    st.markdown("### Pipeline resultaten")

    # ── STAP 1 ────────────────────────────────────────────────────────────────
    with st.status("**Stap 1** — Feature extractie…", expanded=True) as s1:
        user_content = []
        for f, mime_base in [(img_file, "image"), (inv_file, "image")]:
            if f:
                f.seek(0)
                b64 = file_to_b64(f)
                mime = f.type if f.type else "image/jpeg"
                user_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64}
                })
        txt = ""
        if description:
            txt += f"Product description / invoice text:\n{description}\n\n"
        if specs:
            txt += f"Technical specifications:\n{specs}"
        if txt:
            user_content.append({"type": "text", "text": txt.strip()})

        raw1 = call_claude(PROMPT1, user_content)
        json1 = extract_json(raw1)

        if json1:
            c1, c2, c3 = st.columns(3)
            c1.metric("Product", json1.get("product_identification","—")[:40])
            c2.metric("Categorie", json1.get("category_hint","—"))
            c3.metric("Datakwaliteit", json1.get("data_quality","—"))
            with st.expander("Volledige extractie JSON"):
                st.json(json1)
        else:
            st.text(raw1)
        s1.update(label="**Stap 1** — Feature extractie ✓", state="complete")

    # ── STAP 2 ────────────────────────────────────────────────────────────────
    with st.status("**Stap 2** — CN/TARIC classificatie…", expanded=True) as s2:
        step2_input = "Structured product data from feature extractor:\n\n" + (
            json.dumps(json1, indent=2) if json1 else raw1
        )
        raw2 = call_claude(PROMPT2, step2_input)
        json2 = extract_json(raw2)

        if json2:
            c1, c2, c3 = st.columns(3)
            c1.metric("CN code", json2.get("cn_code","—"))
            c2.metric("TARIC code", json2.get("taric_code","—"))
            c3.metric("Confidence", json2.get("confidence","—"))
            if json2.get("warnings"):
                st.warning("Warnings: " + "; ".join(json2["warnings"]))
            with st.expander("Volledige classificatie redenering"):
                st.markdown(raw2)
        else:
            st.text(raw2)
        s2.update(label="**Stap 2** — CN/TARIC classificatie ✓", state="complete")

    # ── STAP 3 ────────────────────────────────────────────────────────────────
    with st.status("**Stap 3** — Validatie…", expanded=True) as s3:
        step3_input = (
            f"Product data:\n{json.dumps(json1, indent=2) if json1 else description}\n\n"
            f"Proposed classification:\n{json.dumps(json2, indent=2) if json2 else raw2}\n\n"
            f"Full reasoning:\n{raw2}"
        )
        raw3 = call_claude(PROMPT3, step3_input)
        json3 = extract_json(raw3)

        with st.expander("Volledige validatie redenering"):
            st.markdown(raw3)
        s3.update(label="**Stap 3** — Validatie ✓", state="complete")

    # ── EINDVONNIS ────────────────────────────────────────────────────────────
    outcome  = json3.get("validation_outcome","UNKNOWN") if json3 else "UNKNOWN"
    code     = (json3 or {}).get("validated_code","") or (json2 or {}).get("cn_code","")
    taric    = (json3 or {}).get("taric_code","") or (json2 or {}).get("taric_code","")
    manual   = bool((json3 or {}).get("manual_review_recommended") or
                    (json2 or {}).get("manual_review_recommended"))
    issues   = (json3 or {}).get("issues",[])

    st.markdown(verdict_html(outcome, code, taric, manual, issues), unsafe_allow_html=True)

    # ── LOG TO GOOGLE SHEETS ──────────────────────────────────────────────────
    row = {
        "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user":         st.session_state.username,
        "description":  description[:200] if description else "",
        "specs":        specs[:200] if specs else "",
        "has_image":    "ja" if img_file else "nee",
        "has_invoice":  "ja" if inv_file else "nee",
        "product_id":   (json1 or {}).get("product_identification",""),
        "category":     (json1 or {}).get("category_hint",""),
        "data_quality": (json1 or {}).get("data_quality",""),
        "cn_code":      (json2 or {}).get("cn_code",""),
        "taric_code":   (json2 or {}).get("taric_code",""),
        "confidence":   (json2 or {}).get("confidence",""),
        "outcome":      outcome,
        "validated_code": code,
        "manual_review":  "ja" if manual else "nee",
        "issues":       "; ".join(issues),
        "raw_step1":    json.dumps(json1) if json1 else raw1[:500],
        "raw_step2":    raw2[:500],
        "raw_step3":    raw3[:500],
    }

    try:
        log_to_sheets(row, st.secrets["GOOGLE_SHEETS_ID"],
                      st.secrets["GOOGLE_SERVICE_ACCOUNT"])
        st.success("✓ Opgeslagen in Google Sheets")
    except Exception as e:
        st.warning(f"Sheets logging mislukt: {e}")

    # ── Sla op in session history ─────────────────────────────────────────────
    st.session_state.history.append({
        "timestamp": datetime.now().strftime("%H:%M"),
        "cn_code":   code,
        "outcome":   outcome,
    })
