"""Stage 1: one call per show. Builds the premise, the title's promise and a
label -> character map (screenplays name the same person differently)."""
from __future__ import annotations

from .llm import LLM
from .parse import Episode, speaker_stats
from .prompts import BIBLE_SYSTEM


def build_bible(title: str, episodes: list[Episode], llm: LLM) -> dict:
    loglines = "\n".join(
        f"Ep {e.ep}: {e.logline}" + (f"  [continuity: {e.continuity}]" if e.continuity else "")
        for e in episodes
    )
    stats = speaker_stats(episodes)
    labels = "\n".join(
        f'- {label} | episodes {_ranges(s["episodes"])} | {s["lines"]} lines | e.g. {s["sample"][:90]}'
        for label, s in sorted(stats.items(), key=lambda kv: kv[1]["episodes"][0])
    )
    user = f"SHOW TITLE: {title}\n\nEPISODE LOGLINES:\n{loglines}\n\nSPEAKER LABELS:\n{labels}"
    bible = llm.json(BIBLE_SYSTEM, user, stage="bible")
    bible["title"] = title

    # label -> canonical name; any label the model forgot keeps its own name
    alias: dict[str, str] = {}
    for ch in bible.get("characters", []):
        for lab in ch.get("labels", []):
            alias[str(lab).upper().strip()] = ch.get("name", lab)
    missing = [lab for lab in stats if lab.upper() not in alias]
    for lab in missing:
        alias[lab.upper()] = lab.title()
    bible["alias"] = alias
    bible["unmapped_labels"] = missing
    return bible


def bible_brief(bible: dict) -> str:
    """Compact context block reused in every per-episode prompt."""
    chars = "; ".join(
        f'{c.get("name")} ({c.get("role", "")})'
        for c in bible.get("characters", []) if c.get("name") != "Minor"
    )
    return (
        f"SHOW: {bible.get('title', '')}\n"
        f"PREMISE: {bible.get('premise', '')}\n"
        f"CORE PROMISE (on_premise moments sell this): {bible.get('title_promise', '')}\n"
        f"CHARACTERS: {chars}"
    )


def _ranges(nums: list[int]) -> str:
    nums = sorted(set(nums))
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else f"{start}")
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(out)
