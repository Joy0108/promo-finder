"""End-to-end run: parse -> show bible -> moments -> cross-episode pairs ->
assemble -> hard checks -> cold-viewer test -> rank -> posting plan -> outputs."""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from .assemble import build_candidates, shortlist
from .beats import mark_beats
from .bible import bible_brief, build_bible
from .critic import cold_view
from .llm import LLM, CacheMiss, LLMConfig
from .parse import load_show
from .prompts import CROSS_SYSTEM, PLAN_SYSTEM, PROMPT_VERSION
from .rank import _words, rank
from .report import seg_str, write_outputs

Progress = Callable[[str, int, int], None]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def run(show_dir: str | Path, out_root: str | Path = "outputs", cfg: LLMConfig | None = None, *,
        title: str | None = None, critic_k: int = 40, top_n: int = 20, workers: int = 6,
        episodes: tuple[int, int] | None = None, progress: Progress | None = None) -> dict:
    t0 = time.time()
    progress = progress or (lambda stage, done, total: None)
    cfg = cfg or LLMConfig.from_env()
    llm = LLM(cfg)
    show_dir = Path(show_dir)
    title = title or show_dir.name

    # 1. parse (code)
    all_eps = load_show(show_dir)
    if episodes:
        all_eps = [e for e in all_eps if episodes[0] <= e.ep <= episodes[1]]
    eps = {e.ep: e for e in all_eps}
    progress("parsed", len(eps), len(eps))

    # 2. show bible (1 call)
    bible = build_bible(title, all_eps, llm)
    progress("bible", 1, 1)

    # 3. moments per episode (1 call each, in parallel)
    beats: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(mark_beats, e, bible, llm): e.ep for e in all_eps}
        for i, f in enumerate(as_completed(futures), 1):
            beats[futures[f]] = f.result()
            progress("moments", i, len(futures))
    beats = dict(sorted(beats.items()))

    # 4. cross-episode stitches (1 call)
    cross = propose_cross(beats, bible, llm) if len(beats) > 1 else {"pairs": []}
    progress("cross", 1, 1)

    # 5. assemble + hard checks (code)
    from .validate import split_valid
    cands = build_candidates(eps, beats, cross.get("pairs", []))
    valid, rejected = split_valid(cands, eps)
    short = shortlist(valid, critic_k)

    # 6. cold-viewer test (1 call per shortlisted clip, in parallel)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(cold_view, c, eps, llm): c for c in short}
        for i, f in enumerate(as_completed(futures), 1):
            futures[f]["viewer"] = f.result()
            progress("cold-viewer", i, len(futures))

    # 7. rank (code) + posting plan (1 call)
    names: set[str] = set()  # character names say nothing about WHICH moment a clip shows
    for ch in bible.get("characters", []):
        for text in [ch.get("name", "")] + list(ch.get("labels", [])):
            names |= _words(str(text))
    ranked = rank(short, top_n=top_n, ignore_words=names)
    try:
        plan = posting_plan(ranked, bible, llm)
    except CacheMiss:  # offline replay after a ranking change: everything else is still exact
        plan = {"why_post": [], "notes": ["Posting notes skipped: offline replay, and this ranking has no cached notes."]}
    progress("ranked", 1, 1)

    u = llm.usage
    spent = u.cost_usd(cfg.model)
    cost = u.cost_usd(cfg.model, include_cached=True)  # what producing these results costs from scratch
    if cost is None:
        cost_display = "n/a (price unknown for this model)"
    elif u.cached_calls:
        approx = "≈" if u.estimated_calls else ""
        note = "; cached costs estimated from text, excluding any hidden reasoning tokens" if u.estimated_calls else ""
        cost_display = (f"{approx}${cost:.2f} per full run (this run spent ${spent:.3f}; "
                        f"{u.cached_calls} of {u.calls} calls from cache{note})")
    else:
        cost_display = f"${cost:.2f}"
    run_info = {
        "show": title, "provider": cfg.provider, "model": cfg.model, "prompt_version": PROMPT_VERSION,
        "episodes": len(eps), "episode_range": [min(eps), max(eps)],
        "runtime_min": round(sum(e.duration for e in all_eps) / 60, 1),
        "episode_durations": {e.ep: e.duration for e in all_eps},
        "blocks": sum(len(e.blocks) for e in all_eps),
        "parse_warnings": sum(len(e.warnings) for e in all_eps),
        "candidates_total": len(cands), "candidates_valid": len(valid), "shortlisted": len(short),
        "llm_calls": u.calls, "cached_calls": u.cached_calls,
        "tokens_in": u.prompt_tokens + u.cached_prompt_tokens,
        "tokens_out": u.completion_tokens + u.cached_completion_tokens,
        "tokens_spent_this_run": [u.prompt_tokens, u.completion_tokens], "by_stage": u.by_stage,
        "cost_usd": cost, "cost_spent_this_run_usd": spent, "cost_display": cost_display,
        "seconds": round(time.time() - t0, 1),
        "settings": {"critic_k": critic_k, "top_n": top_n, "workers": workers},
    }
    out_dir = Path(out_root) / f"{slug(title)}__{slug(cfg.provider + '-' + cfg.model)}"
    ctx = {"run": run_info, "bible": bible, "beats": beats, "cross": cross, "eps": eps,
           "all_candidates": valid + rejected, "ranked": ranked, "plan": plan}
    write_outputs(out_dir, ctx)
    progress("written", 1, 1)
    return {"out_dir": str(out_dir), "run": run_info}


def propose_cross(beats: dict[int, dict], bible: dict, llm: LLM, k: int = 25) -> dict:
    hooks = sorted((h for b in beats.values() for h in b["hooks"]), key=lambda x: -x["score"])[:k]
    cliffs = sorted((c for b in beats.values() for c in b["cliffhangers"]), key=lambda x: -x["score"])[:k]
    if not hooks or not cliffs:
        return {"pairs": []}
    lines = [bible_brief(bible), "", "HOOKS:"]
    lines += [f'{h["id"]} | ep {h["ep"]} | {h["type"]} | score {h["score"]:.0f} | context {h["needs_context"]} | '
              f'premise {h["on_premise"]} | "{h["quote"]}" | {h["what_happens"]}' for h in hooks]
    lines += ["", "CLIFFHANGERS:"]
    lines += [f'{c["id"]} | ep {c["ep"]} | {c["type"]} | score {c["score"]:.0f} | context {c["needs_context"]} | '
              f'premise {c["on_premise"]} | "{c["quote"]}" | {c["open_question"]}' for c in cliffs]
    user = "\n".join(lines)
    hook_ids = {h["id"]: h for h in hooks}
    cliff_ids = {c["id"]: c for c in cliffs}

    def check(pairs: list) -> tuple[list[dict], list[str]]:
        ok, errors = [], []
        for p in pairs:
            if not isinstance(p, dict):
                continue
            h, c = hook_ids.get(p.get("hook_id")), cliff_ids.get(p.get("cliff_id"))
            if not h or not c:
                errors.append(f"{p.get('hook_id')} -> {p.get('cliff_id')}: hook_id must be from HOOKS and cliff_id from CLIFFHANGERS")
            elif h["ep"] >= c["ep"]:
                errors.append(f"{p.get('hook_id')} -> {p.get('cliff_id')}: the cliffhanger must be from a later episode")
            else:
                ok.append(p)
        return ok, errors

    raw = llm.json(CROSS_SYSTEM, user, stage="cross")
    ok, errors = check(raw.get("pairs", []) if isinstance(raw, dict) else [])
    first_errors = list(errors)
    if errors:  # one repair round: tell the model exactly what broke the rules
        fix = (user + "\n\nYour previous answer broke these rules:\n- " + "\n- ".join(errors)
               + "\nReturn a corrected full list of pairs.")
        raw2 = llm.json(CROSS_SYSTEM, fix, stage="cross")
        ok2, errors = check(raw2.get("pairs", []) if isinstance(raw2, dict) else [])
        seen = {(p["hook_id"], p["cliff_id"]) for p in ok}
        ok += [p for p in ok2 if (p["hook_id"], p["cliff_id"]) not in seen]
    return {"pairs": ok, "rejected_first_try": first_errors, "rejected_after_repair": errors}


def posting_plan(ranked: list[dict], bible: dict, llm: LLM, k: int = 10) -> dict:
    top = [c for c in ranked if c.get("tier") != "Alternate"][:k]
    if not top:
        return {"why_post": [], "notes": []}
    lines = [bible_brief(bible), ""]
    for c in top:
        f = c["final"]
        lines.append(
            f'{c["id"]} | rank {c["rank"]} | {seg_str(c["segments"])} | {c["duration"]:.0f}s | '
            f'hook [{c["hook"]["type"]}] "{c["hook"]["quote"]}" | cliffhanger [{c["cliff"]["type"]}] '
            f'"{c["cliff"]["quote"]}" | score {f["score"]} (hook {f["hook"]}, cliff {f["cliffhanger"]}, '
            f'standalone {f["standalone"]}, premise {f["premise"]})'
        )
    raw = llm.json(PLAN_SYSTEM, "\n".join(lines), stage="plan")
    if not isinstance(raw, dict):
        return {"why_post": [], "notes": []}
    raw.setdefault("why_post", [])
    raw.setdefault("notes", [])
    return raw
