# Promo Finder

Reads a microdrama's episode screenplays and returns **ranked, ready-to-cut promo clips** (20 to 60 seconds). Each one comes with:

- the exact segments (episode plus start and end time);
- the hook in the first 3 seconds and the cliffhanger it cuts on;
- a short on-screen headline, checked by code (no names, no spoilers, no clickbait);
- why it works, and when to post it.

It also gives an EDL cut list that opens in Premiere or Resolve with every promo already cut.

Built for the Kuku AI Generalist assignment, Problem 1. On the 50-episode input pack (103 minutes of show), one run takes about a minute or two.

## Use it in the browser

**Live app: [promo-finder-two.vercel.app](https://promo-finder-two.vercel.app/)**

The web app (`app.py` plus `public/`) runs the whole thing online. To use it:

1. Upload the show's `.zip`.
2. Watch each step run, with live progress.
3. Review the results across six tabs:
   - **Ranked promos**: each one with its headline (copy it, or swap in another option that passed), the scores explained, three cold viewers' verdicts and the script.
   - **Baseline vs Promo Finder**: the "one big prompt" shortcut, with its exact prompt and every one of its failures.
   - **Show map**, **Quality checks** and **How it works**.
   - **Downloads**.
4. Mark each promo Use, Maybe or Reject.

A finished run can be saved as a results file and opened again later with no model calls. The command line writes the same file (`results.json`).

Run it on your own machine:

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then put your API key in .env
python app.py                     # open http://localhost:5000
```

### Deploy it (Vercel, free)

1. Push this repo to GitHub. `.gitignore` keeps the screenplays, outputs and `.env` out.
2. On vercel.com, sign in with GitHub. Click **Add New → Project**, import the repo and click **Deploy**. No build settings are needed: Vercel finds the Flask app in `app.py`, and serves `public/` as the page.
3. To let people run it on your key: in **Settings → Environment Variables**, add these (or paste your `.env` file), then redeploy.

   | Variable | Value |
   |---|---|
   | `OPENAI_API_KEY` | your key |
   | `LLM_PROVIDER` | `openai` |
   | `LLM_MODEL` | `gpt-4.1` |

   Anyone with the link can then run it on your key, so set a monthly budget on the key in your OpenAI account. To limit your key to people you give a code to, also add `ACCESS_CODE`. Without any key on the server, visitors paste their own, which is sent with each request and never stored.

Kuku's screenplays are confidential, so the site ships with no show content and stores nothing. Each step is a short request (one per episode, one per clip), well inside Vercel's function time limit.

## Command line

```bash
python run.py "path/to/show folder"                 # writes outputs/<show>__<model>/
python run.py "path/to/show folder" --parse-only    # check the format, no model calls
python run.py "path/to/show folder" --episodes 1-10 # quick partial run
python run.py "path/to/show folder" --judges 1      # one cold viewer per clip instead of three
```

The show folder holds one `.txt` screenplay per episode, in Kuku's export format.

### Swap the model

Every model call goes through one OpenAI-compatible client, so the model is a setting, not code:

```bash
python run.py SHOW --model gpt-4.1-mini                         # OpenAI
python run.py SHOW --provider gemini --model gemini-2.5-flash   # Google
python run.py SHOW --provider groq --model llama-3.3-70b-versatile
python run.py SHOW --provider anthropic --model <claude-model>
python run.py SHOW --provider custom --base-url https://... --api-key ... --model ...   # any OpenAI-compatible API
```

The web app has the same choice. Answers are cached in `.cache/` on your machine:

- a re-run makes no new calls;
- a crashed run resumes where it stopped;
- `--offline` replays a run purely from the cache, and refuses rather than calling the model if something isn't there.

## How it works

```
screenplays ─► 1 PARSE (code) ──────────── every line and shot with its exact start/end time
            ─► 2 SHOW BIBLE (1 call)        premise, the title's promise, one name per character
            ─► 3 MOMENTS (1 call/episode)   hooks + cliffhangers, by LINE NUMBER only
            ─► 4 STITCHES (1 call)          hook in one episode + cliffhanger in a later one
            ─► 5 ASSEMBLE (code)            pairs → clips, cut points snapped to clean edits
            ─► 6 NINE HARD CHECKS (code)    reject anything that breaks a rule
            ─► 7 SHORTLIST (code)           best 40 by a quick code score, with variety
            ─► 8 COLD-VIEWER PANEL (3 calls/clip)  three strangers who see ONLY the clip
            ─► 9 RANK (code)                score + variety rules → ranked list
            ─► 10 POSTING NOTES (1 call)    why each top clip is posted where it is
            ─► 11 HEADLINES (1 call/promo)  on-screen text: 3 options, 5 code checks, never changes a score
```

**The rule: the model judges the drama; code does everything that can be checked.**

- **The model never writes a timestamp.** It picks line numbers, and code reads the times from the script, so a cut point cannot be invented.
- **Cut rules:**
  - open on the camera cut just before the hook, if the hook still lands within 3 seconds;
  - never start or end inside a spoken line, and finish sentences that run across two lines;
  - end on the cliffhanger line, then hold up to 1.5 seconds on a silent reaction shot, never showing the answer;
  - stitch two pieces when hook to cliffhanger runs past 60 seconds.
- **Nine hard checks** run on every clip:
  - 20 to 60 seconds long;
  - the hook lands in the first 3 seconds;
  - it ends on the cliffhanger line;
  - the answer is not shown;
  - no cut inside a line;
  - no cut inside a sentence split across lines;
  - every time is real (it exists in the script);
  - every piece is inside a real episode;
  - the pieces are in story order.
- **"Makes sense on its own" is measured.** Three different cold viewers each see only the clip's text, with no show information:
  - a 28-year-old scrolling TikTok;
  - a 45-year-old genre fan;
  - an impatient 19-year-old.

  Each must explain the clip and score the hook, the cliffhanger and whether it stands alone. Their scores are averaged.
- **Score** = 0.30 × hook + 0.30 × cliffhanger + 0.25 × stands alone + 0.15 × premise, minus small penalties (`promofinder/rank.py`).
  - Hook and cliffhanger each average the editor model's score with the viewers' score.
  - Variety rules then apply: one clip per story moment (including moments an episode replays from the previous one), no overlapping clips, and at most two clips per episode.

- **On-screen headline.** Many people scroll with the sound off, so each ranked promo gets a short line of text for the top of the frame (`promofinder/headline.py`). The model writes three options (a specific question, who does what to whom, a line quoted from the clip) and names its favourite. Code keeps only the options that pass five checks:
  - 3 to 8 words;
  - no clickbait phrases, emojis, hashtags or shouting;
  - no character names (strangers don't know them);
  - no word that only appears in the minute after the cut, so it can't give the answer away;
  - about this clip, and a quoted line must really be said in it.

  If none pass, the model retries once with the failures. Two promos in the list never open with the same two words.

## Outputs (`outputs/<show>__<model>/`)

| File | What it is |
|---|---|
| `results.json` | Everything the web app shows; open it there to review the run again |
| `report.md` | Editor's report: ranked list, posting order and reasons, full details and the script for each clip |
| `promos_ranked.csv` / `.json` | Every ranked promo: headline, segments, hook, cliffhanger, scores, the cold viewers' read |
| `promo_cuts.edl` | Cut list (CMX 3600). Import it into Premiere, Resolve or Avid and relink reel `E031` to episode 31's video; every promo is then pre-cut on the timeline |
| `quality.md` | Hard-check results, rejections, headline checks, the writer-intent check, model-vs-viewer disagreements, flags, known limits |
| `writer_intent_check.json` | Do the cuts land on the moments the screenwriters marked as cliffhangers? |
| `candidates_all.json` | Every clip assembled, with its checks and why any was rejected |
| `beats/E###.json` | Raw model output per episode, plus what code repaired or dropped |
| `bible.json`, `cross_episode_pairs.json`, `posting_plan.json`, `run.json` | Other stage outputs; settings, call and token counts |

## Checking quality

```bash
# 1) the obvious approach as a baseline: the whole show in one prompt, scored by the same checks
python baseline.py "path/to/show folder" --model gpt-4.1

# 2) do two different models find the same moments?
python evaluate.py agree outputs/<run-A> outputs/<run-B>

# 3) against an editor's own picks (made BEFORE looking at the output; template in gold/)
python evaluate.py gold outputs/<run> gold/gold.json
```

Results on the 50-episode pack (gpt-4.1):

| Check | Result |
|---|---|
| One-prompt baseline | 0 of 20 promos usable as they are: 99s average length, 13 of 20 were an entire episode, 7 of 20 hooked in the first 3s |
| Promo Finder hard checks | 282 of 289 clips pass all 9; the rest are dropped before an editor sees them. The top 20 average 35s |
| Writer intent | the cliffhangers land within 4s of 23 of the 24 moments the screenwriters marked |
| Model agreement | among the clips they tested on their own, gpt-4.1-mini found 8 and gpt-5-mini 9 of gpt-4.1's top 10 moments |
| Cold-viewer panel, top 20 | hook 7.1 · cliffhanger 7.9 · stands alone 6.7 out of 10 |
| Panel stability | swapping one single viewer for another moves the median clip about 5 places; dropping one viewer from the panel of three moves it 2, and keeps about 8 of the top 10 |

## Layout

```
app.py            web app (Flask): the API steps, and the page locally
public/           the page: index.html, app.js, styles.css
run.py            command line
baseline.py       the one-prompt baseline (command line)
evaluate.py       model agreement; recall against a reference set
promofinder/
  parse.py        screenplay parser (no model)
  llm.py          provider-agnostic client and cache
  prompts.py      every prompt, versioned
  bible.py        show bible + character label merging
  beats.py        per-episode hooks/cliffhangers, checked against the script
  assemble.py     clip assembly and cut-point rules
  validate.py     the nine hard checks
  critic.py       the cold-viewer panel
  rank.py         scoring and variety rules
  writer_check.py do cuts land on the writers' own cliffhanger marks?
  report.py       output files, including the EDL cut list and results.json
  baseline.py     the one-prompt baseline and its checker
  pipeline.py     the stages, end to end
  web.py          the same stages as small JSON steps, plus the key rules
```
