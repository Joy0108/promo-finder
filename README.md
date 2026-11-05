# Promo Finder

Reads a microdrama's episode screenplays and returns **ranked, ready-to-cut promo clips** (20–60s). For each one it gives the exact segments (episode + start/end), the hook in the first 3 seconds, the cliffhanger it cuts on, why it works, and where to post it in the order.

Built for the Kuku AI Generalist assignment, Problem 1. On the 50-episode input pack (103 minutes of show), one run takes about a minute and costs about $0.80 with `gpt-4.1`.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then put your API key in .env

python run.py "path/to/show folder"            # writes outputs/<show>__<model>/
streamlit run app.py                           # editor app: browse results or run a new show
```

The show folder holds one `.txt` screenplay per episode, in Kuku's export format. Check that a new show parses cleanly without spending anything:

```bash
python run.py "path/to/show folder" --parse-only
```

## Swap the model

Every model call goes through one OpenAI-compatible client, so the model is a setting, not code:

```bash
python run.py SHOW --model gpt-4.1-mini                         # OpenAI
python run.py SHOW --provider gemini --model gemini-2.5-flash   # Google (free tier)
python run.py SHOW --provider groq --model llama-3.3-70b-versatile
python run.py SHOW --provider anthropic --model <claude-model>
python run.py SHOW --provider custom --base-url https://... --api-key ... --model ...   # any OpenAI-compatible API
```

The same choices are in the app's sidebar. Responses are cached in `.cache/`, so a re-run is free and a crashed run resumes. Add `--offline` to replay a run purely from the cache: it never calls the API, and it refuses rather than spends if something isn't cached.

## How it works

```
screenplays ─► 1 PARSE (code) ───────── every line with its exact start/end time
            ─► 2 SHOW BIBLE (1 LLM call) premise, the title's promise, one name per character
            ─► 3 MOMENTS (1 call/episode)  hooks + cliffhangers, by LINE NUMBER only
            ─► 4 STITCHES (1 call)         hook in one episode + cliffhanger in a later one
            ─► 5 ASSEMBLE (code)          pairs → clips, cut points snapped to clean edits
            ─► 6 HARD CHECKS (code)       reject anything that breaks a rule
            ─► 7 COLD VIEWER (1 call/clip) a model that sees ONLY the clip explains it
            ─► 8 RANK (code)              score + variety rules → ranked list
            ─► 9 POSTING NOTES (1 call)    why each top clip is posted where it is
```

**The rule: the model judges drama, code does everything that can be checked.**

- The model never writes a timestamp. It picks line numbers, and code reads the times from the script, so a cut point cannot be invented.
- Hard checks run on every clip:
  - 20–60s long
  - the hook lands in the first 3s
  - it ends on the cliffhanger line, and the answer is not shown
  - no cut inside a spoken line, or inside a sentence that runs across two lines
  - every timestamp exists in the script
  - segments are in story order
- "Makes sense on its own" is *measured*. A separate model call sees only the clip's text, with no show context and no character names beyond the on-screen labels. It has to say who's who, what the hook is and what's left unresolved, and it scores the clip.
- The ranking follows the scoring formula in `promofinder/rank.py`, then applies variety rules:
  - one clip per story moment, including moments an episode replays from the previous one
  - no overlapping clips
  - at most two clips per episode

## Outputs (`outputs/<show>__<model>/`)

| File | What it is |
|---|---|
| `report.md` | Editor's report: ranked list, posting order and reasons, full details and the script for each clip |
| `promos_ranked.csv` / `.json` | Every ranked promo: segments, hook, cliffhanger, scores, cold-viewer read |
| `promo_cuts.edl` | Cut list (CMX 3600). Import it into Premiere, Resolve or Avid and relink reel `E031` to episode 31's video; every promo is then pre-cut on the timeline |
| `quality.md` | Hard-check results, rejections, the writer-intent check, model-vs-viewer disagreements, flags, known limits |
| `writer_intent_check.json` | Do the cuts land on the moments the screenwriters marked as cliffhangers? |
| `candidates_all.json` | Every clip assembled, with its checks and why any was rejected |
| `beats/E###.json` | Raw model output per episode, plus what code repaired or dropped |
| `bible.json`, `cross_episode_pairs.json`, `posting_plan.json`, `run.json` | Other stage outputs; tokens, cost and settings |

## Checking quality

```bash
# 1) the obvious approach as a baseline: the whole show in one prompt, scored by the same checks
python baseline.py "path/to/show folder" --model gpt-4.1

# 2) do two different models find the same moments?
python evaluate.py agree outputs/<run-A> outputs/<run-B>

# 3) against an editor's own picks (made BEFORE looking at the output; template in gold/)
python evaluate.py gold outputs/<run> gold/gold.json
```

Results on the 50-episode pack:

| Check | Result |
|---|---|
| One-prompt baseline (gpt-4.1) | 0 of 20 promos usable as they are: 99s average length, 13 of 20 were an entire episode, 7 of 20 hooked in the first 3s |
| Promo Finder hard checks | 282 of 289 clips pass all 9; the rest are dropped before an editor sees them |
| Writer intent | the cliffhangers land within 4s of 23 of the 24 moments the screenwriters marked |
| Model agreement | gpt-5-mini and gpt-4.1-mini each independently found 8 of gpt-4.1's top 10 moments |
| Cold viewer, top 20 | hook 7.2 · cliffhanger 8.1 · stands alone 6.6 out of 10 |

The app also records an editor's Use / Maybe / Reject verdict on each clip (`feedback.json`), so the weights can be tuned against real decisions.

## Deploy (Streamlit Community Cloud, free)

1. Push this folder to a GitHub repo. `.gitignore` keeps the screenplays, outputs and `.env` out.
2. On share.streamlit.io, create an app from the repo with `app.py` as the main file.
3. Optional: under *Secrets*, add `OPENAI_API_KEY = "..."` and `APP_PASSWORD = "..."`, so reviewers can run without their own key.

Kuku's screenplays are confidential, so the hosted app ships with no show content. Upload a show's `.txt` files (or a results `.zip`) in the app.

## Layout

```
run.py            CLI
app.py            Streamlit editor app
baseline.py       the one-prompt baseline and its checker
evaluate.py       model agreement; recall against a reference set
promofinder/
  parse.py        screenplay parser (no LLM)
  llm.py          provider-agnostic client, cache, cost tracking
  prompts.py      every prompt, versioned
  bible.py        show bible + character label merging
  beats.py        per-episode hooks/cliffhangers, checked against the script
  assemble.py     clip assembly and cut-point rules
  validate.py     hard checks
  critic.py       cold-viewer test
  rank.py         scoring and variety rules
  writer_check.py do cuts land on the writers' own cliffhanger marks?
  report.py       output files, including the EDL cut list
  pipeline.py     orchestration
```
