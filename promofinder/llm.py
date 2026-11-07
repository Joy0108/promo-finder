"""One thin, provider-agnostic LLM client.

Every provider below speaks the OpenAI chat-completions protocol, so swapping
the model is a config change (env var, CLI flag or the app's sidebar), never a
code change. Responses are cached on disk: re-running a show makes no new calls, and
a crashed run resumes where it stopped.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PROVIDERS: dict[str, dict] = {
    "openai":     {"base_url": None, "key_env": "OPENAI_API_KEY"},
    "gemini":     {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key_env": "GEMINI_API_KEY"},
    "groq":       {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY"},
    "anthropic":  {"base_url": "https://api.anthropic.com/v1/", "key_env": "ANTHROPIC_API_KEY"},
    "ollama":     {"base_url": "http://localhost:11434/v1", "key_env": None},
    "custom":     {"base_url": None, "key_env": "LLM_API_KEY"},
}

@dataclass
class LLMConfig:
    provider: str = "openai"
    model: str = "gpt-4.1-mini"
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.2
    cache_dir: str | None = ".cache/llm"
    offline: bool = False  # replay cached responses only; never call the API
    timeout: float = 240.0   # seconds per request
    max_retries: int = 6     # SDK-level retries on 429/5xx
    attempts: int = 5        # our retries on unparseable output or provider errors

    @classmethod
    def from_env(cls, provider: str | None = None, model: str | None = None,
                 api_key: str | None = None, base_url: str | None = None) -> "LLMConfig":
        provider = (provider or os.getenv("LLM_PROVIDER") or "openai").lower()
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider '{provider}'. Choose one of: {', '.join(PROVIDERS)}")
        spec = PROVIDERS[provider]
        key = api_key or os.getenv("LLM_API_KEY") or (os.getenv(spec["key_env"]) if spec["key_env"] else "ollama")
        url = base_url or os.getenv("LLM_BASE_URL") or spec["base_url"]
        return cls(provider=provider, model=model or os.getenv("LLM_MODEL") or "gpt-4.1-mini",
                   api_key=key, base_url=url or None)

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass
class Usage:
    calls: int = 0
    cached_calls: int = 0
    prompt_tokens: int = 0          # spent in this run
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0   # tokens behind the responses served from cache
    cached_completion_tokens: int = 0
    estimated_calls: int = 0        # cached entries whose original usage had to be estimated
    by_stage: dict = field(default_factory=dict)


class LLMError(RuntimeError):
    pass


class CacheMiss(LLMError):
    """Raised in offline mode when a response is not in the cache."""


class LLM:
    def __init__(self, cfg: LLMConfig):
        if not cfg.api_key and not cfg.offline:
            raise LLMError(f"no API key for provider '{cfg.provider}'. Set it in .env or pass --api-key.")
        from openai import OpenAI  # imported lazily so parsing works without the SDK

        self.cfg = cfg
        # the SDK retries 429/5xx itself, honouring the provider's retry-after headers
        self.client = OpenAI(api_key=cfg.api_key or "offline", base_url=cfg.base_url,
                             max_retries=cfg.max_retries, timeout=cfg.timeout)
        self.usage = Usage()
        self._lock = threading.Lock()
        self._supports_json_mode = True
        self._supports_temperature = True
        self.cache = Path(cfg.cache_dir) if cfg.cache_dir else None
        if self.cache:
            try:
                self.cache.mkdir(parents=True, exist_ok=True)
            except OSError:  # read-only file system (serverless): run without a cache
                self.cache = None

    # ------------------------------------------------------------------ public
    def json(self, system: str, user: str, *, stage: str = "misc", temperature: float | None = None) -> dict:
        """Call the model and return parsed JSON. Retries on API and parse errors."""
        temp = self.cfg.temperature if temperature is None else temperature
        key = hashlib.sha256(json.dumps([self.cfg.provider, self.cfg.model, system, user, temp]).encode()).hexdigest()
        hit = self._cache_get(key)
        if hit is not None:
            data, usage = hit
            if usage is None:  # entry from before usage was stored: estimate at ~4 chars/token
                usage = ((len(system) + len(user)) // 4, len(json.dumps(data)) // 4)
                with self._lock:
                    self.usage.estimated_calls += 1
            self._record(stage, 0, 0, cached=True, cached_usage=usage)
            return data
        if self.cfg.offline:
            raise CacheMiss(f"offline mode: no cached '{stage}' response for these inputs")

        last_err: Exception | None = None
        for attempt in range(self.cfg.attempts):
            try:
                text, pt, ct = self._call(system, user, temp)
                data = parse_json(text)
                self._record(stage, pt, ct)
                self._cache_put(key, data, (pt, ct))
                return data
            except LLMError as e:  # unparseable output: ask again
                last_err = e
            except Exception as e:  # noqa: BLE001 - provider errors vary by SDK/provider
                last_err = e
                msg = str(e).lower()
                if "response_format" in msg or "json_object" in msg:
                    self._supports_json_mode = False
                    continue
                if "temperature" in msg and ("unsupported" in msg or "not support" in msg):
                    self._supports_temperature = False
                    continue
                if any(s in msg for s in ("invalid api key", "incorrect api key", "401", "model_not_found", "does not exist")):
                    raise LLMError(f"{self.cfg.label}: {e}") from e
            if attempt + 1 < self.cfg.attempts:
                time.sleep(min(30, 2 ** attempt + 0.5))
        raise LLMError(f"{self.cfg.label} failed after retries: {last_err}")

    # ----------------------------------------------------------------- private
    def _call(self, system: str, user: str, temp: float) -> tuple[str, int, int]:
        kwargs: dict = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self._supports_temperature:
            kwargs["temperature"] = temp
        if self._supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self.client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content or ""
        u = getattr(resp, "usage", None)
        return text, getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0

    def _record(self, stage: str, pt: int, ct: int, cached: bool = False,
                cached_usage: tuple[int, int] | None = None) -> None:
        with self._lock:
            u = self.usage
            u.calls += 1
            u.cached_calls += int(cached)
            u.prompt_tokens += pt
            u.completion_tokens += ct
            if cached_usage:
                u.cached_prompt_tokens += cached_usage[0]
                u.cached_completion_tokens += cached_usage[1]
            s = u.by_stage.setdefault(stage, {"calls": 0, "cached": 0, "prompt_tokens": 0, "completion_tokens": 0})
            s["calls"] += 1
            s["cached"] += int(cached)
            s["prompt_tokens"] += pt + (cached_usage[0] if cached_usage else 0)
            s["completion_tokens"] += ct + (cached_usage[1] if cached_usage else 0)

    def _cache_get(self, key: str) -> tuple[object, tuple[int, int] | None] | None:
        if not self.cache:
            return None
        p = self.cache / f"{key}.json"
        if not p.exists():
            return None
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if isinstance(entry, dict) and entry.get("_v") == 2:
            return entry["data"], tuple(entry["usage"])
        return entry, None  # legacy entry: response only

    def _cache_put(self, key: str, data, usage: tuple[int, int]) -> None:
        if self.cache:
            entry = {"_v": 2, "model": self.cfg.label, "usage": list(usage), "data": data}
            (self.cache / f"{key}.json").write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")


def parse_json(text: str):
    """Parse model output as JSON, tolerating code fences and leading prose."""
    text = (text or "").strip()
    if not text:
        raise LLMError("empty response")
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start >= 0:
        for end in range(len(text), start, -1):
            if text[end - 1] in "}]":
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    continue
    raise LLMError(f"could not parse JSON from model output: {text[:200]}")
