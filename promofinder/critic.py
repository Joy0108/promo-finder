"""Stage 5: the cold-viewer test. Separate calls see ONLY the clip (no show
bible, no episode context, raw on-screen labels) and must explain it. This turns
'makes sense on its own' from a hope into a measured score.

A panel of three different strangers judges each clip and their scores are
averaged, so one judge's whim cannot move a clip up or down the list."""
from __future__ import annotations

from .llm import LLM
from .parse import Episode, fmt
from concurrent.futures import ThreadPoolExecutor
from statistics import mean

from .prompts import CRITIC_PANEL

EPS = 0.05


def clip_blocks(seg: dict, ep: Episode):
    """Blocks a viewer would see/hear inside the segment (>= 0.3s on screen)."""
    for b in ep.blocks:
        visible = min(b.end, seg["end"]) - max(b.start, seg["start"])
        if visible >= 0.3 or (b.is_dialogue and b.start >= seg["start"] - EPS and b.end <= seg["end"] + EPS):
            yield b


def _who(b, raw_labels: bool = True) -> str:
    if b.is_dialogue:
        return f"{b.speaker}" + (f" ({b.tags})" if b.tags else "")
    return {"sfx": "SOUND"}.get(b.kind, b.kind.upper())


def render_for_viewer(cand: dict, eps: dict[int, Episode]) -> str:
    """Clip text with times relative to the clip start; no episode numbers, no names added."""
    out, offset = [], 0.0
    for i, seg in enumerate(cand["segments"]):
        if i:
            out.append("--- CUT ---")
        ep = eps[seg["ep"]]
        for b in clip_blocks(seg, ep):
            t = offset + max(0.0, b.start - seg["start"])
            out.append(f"[{t:04.1f}s] {_who(b)}: {b.text}")
        offset += seg["end"] - seg["start"]
    return "\n".join(out)


def render_excerpt(cand: dict, eps: dict[int, Episode]) -> str:
    """Same clip with source timestamps, for the editor's report."""
    out = []
    for i, seg in enumerate(cand["segments"]):
        if i:
            out.append("--- CUT ---")
        ep = eps[seg["ep"]]
        for b in clip_blocks(seg, ep):
            out.append(f"[E{ep.ep:03d} {fmt(b.start)}] {_who(b)}: {b.text}")
    return "\n".join(out)


def one_view(system: str, clip_text: str, llm: LLM) -> dict:
    raw = llm.json(system, clip_text, stage="critic", temperature=0.3)

    def score(key):
        try:
            return max(1.0, min(10.0, float(raw.get(key, 1))))
        except (TypeError, ValueError):
            return 1.0

    confusing = raw.get("confusing") or []
    if isinstance(confusing, str):
        confusing = [confusing]
    return {
        "what_i_understood": str(raw.get("what_i_understood", "")),
        "first_3_seconds": str(raw.get("first_3_seconds", "")),
        "hook_score": score("hook_score"),
        "open_question": str(raw.get("open_question", "")),
        "cliffhanger_score": score("cliffhanger_score"),
        "standalone_score": score("standalone_score"),
        "confusing": [str(x) for x in confusing if str(x).strip()],
        "would_watch_next": bool(raw.get("would_watch_next", False)),
        "why_it_works": str(raw.get("why_it_works", "")),
    }


def cold_view(cand: dict, eps: dict[int, Episode], llm: LLM, judges: int = 3) -> dict:
    """Ask `judges` different cold viewers (1-3) and combine their verdicts."""
    text = render_for_viewer(cand, eps)
    panel = CRITIC_PANEL[:max(1, min(judges, len(CRITIC_PANEL)))]
    with ThreadPoolExecutor(max_workers=len(panel)) as pool:
        views = list(pool.map(lambda p: one_view(p[1], text, llm), panel))
    return combine([name for name, _ in panel], views)


def combine(names: list[str], views: list[dict]) -> dict:
    """Scores are the panel's average; 'would watch next' is the majority; the
    confusing points are those of the median viewer (by how many each listed).
    The words (what they understood, why it works) are viewer 1's."""
    first = views[0]
    if len(views) == 1:
        return {**first, "panel": [_row(names[0], first)]}
    by_count = sorted(views, key=lambda v: len(v["confusing"]))
    return {
        **first,
        "hook_score": round(mean(v["hook_score"] for v in views), 2),
        "cliffhanger_score": round(mean(v["cliffhanger_score"] for v in views), 2),
        "standalone_score": round(mean(v["standalone_score"] for v in views), 2),
        "would_watch_next": sum(v["would_watch_next"] for v in views) * 2 > len(views),
        "confusing": by_count[len(by_count) // 2]["confusing"],
        "panel": [_row(n, v) for n, v in zip(names, views)],
    }


def _row(name: str, v: dict) -> dict:
    return {"viewer": name, "hook": v["hook_score"], "cliffhanger": v["cliffhanger_score"],
            "standalone": v["standalone_score"], "would_watch_next": v["would_watch_next"],
            "understood": v["what_i_understood"], "confusing": v["confusing"], "why_it_works": v["why_it_works"]}
