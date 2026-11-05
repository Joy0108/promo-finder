"""A free, human-made reference: the screenwriters' own cliffhanger marks.

The scripts sometimes say where the writers meant the suspense to peak
("...swells to an ominous cliffhanger peak", "Cut to black"). This checks whether
the tool's cut points land on those moments.

Caveat, stated in the report: the model can read those words too, so this
measures cut-point precision more than taste.
"""
from __future__ import annotations

import re

from .parse import Episode, fmt

WRITER_RE = re.compile(r"cliffhanger|cuts? to black|cutting to black|abrupt(ly)? cut|smash cut|freeze[- ]frame", re.I)
MERGE = 5.0      # marks closer than this are one moment
TOLERANCE = 4.0  # a cut within this many seconds of the mark counts as landing on it


def writer_moments(ep: Episode) -> list[float]:
    """Times (seconds) where the writers marked a cliffhanger in this episode."""
    marks = sorted(b.end for b in ep.blocks if WRITER_RE.search(b.text))
    moments: list[float] = []
    for t in marks:
        if moments and t - moments[-1] <= MERGE:
            moments[-1] = t
        else:
            moments.append(t)
    return moments


def check(eps: dict[int, Episode], beats: dict[int, dict], ranked_main: list[dict]) -> dict:
    marked = {e: writer_moments(ep) for e, ep in eps.items()}
    marked = {e: m for e, m in marked.items() if m}
    total = sum(len(m) for m in marked.values())

    found, missed = 0, []
    for e, moments in marked.items():
        cuts = [eps[e].blocks[c["cut"]].end for c in beats.get(e, {}).get("cliffhangers", [])]
        for t in moments:
            if any(abs(c - t) <= TOLERANCE for c in cuts):
                found += 1
            else:
                missed.append(f"E{e:03d} {fmt(t)}")

    on_mark = 0
    for r in ranked_main:
        last = r["segments"][-1]
        if any(abs(last["end_s"] - t) <= TOLERANCE for t in marked.get(last["episode"], [])):
            on_mark += 1
    return {
        "episodes_with_marks": len(marked), "writer_marked_moments": total,
        "tool_cliffhanger_on_mark": found, "missed": missed,
        "main_list": len(ranked_main), "main_list_ending_on_mark": on_mark,
        "tolerance_s": TOLERANCE,
    }
