import streamlit as st
import json
import base64
import uuid
from datetime import datetime
from anthropic import Anthropic
from utils.sheets import (log_to_sheets, get_pending_reviews, get_all_history,
                           save_senior_review, lookup_verified)
from utils.prompts import PROMPT1, PROMPT2, PROMPT3

st.set_page_config(page_title="DKM Classifier", page_icon="🔍", layout="wide")

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
    .badge-pending  { background:#3d2e0a; color:#f0a030; border-radius:4px;
        padding:2px 8px; font-size:0.75rem; font-weight:600; }
    .badge-verified { background:#0a2a3d; color:#4ab0f0; border-radius:4px;
        padding:2px 8px; font-size:0.75rem; font-weight:600; }
    .badge-rejected { background:#3d0f0f; color:#f04a4a; border-radius:4px;
        padding:2px 8px; font-size:0.75rem; font-weight:600; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
for key, default in [("username",""), ("history",[]), ("page","classify")]:
    if key not in st.session_state:
        st.session_state[key] = default

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
    if st.button("🔍  Classify product",    use_container_width=True):
        st.session_state.page = "classify"
    if st.button("📋  Senior review",        use_container_width=True):
        st.session_state.page = "review"
    if st.button("📊  History & analytics",  use_container_width=True):
        st.session_state.page = "history"

    st.divider()
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

def verdict_html(outcome, code, taric, manual, issues, verified_by=None):
    if verified_by:
        css, icon, label = "verdict-verified", "✓✓", f"Verified by previous inquiry ({verified_by})"
    elif "NOT VALIDATED" in outcome:
        css, icon, label = "verdict-invalid",   "✗",  "Not validated"
    elif "PARTIAL" in outcome:
        css, icon, label = "verdict-partial",   "~",  "Partially validated"
    else:
        css, icon, label = "verdict-validated", "✓",  "Validated"
    code_str   = code or "—"
    if taric and taric != code:
        code_str += f" / {taric}"
    issues_str = ("<br><small style='color:#aaa'>Issues: " + "; ".join(issues) + "</small>") if issues else ""
    manual_str = "" if verified_by else ("<br><small style='color:#f0a030'>⚠ Manual review recommended</small>" if manual else "")
    return f"""<div class='{css}'>
        <div style='font-size:0.8rem;font-weight:600;letter-spacing:0.06em;
                    text-transform:uppercase;margin-bottom:0.5rem;'>{icon} {label}</div>
        <div class='cn-code'>{code_str}</div>{manual_str}{issues_str}</div>"""

def build_decision_tree(description, specs, json1, json2, json3, raw2) -> str:
    lines = ["CLASSIFICATION DECISION TREE", "=" * 60]
    lines.append("\n▸ INPUT")
    lines.append(f"  Description : {(description or '—')[:120]}")
    if specs:
        lines.append(f"  Specs       : {specs[:120]}")
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

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: CLASSIFY
# ═══════════════════════════════════════════════════════════════════════════════
if st.session_state.page == "classify":
    st.markdown("## CN/TARIC Classification Tool")
    st.markdown("<span style='color:#888;font-size:0.85rem;'>Powered by DKM Classification Engine · 3-step AI pipeline</span>", unsafe_allow_html=True)
    st.divider()
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

    if run_btn:
        if not description and not img_file and not inv_file:
            st.warning("Please provide at least a product description or upload an image.")
            st.stop()
        if not st.session_state.username.strip():
            st.warning("Please enter your name or initials in the sidebar first.")
            st.stop()

        # ── Check verified lookup ──────────────────────────────────────────────
        verified_match = None
        if description:
            try:
                sid, sac = get_secrets()
                verified_match = lookup_verified(description, sid, sac)
            except Exception:
                pass

        st.divider()
        st.markdown("### Pipeline results")

        if verified_match:
            st.info(f"ℹ️  Similar product found in verified lookup — running full pipeline for confirmation.")

        # ── STEP 1 ────────────────────────────────────────────────────────────
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

        # ── STEP 2 ────────────────────────────────────────────────────────────
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

        # ── STEP 3 ────────────────────────────────────────────────────────────
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

        # ── FINAL VERDICT ──────────────────────────────────────────────────────
        outcome = json3.get("validation_outcome","UNKNOWN") if json3 else "UNKNOWN"
        code    = (json3 or {}).get("validated_code","") or (json2 or {}).get("cn_code","")
        taric   = (json3 or {}).get("taric_code","")     or (json2 or {}).get("taric_code","")
        manual  = bool((json3 or {}).get("manual_review_recommended") or
                       (json2 or {}).get("manual_review_recommended"))
        issues  = (json3 or {}).get("issues",[])

        verified_by = None
        if verified_match and verified_match.get("cn_code") == code:
            verified_by = f"{verified_match.get('senior_user','senior')} on {verified_match.get('senior_timestamp','')[:10]}"

        st.markdown(verdict_html(outcome, code, taric, manual, issues, verified_by), unsafe_allow_html=True)

        # ── DECISION TREE ──────────────────────────────────────────────────────
        decision_tree = build_decision_tree(description, specs, json1, json2, json3, raw2)
        with st.expander("📋  Decision tree / audit trail", expanded=False):
            st.markdown(f"<div class='tree-box'>{decision_tree}</div>", unsafe_allow_html=True)

        # ── LOG TO SHEETS ──────────────────────────────────────────────────────
        row_id = str(uuid.uuid4())[:8]
        row = {
            "timestamp":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "user":           st.session_state.username,
            "description":    description[:200] if description else "",
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
        }
        try:
            sid, sac = get_secrets()
            log_to_sheets(row, sid, sac)
            st.success("✓ Saved to Google Sheets")
        except Exception as e:
            import traceback
            st.warning(f"Sheets logging failed: {type(e).__name__}: {e}")
            st.code(traceback.format_exc(), language="text")

        st.session_state.history.append({"timestamp": datetime.now().strftime("%H:%M"),
                                          "cn_code": code, "outcome": outcome})

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: SENIOR REVIEW
# ═══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "review":
    st.markdown("## Senior Review")
    st.markdown("<span style='color:#888;font-size:0.85rem;'>Review and confirm or reject AI classifications</span>", unsafe_allow_html=True)
    st.divider()

    if not st.session_state.username.strip():
        st.warning("Please enter your name or initials in the sidebar first.")
        st.stop()

    try:
        sid, sac  = get_secrets()
        pending   = get_pending_reviews(sid, sac)
    except Exception as e:
        st.error(f"Could not load reviews: {e}")
        st.stop()

    if not pending:
        st.success("✓ All classifications have been reviewed.")
        st.stop()

    st.markdown(f"**{len(pending)} classification(s) pending review**")
    st.divider()

    for rec in pending:
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

        outcome_color = "#4a9e4a" if "VALIDATED" in outcome and "NOT" not in outcome else (
                        "#c8880a" if "PARTIAL" in outcome else "#c84a4a")

        with st.container():
            st.markdown(f"<div class='review-card'>", unsafe_allow_html=True)
            c1, c2, c3, c4 = st.columns([3,2,2,2])
            c1.markdown(f"**{desc[:80]}{'...' if len(desc)>80 else ''}**  \n<span style='color:#888;font-size:0.78rem'>{ts} · by {user}</span>", unsafe_allow_html=True)
            c2.metric("CN code",    cn)
            c3.metric("TARIC code", taric)
            c4.metric("Confidence", conf)

            if issues:
                st.warning(f"Issues: {issues}")

            with st.expander(f"📋 Decision tree — {row_id}"):
                st.markdown(f"<div class='tree-box'>{tree}</div>", unsafe_allow_html=True)

            col_v, col_c = st.columns([1,3])
            with col_v:
                verdict = st.selectbox("Verdict", ["CONFIRMED", "REJECTED", "NEEDS_MORE_INFO"],
                                       key=f"verdict_{row_id}")
            with col_c:
                comment = st.text_input("Comment (optional — shown on future matches)",
                                        key=f"comment_{row_id}",
                                        placeholder="e.g. Confirmed after checking TARIC chapter note 3(b)")

            if st.button(f"✔  Submit review", key=f"submit_{row_id}"):
                try:
                    save_senior_review(
                        row_id       = row_id,
                        verdict      = verdict,
                        comment      = comment,
                        senior_user  = st.session_state.username,
                        cn_code      = cn,
                        taric_code   = taric,
                        description  = desc,
                        spreadsheet_id      = sid,
                        service_account_info= sac,
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

    # ── Summary metrics ────────────────────────────────────────────────────────
    total     = len(df)
    validated = len(df[df["outcome"].str.contains("VALIDATED",na=False) & ~df["outcome"].str.contains("NOT",na=False)])
    partial   = len(df[df["outcome"].str.contains("PARTIAL",na=False)])
    rejected  = len(df[df["outcome"].str.contains("NOT VALIDATED",na=False)])
    reviewed  = len(df[df["senior_reviewed"].str.lower() == "yes"]) if "senior_reviewed" in df.columns else 0

    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Total",      total)
    c2.metric("Validated",  validated)
    c3.metric("Partial",    partial)
    c4.metric("Rejected",   rejected)
    c5.metric("Sr. reviewed", reviewed)

    st.divider()

    # ── Filters ────────────────────────────────────────────────────────────────
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

    # ── Table ──────────────────────────────────────────────────────────────────
    display_cols = ["timestamp","user","description","cn_code","taric_code",
                    "confidence","outcome","senior_reviewed","senior_verdict","senior_user"]
    available = [c for c in display_cols if c in filtered.columns]
    st.dataframe(filtered[available], use_container_width=True, height=400)
