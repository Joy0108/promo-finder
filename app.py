"""Promo Finder: editor-facing app.

    streamlit run app.py

Two modes: open a finished run (from outputs/ or an uploaded results .zip), or
upload a show's screenplays and run the pipeline with any model.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from promofinder.llm import PROVIDERS, LLMConfig, LLMError
from promofinder.parse import load_show
from promofinder.pipeline import run as run_pipeline

load_dotenv()
ROOT = Path(__file__).parent
OUTPUTS = ROOT / "outputs"
TIER_COLORS = {"Post first": "#d4553a", "Next up": "#e0a030", "Backlog": "#7a8ca8", "Alternate": "#b8bcc4"}

st.set_page_config(page_title="Promo Finder", page_icon="🎬", layout="wide")


def secret(name: str) -> str | None:
    try:
        return st.secrets.get(name) or os.getenv(name)
    except Exception:  # no secrets file
        return os.getenv(name)


# ---------------------------------------------------------------- loading runs
def find_runs() -> list[Path]:
    default = os.getenv("LLM_MODEL", "gpt-4.1")
    runs = [p.parent for p in OUTPUTS.glob("*/run.json")]
    # the configured default model first, then most recent
    return sorted(runs, key=lambda p: (not p.name.endswith(default.replace(".", "-")), -(p / "run.json").stat().st_mtime))


@st.cache_data(show_spinner=False)
def load_run(path: str) -> dict:
    p = Path(path)
    read = lambda name, default=None: json.loads((p / name).read_text(encoding="utf-8")) if (p / name).exists() else default
    return {
        "dir": str(p), "run": read("run.json", {}), "bible": read("bible.json", {}),
        "ranked": read("promos_ranked.json", []), "plan": read("posting_plan.json", {}),
        "quality": (p / "quality.md").read_text(encoding="utf-8") if (p / "quality.md").exists() else "",
        "report": (p / "report.md").read_text(encoding="utf-8") if (p / "report.md").exists() else "",
        "csv": (p / "promos_ranked.csv").read_bytes() if (p / "promos_ranked.csv").exists() else b"",
        "edl": (p / "promo_cuts.edl").read_text(encoding="ascii") if (p / "promo_cuts.edl").exists() else "",
        "writer": read("writer_intent_check.json", {}),
        "beats": [json.loads(f.read_text(encoding="utf-8")) for f in sorted((p / "beats").glob("E*.json"))]
        if (p / "beats").exists() else [],
    }


def extract_zip(data: bytes, dest: Path, suffixes: tuple[str, ...], keep_folders: bool = False) -> None:
    """Extract only wanted files. Folder structure is kept only when asked, and every
    path is checked to stay inside `dest` (no path traversal)."""
    root = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            if info.is_dir() or not name.lower().endswith(suffixes) or name.startswith("."):
                continue
            target = (dest / info.filename) if keep_folders else (dest / name)
            target = target.resolve()
            if root not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))


# ---------------------------------------------------------------- sidebar
st.sidebar.title("🎬 Promo Finder")
st.sidebar.caption("Finds ranked, ready-to-cut promo clips in microdrama screenplays.")
mode = st.sidebar.radio("Mode", ["Open results", "Run on new screenplays"], label_visibility="collapsed")

run_dir: str | None = None

if mode == "Open results":
    runs = find_runs()
    choice = None
    if runs:
        labels = {f"{json.loads((r / 'run.json').read_text(encoding='utf-8')).get('show', r.name)} · "
                  f"{json.loads((r / 'run.json').read_text(encoding='utf-8')).get('model', '')}": r for r in runs}
        choice = st.sidebar.selectbox("Finished runs", list(labels))
        run_dir = str(labels[choice])
    up = st.sidebar.file_uploader("…or open a results .zip", type=["zip"],
                                  help="The raw-outputs zip works as is, even with several runs inside.")
    if up is not None:
        if st.session_state.get("zip_name") != up.name:
            tmp = Path(tempfile.mkdtemp(prefix="promo_results_"))
            extract_zip(up.getvalue(), tmp, (".json", ".md", ".csv", ".edl"), keep_folders=True)
            st.session_state["zip_name"], st.session_state["zip_dir"] = up.name, str(tmp)
        found = sorted(p.parent for p in Path(st.session_state["zip_dir"]).rglob("promos_ranked.json"))
        if not found:
            st.sidebar.error("That zip has no promos_ranked.json.")
        else:
            pick = st.sidebar.selectbox("Run in the zip", found, format_func=lambda p: p.name) if len(found) > 1 else found[0]
            run_dir = str(pick)
    if not run_dir:
        st.title("🎬 Promo Finder")
        st.markdown(
            "Reads a microdrama's episode scripts and returns **ranked, ready-to-cut promos** (20–60s). For each one "
            "you get the exact start and end of every piece, the hook in the first 3 seconds, the cliffhanger it cuts "
            "on, why it works, a cold viewer's read of it, and a cut list that opens in Premiere or DaVinci.\n\n"
            "This hosted copy has no show content in it, because the scripts are confidential. Two ways to try it:\n\n"
            "1. **See the results:** upload `raw_outputs.zip` (from the submission's raw outputs link) in the sidebar.\n"
            "2. **Run it on a show:** choose **Run on new screenplays** in the sidebar, upload the episode `.txt` files "
            "(or a `.zip` of them), pick a model, and press **Find promos**."
        )
        st.stop()

else:
    st.sidebar.subheader("1 · Screenplays")
    files = st.sidebar.file_uploader("Episode .txt files, or one .zip of them", type=["txt", "zip"],
                                     accept_multiple_files=True)
    title = st.sidebar.text_input("Show title", value="")
    st.sidebar.subheader("2 · Model")
    provider = st.sidebar.selectbox("Provider", list(PROVIDERS), index=list(PROVIDERS).index(os.getenv("LLM_PROVIDER", "openai")))
    model = st.sidebar.text_input("Model", value=os.getenv("LLM_MODEL", "gpt-4.1"))
    base_url = st.sidebar.text_input("Endpoint override (optional)", value="",
                                     help="Any OpenAI-compatible endpoint, e.g. one Kuku provides.")
    key_env = PROVIDERS[provider]["key_env"]
    has_server_key = bool(key_env and secret(key_env))
    api_key = st.sidebar.text_input("API key", type="password",
                                    help="Leave empty to use the server's key (password protected).")
    gate = secret("APP_PASSWORD")
    password = st.sidebar.text_input("App password (for the server key)", type="password") if (has_server_key and gate and not api_key) else ""
    st.sidebar.subheader("3 · Settings")
    critic_k = st.sidebar.slider("Clips sent to the cold-viewer test", 10, 80, 40, 5)
    top_n = st.sidebar.slider("Size of the ranked list", 5, 40, 20, 5)
    go = st.sidebar.button("Find promos", type="primary", use_container_width=True)

    if go:
        if not files:
            st.sidebar.error("Upload the screenplays first.")
            st.stop()
        key = api_key or None
        if not key and has_server_key:
            if gate and password != gate:
                st.sidebar.error("Wrong app password. Paste your own API key instead.")
                st.stop()
            key = secret(key_env)
        work = Path(tempfile.mkdtemp(prefix="promo_show_"))
        for f in files:
            if f.name.lower().endswith(".zip"):
                extract_zip(f.getvalue(), work, (".txt",))
            else:
                (work / Path(f.name).name).write_bytes(f.getvalue())
        try:
            eps = load_show(work)
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not read the screenplays: {e}")
            st.stop()
        st.write(f"Read **{len(eps)} episodes** ({sum(e.duration for e in eps) / 60:.1f} min). Running `{provider}/{model}`…")
        bar = st.progress(0.0, text="starting")
        weights = {"parsed": 0.02, "bible": 0.05, "moments": 0.55, "cross": 0.6, "cold-viewer": 0.95, "ranked": 0.98, "written": 1.0}
        prev = {"parsed": 0, "bible": 0.02, "moments": 0.05, "cross": 0.55, "cold-viewer": 0.6, "ranked": 0.95, "written": 0.98}

        def progress(stage: str, done: int, total: int) -> None:
            lo, hi = prev.get(stage, 0), weights.get(stage, 1)
            bar.progress(min(1.0, lo + (hi - lo) * done / max(1, total)), text=f"{stage} {done}/{total}")

        try:
            cfg = LLMConfig.from_env(provider, model, key, base_url or None)
            cfg.cache_dir = str(ROOT / ".cache" / "llm")
            res = run_pipeline(work, OUTPUTS, cfg, title=title or None, critic_k=critic_k, top_n=top_n, progress=progress)
        except LLMError as e:
            st.error(f"Model error: {e}")
            st.stop()
        st.session_state["last_run"] = res["out_dir"]
        load_run.clear()
    run_dir = st.session_state.get("last_run")
    if not run_dir:
        st.markdown("### Find promos in a new show")
        st.markdown(
            "1. Upload the episode screenplays (Kuku's `.txt` export, one file per episode, or a `.zip`).\n"
            "2. Pick any provider and model, or paste an endpoint Kuku provides.\n"
            "3. Press **Find promos**. A 50-episode show takes about 3 to 5 minutes."
        )
        st.stop()

# ---------------------------------------------------------------- results view
R = load_run(run_dir)
info, bible, ranked = R["run"], R["bible"], R["ranked"]
main = [r for r in ranked if r["tier"] != "Alternate"]

st.title(bible.get("title") or info.get("show", "Promo picks"))
st.caption(f"**Premise:** {bible.get('premise', '')}  \n**The title promises:** {bible.get('title_promise', '')}")
c = st.columns(5)
c[0].metric("Episodes", info.get("episodes"))
c[1].metric("Show runtime", f"{info.get('runtime_min')} min")
c[2].metric("Valid clips found", info.get("candidates_valid"))
c[3].metric("Ranked promos", len(main))
cost = info.get("cost_usd")
c[4].metric("Cost per full run", f"${cost:.2f}" if cost is not None else "n/a",
            help=f"{info.get('provider')}/{info.get('model')} · {info.get('cost_display', '')}")

tab_rank, tab_map, tab_time, tab_quality, tab_files = st.tabs(
    ["Ranked promos", "Show map", "Timeline", "Quality checks", "Downloads"])

reasons = {p.get("promo_id"): p.get("reason", "") for p in R["plan"].get("why_post", [])}
fb_path = Path(run_dir) / "feedback.json"
feedback = json.loads(fb_path.read_text(encoding="utf-8")) if fb_path.exists() else {}

with tab_rank:
    tiers = st.multiselect("Show tiers", ["Post first", "Next up", "Backlog", "Alternate"], default=["Post first", "Next up", "Backlog"])
    for r in [x for x in ranked if x["tier"] in tiers]:
        segs = " + ".join(f"E{s['episode']} {s['start']}–{s['end']}" for s in r["segments"])
        head = f"#{r['rank']} · {r['tier']} · {r['score']} · {segs} · {r['duration_s']:.0f}s"
        with st.expander(head, expanded=r["rank"] <= 3):
            left, right = st.columns([3, 2])
            with left:
                st.markdown(f"**Hook** (E{r['hook']['episode']} {r['hook']['at']} · {r['hook']['type']}"
                            f"{' · visual' if r['hook']['visual'] else ''}): {r['hook']['quote']}  \n_{r['hook']['what_happens']}_")
                st.markdown(f"**Cliffhanger** (E{r['cliffhanger']['episode']} {r['cliffhanger']['at']} · {r['cliffhanger']['type']}): "
                            f"{r['cliffhanger']['quote']}  \n_{r['cliffhanger']['open_question']}_")
                st.markdown(f"**Why it works:** {r['why_it_works']}")
                if r["id"] in reasons:
                    st.markdown(f"**Why post it here:** {reasons[r['id']]}")
                if r.get("stitch_story"):
                    st.markdown(f"**Stitch:** {r['stitch_story']}")
                seg_df = pd.DataFrame([{"Part": i + 1, "Episode": s["episode"], "Start": s["start"], "End": s["end"],
                                        "Seconds": s["length_s"]} for i, s in enumerate(r["segments"])])
                st.dataframe(seg_df, hide_index=True, use_container_width=True)
            with right:
                s, v = r["scores"], r["cold_viewer"]
                m = st.columns(3)
                m[0].metric("Hook", s["hook"])
                m[1].metric("Cliffhanger", s["cliffhanger"])
                m[2].metric("Stands alone", s["standalone"])
                st.markdown(f"**Cold viewer understood:** {v['what_i_understood']}")
                if v["confusing"]:
                    st.markdown("**Confused by:** " + "; ".join(v["confusing"]))
                if s.get("penalties"):
                    st.caption("Penalties: " + ", ".join(f"{k} −{val}" for k, val in s["penalties"].items()))
                prev_fb = feedback.get(r["id"], {})
                verdict = st.radio("Editor verdict", ["—", "Use", "Maybe", "Reject"], horizontal=True, key=f"v_{r['id']}",
                                   index=["—", "Use", "Maybe", "Reject"].index(prev_fb.get("verdict", "—")))
                note = st.text_input("Note", value=prev_fb.get("note", ""), key=f"n_{r['id']}")
                if verdict != prev_fb.get("verdict", "—") or note != prev_fb.get("note", ""):
                    feedback[r["id"]] = {"verdict": verdict, "note": note, "rank": r["rank"], "segments": segs}
                    try:
                        fb_path.write_text(json.dumps(feedback, indent=2), encoding="utf-8")
                    except OSError:
                        pass
            lines = r["script"].splitlines()
            st.code("\n".join(lines), language=None)

with tab_map:
    st.caption("Where the show peaks: the strongest hook and cliffhanger the model found in each episode (out of 10). "
               "Dots mark episodes that supply a ranked promo.")
    if R["beats"]:
        rows = []
        used = {s["episode"] for r in main for s in r["segments"]}
        for b in R["beats"]:
            for kind, items in (("Best hook", b.get("hooks", [])), ("Best cliffhanger", b.get("cliffhangers", []))):
                top = max(items, key=lambda x: x["score"], default=None)
                rows.append({"episode": b["ep"], "series": kind, "score": top["score"] if top else 0,
                             "moment": (top or {}).get("quote", "")[:90], "type": (top or {}).get("type", "")})
        df = pd.DataFrame(rows)
        line = alt.Chart(df).mark_line(point=False, strokeWidth=2).encode(
            x=alt.X("episode:Q", title="Episode", scale=alt.Scale(nice=False)),
            y=alt.Y("score:Q", title="Strongest moment (model score)", scale=alt.Scale(domain=[4, 10])),
            color=alt.Color("series:N", scale=alt.Scale(range=["#d4553a", "#3a6fd4"]), title=None),
            tooltip=["episode", "series", "score", "type", "moment"])
        dots = alt.Chart(df[df["episode"].isin(used) & (df["series"] == "Best cliffhanger")]).mark_point(
            filled=True, size=70, color="#1b1d22").encode(x="episode:Q", y="score:Q", tooltip=["episode", "moment"])
        st.altair_chart((line + dots).properties(height=320), use_container_width=True)
        types = pd.DataFrame([{"type": h["type"]} for b in R["beats"] for h in b.get("hooks", [])])
        if not types.empty:
            st.markdown("**What kind of hooks this show has**")
            st.bar_chart(types["type"].value_counts(), horizontal=True, height=260)
    w = R["writer"]
    if w.get("writer_marked_moments"):
        st.info(f"The screenwriters marked **{w['writer_marked_moments']}** moments as a cliffhanger or cut to black. "
                f"The tool's cliffhangers land within {w['tolerance_s']:.0f}s of **{w['tool_cliffhanger_on_mark']}** of them, "
                f"and **{w['main_list_ending_on_mark']}/{w['main_list']}** ranked promos end on one.")

with tab_time:
    st.caption("Where the ranked promos sit in the show. Grey = episode length; coloured = promo segments.")
    rows = []
    for r in ranked:
        for i, s in enumerate(r["segments"]):
            rows.append({"episode": s["episode"], "start": s["start_s"], "end": s["end_s"], "tier": r["tier"],
                         "promo": f"#{r['rank']} {r['id']}", "part": i + 1, "score": r["score"],
                         "hook": r["hook"]["quote"][:80], "cliffhanger": r["cliffhanger"]["quote"][:80]})
    if rows:
        df = pd.DataFrame(rows)
        df = df[df["tier"].isin(["Post first", "Next up", "Backlog"])]
        durations = {int(k): v for k, v in (info.get("episode_durations") or {}).items()}
        n_eps = info.get("episode_range", [1, int(df["episode"].max())])
        base = pd.DataFrame({"episode": range(n_eps[0], n_eps[1] + 1)})
        base["start"] = 0
        base["end"] = [durations.get(e, 200) for e in base["episode"]]
        grey = alt.Chart(base).mark_bar(color="#d9dbe1", opacity=0.6, height=8).encode(
            x=alt.X("start:Q", title="seconds into episode"), x2="end:Q", y=alt.Y("episode:O", title="Episode"))
        bars = alt.Chart(df).mark_bar(height=10, cornerRadius=2).encode(
            x="start:Q", x2="end:Q", y="episode:O",
            color=alt.Color("tier:N", scale=alt.Scale(domain=list(TIER_COLORS)[:3], range=list(TIER_COLORS.values())[:3])),
            tooltip=["promo", "part", "score", "hook", "cliffhanger", "start", "end"])
        st.altair_chart((grey + bars).properties(height=max(300, 14 * len(base))), use_container_width=True)

with tab_quality:
    st.markdown(R["quality"])

with tab_files:
    st.download_button("Ranked promos (CSV)", R["csv"], file_name="promos_ranked.csv", mime="text/csv")
    st.download_button("Ranked promos (JSON)", json.dumps(ranked, indent=2, ensure_ascii=False),
                       file_name="promos_ranked.json", mime="application/json")
    st.download_button("Editor report (Markdown)", R["report"], file_name="report.md", mime="text/markdown")
    if R["edl"]:
        st.download_button("Cut list for Premiere / Resolve (EDL)", R["edl"], file_name="promo_cuts.edl",
                           mime="text/plain", help="Import, relink reel E031 to episode 31's video, and every promo is pre-cut.")
    if feedback:
        st.download_button("Editor verdicts (JSON)", json.dumps(feedback, indent=2), file_name="feedback.json",
                           mime="application/json")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in Path(run_dir).rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(run_dir))
    st.download_button("Whole results folder (.zip)", buf.getvalue(), file_name=f"{Path(run_dir).name}.zip",
                       mime="application/zip")
