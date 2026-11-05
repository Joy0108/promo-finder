"""The obvious approach, as a baseline.

Paste the whole show into one prompt, ask for promos in the brief's own words,
then score the answer with the same code checks Promo Finder's clips must pass.
This is the "why not just ask ChatGPT?" experiment.

    python baseline.py "path/to/show" --model gpt-4.1          # one API call
    python baseline.py "path/to/show" --response answer.json   # re-check a saved answer, no API
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from promofinder.llm import LLM, LLMConfig, LLMError
from promofinder.parse import Episode, fmt, load_show, to_seconds
from promofinder.pipeline import slug

SYSTEM = "You are a senior promo editor at a microdrama app. Answer in JSON only."

TASK = """A promo is a 20 to 60-second clip taken from the show itself and posted on Instagram, TikTok or YouTube to pull new viewers in. It's usually one continuous stretch, but it can also join a few moments, from one episode or across several. A good promo:
- opens on a hook: something in the first 3 seconds that makes a scrolling viewer stop, like a slap, a reveal or a shocking line
- ends on a cliffhanger: it cuts at an unresolved moment, so the viewer needs to know what happens next
- makes sense on its own to someone who has never seen the show

Find the promos across all episodes. For each one, give the episode and the start and end timestamps of every segment, in order, plus the hook, the cliffhanger and one line on why it works. Rank them, best first. List up to 20.

Return JSON only:
{"promos": [{"rank": 1, "segments": [{"episode": 1, "start": "MM:SS.ss", "end": "MM:SS.ss"}], "hook": "the exact line or action in the first 3 seconds", "cliffhanger": "the exact line or action it cuts on", "why": "one line"}]}

SCREENPLAYS:
"""

CHECKS = {
    "episode_exists": "segments point to real episodes",
    "times_valid": "times parse and sit inside the episode",
    "duration_20_60s": "total length is 20-60s",
    "starts_on_line_or_shot": "each cut-in lands on a real line or shot start (±0.15s)",
    "ends_on_line_or_shot": "each cut-out lands on a real line or shot end (±0.15s)",
    "no_cut_mid_line": "no cut falls inside a spoken line",
    "hook_in_first_3s": "the quoted hook really is in the script, in the first 3s",
    "cliffhanger_at_end": "the quoted cliffhanger really is in the script, at the cut",
}


STOP = set("""the and for with that this from into over was were are her his him she they them you your its
then than but not all any has had have will would can could just very what who why how when where about
as at by of on in to up out off our we me my it is be an or so do if""".split())


def _norm(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", str(text).lower()) if w not in STOP and len(w) > 2]


def _matches(claim: str, window_text: str) -> bool:
    """Lenient on purpose, to be fair to a model that paraphrases: the claim counts
    as found if any line it quotes, or half the key words of any sentence of it,
    appear in what is on screen in that time window."""
    target = set(_norm(window_text))
    quoted = re.findall(r"['\"“‘]([^'\"”’]{6,})['\"”’]", str(claim))
    for q in quoted:
        words = _norm(q)
        if words and sum(w in target for w in words) / len(words) >= 0.6:
            return True
    for part in re.split(r"[.!?;]\s+|\s+/\s+", str(claim)):
        words = _norm(part)
        if len(words) >= 3 and sum(w in target for w in words) / len(words) >= 0.5:
            return True
    return False


def check_promo(p: dict, eps: dict[int, Episode]) -> dict[str, bool]:
    c = {k: False for k in CHECKS}
    segs = []
    try:
        for s in p.get("segments", []):
            segs.append((int(s["episode"]), to_seconds(str(s["start"])), to_seconds(str(s["end"]))))
    except (KeyError, TypeError, ValueError):
        return c
    if not segs:
        return c
    c["episode_exists"] = all(e in eps for e, _, _ in segs)
    if not c["episode_exists"]:
        return c
    c["times_valid"] = all(0 <= a < b <= eps[e].duration + 0.05 for e, a, b in segs)
    if not c["times_valid"]:
        return c
    c["duration_20_60s"] = 20 - 0.05 <= sum(b - a for _, a, b in segs) <= 60 + 0.05
    c["starts_on_line_or_shot"] = all(any(abs(x.start - a) <= 0.15 for x in eps[e].blocks) for e, a, _ in segs)
    c["ends_on_line_or_shot"] = all(any(abs(x.end - b) <= 0.15 for x in eps[e].blocks) for e, _, b in segs)
    c["no_cut_mid_line"] = not any(
        d.is_dialogue and d.start < t - 0.05 and d.end > t + 0.05
        for e, a, b in segs for t in (a, b) for d in eps[e].blocks)
    e0, a0, _ = segs[0]
    first3 = " ".join(x.text for x in eps[e0].blocks if a0 - 0.5 <= x.start <= a0 + 3.0)
    c["hook_in_first_3s"] = _matches(p.get("hook", ""), first3)
    e1, _, b1 = segs[-1]
    last5 = " ".join(x.text for x in eps[e1].blocks if b1 - 5.0 <= x.end <= b1 + 0.5)
    c["cliffhanger_at_end"] = _matches(p.get("cliffhanger", ""), last5)
    return c


def _length(p: dict) -> float | None:
    try:
        return sum(to_seconds(str(s["end"])) - to_seconds(str(s["start"])) for s in p.get("segments", []))
    except (KeyError, TypeError, ValueError):
        return None


def _whole_episode(p: dict, eps: dict[int, Episode]) -> bool:
    try:
        return any(to_seconds(str(s["start"])) <= 1.0 and to_seconds(str(s["end"])) >= eps[int(s["episode"])].duration - 1.0
                   for s in p.get("segments", []))
    except (KeyError, TypeError, ValueError):
        return False


def report(promos: list[dict], eps: dict[int, Episode], meta: dict) -> tuple[dict, str]:
    rows = []
    for p in promos:
        checks = check_promo(p, eps)
        segs = " + ".join(f"E{s.get('episode')} {s.get('start')}-{s.get('end')}" for s in p.get("segments", []))
        rows.append({"rank": p.get("rank"), "segments": segs, "hook": p.get("hook", ""),
                     "cliffhanger": p.get("cliffhanger", ""), "checks": checks,
                     "passes_all": all(checks.values())})
    n = len(rows)
    rates = {k: sum(r["checks"][k] for r in rows) for k in CHECKS}
    usable = sum(r["passes_all"] for r in rows)
    lengths = [x for x in (_length(p) for p in promos) if x is not None]
    whole = sum(_whole_episode(p, eps) for p in promos)
    avg_len = round(sum(lengths) / len(lengths), 1) if lengths else None
    summary = {"promos": n, "usable_as_is": usable, "pass_counts": rates, "avg_length_s": avg_len,
               "longest_s": max(lengths) if lengths else None, "whole_episode_picks": whole, **meta}
    L = [
        "# Baseline: the whole show in one prompt",
        "",
        f"Model `{meta['model']}` · one call · {meta['tokens_in']:,} tokens in / {meta['tokens_out']:,} out · cost {meta['cost']}",
        "",
        f"**{usable} of {n} promos pass every check** (Promo Finder only shows clips that pass all of them).",
        f"Average length {avg_len}s (longest {summary['longest_s']}s); {whole} of {n} are an entire episode, start to end.",
        "",
        "The hook and cliffhanger checks are deliberately lenient: a paraphrase counts if half its key words, "
        "or a line it quotes, appear on screen in that window.",
        "",
        "| Check | Passed |",
        "|---|---|",
    ] + [f"| {CHECKS[k]} | {rates[k]}/{n} |" for k in CHECKS] + [
        "",
        "| # | Segments | Hook | Failed checks |",
        "|---|---|---|---|",
    ] + [f"| {r['rank']} | {r['segments']} | {str(r['hook'])[:60]} | "
         f"{', '.join(k for k, ok in r['checks'].items() if not ok) or 'none'} |" for r in rows]
    return {"summary": summary, "promos": rows}, "\n".join(L) + "\n"


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Naive one-prompt baseline, scored by Promo Finder's checks.")
    ap.add_argument("show_dir")
    ap.add_argument("--provider")
    ap.add_argument("--model")
    ap.add_argument("--out", default="outputs")
    ap.add_argument("--response", help="check a saved JSON answer instead of calling the API")
    ap.add_argument("--offline", action="store_true", help="use only a cached answer; never call the API")
    args = ap.parse_args()

    show = Path(args.show_dir)
    episodes = load_show(show)
    eps = {e.ep: e for e in episodes}
    cfg = LLMConfig.from_env(args.provider, args.model)
    cfg.offline = args.offline
    meta = {"model": cfg.label, "tokens_in": 0, "tokens_out": 0, "cost": "n/a"}

    if args.response:
        raw = json.loads(Path(args.response).read_text(encoding="utf-8"))
        meta["model"] = f"saved answer ({Path(args.response).name})"
    else:
        files = sorted(show.glob("*.txt"))
        text = "\n\n".join(f"===== FILE: {f.name} =====\n{f.read_text(encoding='utf-8-sig', errors='replace')}" for f in files)
        llm = LLM(cfg)
        try:
            raw = llm.json(SYSTEM, TASK + text, stage="baseline")
        except LLMError as e:
            print(f"LLM error: {e}", file=sys.stderr)
            return 2
        u = llm.usage
        meta.update(tokens_in=u.prompt_tokens + u.cached_prompt_tokens,
                    tokens_out=u.completion_tokens + u.cached_completion_tokens)
        cost = u.cost_usd(cfg.model, include_cached=True)
        meta["cost"] = f"${cost:.2f}" if cost is not None else "n/a"

    promos = raw.get("promos", []) if isinstance(raw, dict) else []
    data, md = report(promos, eps, meta)
    out = Path(args.out) / f"baseline__{slug(cfg.provider + '-' + cfg.model)}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "baseline_response.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "baseline_check.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "baseline_check.md").write_text(md, encoding="utf-8")
    s = data["summary"]
    print(f"{s['usable_as_is']} of {s['promos']} baseline promos pass every check.")
    for k, v in s["pass_counts"].items():
        print(f"  {CHECKS[k]:<62} {v}/{s['promos']}")
    print(f"Outputs: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
