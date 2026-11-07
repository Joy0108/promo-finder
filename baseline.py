"""The obvious approach, as a baseline (command line).

Paste the whole show into one prompt, ask for promos in the brief's own words,
then score the answer with the same code checks Promo Finder's clips must pass.
This is the "why not just ask ChatGPT?" experiment. The logic lives in
promofinder/baseline.py, which the web app uses too.

    python baseline.py "path/to/show" --model gpt-4.1          # one model call
    python baseline.py "path/to/show" --response answer.json   # re-check a saved answer, no call
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from promofinder.baseline import CHECKS, ask, bundle
from promofinder.llm import LLM, LLMConfig, LLMError
from promofinder.parse import load_show
from promofinder.pipeline import slug


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Naive one-prompt baseline, scored by Promo Finder's checks.")
    ap.add_argument("show_dir")
    ap.add_argument("--provider")
    ap.add_argument("--model")
    ap.add_argument("--out", default="outputs")
    ap.add_argument("--title", help="show title (default: folder name, same as run.py)")
    ap.add_argument("--response", help="check a saved JSON answer instead of calling the model")
    ap.add_argument("--offline", action="store_true", help="use only a cached answer; never call the model")
    args = ap.parse_args()

    show = Path(args.show_dir)
    eps = {e.ep: e for e in load_show(show)}
    files = [{"name": f.name, "text": f.read_text(encoding="utf-8-sig", errors="replace")}
             for f in sorted(show.glob("*.txt"))]
    cfg = LLMConfig.from_env(args.provider, args.model)
    cfg.offline = args.offline
    # the show title lets a results page pair this baseline with the right run
    meta = {"show": args.title or show.name, "model": cfg.label, "tokens_in": 0, "tokens_out": 0}

    if args.response:
        raw = json.loads(Path(args.response).read_text(encoding="utf-8"))
        meta["model"] = f"saved answer ({Path(args.response).name})"
    else:
        llm = LLM(cfg)
        try:
            raw = ask(files, llm)
        except LLMError as e:
            print(f"LLM error: {e}", file=sys.stderr)
            return 2
        u = llm.usage
        meta.update(tokens_in=u.prompt_tokens + u.cached_prompt_tokens,
                    tokens_out=u.completion_tokens + u.cached_completion_tokens)

    data = bundle(raw, files, eps, meta)
    out = Path(args.out) / f"baseline__{slug(meta['show'])}__{slug(cfg.provider + '-' + cfg.model)}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "baseline_response.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "baseline_check.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "baseline_check.md").write_text(data["report_md"], encoding="utf-8")
    s = data["summary"]
    print(f"{s['usable_as_is']} of {s['promos']} baseline promos pass every check.")
    for k, v in s["pass_counts"].items():
        print(f"  {CHECKS[k]:<62} {v}/{s['promos']}")
    print(f"Outputs: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
