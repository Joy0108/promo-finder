"""Stage 6 (pure code): final score and a varied ranking.

final = 0.30 hook + 0.30 cliffhanger + 0.25 stands-alone + 0.15 premise
  hook        = mean(editor-model hook score, cold-viewer hook score)
  cliffhanger = mean(editor-model cliffhanger score, cold-viewer cliffhanger score)
  stands-alone= cold-viewer standalone score
  premise     = 6, +2 if the hook sells the show's core promise, +2 if the cliffhanger does
minus small penalties: viewer would not watch next (-0.75), each confusing point
beyond the second (-0.25), stitched clip (-0.2), opens on a recap (-0.3).

Variety rules for the main list: one clip per hook and per cliffhanger, no
overlapping clips, no replay of the same story moment in an adjacent episode,
at most two clips per episode.

The weights are a starting point an editor tunes with real ad results.
"""
from __future__ import annotations

import re
from collections import Counter

from .assemble import overlap

STOP = set("""the a an and or of to in on at for with as by is are was were be been it its this that these those
his her their she he they them you your i me my we our from into over up down out than then so but not no
yes do does did have has had will would can could just very what who why how when where all any
cliffhanger cut cuts cutting close shot camera extreme macro frame black abrupt abruptly scene
react reacts reaction next happen happens""".split())
SAME_MOMENT = 0.35  # calibrated on this show: true replays scored 0.45, the closest false match 0.40 before names were ignored


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 2 and w not in STOP}


def _moment_text(cand: dict) -> tuple[set[str], set[str]]:
    h, c = cand["hook"], cand["cliff"]
    return _words(f"{h['quote']} {h['what_happens']}"), _words(f"{c['quote']} {c['open_question']}")


def same_moment(a: dict, b: dict, common: set[str]) -> bool:
    """Episodes often replay the previous episode's peak. Two clips from the same or
    adjacent episodes whose hooks or cliffhangers share most of their distinctive
    words are the same story moment, even if their timestamps never overlap."""
    ea, eb = {s["ep"] for s in a["segments"]}, {s["ep"] for s in b["segments"]}
    if min(abs(x - y) for x in ea for y in eb) > 1:
        return False
    ha, ca = (x - common for x in _moment_text(a))
    hb, cb = (x - common for x in _moment_text(b))

    def oc(x: set, y: set) -> float:  # overlap coefficient; tiny sets are too noisy to judge
        return len(x & y) / min(len(x), len(y)) if min(len(x), len(y)) >= 4 else 0.0

    # cliffhanger-vs-hook catches the recap pattern: episode N's ending replayed as N+1's opening
    return max(oc(ca, cb), oc(ha, hb), oc(ca, hb), oc(ha, cb)) >= SAME_MOMENT

WEIGHTS = {"hook": 0.30, "cliffhanger": 0.30, "standalone": 0.25, "premise": 0.15}
TIERS = [(5, "Post first"), (15, "Next up")]


def final_score(cand: dict) -> dict:
    v = cand["viewer"]
    h, c = cand["hook"], cand["cliff"]
    hook = (h["score"] + v["hook_score"]) / 2
    cliff = (c["score"] + v["cliffhanger_score"]) / 2
    stand = v["standalone_score"]
    premise = 6.0 + 2.0 * h["on_premise"] + 2.0 * c["on_premise"]
    score = (WEIGHTS["hook"] * hook + WEIGHTS["cliffhanger"] * cliff
             + WEIGHTS["standalone"] * stand + WEIGHTS["premise"] * premise)
    penalties = {
        "viewer_would_not_watch_next": 0.0 if v["would_watch_next"] else 0.75,
        "confusing_points": 0.25 * max(0, len(v["confusing"]) - 2),
        "stitched": 0.0 if cand["kind"] == "continuous" else 0.2,
        "opens_on_recap": 0.3 if h["recap"] else 0.0,
    }
    score -= sum(penalties.values())
    return {"score": round(score, 2), "hook": round(hook, 2), "cliffhanger": round(cliff, 2),
            "standalone": round(stand, 2), "premise": premise > 6.0, "premise_score": premise,
            "penalties": {k: v for k, v in penalties.items() if v}}


def rank(cands: list[dict], top_n: int = 20, per_episode: int = 2, ignore_words: set[str] | None = None) -> list[dict]:
    """Greedy best-first with variety: no shared hook or cliffhanger, no overlapping
    clips, at most `per_episode` clips per episode in the main list. Everything else
    follows as alternates."""
    for cand in cands:
        cand["final"] = final_score(cand)
    # words shared by many moments (character names, the show's setting) say nothing
    # about which moment it is, so they are ignored when comparing moments
    df = Counter(w for c in cands for part in _moment_text(c) for w in part)
    common = {w for w, n in df.items() if n > 0.15 * max(1, 2 * len(cands))} | (ignore_words or set())

    pool = sorted(cands, key=lambda x: -x["final"]["score"])
    main, alternates = [], []
    used_h, used_c, per_ep = set(), set(), Counter()
    for cand in pool:
        eps_in = {s["ep"] for s in cand["segments"]}
        reason = None
        if cand["hook_id"] in used_h or cand["cliff_id"] in used_c:
            reason = "shares its hook or cliffhanger with a higher pick"
        elif any(overlap(cand, m) > 0.3 for m in main):
            reason = "overlaps a higher pick"
        elif any(same_moment(cand, m, common) for m in main):
            reason = "same story moment as a higher pick (replayed in an adjacent episode)"
        elif any(per_ep[e] >= per_episode for e in eps_in):
            reason = f"episode already has {per_episode} picks"
        if len(main) < top_n and reason is None:
            main.append(cand)
            used_h.add(cand["hook_id"])
            used_c.add(cand["cliff_id"])
            for e in eps_in:
                per_ep[e] += 1
        else:
            cand["alternate_because"] = reason or "main list full"
            alternates.append(cand)
    ranked = main + alternates
    for i, cand in enumerate(ranked, start=1):
        cand["rank"] = i
        if i > len(main):
            cand["tier"] = "Alternate"
        else:
            cand["tier"] = next((name for limit, name in TIERS if i <= limit), "Backlog")
    return ranked
