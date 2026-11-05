"""Quality evaluation.

1) Against a human reference set (an editor's own picks, made BEFORE looking at
   the tool's output):

    python evaluate.py gold outputs/<run-folder> gold/gold.json

   gold.json is a list of moments. Times are optional; an entry with only an
   episode counts as found if any top-k promo uses that episode.
    [{"episode": 1, "start": "00:33.70", "end": "01:05.60", "note": "jilted at the altar"},
     {"episode": 36, "note": "dead Duke is coming home"}]

2) Model agreement between two runs (different models on the same show):

    python evaluate.py agree outputs/<run-A> outputs/<run-B>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from promofinder.parse import to_seconds


def load_ranked(run_dir: str, include_alternates: bool = False) -> list[dict]:
    data = json.loads((Path(run_dir) / "promos_ranked.json").read_text(encoding="utf-8"))
    return data if include_alternates else [r for r in data if r["tier"] != "Alternate"]


def found(gold: dict, promo: dict) -> bool:
    for s in promo["segments"]:
        if s["episode"] != gold["episode"]:
            continue
        if "start" not in gold or "end" not in gold:
            return True
        g0, g1 = to_seconds(gold["start"]), to_seconds(gold["end"])
        ov = max(0.0, min(g1, s["end_s"]) - max(g0, s["start_s"]))
        if ov >= min(10.0, 0.5 * (g1 - g0)):
            return True
    return False


def eval_gold(run_dir: str, gold_path: str) -> None:
    ranked = load_ranked(run_dir)
    gold = json.loads(Path(gold_path).read_text(encoding="utf-8"))
    print(f"{len(gold)} reference moments · {len(ranked)} ranked promos in {run_dir}\n")
    for k in (5, 10, 20):
        top = ranked[:k]
        hits = sum(any(found(g, p) for p in top) for g in gold)
        print(f"  recall@{k:<2} {hits}/{len(gold)} = {hits / len(gold):.0%}")
    print("\nPer moment (best rank that covers it):")
    for g in gold:
        ranks = [p["rank"] for p in ranked if found(g, p)]
        where = f"#{min(ranks)}" if ranks else "MISSED"
        span = f" {g.get('start', '')}-{g.get('end', '')}" if "start" in g else ""
        print(f"  E{g['episode']:03d}{span:<20} {where:<7} {g.get('note', '')}")


def same_clip(a: dict, b: dict) -> bool:
    ov, la, lb = 0.0, a["duration_s"], b["duration_s"]
    for x in a["segments"]:
        for y in b["segments"]:
            if x["episode"] == y["episode"]:
                ov += max(0.0, min(x["end_s"], y["end_s"]) - max(x["start_s"], y["start_s"]))
    return ov >= 0.5 * min(la, lb)


def _ov(x: dict, y: dict) -> float:
    return max(0.0, min(x["end_s"], y["end_s"]) - max(x["start_s"], y["start_s"])) if x["episode"] == y["episode"] else 0.0


def same_moment(a: dict, b: dict, min_s: float = 3.0) -> bool:
    """Looser than same_clip: the two promos open on the same hook or cut on the
    same cliffhanger (their first or last segments overlap by >= 3s), even if they
    are trimmed or paired differently."""
    return _ov(a["segments"][0], b["segments"][0]) >= min_s or _ov(a["segments"][-1], b["segments"][-1]) >= min_s


def eval_agree(run_a: str, run_b: str, k: int = 10) -> None:
    a = load_ranked(run_a)[:k]
    b_main, b_all = load_ranked(run_b), load_ranked(run_b, include_alternates=True)
    clip_k = sum(any(same_clip(x, y) for y in b_main[:k]) for x in a)
    clip_main = sum(any(same_clip(x, y) for y in b_main) for x in a)
    mom_main = sum(any(same_moment(x, y) for y in b_main) for x in a)
    mom_all = sum(any(same_moment(x, y) for y in b_all) for x in a)
    print(f"A's top {k}, and how many model B also found:")
    print(f"  same clip in B's top {k}:         {clip_k}/{k}")
    print(f"  same clip in B's ranked list:     {clip_main}/{k}")
    print(f"  same hook or cliffhanger moment in B's ranked list:      {mom_main}/{k}")
    print(f"  same hook or cliffhanger moment anywhere B tested:       {mom_all}/{k}")
    print("\n  A's pick -> best matching rank in B")
    for x in a:
        ranks = [y["rank"] for y in b_all if same_moment(x, y)]
        segs = " + ".join(f"E{s['episode']} {s['start']}" for s in x["segments"])
        quote = x["cliffhanger"]["quote"].strip('"“” ')[:55]
        print(f"  #{x['rank']:<3} {segs:<26} {('#' + str(min(ranks))) if ranks else 'not found':<10} {quote}")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "gold":
        eval_gold(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 4 and sys.argv[1] == "agree":
        eval_agree(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)
