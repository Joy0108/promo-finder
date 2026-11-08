// Promo Finder: the page. Plain JavaScript, no build step.
// The browser drives a run one short request at a time (see promofinder/web.py).

// ------------------------------------------------------------------ helpers
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pad = (n, w = 2) => String(n).padStart(w, '0');
const tc = (s) => { s = Math.max(0, +s || 0); const m = Math.floor(s / 60); return `${pad(m)}:${(s - m * 60).toFixed(2).padStart(5, '0')}`; };
const clock = (ms) => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${pad(s % 60)}`; };
const n1 = (x) => (Math.round(x * 10) / 10).toFixed(1);
const n2 = (x) => (+x).toFixed(2);
const sum = (a) => a.reduce((x, y) => x + y, 0);
const mean = (a) => (a.length ? sum(a) / a.length : 0);
const slug = (t) => String(t).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60);
const unquote = (t) => String(t ?? '').trim().replace(/^["“”']+|["“”']+$/g, '').trim();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const store = (kind) => ({
  get(k, d) { try { const v = window[kind].getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { window[kind].setItem(k, JSON.stringify(v)); } catch { /* storage off: fine */ } },
});
const LS = store('localStorage');
const SS = store('sessionStorage');

const MODELS = {
  openai: ['gpt-4.1', 'gpt-4.1-mini', 'gpt-5-mini'],
  gemini: ['gemini-2.5-flash', 'gemini-3.6-flash', 'gemini-3.8-flash', 'gemini-3.5-flash-lite'],
  groq: ['llama-3.3-70b-versatile'],
  openrouter: ['openai/gpt-4.1', 'google/gemini-2.5-flash'],
  anthropic: ['claude-sonnet-5-5', 'claude-haiku-5-5'],
};
const PROVIDER_NAMES = { openai: 'OpenAI', gemini: 'Google Gemini', groq: 'Groq', openrouter: 'OpenRouter', anthropic: 'Anthropic' };
const CONCURRENCY = 6;
const TYPE_WORDS = { identity_reveal: 'identity reveal', power_reversal: 'power reversal' };
const typeWord = (t) => TYPE_WORDS[t] || String(t || '').replace(/_/g, ' ');

// ------------------------------------------------------------------ state
const state = {
  view: 'home',          // home | running | results | method
  config: null,
  files: [],             // [{name, text}]
  title: '',
  check: null,           // /api/check answer for the loaded files
  loadError: '',
  settings: {
    provider: 'openai', model: 'gpt-4.1', keyMode: 'server', apiKey: '', code: SS.get('pf:code', ''),
    baseline: true, judges: 3, critic_k: 40,
  },
  run: null,
  results: null,
  tab: 'promos',
  sel: null,
  showAlts: false,
};

// ------------------------------------------------------------------ server
async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    if (!r.ok) throw new Error();
    state.config = await r.json();
  } catch {
    state.config = { server_key: false, code_required: false, provider: 'openai', model: 'gpt-4.1', providers: Object.keys(MODELS), offline: true };
  }
  const c = state.config;
  state.settings.provider = c.provider && MODELS[c.provider] ? c.provider : 'openai';
  state.settings.model = c.model || 'gpt-4.1';
  state.settings.keyMode = c.server_key ? 'server' : 'own';
}

function authHeaders() {
  const s = state.settings;
  const h = { 'Content-Type': 'application/json', 'X-Provider': s.provider, 'X-Model': s.model.trim() };
  if (s.keyMode === 'own') h['X-Api-Key'] = s.apiKey.trim();
  else if (s.code) h['X-Access-Code'] = s.code.trim();
  return h;
}

// When the provider rate-limits the key (HTTP 429), slow the whole run down and
// wait, instead of failing: free and new keys often allow only a few calls a minute.
function slowDown() {
  if (state.run) state.run.limit = 2;
  const box = $('#run-error');
  if (box && !box.querySelector('.bad')) {
    box.innerHTML = '<div class="notice" style="margin-top:16px">The model provider is limiting how fast this key can go, so the run has slowed down and is retrying on its own. This can take a while on a free key.</div>';
  }
}

async function api(step, body, tries = 3) {
  let last = '';
  let waits = 0;
  for (let i = 0; i < tries + waits; i++) {
    let r;
    try {
      r = await fetch(`/api/${step}`, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) });
    } catch (e) {
      last = 'Network error: the server could not be reached.';
      await sleep(1500 * (i + 1));
      continue;
    }
    if (r.ok) return r.json();
    try { last = (await r.json()).error; } catch { last = `${r.status} ${r.statusText}`; }
    if (r.status === 504) last = 'The step took too long and timed out.';
    if (r.status === 429 && waits < 5 && !/daily quota|no credit|billing/i.test(last)) {
      waits++;
      slowDown();
      await sleep(Math.min(60000, 10000 * waits) + Math.random() * 3000);
      continue;
    }
    if (r.status === 429) break;
    if ([400, 401, 404, 413].includes(r.status)) break;
    await sleep(1500 * (i + 1));
  }
  throw new Error(last || `The ${step} step failed.`);
}

async function pool(items, n, fn, run) {
  let next = 0;
  const worker = async (w) => {
    while (next < items.length && !run.stop) {
      if (run.limit && w >= run.limit) return; // slowed down after a rate limit
      const k = next++;
      await fn(items[k], k);
    }
  };
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, (_, w) => worker(w)));
}

// ------------------------------------------------------------------ files
async function readUpload(fileList) {
  const out = [];
  let title = '';
  for (const f of [...fileList]) {
    if (/\.zip$/i.test(f.name)) {
      if (!window.JSZip) throw new Error('The zip reader did not load. Check your connection and reload the page.');
      const zip = await window.JSZip.loadAsync(f);
      const entries = Object.values(zip.files).filter((e) => !e.dir && /\.txt$/i.test(e.name) && !/(^|\/)(__MACOSX|\.)/.test(e.name));
      for (const e of entries) out.push({ name: e.name.split('/').pop(), text: await e.async('string') });
      const dirs = new Set(entries.map((e) => e.name.split('/').slice(0, -1).pop()).filter(Boolean));
      title = title || (dirs.size === 1 ? [...dirs][0] : f.name.replace(/\.zip$/i, ''));
    } else if (/\.txt$/i.test(f.name)) {
      out.push({ name: f.name, text: await f.text() });
    } else if (/\.json$/i.test(f.name)) {
      throw new Error('That looks like a results file. Use "Open a results file" below instead.');
    }
  }
  const seen = new Map();
  out.forEach((f) => seen.set(f.name, f));
  return { files: [...seen.values()].sort((a, b) => a.name.localeCompare(b.name)), title };
}

async function takeFiles(fileList) {
  state.loadError = '';
  state.check = null;
  renderHomeLoaded('Reading…');
  try {
    const { files, title } = await readUpload(fileList);
    if (!files.length) throw new Error('No .txt screenplays found. Upload the show\'s .zip, or the episode .txt files.');
    state.files = files;
    state.title = title || state.title;
    const r = await fetch('/api/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ files }) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || 'These files could not be read.');
    state.check = data;
  } catch (e) {
    state.loadError = e.message;
    state.files = [];
  }
  render();
}

async function openSaved(fileList) {
  let results = null;
  let baseline = null;
  try {
    for (const f of [...fileList]) {
      const data = JSON.parse(await f.text());
      if (data.format === 'promo-finder-results/1') results = data;
      else if (data.format === 'promo-finder-baseline/1') baseline = data;
      else if (data.summary && Array.isArray(data.promos) && data.promos[0]?.checks) baseline = data;
      else throw new Error(`${f.name} is not a Promo Finder results file.`);
    }
  } catch (e) {
    alert(e.message.startsWith('Unexpected') ? 'That file is not valid JSON.' : e.message);
    return;
  }
  if (!results && baseline && state.results) {
    state.results.baseline = baseline;
    state.tab = 'baseline';
    render();
    return;
  }
  if (!results) { alert('Choose a results file (results.json). You can add a baseline file with it.'); return; }
  if (baseline) results.baseline = baseline;
  showResults(results);
}

// ------------------------------------------------------------------ routing
function go(view) {
  state.view = view;
  render();
  window.scrollTo(0, 0);
}

function render() {
  const app = $('#app');
  if (state.view === 'home') app.innerHTML = homeView();
  else if (state.view === 'running') app.innerHTML = runningView();
  else if (state.view === 'results') app.innerHTML = resultsView();
  else if (state.view === 'method') app.innerHTML = methodView(true);
  bind();
}

// ------------------------------------------------------------------ home
function homeView() {
  return `
  <section class="home">
    <div>
      <p class="kicker">For promo editors</p>
      <h1>Find the promos hiding in a show's scripts.</h1>
      <p class="lede">Upload a show's episode scripts. Promo Finder finds the strongest hooks and cliffhangers, cuts them into 20 to 60 second clips, checks every cut, and hands you a ranked list that's ready for the edit, each promo with its own on-screen headline.</p>
      <ol class="how-mini">
        <li><span class="n">01</span><div><b>Upload the show</b> as a .zip of episode scripts.</div></li>
        <li><span class="n">02</span><div><b>AI picks the moments, code does the cutting.</b> Every clip is checked against nine rules, then judged by three cold viewers.</div></li>
        <li><span class="n">03</span><div><b>Review and export.</b> Exact in and out points, a checked headline for the top of the frame, the reasoning behind each pick, and a cut list for Premiere or Resolve.</div></li>
      </ol>
      <p class="open-saved">Already have results? <button class="linkish" type="button" id="open-btn">Open a results file</button>
        <input type="file" id="open-input" accept=".json,application/json" multiple hidden></p>
    </div>
    <div class="card new-run">
      <h2>New run</h2>
      <p class="faint small" style="margin:0">About a minute or two for a 50-episode show.</p>
      ${state.config?.offline ? '<div class="notice bad" style="margin-top:14px">The analysis server isn\'t reachable from this page. You can still open a saved results file.</div>' : ''}
      <label class="drop" id="drop" tabindex="0">
        <input type="file" id="file-input" accept=".zip,.txt" multiple hidden>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 15V4m0 0L7.5 8.5M12 4l4.5 4.5"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg>
        <span class="drop-title">Drop the show's .zip here</span>
        <span class="drop-sub">or click to choose · .zip or .txt files</span>
      </label>
      <div id="loaded">${loadedBlock()}</div>
      ${settingsBlock()}
      <label class="check-label"><input type="checkbox" id="opt-baseline" ${state.settings.baseline ? 'checked' : ''}>
        <span>Also run the "one big prompt" baseline for comparison</span></label>
      <button class="btn primary wide" type="button" id="go" ${canStart() ? '' : 'disabled'}>Find promos</button>
      <p class="fine">Scripts go only to the model provider you pick. Nothing is stored.</p>
    </div>
  </section>`;
}

function loadedBlock(msg) {
  if (msg) return `<div class="loaded"><span class="faint">${esc(msg)}</span></div>`;
  if (state.loadError) return `<div class="notice bad">${esc(state.loadError)}</div>`;
  if (!state.check) return '';
  const c = state.check;
  const warn = c.warnings ? `<span class="tag warn">${c.warnings} format warning${c.warnings > 1 ? 's' : ''}</span>` : '<span class="tag code">format OK</span>';
  return `
    <div class="loaded">
      <span class="count">${c.episodes.length}</span>
      <span>episodes · ${c.minutes} min · ${c.blocks.toLocaleString('en-US')} timed lines and shots</span>
      ${warn}
    </div>
    <label class="field"><span>Show title</span><input class="input" id="title" value="${esc(state.title)}" maxlength="120"></label>`;
}

function renderHomeLoaded(msg) {
  const el = $('#loaded');
  if (el) el.innerHTML = loadedBlock(msg);
}

function settingsBlock() {
  const s = state.settings;
  const c = state.config || {};
  const providers = (c.providers || Object.keys(MODELS)).filter((p) => MODELS[p]);
  const keyChoice = c.server_key ? `
      <div class="field"><span>Key</span>
        <div class="seg-ctl" role="group">
          <button type="button" data-key="server" aria-pressed="${s.keyMode === 'server'}">${c.code_required ? 'Access code' : "Site's key"}</button>
          <button type="button" data-key="own" aria-pressed="${s.keyMode === 'own'}">My own API key</button>
        </div></div>` : '';
  const keyField = s.keyMode === 'own'
    ? `<label class="field"><span>${esc(PROVIDER_NAMES[s.provider] || s.provider)} API key <span class="faint">(used for this run only, never stored)</span></span>
         <input class="input" id="api-key" type="password" autocomplete="off" value="${esc(s.apiKey)}" placeholder="Paste your key"></label>`
    : (c.code_required ? `<label class="field"><span>Access code</span><input class="input" id="code" type="password" autocomplete="off" value="${esc(s.code)}" placeholder="From the site owner"></label>` : '');
  return `
    <div class="settings-grid">
      <label class="field"><span>Provider</span>
        <select class="input" id="provider">${providers.map((p) => `<option value="${p}" ${p === s.provider ? 'selected' : ''}>${esc(PROVIDER_NAMES[p] || p)}</option>`).join('')}</select></label>
      <label class="field"><span>Model</span>
        <input class="input" id="model" list="model-list" value="${esc(s.model)}" spellcheck="false">
        <datalist id="model-list">${(MODELS[s.provider] || []).map((m) => `<option value="${m}">`).join('')}</datalist></label>
    </div>
    ${keyChoice}
    ${keyField}`;
}

function canStart() {
  const s = state.settings;
  if (!state.check || !state.files.length || state.config?.offline) return false;
  if (!s.model.trim()) return false;
  if (s.keyMode === 'own') return s.apiKey.trim().length > 8;
  return state.config?.server_key && (!state.config.code_required || s.code.trim().length > 0);
}

function refreshStart() {
  const b = $('#go');
  if (b) b.disabled = !canStart();
}

// ------------------------------------------------------------------ running
const STAGES = [
  ['check', 'Read the scripts', 'every line and shot, with its time'],
  ['bible', 'Understand the show', 'premise, the title\'s promise, who is who'],
  ['moments', 'Mark hooks and cliffhangers', 'one pass per episode, by line number'],
  ['cross', 'Pair moments across episodes', 'two-part promos'],
  ['assemble', 'Cut the clips and run the nine checks', 'code only'],
  ['critic', 'Cold-viewer panel', 'three strangers judge each clip'],
  ['finish', 'Rank and write the headlines', 'score, variety, on-screen text, cut list'],
];

function runningView() {
  const run = state.run;
  const stages = [...STAGES, ...(run.withBaseline ? [['baseline', 'Baseline: one big prompt', 'runs alongside, for comparison']] : [])];
  return `
  <section>
    <div class="run-head">
      <div><p class="kicker">Finding promos in</p><h1>${esc(run.title)}</h1></div>
      <div class="timer" id="timer">${clock(Date.now() - run.t0)}</div>
    </div>
    <div class="run-grid">
      <div>
        <ol class="stages">${stages.map(([k, label, what]) => `
          <li data-stage="${k}" class="${run.stages[k]?.state || ''}"><span class="dot"></span>
            <span>${label}<span class="what">${esc(run.stages[k]?.note || what)}</span></span>
            <span class="cnt" data-count="${k}">${esc(run.stages[k]?.count || '')}</span></li>`).join('')}
        </ol>
        <div id="run-error"></div>
      </div>
      <div>
        <div class="card panel"><h3>Episodes</h3><div class="cells" id="ep-cells">${run.eps.map((e) => cellHtml(e)).join('')}</div></div>
        <div class="card panel"><h3>Moments found</h3><ul class="feed" id="feed"><li><span class="faint">…</span><span class="faint">Hooks appear here as each episode is read.</span></li></ul></div>
        <div class="card panel" id="clip-panel" ${run.asm ? '' : 'hidden'}><h3>Clips with the cold viewers</h3><div class="cells" id="clip-cells">${clipCells()}</div></div>
      </div>
    </div>
  </section>`;
}

function cellHtml(ep) {
  const b = state.run.beats[ep];
  const pips = b ? `<span class="pips">${'<i></i>'.repeat(Math.min(4, b.hooks.length))}${'<i class="c"></i>'.repeat(Math.min(4, b.cliffhangers.length))}</span>` : '';
  return `<span class="cell ${b ? 'done' : ''}" data-ep="${ep}" title="Episode ${ep}">${ep}${pips}</span>`;
}

function clipCells() {
  const run = state.run;
  if (!run.asm) return '';
  return run.asm.shortlist.map((c, i) => `<span class="cell ${run.viewers[c.id] ? 'done' : ''}" data-clip="${c.id}" title="${esc(c.id)}">${i + 1}</span>`).join('');
}

function setStage(k, st, note, count) {
  const run = state.run;
  run.stages[k] = { ...(run.stages[k] || {}), state: st };
  if (note !== undefined) run.stages[k].note = note;
  if (count !== undefined) run.stages[k].count = count;
  const li = $(`.stages li[data-stage="${k}"]`);
  if (!li) return;
  li.className = st;
  if (note !== undefined) li.querySelector('.what').textContent = note;
  if (count !== undefined) li.querySelector('.cnt').textContent = count;
}

function feed(ep, h) {
  const el = $('#feed');
  if (!el) return;
  if (el.querySelector('.faint')) el.innerHTML = '';
  const li = document.createElement('li');
  li.innerHTML = `<span class="mono faint">E${pad(ep, 3)}</span><span><span class="q">“${esc(unquote(h.quote).slice(0, 110))}”</span> <span class="faint small">${esc(typeWord(h.type))} · ${h.score}/10</span></span>`;
  el.prepend(li);
  while (el.children.length > 6) el.lastChild.remove();
}

function addUsage(u) {
  if (!u) return;
  const t = state.run.usage;
  for (const k of Object.keys(t)) t[k] += u[k] || 0;
}

function startRun() {
  const s = state.settings;
  const files = state.files;
  const byName = new Map(files.map((f) => [f.name, f]));
  const run = {
    t0: Date.now(), title: state.title.trim() || 'Untitled show', files,
    eps: state.check.episodes.map((e) => e.ep),
    epFile: Object.fromEntries(state.check.episodes.map((e) => [e.ep, byName.get(e.source)])),
    bible: null, beats: {}, cross: null, asm: null, viewers: {}, results: null,
    usage: { calls: 0, cached_calls: 0, tokens_in: 0, tokens_out: 0 },
    withBaseline: s.baseline, baseline: null, baselineErr: '', baselineP: null,
    judges: s.judges, critic_k: s.critic_k, stages: {}, stop: false,
  };
  state.run = run;
  run.stages.check = { state: 'done', note: `${run.eps.length} episodes, ${state.check.minutes} min` };
  go('running');
  run.timer = setInterval(() => { const t = $('#timer'); if (t) t.textContent = clock(Date.now() - run.t0); }, 500);
  pipeline(run);
}

async function pipeline(run) {
  run.stop = false;
  $('#run-error').innerHTML = '';
  const body = { title: run.title, files: run.files };
  try {
    if (run.withBaseline && !run.baseline && !run.baselineP) {
      setStage('baseline', 'active', 'one call with all the scripts');
      run.baselineP = api('baseline', body, 2).then((r) => {
        run.baseline = r.baseline;
        addUsage(r.usage);
        const sm = r.baseline.summary;
        setStage('baseline', 'done', `${sm.usable_as_is} of ${sm.promos} of its promos are usable as they are`);
      }).catch((e) => {
        run.baselineErr = e.message;
        run.baselineP = null;
        setStage('baseline', 'failed', `Failed: ${e.message}`);
      });
    }
    if (!run.bible) {
      setStage('bible', 'active');
      const r = await api('bible', body);
      run.bible = r.bible;
      addUsage(r.usage);
      const n = (r.bible.characters || []).filter((c) => c.name !== 'Minor').length;
      setStage('bible', 'done', `${n} characters; ${Object.keys(r.bible.alias || {}).length} script labels mapped to them`);
    }

    const todo = run.eps.filter((e) => !run.beats[e]);
    setStage('moments', 'active', undefined, `${run.eps.length - todo.length}/${run.eps.length}`);
    let failed = null;
    await pool(todo, CONCURRENCY, async (ep) => {
      const cell = $(`[data-ep="${ep}"]`);
      cell?.classList.add('busy');
      try {
        const r = await api('moments', { file: run.epFile[ep], bible: run.bible });
        run.beats[ep] = r.beats;
        addUsage(r.usage);
        if (cell) cell.outerHTML = cellHtml(ep);
        const top = [...r.beats.hooks].sort((a, b) => b.score - a.score)[0];
        if (top) feed(ep, top);
        setStage('moments', 'active', undefined, `${Object.keys(run.beats).length}/${run.eps.length}`);
      } catch (e) {
        cell?.classList.remove('busy');
        cell?.classList.add('failed');
        failed = failed || e;
        run.stop = true;
      }
    }, run);
    if (failed) throw Object.assign(failed, { stage: 'moments' });
    const all = Object.values(run.beats);
    setStage('moments', 'done', `${sum(all.map((b) => b.hooks.length))} hooks and ${sum(all.map((b) => b.cliffhangers.length))} cliffhangers marked`);

    if (!run.cross) {
      setStage('cross', 'active');
      const r = await api('cross', { beats: run.beats, bible: run.bible }).catch((e) => { throw Object.assign(e, { stage: 'cross' }); });
      run.cross = r.cross;
      addUsage(r.usage);
      setStage('cross', 'done', `${run.cross.pairs.length} pairs proposed and checked`);
    }

    if (!run.asm) {
      setStage('assemble', 'active');
      run.asm = await api('assemble', { ...body, beats: run.beats, cross: run.cross, critic_k: run.critic_k })
        .catch((e) => { throw Object.assign(e, { stage: 'assemble' }); });
      setStage('assemble', 'done', `${run.asm.valid} of ${run.asm.candidates_total} clips passed all nine checks; best ${run.asm.shortlist.length} go to the panel`);
      $('#clip-panel').hidden = false;
      $('#clip-cells').innerHTML = clipCells();
    }

    const clips = run.asm.shortlist.filter((c) => !run.viewers[c.id]);
    setStage('critic', 'active', undefined, `${run.asm.shortlist.length - clips.length}/${run.asm.shortlist.length}`);
    failed = null;
    await pool(clips, CONCURRENCY, async (c) => {
      const cell = $(`[data-clip="${c.id}"]`);
      cell?.classList.add('busy');
      try {
        const need = [...new Set(c.segments.map((s) => s.ep))].map((e) => run.epFile[e]);
        const r = await api('critic', { files: need, cand: c, judges: run.judges });
        run.viewers[c.id] = { hook_id: c.hook_id, cliff_id: c.cliff_id, viewer: r.viewer };
        addUsage(r.usage);
        cell?.classList.remove('busy');
        cell?.classList.add('done');
        setStage('critic', 'active', undefined, `${Object.keys(run.viewers).length}/${run.asm.shortlist.length}`);
      } catch (e) {
        cell?.classList.remove('busy');
        cell?.classList.add('failed');
        failed = failed || e;
        run.stop = true;
      }
    }, run);
    if (failed) throw Object.assign(failed, { stage: 'critic' });
    setStage('critic', 'done', `${run.asm.shortlist.length} clips, ${run.judges} viewers each`);

    if (!run.results) {
      setStage('finish', 'active');
      const r = await api('finish', {
        ...body, bible: run.bible, beats: run.beats, cross: run.cross, viewers: run.viewers,
        critic_k: run.critic_k, top_n: 20, judges: run.judges, usage: run.usage, seconds: (Date.now() - run.t0) / 1000,
      }).catch((e) => { throw Object.assign(e, { stage: 'finish' }); });
      run.results = r.results;
    }
    if (run.baselineP && !run.baseline) {
      setStage('finish', 'active', 'done; waiting for the baseline to finish');
      await run.baselineP;
    }
    setStage('finish', 'done');
    clearInterval(run.timer);
    const res = run.results;
    res.baseline = run.baseline || (run.withBaseline ? { error: run.baselineErr || 'The baseline did not finish.' } : null);
    showResults(res);
  } catch (e) {
    const k = e.stage || Object.keys(run.stages).find((s) => run.stages[s].state === 'active') || 'bible';
    setStage(k, 'failed');
    $('#run-error').innerHTML = `
      <div class="notice bad" style="margin-top:16px">
        <p><b>That step failed.</b> ${esc(e.message)}</p>
        <div class="row"><button class="btn small" type="button" id="retry">Try again from here</button>
        <button class="btn small" type="button" id="back-home">Back</button></div>
      </div>`;
    $('#retry').onclick = () => pipeline(run);
    $('#back-home').onclick = () => { clearInterval(run.timer); go('home'); };
  }
}

// ------------------------------------------------------------------ results
function showResults(raw) {
  const promos = [...raw.promos].sort((a, b) => a.rank - b.rank);
  const R = {
    raw, run: raw.run, promos, main: promos.filter((p) => p.tier !== 'Alternate'),
    alts: promos.filter((p) => p.tier === 'Alternate'),
  };
  R.reason = Object.fromEntries((raw.plan?.why_post || []).map((x) => [x.promo_id, x.reason]));
  R.decKey = `pf:dec:${slug(raw.run.show)}:${raw.run.model}`;
  R.dec = LS.get(R.decKey, {});
  R.headKey = `${R.decKey}:headline`;
  R.heads = LS.get(R.headKey, {});
  Object.defineProperty(R, 'baseline', { get: () => raw.baseline || null, set: (v) => { raw.baseline = v; } });
  state.results = R;
  state.sel = R.main[0]?.id || null;
  state.tab = 'promos';
  go('results');
}

const TABS = [['promos', 'Ranked promos'], ['baseline', 'Baseline vs Promo Finder'], ['map', 'Show map'], ['checks', 'Quality checks'], ['method', 'How it works'], ['downloads', 'Downloads']];

function resultsView() {
  const R = state.results;
  const run = R.run;
  const tested = run.cold_viewer_tested ?? run.shortlisted;
  return `
  <section>
    <div class="res-head">
      <div>
        <p class="kicker">Results</p>
        <h1>${esc(run.show)}</h1>
        <p class="meta">${esc(run.provider)}/${esc(run.model)} · ${run.episodes} episodes · ${run.runtime_min} min of show · ${run.judges || 1} cold viewer${(run.judges || 1) > 1 ? 's' : ''} per clip · prompts ${esc(run.prompt_version)}</p>
      </div>
      <div class="row">
        <button class="btn" type="button" data-dl="results">Save results file</button>
        <button class="btn" type="button" data-go="home">New run</button>
      </div>
    </div>
    <div class="stats">
      <div class="stat"><b>${run.runtime_min} min</b><span>of episodes read</span></div>
      <div class="stat"><b>${run.candidates_total}</b><span>clips cut from hook and cliffhanger pairs</span></div>
      <div class="stat"><b>${run.candidates_valid}</b><span>passed all nine checks</span></div>
      <div class="stat"><b>${tested}</b><span>judged by ${run.judges || 1} cold viewer${(run.judges || 1) > 1 ? 's' : ''} each</span></div>
      <div class="stat"><b>${R.main.length}</b><span>ranked promos, ready to cut</span></div>
    </div>
    <div class="tabs" role="tablist">${TABS.map(([k, label]) => `<button type="button" role="tab" data-tab="${k}" aria-selected="${state.tab === k}">${label}</button>`).join('')}</div>
    <div id="tab">${tabHtml()}</div>
  </section>`;
}

function tabHtml() {
  const R = state.results;
  switch (state.tab) {
    case 'promos': return promosTab(R);
    case 'baseline': return baselineTab(R);
    case 'map': return mapTab(R);
    case 'checks': return checksTab(R);
    case 'method': return methodView(false);
    case 'downloads': return downloadsTab(R);
    default: return '';
  }
}

function setTab(k) {
  state.tab = k;
  $$('.tabs button').forEach((b) => b.setAttribute('aria-selected', b.dataset.tab === k));
  $('#tab').innerHTML = tabHtml();
  bindTab();
}

// ---- promos
function promosTab(R) {
  const item = (p) => {
    const d = R.dec[p.id];
    const s0 = p.segments[0];
    const segs = p.segments.length > 1 ? ` +${p.segments.length - 1}` : '';
    return `<button type="button" class="pitem" data-id="${p.id}" aria-current="${state.sel === p.id}">
      <span class="rk">${p.rank}</span>
      <span class="tc">E${s0.episode} ${s0.start}${segs} · ${Math.round(p.duration_s)}s ${d ? `<span class="dec ${d}">${d}</span>` : ''}</span>
      <span class="sc">${n2(p.score)}</span>
      <span class="q${headOf(R, p) ? ' hl' : ''}">${esc(headOf(R, p) || unquote(p.hook.quote))}</span></button>`;
  };
  const sel = R.promos.find((p) => p.id === state.sel) || R.main[0];
  return `
  <div class="promos">
    <div class="plist" id="plist">
      <div class="group">Ranked · ${R.main.length}</div>
      ${R.main.map(item).join('')}
      ${R.alts.length ? `<div class="group">Alternates · held back for variety</div>
        ${state.showAlts ? R.alts.map(item).join('') : `<div style="padding:12px 14px"><button class="linkish small" type="button" id="show-alts">Show ${R.alts.length} alternates</button></div>`}` : ''}
    </div>
    <article class="card detail" id="detail">${sel ? promoDetail(R, sel) : '<p class="empty">No promos in this run.</p>'}</article>
  </div>`;
}

function kindWords(p) {
  const n = p.segments.length;
  const eps = new Set(p.segments.map((s) => s.episode)).size;
  if (n === 1) return `One continuous clip, ${Math.round(p.duration_s)} seconds`;
  if (eps > 1) return `${n} pieces from ${eps} episodes, ${Math.round(p.duration_s)} seconds`;
  return `${n} pieces stitched, ${Math.round(p.duration_s)} seconds`;
}

const PENALTY_WORDS = {
  viewer_would_not_watch_next: 'viewers wouldn\'t watch next',
  confusing_points: 'too many confusing points',
  stitched: 'stitched clip',
  opens_on_recap: 'opens on a recap',
};
const CHECK_WORDS = {
  duration_20_60s: '20 to 60 seconds long', hook_in_first_3s: 'Hook lands in the first 3 seconds',
  ends_on_cliffhanger: 'Ends on the cliffhanger line', answer_not_shown: 'Doesn\'t show the answer',
  no_cut_mid_line: 'Never cuts inside a spoken line', no_cut_mid_sentence: 'Never cuts a sentence split across lines',
  timestamps_from_script: 'Every cut is a real time from the script', within_episode: 'Inside a real episode',
  segments_in_story_order: 'Pieces in story order',
};

const HEAD_STYLE = { curiosity: 'Question', situation: 'Who does what', line: 'Line from the clip' };
const headOf = (R, p) => R.heads[p.id] || p.headline || '';

function headBlock(R, p) {
  const opts = (p.headline_options || []).filter((o) => o.passed);
  const cur = headOf(R, p);
  if (!cur) return `<h2>${kindWords(p)}</h2>`;
  const style = opts.find((o) => o.text === cur)?.style;
  const others = opts.filter((o) => o.text !== cur);
  return `
    <h2 class="headline">${esc(cur)}</h2>
    <p class="dsub">${kindWords(p)} <span class="sep">·</span> On-screen text${style ? `, ${(HEAD_STYLE[style] || style).toLowerCase()}` : ''}
      <button class="linkish" type="button" data-copy="${esc(cur)}">Copy</button>${R.heads[p.id] ? ' <span class="faint">(your pick)</span>' : ''}</p>
    ${others.length ? `<div class="alts"><span class="faint small">Other options that passed the checks:</span>
      ${others.map((o) => `<button type="button" class="alt-head" data-head="${esc(o.text)}" title="${esc(HEAD_STYLE[o.style] || o.style)}: use this one instead">${esc(o.text)}</button>`).join('')}</div>` : ''}`;
}

function promoDetail(R, p) {
  const s = p.scores || {};
  const v = p.cold_viewer || {};
  const durs = R.run.episode_durations || {};
  const panel = v.panel || [];
  const pen = Object.entries(s.penalties || {});
  const vh = panel.length ? mean(panel.map((x) => x.hook)) : v.hook_score;
  const vc = panel.length ? mean(panel.map((x) => x.cliffhanger)) : v.cliffhanger_score;
  const flags = [];
  if (p.hook.visual) flags.push('<span class="tag warn">Visual hook: check the footage</span>');
  if (!p.setup_included) flags.push('<span class="tag warn">Cliffhanger set-up trimmed</span>');
  if (p.hook.recap) flags.push('<span class="tag warn">Opens on a recap</span>');
  if (p.hook.needs_context === 'heavy' || p.cliffhanger.needs_context === 'heavy') flags.push('<span class="tag warn">Needs context</span>');
  const meter = (cls, label, val, src) => `
    <div class="meter ${cls}"><span>${label}</span><span class="track"><span class="fill" style="width:${Math.max(0, Math.min(100, val * 10))}%"></span></span><span class="v">${n1(val)}</span>
    <span class="src">${src}</span></div>`;
  const formula = `0.30×${n1(s.hook)} + 0.30×${n1(s.cliffhanger)} + 0.25×${n1(s.standalone)} + 0.15×${s.premise_score ?? (s.premise ? 8 : 6)}`
    + (pen.length ? ` − ${n2(sum(pen.map(([, x]) => x)))}` : '') + ` = ${n2(s.score)}`;
  const decision = R.dec[p.id];
  return `
    <div class="dhead">
      <div>
        <p class="kicker">#${p.rank} · ${esc(p.tier)}${p.alternate_because ? ` · ${esc(p.alternate_because)}` : ''}</p>
        ${headBlock(R, p)}
      </div>
      <div class="score-big"><b>${n2(p.score)}</b><span>score out of 10</span></div>
    </div>
    <div class="segs">${p.segments.map((g) => {
      const d = +durs[g.episode] || Math.max(g.end_s, 1);
      return `<div class="seg"><span class="ep">Episode ${g.episode}</span>
        <div><span class="times">${g.start} → ${g.end}</span> <span class="faint small">${n1(g.length_s)}s</span>
        <div class="bar" title="Where this piece sits in episode ${g.episode}"><i style="left:${(g.start_s / d) * 100}%;width:${Math.max(0.8, ((g.end_s - g.start_s) / d) * 100)}%"></i></div></div></div>`;
    }).join('')}</div>
    ${p.stitch_story ? `<p class="muted small" style="margin-top:-8px">Stitched story: ${esc(p.stitch_story)}</p>` : ''}
    <div class="beat"><p class="lab"><b>Hook</b> · E${p.hook.episode} at ${p.hook.at} · ${esc(typeWord(p.hook.type))}${p.hook.visual ? ' · visual' : ''}</p>
      <p class="quote">“${esc(unquote(p.hook.quote))}”</p><p class="gloss">${esc(p.hook.what_happens)}</p></div>
    <div class="beat cliff"><p class="lab"><b>Cliffhanger</b> · E${p.cliffhanger.episode}, cuts at ${p.cliffhanger.at} · ${esc(typeWord(p.cliffhanger.type))}</p>
      <p class="quote">“${esc(unquote(p.cliffhanger.quote))}”</p><p class="gloss">${esc(p.cliffhanger.open_question)}</p></div>
    <div class="why"><b>Why it works:</b> ${esc(p.why_it_works)}${R.reason[p.id] ? `<br><b>When to post it:</b> ${esc(R.reason[p.id])}` : ''}</div>
    ${flags.length ? `<div class="flags" style="margin:-8px 0 20px">${flags.join('')}</div>` : ''}
    <div class="cols">
      <div>
        <p class="sec-title">How the score is made</p>
        ${meter('h', 'Hook', s.hook, `editor model ${p.hook.editor_score} · cold viewers ${n1(vh)} (average of the two)`)}
        ${meter('c', 'Cliffhanger', s.cliffhanger, `editor model ${p.cliffhanger.editor_score} · cold viewers ${n1(vc)} (average of the two)`)}
        ${meter('', 'Stands alone', s.standalone, 'cold viewers only: does it make sense to a stranger?')}
        ${meter('', 'Sells premise', s.premise_score ?? (s.premise ? 8 : 6), `6, plus 2 if the hook sells the show's promise (${p.hook.on_premise ? 'yes' : 'no'}), plus 2 if the cliffhanger does (${p.cliffhanger.on_premise ? 'yes' : 'no'})`)}
        <div class="formula" title="30% hook, 30% cliffhanger, 25% stands alone, 15% premise, minus penalties">${formula}</div>
        ${pen.length ? `<p class="small faint" style="margin-top:6px">Penalties: ${pen.map(([k, x]) => `${esc(PENALTY_WORDS[k] || k)} −${n2(x)}`).join(' · ')}</p>` : ''}
      </div>
      <div>
        <p class="sec-title">The cold viewers</p>
        ${panel.length ? `<table class="viewers"><thead><tr><th>Viewer</th><th>Hook</th><th>Cliff</th><th>Alone</th><th>Next ep?</th></tr></thead><tbody>
          ${panel.map((x) => `<tr><td>${esc(String(x.viewer).split(' (')[0])}<span class="plat">${esc((String(x.viewer).split(' (')[1] || '').replace(')', ''))}</span></td><td class="n">${x.hook}</td><td class="n">${x.cliffhanger}</td><td class="n">${x.standalone}</td><td>${x.would_watch_next ? 'yes' : 'no'}</td></tr>`).join('')}
          </tbody></table>` : ''}
        <p class="understood">What a stranger understood: <q>${esc(v.what_i_understood)}</q></p>
        ${(v.confusing || []).length ? `<p class="small muted">Confused by: ${v.confusing.map(esc).join('; ')}</p>` : ''}
      </div>
    </div>
    <p class="sec-title">Nine checks</p>
    <ul class="checklist">${Object.entries(p.checks || {}).map(([k, ok]) => `<li class="${ok ? 'ok' : 'no'}">${esc(CHECK_WORDS[k] || k)}</li>`).join('')}</ul>
    <p class="sec-title" style="margin-top:22px">Script of the clip</p>
    ${scriptHtml(p)}
    <div class="decide">
      <span class="small muted">Your call:</span>
      ${['use', 'maybe', 'reject'].map((d) => `<button type="button" class="btn small ${d}" data-dec="${d}" aria-pressed="${decision === d}">${d[0].toUpperCase() + d.slice(1)}</button>`).join('')}
      <span class="faint small">Saved in this browser. Export from Downloads.</span>
    </div>`;
}

function scriptHtml(p) {
  const lines = String(p.script || '').split('\n');
  const hookTag = `[E${pad(p.hook.episode, 3)} ${p.hook.at}]`;
  const key = (t) => unquote(t).toLowerCase().replace(/[^a-z0-9 ]/g, '').slice(0, 24);
  const ck = key(p.cliffhanger.quote);
  let cliffAt = -1;
  if (ck.length > 6) lines.forEach((l, i) => { if (l.toLowerCase().replace(/[^a-z0-9 ]/g, '').includes(ck)) cliffAt = i; });
  return `<pre class="script">${lines.map((l, i) => {
    if (l.trim() === '--- CUT ---') return '<span class="ln cut">— cut —</span>';
    const cls = l.startsWith(hookTag) ? 'hk' : (i === cliffAt ? 'ct' : '');
    return `<span class="ln ${cls}">${esc(l)}</span>`;
  }).join('')}</pre>`;
}

// ---- baseline
const TOOL_KEYS = {
  'Length is 20 to 60 seconds': ['duration_20_60s'],
  'Hook is in the first 3 seconds': ['hook_in_first_3s'],
  'Ends on its cliffhanger': ['ends_on_cliffhanger'],
  'Never cuts into a spoken line': ['no_cut_mid_line'],
  'Cuts on a real line or shot': ['timestamps_from_script'],
  'Times are inside a real episode': ['within_episode'],
};

function toolStats(R) {
  const main = R.main;
  const durs = R.run.episode_durations || {};
  const whole = main.filter((p) => p.segments.some((g) => g.start_s <= 1 && g.end_s >= (+durs[g.episode] || 1e9) - 1)).length;
  const rules = Object.fromEntries(Object.entries(TOOL_KEYS).map(([rule, keys]) => [rule, main.filter((p) => keys.every((k) => p.checks?.[k])).length]));
  return {
    n: main.length, usable: main.filter((p) => Object.values(p.checks || {}).every(Boolean)).length,
    avg: mean(main.map((p) => p.duration_s)), whole, rules,
  };
}

function baselineTab(R) {
  const B = R.baseline;
  if (!B || B.error) {
    const canRun = state.files.length && state.check && slug(state.title) === slug(R.run.show);
    return `<div class="card empty">
      <h3>No baseline in this run</h3>
      <p>${B?.error ? `It failed: ${esc(B.error)}` : 'The "one big prompt" comparison wasn\'t run for these results.'}</p>
      <p class="small faint">${canRun ? '' : 'Start a new run with the baseline option ticked, or open a baseline file together with this results file.'}</p>
      ${canRun ? '<button class="btn primary" type="button" id="run-baseline">Run the baseline on this show</button>' : ''}
    </div>`;
  }
  const S = B.summary;
  const T = toolStats(R);
  const brows = Object.entries(B.rules || {}).map(([rule, keys]) => [rule, B.promos.filter((r) => keys.every((k) => r.checks[k])).length]);
  const bar = (cls, label, k, n) => `<div class="bline ${cls}"><span class="t" title="${label}"><i style="width:${n ? (k / n) * 100 : 0}%"></i></span><span>${k}/${n}</span></div>`;
  return `
  <div class="split">
    <div>
      <h2 style="margin-bottom:10px">Why not just ask ChatGPT?</h2>
      <p class="muted">The obvious shortcut: paste every script into one prompt and ask the brief's exact question. Below is that exact prompt, what came back, and how its answer scores against the same rules every Promo Finder clip has to pass.</p>
      <ul class="why-list muted">
        <li><b>It writes the timestamps itself, and nothing checks them.</b> Across ${S.tokens_in ? `${(+S.tokens_in).toLocaleString('en-US')} tokens of script` : 'a whole show of script'}, it can't keep count of seconds, so cuts land mid-line or mid-shot.</li>
        <li><b>It grabs whole episodes.</b> Each episode is written to open on a hook and end on a cliffhanger, so "the whole episode" looks like a perfect promo to it.</li>
        <li><b>It can't test itself.</b> Nobody asks a stranger whether the clip makes sense on its own.</li>
      </ul>
    </div>
    <div class="vs card" style="margin:0">
      <div><h3>One big prompt</h3><p class="sub">${esc(S.model || '')}</p>
        <dl>
          <dt>Usable as they are</dt><dd class="bad">${S.usable_as_is} of ${S.promos}</dd>
          <dt>Average length</dt><dd class="${S.avg_length_s > 60 ? 'bad' : ''}">${Math.round(S.avg_length_s ?? 0)}s</dd>
          <dt>Just a whole episode</dt><dd class="${S.whole_episode_picks ? 'bad' : ''}">${S.whole_episode_picks} of ${S.promos}</dd>
          <dt>Hook in the first 3 seconds</dt><dd>${S.pass_counts?.hook_in_first_3s ?? '–'} of ${S.promos}</dd>
        </dl></div>
      <div><h3>Promo Finder</h3><p class="sub">${esc(R.run.provider)}/${esc(R.run.model)}, same show</p>
        <dl>
          <dt>Usable as they are</dt><dd class="good">${T.usable} of ${T.n}</dd>
          <dt>Average length</dt><dd>${Math.round(T.avg)}s</dd>
          <dt>Just a whole episode</dt><dd>${T.whole} of ${T.n}</dd>
          <dt>Hook in the first 3 seconds</dt><dd>${T.rules['Hook is in the first 3 seconds']} of ${T.n}</dd>
        </dl></div>
    </div>
  </div>

  <div class="block">
    <h3>Rule by rule</h3>
    <div class="legend"><span><i style="background:var(--fg-3)"></i>One big prompt</span><span><i style="background:var(--fg)"></i>Promo Finder</span></div>
    <div class="bars">${brows.map(([rule, k]) => `
      <div class="brow"><span>${esc(rule)}</span><div class="pair">
        ${bar('', 'One big prompt', k, S.promos)}
        ${bar('pf', 'Promo Finder', T.rules[rule] ?? 0, T.n)}
      </div></div>`).join('')}</div>
  </div>

  <div class="block">
    <h3>The exact prompt</h3>
    <p class="small muted">System: ${esc(B.prompt?.system || '')}</p>
    <pre class="prompt-box">${esc(B.prompt?.task || '')}<span class="att">… ${esc(B.prompt?.attached || 'then every screenplay, pasted in full')}</span></pre>
  </div>

  <div class="block">
    <h3>The ${S.promos} promos it gave, and what went wrong</h3>
    <div class="table-wrap"><table class="table">
      <thead><tr><th>Rank</th><th>Clip it gave</th><th>Length</th><th>Opens on</th><th>What went wrong</th></tr></thead>
      <tbody>${B.promos.map((r) => `<tr><td>${esc(r.rank)}</td><td class="mono">${esc(r.segments)}</td><td class="mono">${r.length_s != null ? Math.round(r.length_s) + 's' : '–'}</td>
        <td>${esc(String(r.hook).slice(0, 140))}</td><td class="${r.passes_all ? '' : 'bad'}">${esc(r.problems || (r.passes_all ? 'nothing, usable as is' : 'fails a check'))}</td></tr>`).join('')}</tbody>
    </table></div>
    <p class="small faint" style="margin-top:8px">The hook and cliffhanger checks are lenient on purpose: a paraphrase counts if half its key words, or a line it quotes, appear on screen at that point.</p>
  </div>

  <div class="block">
    <h3>What Promo Finder does differently</h3>
    <div class="table-wrap"><table class="table">
      <thead><tr><th>The shortcut's problem</th><th>Promo Finder</th></tr></thead>
      <tbody>
        <tr><td>Makes up timestamps</td><td>The AI only points at line numbers. Code copies every time from the script, so a cut point can't be invented.</td></tr>
        <tr><td>Cuts mid-line and mid-shot</td><td>Code opens on the camera cut before the hook, never starts or ends inside a line, and finishes sentences split across lines.</td></tr>
        <tr><td>Whole episodes, ${Math.round(S.avg_length_s ?? 0)}-second "promos" on average</td><td>Every clip must be 20 to 60 seconds; a long hook-to-cliffhanger is stitched from two pieces instead.</td></tr>
        <tr><td>Hook buried after the first 3 seconds</td><td>A hard check: the hook must land within 3 seconds of the first frame.</td></tr>
        <tr><td>No one checks it makes sense</td><td>Three cold viewers who know nothing about the show must explain each clip and score it.</td></tr>
        <tr><td>One pass over ${S.tokens_in ? `${Math.round(S.tokens_in / 1000)},000 tokens` : 'the whole show'}</td><td>One focused pass per episode, run in parallel, plus a show bible so names stay consistent.</td></tr>
      </tbody>
    </table></div>
  </div>`;
}

// ---- show map
function mapTab(R) {
  const eps = R.raw.map || [];
  if (!eps.length) return '<p class="empty">No map in this file.</p>';
  const maxD = Math.max(...eps.map((e) => e.duration), 1);
  const W = 980; const L = 52; const Rp = 34; const T = 30; const rowH = 16;
  const H = T + eps.length * rowH + 14;
  const x = (t) => L + (t / maxD) * (W - L - Rp);
  const rowOf = Object.fromEntries(eps.map((e, i) => [e.ep, T + i * rowH + rowH / 2]));
  const grid = [];
  for (let t = 0; t <= maxD; t += 30) {
    grid.push(`<line class="grid" x1="${x(t)}" x2="${x(t)}" y1="${T - 6}" y2="${H - 10}"/><text class="axis" x="${x(t)}" y="${T - 12}" text-anchor="middle">${Math.floor(t / 60)}:${pad(t % 60)}</text>`);
  }
  const spans = R.main.flatMap((p) => p.segments.map((g, i) => {
    const y = rowOf[g.episode];
    if (y == null) return '';
    const label = i === 0 ? `<text class="rk" data-id="${p.id}" x="${x(g.start_s) - 3}" y="${y + 3.2}" text-anchor="end">${p.rank}</text>` : '';
    return `<rect class="span" data-id="${p.id}" x="${x(g.start_s)}" y="${y - 6}" width="${Math.max(2, x(g.end_s) - x(g.start_s))}" height="12" rx="2"><title>#${p.rank}: ${esc(unquote(p.hook.quote))} → ${esc(unquote(p.cliffhanger.quote))}</title></rect>${label}`;
  })).join('');
  const rows = eps.map((e) => {
    const y = rowOf[e.ep];
    return `<text class="eplab" x="${L - 18}" y="${y + 3.4}" text-anchor="end">E${pad(e.ep)}</text>
      <line class="eprow" x1="${x(0)}" x2="${x(e.duration)}" y1="${y}" y2="${y}"/>
      ${e.writer_marks.map((t) => `<line class="wmark" x1="${x(t)}" x2="${x(t)}" y1="${y - 5}" y2="${y + 5}"><title>Writers marked a cliffhanger at ${tc(t)}</title></line>`).join('')}
      ${e.hooks.map((h) => `<circle class="hook" cx="${x(h.t)}" cy="${y}" r="${2 + h.score / 4}"><title>Hook ${tc(h.t)} · ${esc(typeWord(h.type))} · ${h.score}/10: ${esc(h.quote)}</title></circle>`).join('')}
      ${e.cliffs.map((c) => { const r = 1.6 + c.score / 4.5; return `<rect class="cliff" x="${x(c.t) - r}" y="${y - r}" width="${2 * r}" height="${2 * r}" transform="rotate(45 ${x(c.t)} ${y})"><title>Cliffhanger ${tc(c.t)} · ${esc(typeWord(c.type))} · ${c.score}/10: ${esc(c.quote)}</title></rect>`; }).join('')}`;
  }).join('');
  const w = R.raw.writer || {};
  return `
    <div class="split" style="margin-bottom:16px">
      <div><h2 style="margin-bottom:8px">Where the drama is</h2>
        <p class="muted">Every episode as a row, start to end. Dots are the hooks the model marked and diamonds the cliffhangers (bigger means a higher score). Shaded bars are the ranked promos; click one to open it.</p></div>
      <div>${w.writer_marked_moments ? `<div class="notice ok"><b>Checked against the writers.</b> The scripts mark ${w.writer_marked_moments} cliffhangers themselves (the black ticks). The tool's cliffhangers land within ${w.tolerance_s} seconds of ${w.tool_cliffhanger_on_mark} of them${w.missed?.length ? ` (missed: ${w.missed.map(esc).join(', ')})` : ''}, and ${w.main_list_ending_on_mark} of the ${w.main_list} ranked promos end on one.</div>` : ''}</div>
    </div>
    <div class="legend">
      <span><svg width="12" height="12"><circle cx="6" cy="6" r="4" fill="var(--hook)"/></svg> Hook</span>
      <span><svg width="12" height="12"><rect x="2.5" y="2.5" width="7" height="7" transform="rotate(45 6 6)" fill="var(--cliff)"/></svg> Cliffhanger</span>
      <span><svg width="12" height="12"><line x1="6" x2="6" y1="1" y2="11" stroke="var(--fg)" stroke-width="1.6"/></svg> Writers' cliffhanger mark</span>
      <span><i style="background:var(--fg);opacity:.18;width:18px"></i>Ranked promo (number is its rank)</span>
    </div>
    <div class="card map map-wrap"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Map of hooks, cliffhangers and promos across the episodes">${grid.join('')}${spans}${rows}</svg></div>`;
}

// ---- checks
function headlineChecks(h) {
  if (!h) return '';
  if (!h.options) return '<h3 style="margin-top:24px">On-screen headlines</h3><p class="muted">None were written in this run.</p>';
  const fails = h.failed_by_check || {};
  return `<h3 style="margin-top:24px">On-screen headlines (AI writes, code checks)</h3>
    <p class="small muted">${h.with_headline} of ${h.promos} promos have a headline that passed all five checks. ${h.options} options written, ${h.passed} passed${h.retried ? `; ${h.retried} promo${h.retried > 1 ? 's' : ''} needed a retry` : ''}.</p>
    <ul class="rules">${Object.entries(h.checks || {}).map(([k, label]) => `<li><span>${esc(label)}</span><span>${fails[k] ? `dropped ${fails[k]}` : 'no failures'}</span></li>`).join('')}</ul>`;
}

function checksTab(R) {
  const q = R.raw.quality || {};
  const run = R.run;
  const rej = q.rejections || {};
  const w = R.raw.writer || {};
  const a = q.averages || {};
  return `
    <div class="funnel">
      <div><b>${q.candidates_total}</b><span>clips cut from every hook and later cliffhanger</span></div>
      <div><b>${q.candidates_valid}</b><span>passed all nine checks</span></div>
      <div><b>${run.cold_viewer_tested ?? run.shortlisted}</b><span>shortlisted and judged by the panel</span></div>
      <div><b>${R.main.length}</b><span>ranked, with variety rules</span></div>
    </div>
    <div class="split">
      <div class="block">
        <h3>The nine checks (code, on every clip)</h3>
        <p class="small muted">A clip that fails any of these is never shown to an editor. It is logged with the reason instead.</p>
        <ul class="rules">${Object.entries(q.checks || CHECK_WORDS).map(([k, label]) => `<li><span>${esc(label)}</span><span>${rej[k] ? `dropped ${rej[k]}` : 'no failures'}</span></li>`).join('')}</ul>
        ${headlineChecks(q.headlines)}
      </div>
      <div>
        <div class="block">
          <h3>Against the writers' own marks</h3>
          ${w.writer_marked_moments ? `<p>The screenwriters wrote "cliffhanger" or "cut to black" at ${w.writer_marked_moments} moments in ${w.episodes_with_marks} episodes. The tool chose a cliffhanger within ${w.tolerance_s} seconds of <b>${w.tool_cliffhanger_on_mark}</b> of them${w.missed?.length ? ` (missed ${w.missed.map(esc).join(', ')})` : ''}.</p>
            <p class="small faint">The model can read those words too, so this tests how precisely it cuts more than its taste.</p>` : '<p class="muted">This show\'s scripts carry no writer cliffhanger marks.</p>'}
        </div>
        <div class="block">
          <h3>The cold-viewer panel, on the ranked list</h3>
          <p>Average scores out of 10: hook <b>${a.hook}</b> · cliffhanger <b>${a.cliffhanger}</b> · stands alone <b>${a.standalone}</b>. Would watch the next episode: ${q.would_watch_next} of ${q.main_list}.</p>
          <p class="small faint">${q.judges || 1} viewer${(q.judges || 1) > 1 ? 's' : ''} per clip, averaged.</p>
        </div>
        <div class="block">
          <h3>What the model marked</h3>
          <p>${q.hooks} hooks and ${q.cliffhangers} cliffhangers; code repaired or dropped ${q.repaired} bad line numbers. Cross-episode pairs: ${q.cross?.pairs ?? 0} kept (${q.cross?.broke_rules_first_try ?? 0} broke the rules first time and were sent back). Format warnings: ${q.parse_warnings ?? 0}.</p>
        </div>
      </div>
    </div>
    <div class="split">
      <div class="block">
        <h3>Check these first</h3>
        <p class="small muted">Flags on the ranked list: things only a person, or the footage, can settle.</p>
        ${q.flags?.length ? `<ul class="rules">${q.flags.map((f) => `<li><span><button class="linkish" type="button" data-open="${f.id}">#${f.rank}</button> ${f.reasons.map(esc).join('; ')}</span><span></span></li>`).join('')}</ul>` : '<p class="muted">None.</p>'}
      </div>
      <div class="block">
        <h3>Where the editor model and the viewers disagree</h3>
        <p class="small muted">A gap of 3 points or more means one of the two judgements is off.</p>
        ${q.disagree?.length ? `<ul class="rules">${q.disagree.map((d) => `<li><span><button class="linkish" type="button" data-open="${d.id}">#${d.rank}</button> hook ${d.hook_editor} vs ${n1(d.hook_viewer)}, cliffhanger ${d.cliff_editor} vs ${n1(d.cliff_viewer)}</span><span></span></li>`).join('')}</ul>` : '<p class="muted">None.</p>'}
      </div>
    </div>`;
}

// ---- method
function methodView(standalone) {
  const back = standalone && state.results ? '<p><button class="linkish" type="button" data-go="results">← Back to the results</button></p>' : '';
  return `
  <section class="method">
    ${back}
    ${standalone ? '<p class="kicker">How it works</p><h1 style="margin-bottom:14px">The AI judges the drama. Code does everything that can be checked.</h1>' : '<h2 style="margin-bottom:10px">The AI judges the drama. Code does everything that can be checked.</h2>'}
    <p class="lede">A language model is good at feeling whether a moment would stop a stranger's thumb, and bad at counting seconds across 50 scripts. So it never writes a timestamp. It points at line numbers; code reads the real times, cuts the clips and tests them.</p>
    <ol class="steps">
      <li><div><h3>Read the scripts <span class="tag code">code</span></h3>
        <p>Every line of dialogue and every camera shot is stored with its exact start and end time. Every timestamp the tool ever outputs is copied from here, so a cut point can't be made up.</p></div></li>
      <li><div><h3>Understand the show <span class="tag ai">AI · 1 call</span></h3>
        <p>Reads every episode's logline and every speaker label once, and writes the premise, the promise the title makes, and who is who. Scripts call the same person different things ("the bearded stranger", later "Alexander"); this show bible merges them, so every later step uses the same names.</p></div></li>
      <li><div><h3>Mark hooks and cliffhangers <span class="tag ai">AI · 1 call per episode, in parallel</span></h3>
        <p>Reads one numbered episode and points at lines only: where a hook starts and peaks, where a cliffhanger is set up, where it cuts, and where (if anywhere) the episode answers it. Each moment gets a 1 to 10 score, a type (betrayal, reveal, threat…), and flags: does a stranger need context, does it sell the premise, is it a recap. Code then checks every line number, and if a hook peaks more than 3 seconds after it starts, the opening is moved to the peak.</p></div></li>
      <li><div><h3>Pair moments across episodes <span class="tag ai">AI · 1 call, plus one repair</span></h3>
        <p>Proposes two-part promos that join a hook from one episode to a cliffhanger from a later one. Code rejects any pair that breaks the rules and sends the errors back once.</p></div></li>
      <li><div><h3>Cut the clips <span class="tag code">code</span></h3>
        <p>Every hook is paired with every later cliffhanger in its episode, then cut by rule:</p>
        <ul><li>open on the camera cut just before the hook, if the hook still lands within 3 seconds;</li>
          <li>never start or end inside someone's line, and finish sentences that run across two lines;</li>
          <li>end on the cliffhanger line, hold up to 1.5 seconds on a silent reaction shot, and never show the answer;</li>
          <li>if hook to cliffhanger runs past 60 seconds, stitch the hook's own moment to the run-up to the cliffhanger.</li></ul></div></li>
      <li><div><h3>Nine hard checks <span class="tag code">code</span></h3>
        <p>20 to 60 seconds · hook in the first 3 seconds · ends on the cliffhanger · answer not shown · no cut inside a line · no cut inside a sentence split across lines · every cut is a real time from the script · inside a real episode · pieces in story order. A clip that fails any check is dropped and logged.</p></div></li>
      <li><div><h3>Shortlist <span class="tag code">code</span></h3>
        <p>A quick code score (the moments' scores, premise, how much context is needed, whether the set-up is included, a 25 to 45 second sweet spot) picks the 40 most promising clips, using each hook or cliffhanger at most twice and skipping near-duplicates.</p></div></li>
      <li><div><h3>Cold-viewer panel <span class="tag ai">AI · 3 calls per clip</span></h3>
        <p>Three different strangers see only the clip's text, timed from zero, with no show information and the raw on-screen names: a 28-year-old scrolling TikTok, a 45-year-old genre fan who has seen every twist, and an impatient 19-year-old on YouTube Shorts. Each says what they understood and scores the hook, the cliffhanger and whether it makes sense on its own. Scores are averaged, so one judge's mood can't move a clip far.</p></div></li>
      <li><div><h3>Rank <span class="tag code">code</span></h3>
        <p>The score below, then variety rules: one clip per hook and per cliffhanger, no overlapping clips, no replay of the same moment in the next episode (it compares the words of hooks and cliffhangers, ignoring character names), and at most two promos per episode.</p></div></li>
      <li><div><h3>Posting notes <span class="tag ai">AI · 1 call</span></h3>
        <p>One line per top clip on the job it does at that point in the schedule.</p></div></li>
      <li><div><h3>On-screen headline <span class="tag ai">AI · 1 call per promo</span> <span class="tag code">code checks</span></h3>
        <p>Many people scroll with the sound off, so every promo gets a short line of text for the top of the frame. The AI writes three options (a specific question, who does what to whom, and a line quoted from the clip) and names its favourite. Code keeps only the options that pass five checks: 3 to 8 words · no clickbait, emojis or hashtags · no character names, since strangers don't know them · no word that only appears after the cut, so it can't give the answer away · about this clip, and a quote must really be said in it. If none pass, the AI retries once with the failures. Two promos never open with the same two words. Headlines are written after ranking, so they never change a score.</p></div></li>
      <li><div><h3>Outputs <span class="tag code">code</span></h3>
        <p>The ranked list, a quality report, a spreadsheet, and an EDL cut list that opens in Premiere, Resolve or Avid with every promo already on the timeline.</p></div></li>
    </ol>
    <h3 style="margin-bottom:10px">The score</h3>
    <div class="card formula-card">
      <p class="eq">score = 0.30 × hook + 0.30 × cliffhanger + 0.25 × stands alone + 0.15 × premise − penalties</p>
      <ul class="why-list muted small" style="margin-top:10px">
        <li><b>Hook</b> and <b>cliffhanger</b>: the average of the editor model's score and the cold viewers' score.</li>
        <li><b>Stands alone</b>: the cold viewers' average. Does it make sense with zero context?</li>
        <li><b>Premise</b>: 6, plus 2 if the hook sells the show's promise, plus 2 if the cliffhanger does.</li>
        <li><b>Penalties</b>: viewers wouldn't watch next −0.75 · each confusing point beyond two −0.25 · stitched clip −0.2 · opens on a recap −0.3.</li>
      </ul>
      <p class="small faint" style="margin:8px 0 0">The weights are a starting point. Real click-through and 3-second retention should tune them; the editor's Use, Maybe and Reject calls are saved for that.</p>
    </div>
  </section>`;
}

// ---- downloads
function downloadsTab(R) {
  const f = R.raw.files || {};
  const items = [
    ['results', 'Results file', 'Open it on this site later to review this run again, with no model calls.', 'results.json'],
    ['edl', 'Cut list for Premiere / Resolve', 'Every ranked promo already cut, back to back. Relink reel E031 to episode 31.', 'promo_cuts.edl'],
    ['csv', 'Ranked promos spreadsheet', 'Opens in Excel or Google Sheets.', 'promos_ranked.csv'],
    ['report', 'Editor\'s report', 'Every promo with its timing, hook, cliffhanger, scores and script.', 'report.md'],
    ['quality', 'Quality report', 'Checks, failures, the writers\' marks and the cold-viewer averages.', 'quality.md'],
    ['decisions', 'Your Use / Maybe / Reject calls', `${Object.keys(R.dec).length} marked so far.`, 'decisions.csv'],
    ...(R.baseline && !R.baseline.error ? [['baseline', 'Baseline check', 'The one-big-prompt answer and how it scored.', 'baseline_check.md']] : []),
    ['zip', 'Everything', 'All of the above in one zip.', '.zip'],
  ];
  return `<div class="downloads">${items.map(([k, title, what, file]) => `
    <button type="button" class="card dl" data-dl="${k}" ${(['edl', 'csv', 'report', 'quality'].includes(k) && !f[{ edl: 'promo_cuts.edl', csv: 'promos_ranked.csv', report: 'report.md', quality: 'quality.md' }[k]]) ? 'disabled' : ''}>
      <b>${title}</b><span>${what}</span><code>${file}</code></button>`).join('')}</div>`;
}

function decisionsCsv(R) {
  const q = (t) => `"${String(t ?? '').replace(/"/g, '""')}"`;
  const rows = [['rank', 'id', 'decision', 'headline', 'clip', 'hook', 'cliffhanger', 'score']];
  for (const p of R.promos) {
    rows.push([p.rank, p.id, R.dec[p.id] || '', headOf(R, p), p.segments.map((g) => `E${g.episode} ${g.start}-${g.end}`).join(' + '), unquote(p.hook.quote), unquote(p.cliffhanger.quote), p.score]);
  }
  return '﻿' + rows.map((r) => r.map(q).join(',')).join('\r\n') + '\r\n';
}

function fileFor(R, k) {
  const base = `${slug(R.run.show)}__${slug(R.run.model)}`;
  const f = R.raw.files || {};
  switch (k) {
    case 'results': return [`${base}__results.json`, JSON.stringify(R.raw), 'application/json'];
    case 'edl': return ['promo_cuts.edl', f['promo_cuts.edl'], 'text/plain'];
    case 'csv': return ['promos_ranked.csv', '﻿' + (f['promos_ranked.csv'] || ''), 'text/csv'];
    case 'report': return ['report.md', f['report.md'], 'text/markdown'];
    case 'quality': return ['quality.md', f['quality.md'], 'text/markdown'];
    case 'decisions': return ['decisions.csv', decisionsCsv(R), 'text/csv'];
    case 'baseline': return ['baseline_check.md', R.baseline?.report_md || '', 'text/markdown'];
    default: return null;
  }
}

function save(name, data, type) {
  const blob = data instanceof Blob ? data : new Blob([data], { type: `${type};charset=utf-8` });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

async function download(k) {
  const R = state.results;
  if (k !== 'zip') {
    const out = fileFor(R, k);
    if (out && out[1]) save(...out);
    return;
  }
  if (!window.JSZip) { alert('The zip writer did not load. Download the files one by one.'); return; }
  const zip = new window.JSZip();
  for (const key of ['results', 'edl', 'csv', 'report', 'quality', 'decisions', 'baseline']) {
    const out = fileFor(R, key);
    if (out && out[1]) zip.file(out[0], out[1]);
  }
  save(`${slug(R.run.show)}__${slug(R.run.model)}.zip`, await zip.generateAsync({ type: 'blob' }));
}

// ------------------------------------------------------------------ events
function bind() {
  $$('[data-go]').forEach((b) => { b.onclick = () => go(b.dataset.go); });
  if (state.view === 'home') bindHome();
  if (state.view === 'results') {
    $$('.tabs button').forEach((b) => { b.onclick = () => setTab(b.dataset.tab); });
    $$('.res-head [data-dl]').forEach((b) => { b.onclick = () => download(b.dataset.dl); });
    bindTab();
  }
}

function bindHome() {
  const input = $('#file-input');
  const drop = $('#drop');
  input.onchange = () => input.files.length && takeFiles(input.files);
  drop.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove('over'); if (e.dataTransfer.files.length) takeFiles(e.dataTransfer.files); };
  const open = $('#open-input');
  $('#open-btn').onclick = () => open.click();
  open.onchange = () => open.files.length && openSaved(open.files);
  const s = state.settings;
  const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
  on('#title', 'input', (e) => { state.title = e.target.value; });
  on('#provider', 'change', (e) => {
    s.provider = e.target.value;
    s.model = (MODELS[s.provider] || [''])[0];
    rerenderSettings();
  });
  on('#model', 'input', (e) => { s.model = e.target.value; refreshStart(); });
  on('#api-key', 'input', (e) => { s.apiKey = e.target.value; refreshStart(); });
  on('#code', 'input', (e) => { s.code = e.target.value; SS.set('pf:code', s.code); refreshStart(); });
  on('#opt-baseline', 'change', (e) => { s.baseline = e.target.checked; });
  $$('[data-key]').forEach((b) => { b.onclick = () => { s.keyMode = b.dataset.key; rerenderSettings(); }; });
  on('#go', 'click', () => canStart() && startRun());
}

function rerenderSettings() {
  // re-render the form but keep what was typed
  const title = $('#title')?.value;
  render();
  if (title != null && $('#title')) $('#title').value = title;
}

function bindTab() {
  const R = state.results;
  if (state.tab === 'promos') {
    $$('.pitem').forEach((b) => { b.onclick = () => selectPromo(b.dataset.id); });
    const alts = $('#show-alts');
    if (alts) alts.onclick = () => { state.showAlts = true; setTab('promos'); };
    bindDetail();
  }
  if (state.tab === 'map') $$('.map [data-id]').forEach((el) => { el.addEventListener('click', () => { state.sel = el.dataset.id; setTab('promos'); }); });
  if (state.tab === 'checks') $$('[data-open]').forEach((b) => { b.onclick = () => { state.sel = b.dataset.open; setTab('promos'); }; });
  if (state.tab === 'downloads') $$('#tab [data-dl]').forEach((b) => { b.onclick = () => download(b.dataset.dl); });
  if (state.tab === 'baseline') {
    const rb = $('#run-baseline');
    if (rb) rb.onclick = async () => {
      rb.disabled = true;
      rb.textContent = 'Running the baseline… about a minute';
      try {
        const r = await api('baseline', { title: R.run.show, files: state.files }, 2);
        R.baseline = r.baseline;
      } catch (e) { R.baseline = { error: e.message }; }
      setTab('baseline');
    };
  }
}

function bindDetail() {
  $$('[data-copy]').forEach((b) => {
    b.onclick = async () => {
      try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = 'Copied'; } catch { b.textContent = 'Select and copy'; }
      setTimeout(() => { b.textContent = 'Copy'; }, 1400);
    };
  });
  $$('[data-head]').forEach((b) => {
    b.onclick = () => {
      const R = state.results;
      const p = R.promos.find((x) => x.id === state.sel);
      if (b.dataset.head === p.headline) delete R.heads[p.id]; else R.heads[p.id] = b.dataset.head;
      LS.set(R.headKey, R.heads);
      const keep = $('#plist').scrollTop;
      setTab('promos');
      $('#plist').scrollTop = keep;
    };
  });
  $$('[data-dec]').forEach((b) => {
    b.onclick = () => {
      const R = state.results;
      const d = b.dataset.dec;
      if (R.dec[state.sel] === d) delete R.dec[state.sel]; else R.dec[state.sel] = d;
      LS.set(R.decKey, R.dec);
      const keep = $('#plist').scrollTop;
      setTab('promos');
      $('#plist').scrollTop = keep;
    };
  });
}

function selectPromo(id) {
  state.sel = id;
  $$('.pitem').forEach((b) => b.setAttribute('aria-current', b.dataset.id === id));
  const p = state.results.promos.find((x) => x.id === id);
  $('#detail').innerHTML = promoDetail(state.results, p);
  bindDetail();
  if (window.innerWidth < 960) $('#detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

document.addEventListener('keydown', (e) => {
  if (state.view !== 'results' || state.tab !== 'promos' || /input|textarea|select/i.test(e.target.tagName)) return;
  if (!['ArrowDown', 'ArrowUp', 'j', 'k'].includes(e.key)) return;
  const R = state.results;
  const list = state.showAlts ? R.promos : R.main;
  const i = list.findIndex((p) => p.id === state.sel);
  const j = Math.max(0, Math.min(list.length - 1, i + (e.key === 'ArrowDown' || e.key === 'j' ? 1 : -1)));
  if (j !== i) {
    e.preventDefault();
    selectPromo(list[j].id);
    $(`.pitem[data-id="${list[j].id}"]`)?.scrollIntoView({ block: 'nearest' });
  }
});

// ------------------------------------------------------------------ theme
const SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/></svg>';
const MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>';
const systemDark = window.matchMedia('(prefers-color-scheme: dark)');
const theme = () => document.documentElement.dataset.theme || (systemDark.matches ? 'dark' : 'light');
function paintToggle() {
  const b = $('#theme-toggle');
  const dark = theme() === 'dark';
  b.innerHTML = dark ? SUN : MOON;
  b.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
}
$('#theme-toggle').addEventListener('click', () => {
  const next = theme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('pf:theme', next); } catch { /* storage off: fine */ }
  paintToggle();
});
systemDark.addEventListener?.('change', paintToggle);
paintToggle();

$('#home-link').addEventListener('click', (e) => { e.preventDefault(); go(state.results ? 'results' : 'home'); });

// ------------------------------------------------------------------ start
await loadConfig();
render();
