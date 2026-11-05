"""Command line entry point.

    python run.py "path/to/show folder"                       # full run, model from .env
    python run.py "path/to/show folder" --model gpt-4.1        # swap the model
    python run.py "path/to/show folder" --provider gemini --model gemini-2.5-flash
    python run.py "path/to/show folder" --episodes 1-10        # quick partial run
    python run.py "path/to/show folder" --parse-only           # check the format, no API calls
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from promofinder.llm import LLMConfig, LLMError
from promofinder.parse import fmt, load_show
from promofinder.pipeline import run


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Find ranked promo clips in a microdrama screenplay folder.")
    ap.add_argument("show_dir", help="folder of episode .txt screenplays")
    ap.add_argument("--out", default="outputs", help="output root (default: outputs)")
    ap.add_argument("--provider", help="openai | gemini | groq | openrouter | anthropic | ollama | custom")
    ap.add_argument("--model", help="model name for the provider")
    ap.add_argument("--base-url", help="override the provider's endpoint (OpenAI-compatible)")
    ap.add_argument("--api-key", help="API key (default: from .env)")
    ap.add_argument("--title", help="show title (default: folder name)")
    ap.add_argument("--episodes", help="range like 1-10 (default: all)")
    ap.add_argument("--critic-k", type=int, default=40, help="clips sent to the cold-viewer test (default 40)")
    ap.add_argument("--top", type=int, default=20, help="size of the ranked main list (default 20)")
    ap.add_argument("--workers", type=int, default=6, help="parallel LLM calls (default 6)")
    ap.add_argument("--parse-only", action="store_true", help="parse and report the format; no LLM calls")
    ap.add_argument("--offline", action="store_true",
                    help="replay cached model responses only; never call the API (free re-ranking/re-reporting)")
    args = ap.parse_args()

    if args.parse_only:
        eps = load_show(args.show_dir)
        total = sum(e.duration for e in eps)
        print(f"{len(eps)} episodes, {sum(len(e.blocks) for e in eps)} timed blocks, {total / 60:.1f} min")
        for e in eps:
            kinds = {}
            for b in e.blocks:
                kinds[b.kind] = kinds.get(b.kind, 0) + 1
            warn = f"  WARNINGS: {len(e.warnings)}" if e.warnings else ""
            print(f"  E{e.ep:03d} {fmt(e.duration)}  {len(e.blocks):3d} blocks {kinds}{warn}")
            for w in e.warnings[:5]:
                print(f"      - {w}")
        return 0

    ep_range = None
    if args.episodes:
        a, _, b = args.episodes.partition("-")
        ep_range = (int(a), int(b or a))

    cfg = LLMConfig.from_env(args.provider, args.model, args.api_key, args.base_url)
    cfg.offline = args.offline
    print(f"Model: {cfg.label}" + ("  (offline: cache only, no API calls)" if cfg.offline else ""))

    def progress(stage: str, done: int, total: int) -> None:
        print(f"\r  {stage:<12} {done}/{total}   ", end="" if done < total else "\n", flush=True)

    try:
        res = run(args.show_dir, args.out, cfg, title=args.title, critic_k=args.critic_k, top_n=args.top,
                  workers=args.workers, episodes=ep_range, progress=progress)
    except LLMError as e:
        print(f"\nLLM error: {e}", file=sys.stderr)
        return 2
    r = res["run"]
    print(f"\nDone in {r['seconds']}s: {r['candidates_valid']} valid clips, {r['shortlisted']} cold-viewer tested.")
    print(f"LLM calls {r['llm_calls']} ({r['cached_calls']} cached) · tokens {r['tokens_in']:,} in / "
          f"{r['tokens_out']:,} out · cost {r['cost_display']}")
    print(f"Outputs: {res['out_dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
