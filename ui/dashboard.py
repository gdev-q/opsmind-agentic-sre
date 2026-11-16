"""Streamlit dashboard: paste a log, run triage, inspect agent reasoning."""

import os
from pathlib import Path
from typing import Any

import requests
import streamlit as st

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
SAMPLES = Path(__file__).resolve().parent.parent / "sample_logs"
SEV_RENDER = {"P1": st.error, "P2": st.warning, "P3": st.info}

st.set_page_config(page_title="OpsMind AI", layout="wide")


def call(method: str, path: str, timeout: int = 15, **kw: Any) -> tuple[bool, Any]:
    try:
        r = requests.request(method, f"{BACKEND}{path}", timeout=timeout, **kw)
        if r.ok:
            return True, r.json()
        try:
            return False, r.json().get("detail", r.text)
        except ValueError:
            return False, r.text
    except requests.RequestException as e:
        return False, f"Backend unreachable: {e}"


def load_sample() -> None:
    choice = st.session_state.sample
    st.session_state.log = (
        (SAMPLES / choice).read_text(encoding="utf-8") if choice != "(paste your own)" else ""
    )


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("OpsMind AI")
    st.caption("Agentic SRE triage & runbook assistant")
    ok, health = call("GET", "/health")
    if ok:
        st.success(f"API up · search backend: {health['backend']}")
        st.caption(
            f"Elasticsearch: {'online' if health['es_available'] else 'offline (FAISS fallback)'} · "
            f"chunks: {health['chunks_indexed']} · "
            f"LLM key: {'set' if health['llm_configured'] else 'MISSING'}"
        )
    else:
        st.error(health)
    if st.button("Re-index runbooks", use_container_width=True):
        with st.spinner("Embedding and indexing..."):
            ok, res = call("POST", "/ingest", timeout=180)
        if ok:
            st.success(f"{res['files']} files, {res['chunks']} chunks -> {res['backend']}")
        else:
            st.error(res)

# --------------------------------------------------------------------- main
st.header("Incident triage")
samples = ["(paste your own)"] + (sorted(p.name for p in SAMPLES.glob("*.log")) if SAMPLES.exists() else [])
st.selectbox("Load a sample log", samples, key="sample", on_change=load_sample)
st.text_area("Raw error log / stack trace", key="log", height=260)

if st.button("Run triage", type="primary", disabled=not st.session_state.get("log", "").strip()):
    with st.spinner("Agents working: triage -> retrieval -> remediation plan..."):
        ok, res = call("POST", "/triage", timeout=180, json={"log": st.session_state.log})
    st.session_state.result = res if ok else None
    if not ok:
        st.error(res)

res = st.session_state.get("result")
if res:
    t, plan = res["triage"], res["plan"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Service", t["affected_service"])
    c2.metric("Error type", t["error_type"])
    c3.metric("Severity", t["severity"])
    c4.metric("Search backend", res["retrieval_backend"])
    SEV_RENDER[t["severity"]](f"Severity {t['severity']}: {t['error_type']}")

    st.subheader("Root cause hypothesis")
    if not plan["runbook_found"]:
        st.warning("No matching runbook found. The hypothesis below is not runbook-grounded.")
    st.markdown(plan["root_cause_hypothesis"])

    st.subheader("Diagnostic & mitigation steps")
    for i, step in enumerate(plan["steps"], 1):
        st.markdown(f"**{i}. {step['description']}**")
        st.code(step["command"], language="bash")
    if not plan["steps"]:
        st.caption("No runbook-backed steps available.")

    left, right = st.columns(2)
    left.subheader("Escalation")
    left.markdown(plan["escalation"])
    right.subheader("Rollback")
    right.markdown(plan["rollback"])

    st.subheader("Citations")
    st.markdown(", ".join(f"`{c}`" for c in plan["citations"]) or "None")

    with st.expander("Agent reasoning trace", expanded=True):
        for line in res["trace"]:
            st.code(line, language="text")
    with st.expander("Retrieved runbook chunks"):
        for c in res["retrieved"]:
            st.markdown(f"**{c['source']}** · {c['heading']} · score `{c['score']}`")
            st.text(c["content"][:1200])
