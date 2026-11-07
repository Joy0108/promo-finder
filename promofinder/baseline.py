"""The obvious approach, as a baseline.

Paste the whole show into one prompt, ask for promos in the brief's own words,
then score the answer with the same code checks Promo Finder's clips must pass.
This is the "why not just ask ChatGPT?" experiment. Used by baseline.py (command
line) and by the web app.
"""
from __future__ import annotations

import re
import textwrap

from .llm import LLM
from .parse import Episode, clean_text, to_seconds

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

# The same checks in plain words, for people reading the report (the app uses these too).
PLAIN_RULES = {
    "Length is 20 to 60 seconds": ["duration_20_60s"],
    "Hook is in the first 3 seconds": ["hook_in_first_3s"],
    "Ends on its cliffhanger": ["cliffhanger_at_end"],
    "Never cuts into a spoken line": ["no_cut_mid_line"],
    "Cuts on a real line or shot": ["starts_on_line_or_shot", "ends_on_line_or_shot"],
    "Times are inside a real episode": ["episode_exists", "times_valid"],
}
PROBLEM_WORDS = {
    "hook_in_first_3s": "hook isn't in the first 3 seconds",
    "cliffhanger_at_end": "doesn't end on its cliffhanger",
    "no_cut_mid_line": "cuts into a spoken line",
    "starts_on_line_or_shot": "starts mid-shot",
    "ends_on_line_or_shot": "ends mid-shot",
    "episode_exists": "episode doesn't exist",
    "times_valid": "times are outside the episode",
}


def problems_in_words(row: dict) -> str:
    """What went wrong with one baseline promo, in plain words."""
    out = ["just the whole episode"] if row.get("whole_episode") else []
    for key, ok in row["checks"].items():
        if ok:
            continue
        if key == "duration_20_60s":
            length = row.get("length_s")
            out.append(f"too long ({length:.0f}s)" if length and length > 60
                       else f"too short ({length:.0f}s)" if length else "wrong length")
        else:
            out.append(PROBLEM_WORDS.get(key, key))
    return "; ".join(out) or "nothing, usable as is"


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
        length = _length(p)
        rows.append({"rank": p.get("rank"), "segments": segs, "hook": p.get("hook", ""),
                     "cliffhanger": p.get("cliffhanger", ""), "checks": checks,
                     "passes_all": all(checks.values()),
                     "length_s": round(length, 2) if length is not None else None,
                     "whole_episode": _whole_episode(p, eps)})
        rows[-1]["problems"] = problems_in_words(rows[-1])
    n = len(rows)
    rates = {k: sum(r["checks"][k] for r in rows) for k in CHECKS}
    usable = sum(r["passes_all"] for r in rows)
    lengths = [x for x in (_length(p) for p in promos) if x is not None]
    whole = sum(_whole_episode(p, eps) for p in promos)
    avg_len = round(sum(lengths) / len(lengths), 1) if lengths else None
    summary = {"promos": n, "usable_as_is": usable, "pass_counts": rates, "avg_length_s": avg_len,
               "longest_s": max(lengths) if lengths else None, "whole_episode_picks": whole, **meta}
    # Written to read well as plain text (e.g. in Notepad) as well as Markdown.
    def section(title: str) -> list[str]:
        return ["", "", title, "-" * len(title)]

    longest = f"{summary['longest_s']:.0f}s" if summary["longest_s"] is not None else "n/a"
    L = [
        "BASELINE: THE WHOLE SHOW IN ONE PROMPT",
        "======================================",
        "",
        *textwrap.wrap(f"What this is: the obvious shortcut. Every episode script of the show was pasted into one "
                       f"prompt with the exact question from the brief. The answer was then checked with the same "
                       f"rules every Promo Finder clip has to pass.", 100),
        "",
        f"- Show:  {meta['show']}",
        f"- Model: {meta['model']} (one call, {meta['tokens_in']:,} tokens in, {meta['tokens_out']:,} out)",
        *section("RESULT"),
        f"{usable} of {n} promos are usable as they are.",
        "",
        f"- Average length: {avg_len:.0f}s (a promo should be 20 to 60 seconds). Longest: {longest}."
        if avg_len is not None else "- Average length: n/a",
        f"- {whole} of {n} are just a whole episode, from start to end.",
        *section(f"HOW MANY OF THE {n} PASS EACH RULE"),
    ] + [f"- {rule} {'.' * (36 - len(rule))} {sum(all(r['checks'][k] for k in keys) for r in rows)} of {n}"
         for rule, keys in PLAIN_RULES.items()] + [
        "",
        "Every clip Promo Finder shows has passed all of these.",
        *section(f"THE {n} PROMOS IT GAVE, AND WHAT WENT WRONG"),
    ]
    for r in rows:
        length = f" ({r['length_s']:.0f}s)" if r.get("length_s") is not None else ""
        L += ["", f"{r['rank']}. {r['segments']}{length}"]
        L += textwrap.wrap(f"Opens on: {r['hook']}", 100, initial_indent="   ", subsequent_indent="             ")
        L += textwrap.wrap(f"Problem:  {problems_in_words(r)}", 100, initial_indent="   ", subsequent_indent="             ")
    L += ["", "",
          *textwrap.wrap("Note: the hook and cliffhanger checks are lenient on purpose. A paraphrase counts if half "
                         "its key words, or a line it quotes, appear on screen at that point.", 100)]
    return {"summary": summary, "promos": rows}, "\n".join(L) + "\n"


def screenplay_text(files: list[dict]) -> str:
    """Every screenplay, one after another, in file-name order."""
    return "\n\n".join(f"===== FILE: {f['name']} =====\n{clean_text(f['text'])}"
                       for f in sorted(files, key=lambda f: f["name"]))


def ask(files: list[dict], llm: LLM) -> dict:
    """The single call: the brief's question plus the whole show."""
    return llm.json(SYSTEM, TASK + screenplay_text(files), stage="baseline")


def bundle(raw, files: list[dict], eps: dict[int, Episode], meta: dict) -> dict:
    """What the web app shows: the exact prompt, the answer, and how it scored."""
    promos = raw.get("promos", []) if isinstance(raw, dict) else []
    data, md = report(promos, eps, meta)
    return {
        "format": "promo-finder-baseline/1",
        "prompt": {"system": SYSTEM, "task": TASK.rstrip(),
                   "attached": f"then all {len(files)} screenplays, pasted in full "
                               f"({sum(len(clean_text(f['text'])) for f in files):,} characters)"},
        "rules": {rule: keys for rule, keys in PLAIN_RULES.items()},
        **data,
        "report_md": md,
        "response": raw,
    }
