"""Writes the raw-outputs folder: machine-readable results, an editor's report
and a quality report. Nothing here is hand-edited after a run."""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

from . import writer_check
from .critic import render_excerpt
from .parse import Episode, fmt


def clean_quote(text: str) -> str:
    """Display only: drop screenplay block labels the model sometimes copies into a quote."""
    return re.sub(r"^(CAMERA|ACTION|SFX|SOUND FX|SOUND)\s*:\s*", "", str(text).strip())


def seg_str(segs: list[dict]) -> str:
    return " + ".join(f"E{s['ep']:03d} {fmt(s['start'])}-{fmt(s['end'])}" for s in segs)


def promo_record(c: dict, eps: dict[int, Episode]) -> dict:
    h, cl = c["hook"], c["cliff"]
    peak = eps[h["ep"]].blocks[h["peak"]]
    cut = eps[cl["ep"]].blocks[cl["cut"]]
    rec = {
        "rank": c.get("rank"), "tier": c.get("tier"), "id": c["id"],
        "score": c.get("final", {}).get("score"), "kind": c["kind"], "duration_s": c["duration"],
        "segments": [
            {"episode": s["ep"], "start": fmt(s["start"]), "end": fmt(s["end"]),
             "start_s": s["start"], "end_s": s["end"], "length_s": round(s["end"] - s["start"], 2)}
            for s in c["segments"]
        ],
        "hook": {
            "id": h["id"], "episode": h["ep"], "at": fmt(peak.start), "type": h["type"], "quote": clean_quote(h["quote"]),
            "what_happens": h["what_happens"], "editor_score": h["score"],
            "needs_context": h["needs_context"], "on_premise": h["on_premise"], "recap": h["recap"],
            "visual": not peak.is_dialogue,
        },
        "cliffhanger": {
            "id": cl["id"], "episode": cl["ep"], "at": fmt(cut.end), "type": cl["type"], "quote": clean_quote(cl["quote"]),
            "open_question": cl["open_question"], "editor_score": cl["score"],
            "needs_context": cl["needs_context"], "on_premise": cl["on_premise"],
        },
        "why_it_works": c.get("viewer", {}).get("why_it_works", ""),
        "setup_included": c["setup_included"],
        "scores": c.get("final"),
        "cold_viewer": c.get("viewer"),
        "checks": c.get("checks"),
        "script": render_excerpt(c, eps),
    }
    if c.get("story"):
        rec["stitch_story"] = c["story"]
    if c.get("alternate_because"):
        rec["alternate_because"] = c["alternate_because"]
    return rec


def write_outputs(out: Path, ctx: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    eps: dict[int, Episode] = ctx["eps"]
    ranked = [promo_record(c, eps) for c in ctx["ranked"]]

    _dump(out / "run.json", ctx["run"])
    _dump(out / "bible.json", ctx["bible"])
    beats_dir = out / "beats"
    beats_dir.mkdir(exist_ok=True)
    for ep_num, bt in ctx["beats"].items():
        _dump(beats_dir / f"E{ep_num:03d}.json", bt)
    _dump(out / "cross_episode_pairs.json", ctx["cross"])
    _dump(out / "candidates_all.json", [
        {k: v for k, v in c.items() if k not in ("hook", "cliff")} | {"segments_str": seg_str(c["segments"])}
        for c in ctx["all_candidates"]
    ])
    _dump(out / "promos_ranked.json", ranked)
    _dump(out / "posting_plan.json", ctx["plan"])
    _write_csv(out / "promos_ranked.csv", ranked)
    main = [r for r in ranked if r["tier"] != "Alternate"]
    ctx["writer"] = writer_check.check(eps, ctx["beats"], main)
    _dump(out / "writer_intent_check.json", ctx["writer"])
    write_edl(out / "promo_cuts.edl", main, ctx["bible"].get("title", ""))
    (out / "report.md").write_text(_report_md(ctx, ranked), encoding="utf-8")
    (out / "quality.md").write_text(_quality_md(ctx, ranked), encoding="utf-8")
    _dump(out / "results.json", build_results(ctx, ranked, out))


DOWNLOADS = ("report.md", "quality.md", "promos_ranked.csv", "promo_cuts.edl")


def build_results(ctx: dict, ranked: list[dict], out: Path) -> dict:
    """Everything the web app shows, in one file. Open it there to review a run
    again without any model calls (the command line writes the same file)."""
    bible = ctx["bible"]
    return {
        "format": "promo-finder-results/1",
        "run": ctx["run"],
        "bible": {k: bible.get(k) for k in ("title", "premise", "title_promise", "characters", "arcs")},
        "promos": ranked,
        "plan": ctx["plan"],
        "writer": ctx["writer"],
        "quality": quality_summary(ctx, ranked),
        "map": show_map(ctx),
        "files": {name: (out / name).read_text(encoding="utf-8-sig") for name in DOWNLOADS},
    }


def show_map(ctx: dict) -> list[dict]:
    """Per episode: where the hooks, cliffhangers and writers' cliffhanger marks are."""
    rows = []
    for e, ep in ctx["eps"].items():
        bt = ctx["beats"].get(e, {"hooks": [], "cliffhangers": []})
        rows.append({
            "ep": e, "duration": ep.duration, "logline": ep.logline,
            "hooks": [{"id": h["id"], "t": ep.blocks[h["peak"]].start, "score": h["score"], "type": h["type"],
                       "quote": clean_quote(h["quote"])} for h in bt["hooks"]],
            "cliffs": [{"id": c["id"], "t": ep.blocks[c["cut"]].end, "score": c["score"], "type": c["type"],
                        "quote": clean_quote(c["quote"])} for c in bt["cliffhangers"]],
            "writer_marks": writer_check.writer_moments(ep),
        })
    return rows


CHECK_WORDS = {
    "duration_20_60s": "20 to 60 seconds long",
    "hook_in_first_3s": "Hook lands in the first 3 seconds",
    "ends_on_cliffhanger": "Ends on the cliffhanger line",
    "answer_not_shown": "Doesn't show the answer",
    "no_cut_mid_line": "Never cuts inside a spoken line",
    "no_cut_mid_sentence": "Never cuts a sentence split across lines",
    "timestamps_from_script": "Every cut is a real time from the script",
    "within_episode": "Inside a real episode",
    "segments_in_story_order": "Pieces in story order",
}


def quality_summary(ctx: dict, ranked: list[dict]) -> dict:
    run = ctx["run"]
    main = [r for r in ranked if r["tier"] != "Alternate"]
    beats = ctx["beats"]
    cross = ctx.get("cross", {})

    def avg(key):
        vals = [r["cold_viewer"][key] for r in main]
        return round(sum(vals) / len(vals), 1) if vals else 0

    disagree = [{"id": r["id"], "rank": r["rank"],
                 "hook_editor": r["hook"]["editor_score"], "hook_viewer": r["cold_viewer"]["hook_score"],
                 "cliff_editor": r["cliffhanger"]["editor_score"], "cliff_viewer": r["cold_viewer"]["cliffhanger_score"]}
                for r in ranked if abs(r["hook"]["editor_score"] - r["cold_viewer"]["hook_score"]) >= 3
                or abs(r["cliffhanger"]["editor_score"] - r["cold_viewer"]["cliffhanger_score"]) >= 3]
    flags = []
    for r in main:
        why = []
        v = r["cold_viewer"]
        if v["standalone_score"] <= 5:
            why.append(f"stands alone only {v['standalone_score']:.0f}/10")
        if len(v["confusing"]) >= 5:
            why.append(f"{len(v['confusing'])} confusing points")
        if not v["would_watch_next"]:
            why.append("cold viewers would not watch next")
        if not r["setup_included"]:
            why.append("cliffhanger set-up trimmed")
        if r["hook"]["recap"]:
            why.append("opens on a recap")
        if r["hook"]["visual"]:
            why.append("hook is visual: check the footage")
        if why:
            flags.append({"rank": r["rank"], "id": r["id"], "reasons": why})
    return {
        "candidates_total": run["candidates_total"], "candidates_valid": run["candidates_valid"],
        "checks": CHECK_WORDS,
        "rejections": dict(Counter(k for c in ctx["all_candidates"] for k in c.get("rejected_because", [])).most_common()),
        "hooks": sum(len(b["hooks"]) for b in beats.values()),
        "cliffhangers": sum(len(b["cliffhangers"]) for b in beats.values()),
        "repaired": sum(len(b["warnings"]) for b in beats.values()),
        "episodes_without_hook": [e for e, b in beats.items() if not b["hooks"]],
        "episodes_without_cliffhanger": [e for e, b in beats.items() if not b["cliffhangers"]],
        "cross": {"pairs": len(cross.get("pairs", [])), "broke_rules_first_try": len(cross.get("rejected_first_try", [])),
                  "still_wrong_after_repair": len(cross.get("rejected_after_repair", [])),
                  "assembled": sum(1 for c in ctx["all_candidates"] if c["kind"] == "cross-episode")},
        "parse_warnings": run["parse_warnings"],
        "averages": {"hook": avg("hook_score"), "cliffhanger": avg("cliffhanger_score"), "standalone": avg("standalone_score")},
        "would_watch_next": sum(r["cold_viewer"]["would_watch_next"] for r in main), "main_list": len(main),
        "judges": run.get("judges", 1),
        "disagree": disagree, "flags": flags,
    }


def _tc(sec: float, fps: int) -> str:
    f = int(round(sec * fps))
    return f"{f // (3600 * fps):02d}:{f // (60 * fps) % 60:02d}:{f // fps % 60:02d}:{f % fps:02d}"


def _ascii(text: str) -> str:
    text = str(text).replace("“", '"').replace("”", '"').replace("’", "'").replace("—", " - ").replace("…", "...")
    return re.sub(r"[^\x20-\x7e]", "", text)


def write_edl(path: Path, main: list[dict], title: str, fps: int = 30, gap: float = 2.0) -> None:
    """CMX 3600 cut list: an editor imports it into Premiere / Resolve / Avid, relinks
    reel E031 to episode 31's video file, and every promo is already cut on the
    timeline, back to back. Assumes episode files start at 00:00:00:00, as the
    screenplay timings do."""
    lines = [f"TITLE: {_ascii(title)[:60]} - promo cuts", "FCM: NON-DROP FRAME", ""]
    rec, event = 3600.0, 1  # record timeline starts at 01:00:00:00
    for r in main:
        lines.append(f"* PROMO #{r['rank']} {r['id']} {r['duration_s']:.1f}s | HOOK: {_ascii(r['hook']['quote'])[:70]}")
        lines.append(f"* CLIFFHANGER: {_ascii(r['cliffhanger']['quote'])[:80]}")
        for s in r["segments"]:
            length = s["end_s"] - s["start_s"]
            lines.append(f"{event:03d}  E{s['episode']:03d}     AA/V  C        "
                         f"{_tc(s['start_s'], fps)} {_tc(s['end_s'], fps)} {_tc(rec, fps)} {_tc(rec + length, fps)}")
            lines.append(f"* FROM CLIP NAME: EPISODE {s['episode']:03d}")
            rec += length
            event += 1
        rec += gap
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="ascii", errors="ignore")


def _dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _write_csv(path: Path, ranked: list[dict]) -> None:
    cols = ["rank", "tier", "id", "score", "kind", "duration_s", "segments", "hook", "cliffhanger",
            "why_it_works", "hook_score", "cliffhanger_score", "standalone_score", "sells_premise"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in ranked:
            s = r["scores"] or {}
            w.writerow({
                "rank": r["rank"], "tier": r["tier"], "id": r["id"], "score": r["score"], "kind": r["kind"],
                "duration_s": r["duration_s"],
                "segments": " + ".join(f"E{x['episode']:03d} {x['start']}-{x['end']}" for x in r["segments"]),
                "hook": f"[{r['hook']['type']}] {r['hook']['quote']}",
                "cliffhanger": f"[{r['cliffhanger']['type']}] {r['cliffhanger']['quote']}",
                "why_it_works": r["why_it_works"], "hook_score": s.get("hook"),
                "cliffhanger_score": s.get("cliffhanger"), "standalone_score": s.get("standalone"),
                "sells_premise": s.get("premise"),
            })


def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _q(text: str) -> str:
    """Quote without doubled quote marks."""
    return "“" + str(text).strip().strip('"“”').strip() + "”"


def _report_md(ctx: dict, ranked: list[dict]) -> str:
    run, bible = ctx["run"], ctx["bible"]
    main = [r for r in ranked if r["tier"] != "Alternate"]
    alts = [r for r in ranked if r["tier"] == "Alternate"]
    L = [
        f"# Promo picks: {bible.get('title', '')}",
        "",
        f"Model `{run['model']}` · prompts {run['prompt_version']} · {run['episodes']} episodes "
        f"({run['runtime_min']} min of show) · {run['candidates_valid']} valid clips found · "
        f"{run.get('cold_viewer_tested', run['shortlisted'])} tested by {run.get('judges', 1)} cold viewer(s)",
        "",
        f"**Premise:** {bible.get('premise', '')}",
        f"**What the title promises:** {bible.get('title_promise', '')}",
        "",
        "## Ranked promos",
        "",
        "| # | Tier | Clip (episode start-end) | Len | Hook | Cliffhanger | Score |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in main:
        segs = " + ".join(f"E{x['episode']} {x['start']}-{x['end']}" for x in r["segments"])
        L.append(f"| {r['rank']} | {r['tier']} | {segs} | {r['duration_s']:.0f}s | {_md(r['hook']['quote'])[:70]} | "
                 f"{_md(r['cliffhanger']['quote'])[:70]} | {r['score']} |")

    reasons = {p.get("promo_id"): p.get("reason", "") for p in ctx["plan"].get("why_post", [])}
    if reasons:
        L += ["", "## What to post first, and why", "",
              "The order is the ranking above (by score). Each line is the job that clip does at that point.", ""]
        for r in main[:10]:
            if r["id"] in reasons:
                L.append(f"{r['rank']}. **{r['id']}** ({r['hook']['type']} → {r['cliffhanger']['type']}): {reasons[r['id']]}")
        notes = ctx["plan"].get("notes", [])
        if notes:
            L += ["", "Editor notes:"] + [f"- {n}" for n in notes]

    L += ["", "## Details", ""]
    for r in main:
        s, v = r["scores"], r["cold_viewer"]
        L += [
            f"### #{r['rank']} · {r['id']} · {r['tier']} · score {r['score']}",
            "",
            "**Segments (in order):**",
        ]
        for i, x in enumerate(r["segments"], 1):
            L.append(f"{i}. Episode {x['episode']}: {x['start']} → {x['end']} ({x['length_s']:.1f}s)")
        L += [
            f"",
            f"**Total:** {r['duration_s']:.1f}s · {r['kind']}" + (f" · _{r['stitch_story']}_" if r.get("stitch_story") else ""),
            f"",
            f"**Hook (first 3s, E{r['hook']['episode']} {r['hook']['at']}, {r['hook']['type']}"
            f"{', visual' if r['hook']['visual'] else ''}):** {_q(r['hook']['quote'])}: {r['hook']['what_happens']}",
            f"",
            f"**Cliffhanger (E{r['cliffhanger']['episode']} {r['cliffhanger']['at']}, {r['cliffhanger']['type']}):** "
            f"{_q(r['cliffhanger']['quote'])}: {r['cliffhanger']['open_question']}",
            f"",
            f"**Why it works:** {r['why_it_works']}",
            f"",
            f"**Scores:** hook {s['hook']} · cliffhanger {s['cliffhanger']} · stands alone {s['standalone']} · "
            f"sells premise {'yes' if s['premise'] else 'no'}"
            + (f" · penalties {', '.join(f'{k} -{val}' for k, val in s['penalties'].items())}" if s["penalties"] else ""),
            f"",
            f"**Cold viewer understood:** {v['what_i_understood']}"
            + (f" · confused by: {'; '.join(v['confusing'])}" if v["confusing"] else ""),
            *(["", "**Panel:** " + " · ".join(
                f"{p['viewer']}: hook {p['hook']:.0f}, cliffhanger {p['cliffhanger']:.0f}, stands alone {p['standalone']:.0f}"
                for p in v["panel"])] if len(v.get("panel", [])) > 1 else []),
            "",
            "<details><summary>Script of the clip</summary>",
            "",
            "```",
            r["script"],
            "```",
            "</details>",
            "",
        ]
    if alts:
        L += ["## Alternates (valid and tested, held back for variety)", "",
              "| # | Clip | Len | Hook | Cliffhanger | Score | Held back because |", "|---|---|---|---|---|---|---|"]
        for r in alts:
            segs = " + ".join(f"E{x['episode']} {x['start']}-{x['end']}" for x in r["segments"])
            L.append(f"| {r['rank']} | {segs} | {r['duration_s']:.0f}s | {_md(r['hook']['quote'])[:60]} | "
                     f"{_md(r['cliffhanger']['quote'])[:60]} | {r['score']} | {r.get('alternate_because', '')} |")
    return "\n".join(L) + "\n"


def _quality_md(ctx: dict, ranked: list[dict]) -> str:
    run = ctx["run"]
    q = quality_summary(ctx, ranked)
    L = [
        "# Quality report",
        "",
        f"Model `{run['model']}` · {run['episodes']} episodes · {run['llm_calls']} model calls "
        f"({run['cached_calls']} from cache) · {run['tokens_in']:,} tokens in / {run['tokens_out']:,} out · {run['seconds']}s",
        "",
        "## Hard checks (code, every candidate)",
        "",
        f"- Clips assembled: **{q['candidates_total']}**; passed every hard check: **{q['candidates_valid']}**",
        "- Checks: " + "; ".join(w[0].lower() + w[1:] for w in q["checks"].values()) + ".",
    ]
    if q["rejections"]:
        L.append("- Rejections by check: " + ", ".join(f"{k} ×{v}" for k, v in q["rejections"].items()))
    c = q["cross"]
    L.append(f"- Cross-episode stitches: {c['pairs']} valid pairs proposed "
             f"({c['broke_rules_first_try']} broke the rules on the first try and were sent back; "
             f"{c['still_wrong_after_repair']} still wrong after repair) → {c['assembled']} assembled")
    L += [
        f"- Model-marked moments: {q['hooks']} hooks, {q['cliffhangers']} cliffhangers; "
        f"{q['repaired']} line-number problems repaired or dropped by code",
        f"- Episodes with no hook found: {q['episodes_without_hook'] or 'none'} · "
        f"with no cliffhanger: {q['episodes_without_cliffhanger'] or 'none'}",
        f"- Parser warnings: {q['parse_warnings']}",
        "",
        "## Do the cuts land where the writers put the suspense?",
        "",
    ]
    w = ctx.get("writer", {})
    if w.get("writer_marked_moments"):
        L += [
            f"- The screenwriters marked {w['writer_marked_moments']} moments in {w['episodes_with_marks']} episodes "
            f"as a cliffhanger or a cut to black.",
            f"- The tool chose a cliffhanger that cuts within {w['tolerance_s']:.0f}s of {w['tool_cliffhanger_on_mark']} of them"
            + (f" (missed: {', '.join(w['missed'])})" if w["missed"] else "") + ".",
            f"- {w['main_list_ending_on_mark']} of the {w['main_list']} ranked promos end on a writer-marked moment.",
            "- Caveat: the model can read those words, so this checks cut-point precision more than taste.",
        ]
    else:
        L += ["- This show's scripts carry no writer cliffhanger marks."]
    a = q["averages"]
    n = q["judges"]
    L += [
        "",
        f"## Cold-viewer test (main list, {n} viewer{'s' if n != 1 else ''} per clip, averaged)",
        "",
        f"- Average: hook {a['hook']} · cliffhanger {a['cliffhanger']} · stands alone {a['standalone']} (out of 10)",
        f"- Would watch the next episode: {q['would_watch_next']}/{q['main_list']}",
        "",
        "## Where the editor model and the cold viewers disagree (≥3 points)",
        "",
        "Large gaps mean one of the two judgements is wrong; these are the first clips a human should check.",
        "",
    ]
    L += [f"- {d['id']} (#{d['rank']}): hook editor {d['hook_editor']:.0f} vs viewers {d['hook_viewer']:.1f}; "
          f"cliffhanger editor {d['cliff_editor']:.0f} vs viewers {d['cliff_viewer']:.1f}" for d in q["disagree"]] or ["- none"]
    L += ["", "## Flags on the main list", ""]
    L += [f"- #{f['rank']} {f['id']}: {'; '.join(f['reasons'])}" for f in q["flags"]] or ["- none"]
    L += [
        "",
        "## Known limits",
        "",
        "- Works from the screenplay, not the footage: it cannot see whether a slap lands or a shot looks good. Visual hooks are flagged for a human check.",
        "- Cut points are exact to the script's timings; the edit may still need a trim of under a second.",
        "- Scores are model judgements of story strength, not measured ad performance. Real CTR and retention should tune the weights.",
        "- The cold viewers are also models, so they can share the editor model's blind spots; a human panel reduces this.",
    ]
    return "\n".join(L) + "\n"
