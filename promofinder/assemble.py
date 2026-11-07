"""Stage 3 (pure code): pair hooks with later cliffhangers and turn each pair
into exact, editor-ready segments.

Cut rules:
- open on the camera cut just before the hook line, if the hook still lands in 3s
- never start or end inside someone's line
- end at the end of the cliffhanger line, then hold on the reaction shot for up to
  1.5s if nobody starts speaking (the answer is never included)
- if hook -> cliffhanger is longer than 60s, stitch two parts: the hook's own
  mini-moment, then the run-up to the cliffhanger (also across episodes)
"""
from __future__ import annotations

from collections import Counter

from .parse import Episode

MIN_LEN, MAX_LEN = 20.0, 60.0
HOOK_WINDOW = 3.0
CAM_SNAP = 1.0
TAIL_HOLD = 1.5
MIN_PART = 6.0
EPS = 0.05


# ------------------------------------------------- sentences split across lines
TRAIL = ("...", "…", "—", "--")
CONT_GAP = 3.0


def _clean(text: str) -> str:
    return text.strip().strip('"“”').strip()


def continues(ep: Episode, b) -> object | None:
    """The same speaker's next line, if `b` trails off and that line picks it up
    ("...plainness and low value..." -> "...when Clarissa is the true jewel")."""
    if not b.is_dialogue or not _clean(b.text).endswith(TRAIL):
        return None
    for nb in ep.blocks[b.idx + 1:]:
        if nb.start - b.end > CONT_GAP:
            return None
        if nb.is_dialogue:
            same = nb.speaker == b.speaker
            return nb if same and _clean(nb.text).startswith(("...", "…")) else None
    return None


def continued_from(ep: Episode, b) -> object | None:
    """The line `b` is finishing, if `b` starts mid-sentence."""
    if not b.is_dialogue or not _clean(b.text).startswith(("...", "…")):
        return None
    for pb in reversed(ep.blocks[: b.idx]):
        if b.start - pb.end > CONT_GAP:
            return None
        if pb.is_dialogue:
            return pb if pb.speaker == b.speaker and _clean(pb.text).endswith(TRAIL) else None
    return None


# ------------------------------------------------------------------ cut points
def open_at(ep: Episode, start_idx: int, peak_idx: int) -> float:
    b = ep.blocks[start_idx]
    prev = continued_from(ep, b)
    while prev is not None and ep.blocks[peak_idx].start - prev.start <= HOOK_WINDOW:
        b, prev = prev, continued_from(ep, prev)  # don't open mid-sentence
    peak_t = ep.blocks[peak_idx].start
    t = b.start
    for c in ep.blocks:  # open on the camera cut containing the first line
        if (c.kind == "camera" and c.start <= t + EPS and c.end > t + EPS
                and t - c.start <= CAM_SNAP and peak_t - c.start <= HOOK_WINDOW):
            t = min(t, c.start)
            break
    for d in ep.blocks:  # never open in the middle of someone's line
        if d.is_dialogue and d.start < t - EPS and d.end > t + EPS and peak_t - d.start <= HOOK_WINDOW:
            t = d.start
    return round(t, 3)


def close_at(ep: Episode, cut_idx: int, resolved_idx: int | None = None) -> float:
    b = ep.blocks[cut_idx]
    nxt = continues(ep, b)
    while nxt is not None and nxt.idx != resolved_idx:
        b, nxt = nxt, continues(ep, nxt)  # finish the sentence
    t = b.end
    for d in ep.blocks:  # let a line already in progress finish
        if d.is_dialogue and d.idx != resolved_idx and d.start < t - EPS and d.end > t + EPS:
            t = d.end
    for c in ep.blocks:  # brief hold on the reaction shot, if it stays silent
        if c.kind == "camera" and c.start <= t + EPS and c.end > t + EPS and c.end - t <= TAIL_HOLD:
            speech = any(d.is_dialogue and t - EPS < d.start < c.end for d in ep.blocks)
            answer = resolved_idx is not None and ep.blocks[resolved_idx].start < c.end - EPS
            if not speech and not answer:
                t = c.end
            break
    return round(min(t, ep.duration), 3)


def _seg(ep: Episode, s: float, e: float) -> dict:
    return {"ep": ep.ep, "start": round(s, 3), "end": round(e, 3)}


def seg_len(segs: list[dict]) -> float:
    return round(sum(s["end"] - s["start"] for s in segs), 3)


# ------------------------------------------------------------------ candidates
def continuous(ep: Episode, h: dict, c: dict) -> list[dict]:
    return [_seg(ep, open_at(ep, h["start"], h["peak"]), close_at(ep, c["cut"], c["resolved_at"]))]


def stitched(ep_a: Episode, h: dict, ep_b: Episode, c: dict) -> tuple[list[dict], bool] | None:
    """Hook mini-moment + run-up to the cliffhanger. Returns (segments, setup_included)."""
    s1 = open_at(ep_a, h["start"], h["peak"])
    e2 = close_at(ep_b, c["cut"], c["resolved_at"])
    cut_b = ep_b.blocks[c["cut"]]
    setup_t = ep_b.blocks[c["setup"]].start
    same_ep = ep_a.ep == ep_b.ep

    for end_idx in dict.fromkeys([h["beat_end"], h["peak"]]):  # try the full beat, then just the peak
        e1 = close_at(ep_a, end_idx)
        len1 = e1 - s1
        if len1 <= 0:
            continue
        options = []
        for b in ep_b.blocks:
            if not b.is_dialogue or b.start > cut_b.start + EPS:
                continue
            s2 = open_at(ep_b, b.idx, b.idx)
            if same_ep and s2 < e1 + 1.0:
                continue
            len2 = e2 - s2
            total = len1 + len2
            if len2 >= MIN_PART and MIN_LEN <= total <= MAX_LEN:
                options.append((b.start, s2))
        if not options:
            continue
        after = [o for o in options if o[0] >= setup_t - EPS]
        start_b, s2 = min(after) if after else max(options)
        return [_seg(ep_a, s1, e1), _seg(ep_b, s2, e2)], start_b <= setup_t + EPS
    return None


def build_candidates(eps: dict[int, Episode], beats: dict[int, dict], cross_pairs: list[dict]) -> list[dict]:
    cands: list[dict] = []
    for ep_num, bt in beats.items():
        ep = eps[ep_num]
        for h in bt["hooks"]:
            for c in bt["cliffhangers"]:
                if ep.blocks[c["cut"]].end <= ep.blocks[h["peak"]].end + 2.0:
                    continue  # the cliffhanger must come after the hook
                segs = continuous(ep, h, c)
                L = seg_len(segs)
                if MIN_LEN <= L <= MAX_LEN:
                    setup_in = ep.blocks[c["setup"]].start >= segs[0]["start"] - EPS
                    cands.append(_cand("continuous", segs, h, c, setup_in))
                elif L > MAX_LEN:
                    st = stitched(ep, h, ep, c)
                    if st:
                        cands.append(_cand("stitched", st[0], h, c, st[1]))

    hooks = {h["id"]: h for bt in beats.values() for h in bt["hooks"]}
    cliffs = {c["id"]: c for bt in beats.values() for c in bt["cliffhangers"]}
    for p in cross_pairs:
        h, c = hooks.get(p.get("hook_id")), cliffs.get(p.get("cliff_id"))
        if not h or not c or h["ep"] >= c["ep"]:
            continue
        st = stitched(eps[h["ep"]], h, eps[c["ep"]], c)
        if st:
            cand = _cand("cross-episode", st[0], h, c, st[1])
            cand["story"] = p.get("story", "")
            cands.append(cand)

    for i, cand in enumerate(sorted(cands, key=lambda x: -x["prescore"]), start=1):
        cand["id"] = f"P{i:03d}"
    return sorted(cands, key=lambda x: x["id"])


def _cand(kind: str, segs: list[dict], h: dict, c: dict, setup_in: bool) -> dict:
    dur = seg_len(segs)
    return {
        "id": None, "kind": kind, "segments": segs, "duration": dur,
        "hook_id": h["id"], "cliff_id": c["id"], "hook": h, "cliff": c,
        "setup_included": setup_in, "prescore": prescore(h, c, kind, setup_in, dur),
    }


def prescore(h: dict, c: dict, kind: str, setup_in: bool, dur: float) -> float:
    """Quick code-only ranking, used only to choose which candidates go to the cold-viewer test."""
    ctx = {"none": 0.0, "some": 0.3, "heavy": 1.0}
    s = 0.5 * h["score"] + 0.5 * c["score"]
    s += 0.5 if (h["on_premise"] or c["on_premise"]) else 0.0
    s -= ctx[h["needs_context"]] + ctx[c["needs_context"]]
    s -= 0.0 if kind == "continuous" else 0.3
    s -= 0.0 if setup_in else 0.5
    s -= 0.3 if h["recap"] else 0.0
    s += 0.2 if 25 <= dur <= 45 else 0.0
    return round(s, 3)


def overlap(a: dict, b: dict) -> float:
    """Share of the shorter clip that overlaps the other in source time."""
    o = 0.0
    for x in a["segments"]:
        for y in b["segments"]:
            if x["ep"] == y["ep"]:
                o += max(0.0, min(x["end"], y["end"]) - max(x["start"], y["start"]))
    return o / max(1e-6, min(a["duration"], b["duration"]))


def shortlist(cands: list[dict], k: int, per_beat: int = 2) -> list[dict]:
    """Best-first, with variety: each hook/cliffhanger used at most `per_beat` times,
    and no near-duplicate clips."""
    used_h, used_c, chosen = Counter(), Counter(), []
    for cand in sorted(cands, key=lambda x: -x["prescore"]):
        if used_h[cand["hook_id"]] >= per_beat or used_c[cand["cliff_id"]] >= per_beat:
            continue
        if any(overlap(cand, x) > 0.8 for x in chosen):
            continue
        chosen.append(cand)
        used_h[cand["hook_id"]] += 1
        used_c[cand["cliff_id"]] += 1
        if len(chosen) >= k:
            break
    return chosen
