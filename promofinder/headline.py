"""Stage 8: the on-screen headline for each ranked promo.

Many feed videos start with the sound off, so a short line of text at the top
of the frame has to carry the hook on its own. For every ranked promo the AI
writes three options (a curiosity line, a plain who-does-what line and a line
quoted from the clip) and names its favourite. Code checks each option against
five rules and keeps the ones that pass. If none pass, the AI gets one retry
with the exact failures. Last, a variety rule: two promos in the list never open
with the same two words, so the list doesn't read "Will she..." twenty times.

The ranking is already final here: a headline never changes a score."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from .critic import clip_blocks, render_for_viewer
from .llm import LLM, CacheMiss, LLMError
from .parse import Episode
from .prompts import HEADLINE_SYSTEM
from .rank import STOP

STYLES = ("curiosity", "situation", "line")
MAX_WORDS, MIN_WORDS, MAX_CHARS = 8, 3, 50
AFTER_CUT_S = 60.0  # how far past the cut we look for words that would give the answer away

CHECKS = {
    "short": "3 to 8 words, readable in two seconds",
    "no_clickbait": "No clickbait phrases, emojis, hashtags or shouting",
    "no_names": "No character names (strangers don't know them)",
    "no_spoiler": "Doesn't give away what happens after the cut",
    "from_the_clip": "About this clip (a quote must really be said in it)",
}

CLICKBAIT = re.compile(
    r"won'?t believe|wait for it|wait till|watch (till|until|to) the end|stay (till|until)|what happens next|"
    r"you'?ll never guess|never guess|no one expected|nobody expected|plot twist|twist ending|gone wrong|"
    r"must[- ]watch|shocking|jaw[- ]dropping|mind[- ]blowing|unbelievable|insane|crazy|omg|epic|goosebumps|"
    r"speechless|intense|gripping|dramatic|compelling|high stakes|part \d", re.I)
SYMBOLS = re.compile(r"[#@]|[^\x00-\x7f‘’“”—–…éÉ]")  # hashtags, handles, emojis
ROLE_WORDS = set("""lord lady duke duchess dowager father mother sister brother high priest constable matriarch
elder nobleman noblewoman judge king queen prince princess sir madam minor the old young police royal guard maid
butler doctor captain master mistress aunt uncle grand great former""".split())
RELATION_WORDS = set("""sister brother mother father husband wife fiance fiancee groom bride son daughter family
maid servant duke duchess lord lady stranger widow heir mistress lover rival friend enemy villain heroine soldier
beggar wedding altar""".split())


def _stem(w: str) -> str:
    w = w.lower().strip("'").removesuffix("'s")
    if w.endswith(("ss", "us", "is")):
        return w
    for suf in ("ing", "ed", "es", "s", "ly"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z']+", str(text).lower()) if len(w) > 2 and w not in STOP}


def _plain(text: str) -> str:
    """Lowercase words only, so a quote matches the script whatever its punctuation."""
    text = str(text).lower().replace("’", "").replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def character_names(bible: dict) -> set[str]:
    """Name words from the show bible (not the role words in them: 'Lady', 'Duke')."""
    names = set()
    for ch in bible.get("characters", []):
        for w in re.findall(r"[A-Za-z]+", str(ch.get("name", ""))):
            if len(w) > 2 and w.lower() not in ROLE_WORDS:
                names.add(w.lower())
    return names


def clip_context(cand: dict, eps: dict[int, Episode]) -> dict:
    """What the checks need: the clip's own words and lines, and the words that come after its cut."""
    clip = render_for_viewer(cand, eps)
    spoken = [b.text for seg in cand["segments"] for b in clip_blocks(seg, eps[seg["ep"]]) if b.is_dialogue]
    last = cand["segments"][-1]
    ep = eps[last["ep"]]
    after = [b.text for b in ep.blocks if last["end"] - 0.05 <= b.start < last["end"] + AFTER_CUT_S]
    nxt = eps.get(last["ep"] + 1)
    if nxt:  # the answer often comes at the start of the next episode
        after += [b.text for b in nxt.blocks if b.start < AFTER_CUT_S]
    v = cand.get("viewer") or {}  # what the cold viewers took from the clip counts as "about this clip" too
    about = " ".join([clip, cand["hook"].get("what_happens", ""), cand["cliff"].get("open_question", ""),
                      str(v.get("what_i_understood", "")), str(v.get("why_it_works", ""))])
    return {"clip": clip, "spoken": [_plain(s) for s in spoken], "about": _stems(about), "after": _stems(" ".join(after))}


def check_option(style: str, text: str, ctx: dict, names: set[str], allowed: set[str]) -> list[str]:
    """Return the keys of the checks this option fails (empty = it passes all five)."""
    failed = []
    words = re.findall(r"[A-Za-z0-9’']+", text)
    if not (MIN_WORDS <= len(words) <= MAX_WORDS) or len(text) > MAX_CHARS:
        failed.append("short")
    shouting = [w for w in words if len(w) >= 4 and w.isupper()]
    if CLICKBAIT.search(text) or SYMBOLS.search(text) or shouting or re.search(r"[!?]{2,}", text):
        failed.append("no_clickbait")
    if {w.lower().removesuffix("'s").removesuffix("’s") for w in words} & names:
        failed.append("no_names")
    spoilers = (_stems(text) & ctx["after"]) - ctx["about"] - allowed
    if spoilers:
        failed.append("no_spoiler")
    if style == "line":
        quote = _plain(text.strip(' "“”‘’'))
        if not quote or not any(quote in s for s in ctx["spoken"]):
            failed.append("from_the_clip")
    elif not (_stems(text) & ctx["about"]):
        failed.append("from_the_clip")
    return failed


def _user(cand: dict, eps: dict[int, Episode], bible: dict, clip: str) -> str:
    roster = [f"- {ch.get('name')}: {ch.get('role', '')}" for ch in bible.get("characters", [])
              if ch.get("name") and ch.get("name") != "Minor"]
    return "\n".join([
        f"SHOW: {bible.get('title', '')}",
        f"PREMISE: {bible.get('premise', '')}",
        "WHO IS WHO (use this only to say who people are to each other, as roles):",
        *roster,
        "",
        f"THE CLIP OPENS ON: {cand['hook'].get('what_happens', '')}",
        f"IT CUTS ON THE QUESTION: {cand['cliff'].get('open_question', '')}",
        "",
        f"CLIP ({cand['duration']:.0f} seconds):",
        clip,
    ])


def _best(raw) -> str:
    best = str(raw.get("best", "")).strip().lower() if isinstance(raw, dict) else ""
    return best if best in STYLES else ""


def _options(raw) -> list[dict]:
    out = []
    for o in (raw.get("options") if isinstance(raw, dict) else None) or []:
        if isinstance(o, dict) and str(o.get("text", "")).strip():
            style = str(o.get("style", "")).strip().lower()
            out.append({"style": style if style in STYLES else "curiosity", "text": str(o["text"]).strip().strip('"')})
    return out[:5]


def write_headline(cand: dict, eps: dict[int, Episode], bible: dict, llm: LLM, names: set[str],
                   allowed: set[str]) -> dict:
    ctx = clip_context(cand, eps)
    user = _user(cand, eps, bible, ctx["clip"])
    raw = llm.json(HEADLINE_SYSTEM, user, stage="headline", temperature=0.7)
    opts, best = _options(raw), _best(raw)
    for o in opts:
        o["failed"] = check_option(o["style"], o["text"], ctx, names, allowed)
    repaired = False
    if not any(not o["failed"] for o in opts):  # one retry, with exactly what broke
        problems = [f'"{o["text"]}" ({o["style"]}): ' + "; ".join(CHECKS[k].lower() for k in o["failed"]) for o in opts]
        fix = user + "\n\nEvery option you wrote broke a rule:\n- " + "\n- ".join(problems or ["no options returned"]) \
            + "\nWrite three new options that follow every rule."
        raw2 = llm.json(HEADLINE_SYSTEM, fix, stage="headline", temperature=0.7)
        more, best = _options(raw2), _best(raw2) or best
        for o in more:
            o["failed"] = check_option(o["style"], o["text"], ctx, names, allowed)
        seen = {o["text"].lower() for o in opts}
        opts += [o for o in more if o["text"].lower() not in seen]
        repaired = True
    for o in opts:
        o["passed"] = not o["failed"]
    return {"text": "", "style": "", "best": best, "options": opts, "repaired": repaired}


def _opening(text: str) -> str:
    return " ".join(re.findall(r"[a-z’']+", text.lower())[:2])


def pick_headlines(ranked: list[dict]) -> None:
    """In rank order, take the AI's favourite passing option, unless a higher-ranked
    promo already opens with the same two words; then take the next passing one."""
    used: set[str] = set()
    for c in ranked:
        h = c.get("headline")
        if not h:
            continue
        good = [o for o in h["options"] if o["passed"]]
        good.sort(key=lambda o: o["style"] != h.get("best"))  # favourite first, otherwise keep the order
        if not good:
            continue
        pick = next((o for o in good if _opening(o["text"]) not in used), good[0])
        used.add(_opening(pick["text"]))
        h["text"], h["style"] = pick["text"], pick["style"]


def add_headlines(ranked: list[dict], eps: dict[int, Episode], bible: dict, llm: LLM, workers: int = 6) -> dict:
    """Write a headline for every ranked promo, in parallel. Returns a short note
    for the run log; a promo whose call fails just has no headline."""
    names = character_names(bible)
    allowed = _stems(f"{bible.get('title', '')} {bible.get('premise', '')} {bible.get('title_promise', '')}") \
        | {_stem(w) for w in RELATION_WORDS}
    skipped = {"offline": 0, "error": 0}

    def one(c: dict) -> None:
        try:
            c["headline"] = write_headline(c, eps, bible, llm, names, allowed)
        except CacheMiss:
            skipped["offline"] += 1
        except (LLMError, ValueError, KeyError, TypeError):
            skipped["error"] += 1

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, ranked))
    pick_headlines(ranked)
    return {k: v for k, v in skipped.items() if v}
