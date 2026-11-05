"""Stage 2: one call per episode. The model marks hooks and cliffhangers by
line number; this module checks every number against the parsed script and
repairs or drops anything that breaks the rules."""
from __future__ import annotations

from .bible import bible_brief
from .llm import LLM
from .parse import Episode, fmt
from .prompts import BEATS_SYSTEM

HOOK_WINDOW = 3.0
VALID_TYPES = {"betrayal", "humiliation", "identity_reveal", "threat", "violence", "injustice",
               "romance", "defiance", "mystery", "rescue", "power_reversal", "other"}
CONTEXT_LEVELS = {"none", "some", "heavy"}


def render_episode(ep: Episode, alias: dict[str, str]) -> str:
    """Numbered script the model reads. Speakers show their canonical name too."""
    out = []
    for b in ep.blocks:
        if b.is_dialogue:
            name = alias.get((b.speaker or "").upper())
            who = b.speaker if not name or name.upper() == (b.speaker or "").upper() else f"{b.speaker} [={name}]"
            if b.tags:
                who += f" ({b.tags})"
        else:
            who = b.kind.upper()
        out.append(f"#{b.idx} {fmt(b.start)}-{fmt(b.end)} {who}: {b.text}")
    return "\n".join(out)


def mark_beats(ep: Episode, bible: dict, llm: LLM) -> dict:
    user = (
        f"{bible_brief(bible)}\n\n"
        f"EPISODE {ep.ep} (duration {fmt(ep.duration)})\n"
        f"LOGLINE: {ep.logline}\n\n"
        f"SCRIPT (#line start-end WHO: text):\n{render_episode(ep, bible.get('alias', {}))}"
    )
    raw = llm.json(BEATS_SYSTEM, user, stage="beats")
    hooks, cliffs, warnings = clean_beats(ep, raw)
    return {"ep": ep.ep, "hooks": hooks, "cliffhangers": cliffs, "warnings": warnings, "raw": raw}


def _idx(value, n: int):
    try:
        i = int(str(value).lstrip("#"))
    except (TypeError, ValueError):
        return None
    return i if 0 <= i < n else None


def _common(item: dict) -> dict:
    t = str(item.get("type", "other")).lower().strip()
    ctx = str(item.get("needs_context", "some")).lower().strip()
    try:
        score = float(item.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    return {
        "type": t if t in VALID_TYPES else "other",
        "score": max(0.0, min(10.0, score)),
        "needs_context": ctx if ctx in CONTEXT_LEVELS else "some",
        "on_premise": bool(item.get("on_premise", False)),
        "recap": bool(item.get("recap", False)),
        "quote": str(item.get("quote", "")).strip(),
    }


def clean_beats(ep: Episode, raw: dict) -> tuple[list[dict], list[dict], list[str]]:
    n = len(ep.blocks)
    B = ep.blocks
    warnings: list[str] = []
    hooks: list[dict] = []
    for item in raw.get("hooks", []) or []:
        start = _idx(item.get("start"), n)
        peak = _idx(item.get("peak", item.get("start")), n)
        end = _idx(item.get("beat_end"), n)
        if start is None and peak is None:
            warnings.append(f"hook dropped: line numbers out of range ({item.get('start')})")
            continue
        start = start if start is not None else peak
        peak = peak if peak is not None else start
        if B[peak].start < B[start].start:
            start, peak = peak, start
        if B[peak].start - B[start].start > HOOK_WINDOW:
            warnings.append(f"hook #{start}->#{peak}: peak lands {B[peak].start - B[start].start:.1f}s in; opening moved to the peak")
            start = peak
        if end is None or B[end].end < B[peak].end:
            end = peak
        c = _common(item)
        if c["score"] < 5:
            continue
        hooks.append({
            **c, "id": f"E{ep.ep:03d}-H{len(hooks) + 1}", "ep": ep.ep,
            "start": start, "peak": peak, "beat_end": end,
            "what_happens": str(item.get("what_happens", "")).strip(),
        })

    cliffs: list[dict] = []
    for item in raw.get("cliffhangers", []) or []:
        cut = _idx(item.get("cut"), n)
        if cut is None:
            warnings.append(f"cliffhanger dropped: line number out of range ({item.get('cut')})")
            continue
        setup = _idx(item.get("setup"), n)
        if setup is None or B[setup].start > B[cut].start:
            setup = _setup_fallback(ep, cut)
        resolved = _idx(item.get("resolved_at"), n) if item.get("resolved_at") is not None else None
        if resolved is not None and B[resolved].start < B[cut].end - 0.05:
            warnings.append(f"cliffhanger #{cut}: resolved_at #{resolved} is not after the cut; ignored")
            resolved = None
        c = _common(item)
        if c["score"] < 5:
            continue
        cliffs.append({
            **c, "id": f"E{ep.ep:03d}-C{len(cliffs) + 1}", "ep": ep.ep,
            "setup": setup, "cut": cut, "resolved_at": resolved,
            "open_question": str(item.get("open_question", "")).strip(),
        })
    return hooks, cliffs, warnings


def _setup_fallback(ep: Episode, cut: int, seconds: float = 20.0) -> int:
    """If the model gave no usable setup, take the first line about 20s before the cut."""
    target = ep.blocks[cut].start - seconds
    for b in ep.blocks:
        if b.start >= target and b.is_dialogue:
            return min(b.idx, cut)
    return cut
