"""Hard checks, run on every candidate. A promo that fails any of them is never
shown to an editor; it is logged with the reason instead."""
from __future__ import annotations

from .assemble import EPS, HOOK_WINDOW, MAX_LEN, MIN_LEN, continued_from, continues
from .parse import Episode


def hard_checks(cand: dict, eps: dict[int, Episode]) -> dict[str, bool]:
    segs = cand["segments"]
    h, c = cand["hook"], cand["cliff"]
    total = sum(s["end"] - s["start"] for s in segs)
    checks: dict[str, bool] = {"duration_20_60s": MIN_LEN - EPS <= total <= MAX_LEN + EPS}

    bounds, order, trace, midline = True, True, True, True
    prev = None
    for s in segs:
        ep = eps[s["ep"]]
        if not (0 <= s["start"] < s["end"] <= ep.duration + EPS):
            bounds = False
        if prev and (s["ep"], s["start"]) < (prev["ep"], prev["end"] - EPS):
            order = False
        starts = {round(b.start, 2) for b in ep.blocks}
        ends = {round(b.end, 2) for b in ep.blocks}
        if round(s["start"], 2) not in starts or round(s["end"], 2) not in ends:
            trace = False
        for t in (s["start"], s["end"]):
            if any(d.is_dialogue and d.start < t - EPS and d.end > t + EPS for d in ep.blocks):
                midline = False
        prev = s
    checks["within_episode"] = bounds
    checks["segments_in_story_order"] = order
    checks["timestamps_from_script"] = trace
    checks["no_cut_mid_line"] = midline

    sentences = True
    for s in segs:
        ep = eps[s["ep"]]
        inside = [b for b in ep.blocks if b.is_dialogue and b.start >= s["start"] - EPS and b.end <= s["end"] + EPS]
        if inside:
            if continued_from(ep, inside[0]) is not None and continued_from(ep, inside[0]).start < s["start"] - EPS:
                sentences = False
            nxt = continues(ep, inside[-1])
            if nxt is not None and nxt.end > s["end"] + EPS:
                sentences = False
    checks["no_cut_mid_sentence"] = sentences

    first = segs[0]
    peak = eps[h["ep"]].blocks[h["peak"]]
    lead = peak.start - first["start"]
    checks["hook_in_first_3s"] = first["ep"] == h["ep"] and -EPS <= lead <= HOOK_WINDOW + EPS

    last = segs[-1]
    cut = eps[c["ep"]].blocks[c["cut"]]
    checks["ends_on_cliffhanger"] = (last["ep"] == c["ep"] and cut.start >= last["start"] - EPS
                                     and cut.end <= last["end"] + EPS)
    if c["resolved_at"] is not None:
        r = eps[c["ep"]].blocks[c["resolved_at"]]
        checks["answer_not_shown"] = r.start >= last["end"] - EPS
    else:
        checks["answer_not_shown"] = True
    return checks


def split_valid(cands: list[dict], eps: dict[int, Episode]) -> tuple[list[dict], list[dict]]:
    valid, rejected = [], []
    for cand in cands:
        cand["checks"] = hard_checks(cand, eps)
        failed = [k for k, ok in cand["checks"].items() if not ok]
        if failed:
            cand["rejected_because"] = failed
            rejected.append(cand)
        else:
            valid.append(cand)
    return valid, rejected
