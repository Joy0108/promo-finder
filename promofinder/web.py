"""The pipeline as small JSON steps, for the web app.

The browser drives a run one step at a time (show bible, one request per
episode, cross-episode pairs, assembly, one request per shortlisted clip,
ranking). Each request is short, so it fits comfortably inside a serverless
function's time limit, and the page can show real progress. Nothing is stored
on the server: every request carries what it needs, and code steps are
recomputed deterministically.

Keys: a visitor can paste their own API key (sent with each request, used for
that request only, never stored). The site owner's key, set as an environment
variable, is used only with the right ACCESS_CODE, so a public link cannot
spend it.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import tempfile
from pathlib import Path

from . import baseline as baseline_mod
from .beats import mark_beats
from .bible import build_bible
from .critic import cold_view
from .llm import LLM, PROVIDERS, LLMConfig
from .parse import load_texts, parse_text
from .pipeline import assemble, finish as finish_run, propose_cross, usage_dict
from .report import write_outputs

WEB_PROVIDERS = ["openai", "gemini", "groq", "openrouter", "anthropic"]
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
MAX_FILES = 300
MAX_CHARS = 4_000_000


def hosted() -> bool:
    """Treat every deployment as public. Only `python app.py` on your own machine
    sets PROMO_LOCAL=1, which lets the server's key work without an access code."""
    return os.getenv("PROMO_LOCAL") != "1"


class InputError(ValueError):
    """The request itself is wrong (HTTP 400)."""


class AuthError(PermissionError):
    """No usable key for this request (HTTP 401)."""


# ------------------------------------------------------------------ keys
def config() -> dict:
    """What the page needs to know before a run: is there a shared key, and does it need a code?"""
    cfg = LLMConfig.from_env()
    code = os.getenv("ACCESS_CODE", "")
    has_key = bool(cfg.api_key) and cfg.provider in WEB_PROVIDERS
    return {"server_key": has_key and (bool(code) or not hosted()), "code_required": bool(code),
            "provider": cfg.provider, "model": cfg.model, "providers": WEB_PROVIDERS, "hosted": hosted()}


def make_llm(headers, long_call: bool = False) -> LLM:
    provider = (headers.get("X-Provider") or "").strip().lower() or None
    model = (headers.get("X-Model") or "").strip() or None
    user_key = (headers.get("X-Api-Key") or "").strip()
    if provider and provider not in (WEB_PROVIDERS if hosted() else PROVIDERS):
        raise InputError(f"unknown provider '{provider}'")
    if model and not MODEL_RE.match(model):
        raise InputError("that model name doesn't look right")

    if user_key:  # the visitor's own key, with the provider's standard endpoint
        provider = provider or "openai"
        cfg = LLMConfig(provider=provider, model=model or "gpt-4.1", api_key=user_key,
                        base_url=PROVIDERS[provider]["base_url"])
    else:
        code = os.getenv("ACCESS_CODE", "")
        if code:
            if not hmac.compare_digest(code.encode(), (headers.get("X-Access-Code") or "").strip().encode()):
                raise AuthError("That access code isn't right. Check it, or use your own API key.")
        elif hosted():
            raise AuthError("This site has no shared key. Add your own API key in Settings.")
        cfg = LLMConfig.from_env(provider, model)
        if not cfg.api_key:
            raise AuthError(f"The server has no key for {cfg.provider}. Add your own API key in Settings.")

    cfg.cache_dir = os.getenv("PROMO_CACHE_DIR") or (None if hosted() else ".cache/llm")
    cfg.offline = os.getenv("PROMO_OFFLINE") == "1"
    # stay well inside the function time limit; the page retries a failed step itself
    cfg.timeout, cfg.max_retries, cfg.attempts = (270.0, 0, 1) if long_call else (120.0, 1, 2)
    return LLM(cfg)


# ------------------------------------------------------------------ input
def _files(body: dict, key: str = "files") -> list[dict]:
    files = body.get(key)
    if not isinstance(files, list) or not files:
        raise InputError("no screenplays in the request")
    if len(files) > MAX_FILES:
        raise InputError(f"too many files (the limit is {MAX_FILES})")
    out, total = [], 0
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str) or not isinstance(f.get("text"), str):
            raise InputError("each file needs a name and its text")
        total += len(f["text"])
        out.append({"name": Path(f["name"].replace("\\", "/")).name, "text": f["text"]})
    if total > MAX_CHARS:
        raise InputError("these scripts are too long for one run")
    return out


def _episodes(files: list[dict]):
    try:
        all_eps = load_texts(files)
    except ValueError as e:
        raise InputError(str(e)) from e
    return all_eps, {e.ep: e for e in all_eps}


def _title(body: dict) -> str:
    title = str(body.get("title") or "").strip()[:120]
    if not title:
        raise InputError("the show needs a title")
    return title


def _beats(body: dict) -> dict[int, dict]:
    raw = body.get("beats")
    if not isinstance(raw, dict):
        raise InputError("missing the marked moments")
    return dict(sorted((int(k), v) for k, v in raw.items()))


def _int(body: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(body.get(key, default))))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ steps
def check(body: dict) -> dict:
    """Parse only, no model calls: does the format read correctly?"""
    all_eps, _ = _episodes(_files(body))
    return {
        "episodes": [{"ep": e.ep, "source": e.source, "duration": e.duration, "blocks": len(e.blocks),
                      "dialogue": sum(b.is_dialogue for b in e.blocks), "warnings": e.warnings[:5],
                      "warning_count": len(e.warnings)} for e in all_eps],
        "minutes": round(sum(e.duration for e in all_eps) / 60, 1),
        "blocks": sum(len(e.blocks) for e in all_eps),
        "warnings": sum(len(e.warnings) for e in all_eps),
    }


def bible(body: dict, llm: LLM) -> dict:
    all_eps, _ = _episodes(_files(body))
    return {"bible": build_bible(_title(body), all_eps, llm), "usage": usage_dict(llm.usage)}


def moments(body: dict, llm: LLM) -> dict:
    f = _files({"files": [body.get("file")]})[0]
    if not isinstance(body.get("bible"), dict):
        raise InputError("missing the show bible")
    return {"beats": mark_beats(parse_text(f["text"], f["name"]), body["bible"], llm), "usage": usage_dict(llm.usage)}


def cross(body: dict, llm: LLM) -> dict:
    beats = _beats(body)
    out = propose_cross(beats, body.get("bible") or {}, llm) if len(beats) > 1 else {"pairs": []}
    return {"cross": out, "usage": usage_dict(llm.usage)}


def assemble_step(body: dict) -> dict:
    _, eps = _episodes(_files(body))
    found = assemble(eps, _beats(body), body.get("cross") or {"pairs": []}, _int(body, "critic_k", 40, 5, 80))
    return {"shortlist": found["shortlist"], "candidates_total": len(found["candidates"]),
            "valid": len(found["valid"]), "rejected": len(found["rejected"])}


def critic(body: dict, llm: LLM) -> dict:
    _, eps = _episodes(_files(body))
    cand = body.get("cand")
    if not isinstance(cand, dict) or not all(s.get("ep") in eps for s in cand.get("segments", [])):
        raise InputError("the clip's episodes are missing from the request")
    viewer = cold_view(cand, eps, llm, _int(body, "judges", 3, 1, 3))
    return {"id": cand["id"], "viewer": viewer, "usage": usage_dict(llm.usage)}


def finish(body: dict, llm: LLM) -> dict:
    files = _files(body)
    all_eps, eps = _episodes(files)
    beats = _beats(body)
    cross_out = body.get("cross") or {"pairs": []}
    critic_k = _int(body, "critic_k", 40, 5, 80)
    found = assemble(eps, beats, cross_out, critic_k)
    viewers = body.get("viewers") or {}
    for c in found["shortlist"]:
        v = viewers.get(c["id"])
        if isinstance(v, dict) and v.get("hook_id") == c["hook_id"] and v.get("cliff_id") == c["cliff_id"]:
            c["viewer"] = _viewer(v.get("viewer"))
    if not any(c.get("viewer") for c in found["shortlist"]):
        raise InputError("no clip came back from the cold-viewer test")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
    ctx = finish_run(_title(body), llm.cfg, all_eps, body.get("bible") or {}, beats, cross_out, found, llm,
                     top_n=_int(body, "top_n", 20, 5, 40), critic_k=critic_k, judges=_int(body, "judges", 3, 1, 3),
                     workers=8, seconds=float(body.get("seconds") or 0), usage_before=usage)
    with tempfile.TemporaryDirectory() as d:
        write_outputs(Path(d), ctx)
        results = json.loads((Path(d) / "results.json").read_text(encoding="utf-8"))
    return {"results": results}


def baseline(body: dict, llm: LLM) -> dict:
    files = _files(body)
    _, eps = _episodes(files)
    raw = baseline_mod.ask(files, llm)
    u = usage_dict(llm.usage)
    meta = {"show": _title(body), "model": llm.cfg.label, "tokens_in": u["tokens_in"], "tokens_out": u["tokens_out"]}
    return {"baseline": baseline_mod.bundle(raw, files, eps, meta), "usage": u}


def _viewer(v) -> dict:
    """A cold-viewer verdict sent back by the page, with the types the ranking expects."""
    if not isinstance(v, dict):
        raise InputError("bad cold-viewer result")
    out = dict(v)
    for k in ("hook_score", "cliffhanger_score", "standalone_score"):
        out[k] = max(1.0, min(10.0, float(v.get(k, 1))))
    out["confusing"] = [str(x) for x in v.get("confusing") or []]
    out["would_watch_next"] = bool(v.get("would_watch_next"))
    for k in ("what_i_understood", "first_3_seconds", "open_question", "why_it_works"):
        out[k] = str(v.get(k, ""))
    return out


# name -> (function, needs a model, one long call)
STEPS = {
    "check": (check, False, False),
    "bible": (bible, True, False),
    "moments": (moments, True, False),
    "cross": (cross, True, False),
    "assemble": (assemble_step, False, False),
    "critic": (critic, True, False),
    "finish": (finish, True, False),
    "baseline": (baseline, True, True),
}
