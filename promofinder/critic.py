"""Stage 5: the cold-viewer test. A separate call sees ONLY the clip (no show
bible, no episode context, raw on-screen labels) and must explain it. This turns
'makes sense on its own' from a hope into a measured score."""
from __future__ import annotations

from .llm import LLM
from .parse import Episode, fmt
from .prompts import CRITIC_SYSTEM

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


def cold_view(cand: dict, eps: dict[int, Episode], llm: LLM) -> dict:
    raw = llm.json(CRITIC_SYSTEM, render_for_viewer(cand, eps), stage="critic", temperature=0.3)

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
