const $ = id => document.getElementById(id);
// runs: the talk list. jobId: the talk on screen. current: its finished transcript. upload: a recording being sent.
let runs = [], jobId = null, shown = null, current = null, upload = null;
let pollTimer = null, runsTimer = null, pollFailures = 0, noticeTimer = null;
// Live updates cost server time (on a hosted site, a server kept awake), so they run only while you are here: they
// pause when the tab is hidden, after IDLE_MS without a click or key press, and after MAX_POLL_FAILURES failed tries
// in a row. Coming back, clicking or typing picks them up again.
const IDLE_MS = 30 * 60 * 1000, MAX_POLL_FAILURES = 6;
let lastActivity = Date.now(), napping = false, wasBusy = false;
// page: 'home', 'dict' or 'run'. view: the talk's step on screen. script: how its words are shown.
let page = 'home', view = 'fix', script = 'gu';
let renderedFor = null, sheetKey = null, summaryShown = null, caps = null, stageNotice = '', gujlishNotice = '', dockAction = null, dockAltAction = null;
let sel = null, touched = { gu: false, latin: false }, suggestTimer = null, suggestTicket = 0, noteTimer = null;
const STAGE_KEYS = ['gujlish', 'translation', 'captions', 'summary'];
const ACTIVE = ['queued', 'loading', 'transcribing'];
const STOPPED = 'Stopped. Start it again to continue where it left off.';  // web.server.STOPPED
const HOSTED = document.body.dataset.hosted === '1';
const CHUNK_BYTES = Number(document.body.dataset.chunkBytes) || 8 * 1024 * 1024;
const MAX_UPLOAD_MB = Number(document.body.dataset.maxUploadMb) || 4096;
const SERVER = HOSTED ? 'the server' : 'the local server';
const EDGE = /^[.,!?;:"'()[\]{}…।॥]+|[.,!?;:"'()[\]{}…।॥]+$/g;  // transcriber.speech.postprocess.EDGE_PUNCTUATION
const STEPS = [['input', 'Input'], ['transcribe', 'Transcribe'], ['fix', 'Fix'], ['translate', 'Translate'], ['summary', 'Summary']];
const DOWNLOADS = {
  transcript: [['txt', 'TXT'], ['docx', 'DOCX'], ['srt', 'SRT']],
  gujlish: [['gujlish-txt', 'TXT'], ['gujlish-docx', 'DOCX'], ['gujlish-srt', 'SRT']],
  translation: [['en-txt', 'TXT'], ['en-docx', 'DOCX'], ['en-srt', 'SRT']],
  summary: [['summary-md', 'MD'], ['summary-docx', 'DOCX']],
};
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
// A 401 means the site's password session ended: go back to the sign-in page.
const signedOut = response => { if (response.status === 401) { location.href = '/login'; return true; } return false; };
const time = seconds => `${Math.floor((seconds || 0) / 60)}:${String(Math.floor((seconds || 0) % 60)).padStart(2, '0')}`;
const minutes = seconds => !seconds ? '' : seconds < 60 ? `${Math.round(seconds)} s` : `${Math.round(seconds / 60)} min`;
const when = iso => iso ? new Date(iso).toLocaleString([], { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' }) : '';
const mb = bytes => `${(bytes / 1024 / 1024).toFixed(1)} MB`;
const running = state => Boolean(state) && (state.status === 'queued' || state.status === 'running');
const busyRun = run => ACTIVE.includes(run.status) || STAGE_KEYS.some(key => running(run[key]));
const words = text => (text || '').split(/\s+/).filter(Boolean);
const bare = text => words(text).map(word => word.replace(EDGE, '')).filter(Boolean).join(' ');
const wordKey = word => word.normalize('NFC').toLowerCase().replace(EDGE, '');
const postJson = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
const el = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = text; return node; };

// Whether live updates may ask the server again: the tab is on screen and you were active recently.
function awake() {
  if (document.hidden) return false;
  if (Date.now() - lastActivity < IDLE_MS) return true;
  if (!napping) { napping = true; $('run-notice').textContent = $('home-notice').textContent = 'Live updates paused while you were away. Click anywhere to refresh.'; }
  return false;
}
function later(fn, ms) { return awake() ? setTimeout(fn, ms) : null; }
async function loadUsage() {
  const box = $('gpu-usage');
  if (!box) return;
  try {
    const response = await fetch('/api/usage');
    if (!response.ok) return;
    const data = await response.json();
    if (!data.gpu) return;
    const used = data.seconds < 3600 ? `${Math.round(data.seconds / 60)} min` : `${(data.seconds / 3600).toFixed(1)} h`;
    box.textContent = ` · GPU this month: ${used}${data.cap_hours ? ` of ${data.cap_hours} h` : ''} · ≈ £${data.cost.toFixed(2)}`;
    box.classList.toggle('over', data.over_cap);
    box.hidden = false;
  } catch { /* the line is only informative */ }
}
function note(text) {
  clearTimeout(noteTimer); $('edit-note').textContent = text;
  noteTimer = setTimeout(() => { $('edit-note').textContent = ''; }, 6000);
}
function notice(text) {
  clearTimeout(noticeTimer); $('run-notice').textContent = text;
  if (text) noticeTimer = setTimeout(() => { $('run-notice').textContent = ''; }, 8000);
}
// A small popup with a few choices; resolves to the chosen value, or null if closed.
function ask(title, text, actions) {
  return new Promise(resolve => {
    const modal = $('modal');
    $('modal-title').textContent = title; $('modal-text').textContent = text;
    $('modal-actions').replaceChildren(...actions.map(({ label, value, kind }) => {
      const button = el('button', `btn ${kind || 'btn-outline'}`, label); button.type = 'button';
      button.onclick = () => { resolve(value); modal.close(); };
      return button;
    }));
    modal.onclose = () => resolve(null);
    modal.showModal();
    // Enter should never delete: focus the safe choice when the first one is destructive.
    const buttons = [...$('modal-actions').children];
    (buttons.find(button => !button.classList.contains('danger')) || buttons[0]).focus();
  });
}
async function copyText(button, text, label) {
  try { await navigator.clipboard.writeText(text || ''); button.textContent = 'Copied ✓'; }
  catch { button.textContent = 'Copy failed'; }
  setTimeout(() => { button.textContent = label; }, 2000);
}

// Summary text comes from a model, so it is rendered with DOM nodes (never innerHTML). A bold time such as **[04:44]**
// becomes a chip that opens that moment of the talk.
const seconds = stamp => stamp.split(':').reduce((total, part) => total * 60 + Number(part), 0);
function inline(parent, text) {
  for (const part of text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/)) {
    if (!part) continue;
    let node;
    const stamp = part.match(/^\*\*\[(\d{1,2}(?::\d{2}){1,2})\]\*\*$/);
    if (stamp) { node = el('button', 'time-chip', stamp[1]); node.type = 'button'; node.dataset.t = seconds(stamp[1]); node.title = 'Show this moment in the talk'; }
    else if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) node = el('strong', '', part.slice(2, -2));
    else if (part.length > 2 && part.startsWith('*') && part.endsWith('*')) node = el('em', '', part.slice(1, -1));
    else node = document.createTextNode(part);
    parent.append(node);
  }
}
function renderMarkdown(element, markdown) {
  element.replaceChildren();
  let list = null;
  for (const raw of markdown.split('\n')) {
    const line = raw.trimEnd();
    if (line.startsWith('- ')) {
      if (!list) { list = el('ul'); element.append(list); }
      const item = el('li'), text = el('span'); inline(text, line.slice(2).trim());
      item.append(...text.querySelectorAll('.time-chip'), text); list.append(item); continue;
    }
    list = null;
    const heading = line.match(/^(#{1,3}) (.*)$/);
    if (heading) { const h = el(`h${heading[1].length + 2}`); inline(h, heading[2]); element.append(h); }
    else if (line.startsWith('> ')) { const q = el('blockquote'); inline(q, line.slice(2)); element.append(q); }
    else if (/^\*[^*]+\*$/.test(line) && !element.querySelector('p')) element.append(el('p', 'oneline', line.slice(1, -1)));  // the one-line summary
    else if (line.trim()) { const p = el('p'); inline(p, line); element.append(p); }
  }
}

// A waveform drawn as bars, filled up to `percent`; null means waiting with no measure yet.
function drawWave(box, percent, stopped) {
  const count = 96;
  if (box.children.length !== count) {
    box.replaceChildren(...Array.from({ length: count }, (_, i) => {
      const bar = el('i'); bar.style.height = `${Math.round(10 + 36 * Math.abs(Math.sin(i * 0.41) * 0.75 + Math.sin(i * 1.9) * 0.25))}px`;
      return bar;
    }));
  }
  const done = percent == null ? 0 : Math.round(count * percent / 100);
  [...box.children].forEach((bar, i) => { bar.className = i < done ? 'done' : i === done && percent != null && !stopped && percent < 100 ? 'head' : ''; });
  box.classList.toggle('scanning', percent == null && !stopped);
  box.classList.toggle('stopped', Boolean(stopped));
}

// ---- Pages: home (drop area and talks), the dictionary, and one talk ----
function showPage(name) {
  page = name;
  $('home').hidden = name !== 'home'; $('dict-view').hidden = name !== 'dict'; $('run-view').hidden = name !== 'run';
  $('all-talks').hidden = name === 'home';
  if (name !== 'run') { closeExport(); $('export-btn').hidden = true; $('dock-wrap').hidden = true; }
  window.scrollTo(0, 0);
}
function openDictionary() {
  clearTimeout(pollTimer); closeEditor(); jobId = null; current = null; sessionStorage.removeItem('asr-job');
  showPage('dict'); loadDictionary();
}
$('open-dict').addEventListener('click', openDictionary);
$('all-talks').addEventListener('click', () => select(null));
$('home-link').addEventListener('click', e => { e.preventDefault(); select(null); });

// ---- Home: the talks with what each is doing now, and a card for the one being uploaded or transcribed ----
function runBadge(run) {
  const percent = run.progress != null ? ` ${run.progress}%` : '';
  if (run.status === 'queued') return ['Waiting', 'busy'];
  if (run.status === 'loading') return ['Starting', 'busy'];
  if (run.status === 'transcribing') return [`Transcribing${percent}`, 'busy'];
  if (run.status === 'paused') return ['Paused', 'paused'];
  if (run.status === 'error') return ['Stopped', 'error'];
  for (const [key, verb] of [['translation', 'Translating'], ['captions', 'Timing captions'], ['summary', 'Writing summary'], ['gujlish', 'Gujlish']]) {
    const state = run[key];
    if (running(state)) return [state.status === 'queued' ? `${verb} soon` : `${verb}${state.progress != null ? ` ${state.progress}%` : ''}`, 'busy'];
  }
  return null;
}
function runPill(run) {
  const badge = runBadge(run);
  if (badge) return badge;
  if (run.status !== 'complete') return [run.status, 'busy'];
  if (run.summary && run.summary.status === 'complete') return ['Summary ready', 'ready'];
  if (run.translation && run.translation.status === 'complete') return ['English ready', 'english'];
  return ['Ready to fix', 'fix'];
}
function talkRow(run) {
  const row = el('button', 'talk-row'); row.type = 'button';
  const text = el('span'); text.style.minWidth = '0';
  text.append(el('span', 'name', run.name), el('span', 'sub', [minutes(run.duration), when(run.started)].filter(Boolean).join(' · ')));
  const [label, kind] = runPill(run);
  row.append(text, el('span', `status-pill ${kind}`, label));
  row.onclick = () => select(run.id);
  return row;
}
function renderRuns() {
  const rows = runs.map(talkRow);
  if (!rows.length) rows.push(el('p', 'muted', 'No talks yet. Drop one above.'));
  $('history').replaceChildren(...rows);
  $('runs-count').textContent = runs.length || '';
  renderBusy();
}
// The card on the home page: the upload in progress, or else the talk being transcribed now.
function renderBusy() {
  const card = $('busy'), action = $('busy-action');
  const active = runs.find(run => run.status === 'transcribing') || runs.find(run => ACTIVE.includes(run.status));
  card.hidden = !upload && !active;
  if (card.hidden) return;
  if (upload) {
    $('busy-label').textContent = upload.error ? 'Upload failed' : upload.checking ? 'Checking the audio' : 'Uploading';
    $('busy-name').textContent = upload.name;
    $('busy-detail').textContent = upload.error || (HOSTED ? `${mb(upload.size)} · sending your recording to the server` : `${mb(upload.size)} · saving to the local workspace`);
    $('busy-percent').textContent = upload.error || upload.checking ? '' : `${upload.percent}%`;
    action.textContent = upload.error ? 'Dismiss' : 'Cancel upload'; action.hidden = false;
    card.dataset.id = '';
    drawWave($('busy-wave'), upload.checking ? null : upload.percent, Boolean(upload.error));
    return;
  }
  const label = { queued: 'Waiting', loading: 'Starting', transcribing: 'Transcribing' }[active.status];
  $('busy-label').textContent = label; $('busy-name').textContent = active.name;
  $('busy-detail').textContent = 'You can open other talks while this runs.';
  $('busy-percent').textContent = active.status === 'transcribing' && active.progress != null ? `${active.progress}%` : '';
  action.textContent = 'Pause'; action.hidden = false; card.dataset.id = active.id;
  drawWave($('busy-wave'), active.status === 'transcribing' ? active.progress : null, false);
}
$('busy').addEventListener('click', e => { if (!e.target.closest('button') && $('busy').dataset.id) select($('busy').dataset.id); });
$('busy-action').addEventListener('click', async () => {
  if (!upload) { const id = $('busy').dataset.id; if (id) act('pause', null, id); return; }
  if (!upload.error && !await ask('Cancel this upload?', `“${upload.name}” stops uploading and is not transcribed.`,
    [{ label: 'Cancel upload', value: true, kind: 'btn-accent danger' }, { label: 'Keep uploading', value: false }])) return;
  if (upload) upload.controller.abort();
  upload = null; renderBusy();
});
async function loadRuns() {
  clearTimeout(runsTimer);
  try {
    const response = await fetch('/api/history');
    if (signedOut(response)) return;
    if (response.ok) { runs = await response.json(); renderRuns(); }
  } catch { /* the server may be restarting: try again shortly */ }
  // Ask again only while something is in progress and you are here. When idle the list refreshes when you come back
  // to the page, so an open tab never keeps the hosted server awake.
  const isBusy = Boolean(upload) || runs.some(busyRun);
  if (wasBusy && !isBusy) loadUsage();  // a run just finished: its GPU time is counted now
  wasBusy = isBusy;
  if (isBusy) runsTimer = later(loadRuns, 3000);
}
function mergeRun(job) {
  const run = runs.find(item => item.id === job.id);
  if (!run) return;
  for (const key of ['status', 'progress', 'message', 'duration', ...STAGE_KEYS]) run[key] = job[key];
  renderRuns();
}

// ---- One talk ----
function select(id) {
  clearTimeout(pollTimer); closeEditor(); closeExport(); notice('');
  jobId = id; shown = null; current = null; renderedFor = null; sheetKey = null; summaryShown = null; stageNotice = ''; gujlishNotice = '';
  if (id) sessionStorage.setItem('asr-job', id); else sessionStorage.removeItem('asr-job');
  if (!id) { showPage('home'); renderRuns(); return; }
  showPage('run');
  const run = runs.find(item => item.id === id);
  renderHead(run || { name: 'Opening…', status: 'loading' });
  part('status'); $('status-label').textContent = 'Opening…'; $('status-detail').textContent = '';
  $('progress-description').textContent = ''; $('progress-percent').textContent = ''; $('spinner').hidden = false;
  drawWave($('progress-wave'), null, false);
  pollFailures = 0; poll();
}
function part(name) {  // 'status' while transcribing, 'result' once there is a transcript
  $('processing').hidden = name !== 'status';
  $('result').hidden = name !== 'result';
  if (name !== 'result') { $('export-btn').hidden = true; $('dock-wrap').hidden = true; }
}
function renderHead(job) {
  $('run-title').textContent = job.name || '';
  const script = job.mode === 'native' ? 'Everything in Gujarati script' : job.mode === 'mixed' ? 'English words in English letters' : '';
  const model = job.model ? job.model[0].toUpperCase() + job.model.slice(1) : '';
  $('run-meta').textContent = [minutes(job.duration), model, script, when(job.started)].filter(Boolean).join(' · ');
  const [label, kind] = job.status === 'complete' && !runBadge(job) ? ['', ''] : runPill(job);
  $('run-state').textContent = label; $('run-state').className = `status-pill ${kind}`;
  const active = ACTIVE.includes(job.status), stopped = job.status === 'paused' || job.status === 'error';
  $('pause-button').hidden = !active;
  $('resume-button').hidden = !stopped;
  $('delete-button').textContent = job.status === 'complete' ? 'Delete' : 'Cancel';
  renderSteps(job);
}
function renderSteps(job) {
  const complete = job.status === 'complete';
  const english = Boolean(current && current.translation_text), summary = Boolean(current && current.summary_markdown);
  const at = complete ? view : 'transcribe';
  const done = { input: true, transcribe: complete, fix: english, translate: english && at !== 'translate', summary: summary && at !== 'summary' };
  const open = { fix: complete, translate: english, summary };
  const nav = $('steps'); nav.replaceChildren();
  STEPS.forEach(([id, label], i) => {
    if (i) { const thread = el('span', `thread${done[id] || id === at ? ' on' : ''}`); thread.setAttribute('aria-hidden', 'true'); nav.append(thread); }
    const step = el('button', `step${id === at ? ' current' : done[id] ? ' done' : ''}`); step.type = 'button';
    step.disabled = id === at || !open[id];
    if (id === at) step.setAttribute('aria-current', 'step');
    step.append(el('span', 'dot', id !== at && done[id] ? '✓' : String(i + 1)), label);
    step.onclick = () => setView(id);
    nav.append(step);
  });
}
function renderStatus(job) {
  part('status');
  const active = ACTIVE.includes(job.status);
  $('spinner').hidden = !active;
  $('status-label').textContent = {
    queued: job.ahead ? `Waiting · ${job.ahead} recording${job.ahead === 1 ? '' : 's'} ahead` : 'Waiting to start…',
    loading: 'Preparing transcription…', transcribing: 'Transcribing…', paused: 'Paused', error: 'Stopped',
  }[job.status] || job.message;
  $('status-detail').textContent = job.message + (active && job.device ? ` · ${job.device.toUpperCase()}` : '');
  const processed = job.processed ? `${time(job.processed)}${job.duration ? ` / ${time(job.duration)}` : ''} audio done` : '';
  const measured = job.status === 'transcribing' || !active;
  $('progress-description').textContent = measured ? processed : 'Progress begins when the model is ready';
  const percent = measured ? (job.progress ?? (active ? null : 0)) : null;
  $('progress-percent').textContent = percent == null ? '' : `${percent}%`;
  drawWave($('progress-wave'), percent, !active);
}
async function poll() {
  clearTimeout(pollTimer);
  const id = jobId;
  if (!id) return;
  try {
    const response = await fetch(`/api/jobs/${id}`);
    if (signedOut(response) || jobId !== id) return;
    if (response.status === 404) {
      runs = runs.filter(run => run.id !== id); select(null); loadRuns(); return;
    }
    if (!response.ok) throw new Error('Could not read progress.');
    const job = await response.json();
    if (jobId !== id) return;
    pollFailures = 0; shown = job;
    mergeRun(job);
    if (job.status === 'complete') {
      renderResult(job); renderHead(job);
      if (STAGE_KEYS.some(key => running(job[key]))) pollTimer = later(poll, 1500);
      return;
    }
    current = null; renderHead(job); renderStatus(job);
    if (ACTIVE.includes(job.status)) pollTimer = later(poll, 1500);
  } catch {
    if (jobId !== id) return;
    // Cloud Run sometimes swaps the server's machine; the new one picks the run up again within seconds.
    pollFailures++;
    if (pollFailures >= MAX_POLL_FAILURES) {
      $('run-notice').textContent = $('home-notice').textContent = `Lost the connection to ${SERVER}. Click anywhere or come back to this tab to try again.`;
      napping = true;
      return;
    }
    if (pollFailures > 2) notice(`Reconnecting to ${SERVER}…`);
    pollTimer = later(poll, Math.min(10000, 1000 * pollFailures));
  }
}
function renderResult(job) {
  const first = renderedFor !== job.id;
  current = job; part('result');
  if (pollFailures === 0 && $('run-notice').textContent.startsWith('Reconnecting')) notice('');
  if (first) { renderedFor = job.id; view = job.translation_text ? 'translate' : 'fix'; loadCaps(); }
  if (view === 'translate' && !job.translation_text) view = 'fix';
  if (view === 'summary' && !job.summary_markdown) view = job.translation_text ? 'translate' : 'fix';
  renderView();
}
function setView(name) {
  if (!current) return;
  view = name; closeEditor(); stageNotice = '';
  renderView(); renderSteps(shown || current);
}
// Everything below the title for a finished transcript: the text (or summary), the export menu and the dock.
function renderView() {
  $('text-view').hidden = view === 'summary';
  $('summary-view').hidden = view !== 'summary';
  $('export-btn').hidden = false;
  for (const button of document.querySelectorAll('.seg button')) button.setAttribute('aria-checked', String(button.dataset.script === script));
  $('hint').textContent = view === 'translate' ? 'English under every line. Double-click a word to keep fixing.' : 'Double-click any word to fix it.';
  if (view !== 'summary') renderSheet();
  if (current.summary_markdown !== summaryShown) {
    summaryShown = current.summary_markdown;
    if (summaryShown) renderMarkdown($('summary'), summaryShown); else $('summary').replaceChildren();
  }
  $('youtube').hidden = !current.youtube;
  if (current.youtube) { $('yt-title').textContent = current.youtube.title; $('yt-description').textContent = current.youtube.description; }
  updateExports(); updateGujlishBar(); updateDock();
}

// ---- The sheet: one line per segment, each word its own element, so word N of a line is the same word in every
// script. A double-click picks a word for the fixer, which opens under its line.
function segmentWords(index, latin) {
  return latin ? ((current.gujlish_tokens || [])[index] || []) : words(current.segments[index].text);
}
function renderSheet() {
  const latin = Boolean(current.gujlish_tokens), english = view === 'translate';
  const key = [current.id, current.rev, script, english ? (current.english_segments || [current.translation_text]).join('\n') : ''].join('|');
  if (key === sheetKey) { markSelection(); return; }
  sheetKey = key;
  $('editor-home').append($('editor'));  // keep the fixer while the lines are replaced
  const sheet = $('sheet');
  if (script !== 'gu' && !latin) { sheet.replaceChildren(el('p', 'muted', 'No Gujlish for this talk yet.')); closeEditor(); return; }
  const rows = [];
  current.segments.forEach((segment, index) => {
    const gu = segmentWords(index, false);
    if (!gu.length) return;
    const lat = latin ? segmentWords(index, true) : [];
    const fixed = new Map(), suggested = new Map();
    for (const fix of segment.fixes || []) for (let i = fix.start; i < fix.end; i++) fixed.set(i, fix.heard);
    for (const hint of segment.suggestions || []) for (let i = hint.start; i < hint.end; i++) suggested.set(i, hint);
    const row = el('div', 'line'), body = el('div'); body.style.minWidth = '0';
    const line = el('p', 'words'); line.dataset.seg = index;
    gu.forEach((word, i) => {
      if (i) line.append(' ');  // so selected or copied text keeps its spaces
      const span = el('span', 'w'); span.dataset.i = i; span.tabIndex = -1; span.setAttribute('role', 'button');
      const main = script === 'lat' ? (lat[i] || '') : word;
      const mainNode = el('span', 'main', main);
      if (script === 'lat' || /^[\x00-\x7f]+$/.test(main)) mainNode.classList.add('latin'); else mainNode.lang = 'gu';
      span.append(mainNode);
      if (script === 'both') span.append(el('span', 'sub', lat[i] || ''));
      if (fixed.has(i)) { span.classList.add('fixed'); span.title = `Fixed · heard “${fixed.get(i)}”`; }
      if (suggested.has(i)) { span.classList.add('suggested'); span.title = `Your dictionary suggests “${suggested.get(i).correct}” · double-click to accept or reject`; }
      line.append(span);
    });
    body.append(line);
    if (english && current.english_segments) {
      const text = current.english_segments[index];
      if (text) body.append(el('p', 'en', text));
    }
    row.append(el('span', 'stamp', time(segment.start)), body); rows.push(row);
  });
  if (english && !current.english_segments && current.translation_text) {  // an older run without per-line English
    const row = el('div', 'line'); row.append(el('span', 'stamp', 'EN'), el('p', 'en', current.translation_text)); rows.push(row);
  }
  sheet.replaceChildren(...rows);
  const first = sheet.querySelector('.w'); if (first) first.tabIndex = 0;  // one Tab stop; arrow keys move between words
  placeEditor(); markSelection();
}
for (const button of document.querySelectorAll('.seg button')) {
  button.addEventListener('click', () => { script = button.dataset.script; closeEditor(); renderView(); });
}
function markSelection() {
  for (const span of document.querySelectorAll('.w.selected')) span.classList.remove('selected');
  if (!sel) return;
  for (const span of $('sheet').querySelectorAll(`.words[data-seg="${sel.seg}"] .w`)) {
    const i = Number(span.dataset.i);
    if (i >= sel.start && i < sel.end) span.classList.add('selected');
  }
}
function wordAt(target) {
  const word = target.closest('.w');
  return word && current ? { seg: Number(word.parentElement.dataset.seg), i: Number(word.dataset.i) } : null;
}
function pickWord(at, extend) {
  const hint = suggestionAt(at.seg, at.i);
  if (extend && sel && sel.seg === at.seg) pick(at.seg, Math.min(sel.start, at.i), Math.max(sel.end, at.i + 1));
  else if (hint) pick(at.seg, hint.start, hint.end, hint);
  else pick(at.seg, at.i, at.i + 1);
}
$('sheet').addEventListener('dblclick', e => {
  const at = wordAt(e.target);
  if (!at) return;
  window.getSelection().removeAllRanges();
  pickWord(at, e.shiftKey);
});
$('sheet').addEventListener('keydown', e => {
  if (!e.target.classList.contains('w')) return;
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickWord(wordAt(e.target), e.shiftKey); return; }
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1, Home: -Infinity, End: Infinity }[e.key];
  if (!step) return;
  e.preventDefault();
  const words = [...$('sheet').querySelectorAll('.w')], at = words.indexOf(e.target);
  focusWord(words[Math.max(0, Math.min(words.length - 1, Number.isFinite(step) ? at + step : step < 0 ? 0 : words.length - 1))]);
});
function focusWord(word) {
  if (!word) return;
  for (const other of $('sheet').querySelectorAll('.w[tabindex="0"]')) other.tabIndex = -1;
  word.tabIndex = 0; word.focus();
}
$('sheet').addEventListener('mouseup', e => {
  if (e.detail > 1) return;  // a double-click is handled above
  const selection = window.getSelection();
  if (selection.isCollapsed || !selection.rangeCount || e.target.closest('#editor')) return;
  const range = selection.getRangeAt(0), node = range.commonAncestorContainer;
  const line = (node.nodeType === 1 ? node : node.parentElement).closest('.words');
  if (!line) return;  // across lines: left as an ordinary text selection
  const hit = [...line.querySelectorAll('.w')].filter(span => range.intersectsNode(span)).map(span => Number(span.dataset.i));
  if (!hit.length) return;
  selection.removeAllRanges();
  pick(Number(line.dataset.seg), Math.min(...hit), Math.max(...hit) + 1);
});

// ---- Gujlish: written after transcription; can be written again, or redone with Gemini ----
async function loadCaps() {
  try { const response = await fetch('/api/capabilities'); caps = response.ok ? await response.json() : null; }
  catch { caps = null; }
  if (current) { updateGujlishBar(); updateDock(); }
}
function stageState(key) { return (current && current[key]) || { status: 'idle' }; }
function updateGujlishBar() {
  const bar = $('gujlish-bar'), state = stageState('gujlish'), busy = running(state);
  const stale = state.status === 'complete' && !current.gujlish_tokens;
  const label = ['idle', 'error'].includes(state.status) || stale || !current.gujlish_tokens ? 'Write Gujlish' : null;
  const alt = Boolean(caps && caps.gemini) && !busy;
  bar.hidden = (script === 'gu' && !busy) || view === 'summary' || (!label && !busy && !alt && !gujlishNotice);
  if (bar.hidden) return;
  $('gujlish-explain').textContent = stale ? 'The Gujlish no longer lines up with the Gujarati. Write it again.'
    : current.gujlish_tokens ? 'Gujarati in Latin letters, word for word. Fix a word in either script: the Gujlish spellings you type are remembered too.'
    : 'Gujarati in Latin letters, word for word, written after transcribing.';
  $('gujlish-button').hidden = !label || busy; $('gujlish-button').textContent = label || '';
  $('gujlish-alt').hidden = !alt; $('gujlish-stop').hidden = !busy;
  $('gujlish-status').textContent = gujlishNotice || (busy || state.status === 'error' || state.message === STOPPED ? state.message || '' : '');
}
$('gujlish-button').addEventListener('click', () => startStage('gujlish'));
$('gujlish-alt').addEventListener('click', () => startStage('gujlish', { engine: 'gemini' }));
$('gujlish-stop').addEventListener('click', () => act('stop', { stage: 'gujlish' }));

// ---- The dock: where the talk stands, and the one thing to do next ----
function updateDock() {
  const wrap = $('dock-wrap'), dock = $('dock');
  wrap.hidden = page !== 'run' || !current;
  if (wrap.hidden) return;
  const gemini = Boolean(caps && caps.gemini);
  const translation = stageState('translation'), captions = stageState('captions'), summary = stageState('summary');
  const working = [translation, captions, summary].find(running);
  const failed = [translation, captions, summary].find(s => s.status === 'error' || s.message === STOPPED);
  const english = translation.status === 'complete', summarised = summary.status === 'complete';
  const stale = Boolean(current.english_stale || current.summary_stale);
  const captionsOnly = !stale && english && summarised && Boolean(current.captions_stale);
  const fixes = current.segments.reduce((total, segment) => total + (segment.fixes || []).length, 0);
  const fixed = fixes ? `${fixes} word${fixes === 1 ? '' : 's'} fixed` : 'Fix any misheard words first';
  const translate = (label, path = 'english') => gemini ? [label, () => startStage(path)] : null;
  let kind = 'ok', text, go = null, alt = null;
  if (working) {
    kind = 'busy'; text = working.message || 'Working…';
    go = ['Stop', () => act('stop', { stage: 'english' })];
  } else if (english && current.youtube_missing && !current.english_stale) {
    kind = 'stale'; text = 'This summary was made before YouTube titles and descriptions'; go = translate('Update English & summary');
  } else if (english && stale) {
    kind = 'stale'; text = 'You fixed words since the English was made'; go = translate('Update English & summary');
  } else if (captionsOnly) {
    kind = 'stale'; text = 'The English subtitles aren’t timed to the speech yet'; go = gemini ? ['Time the subtitles', () => startStage('captions')] : null;
  } else if (english && view === 'fix') {
    text = fixes ? `${fixed} · English matches` : 'English matches your fixes'; go = ['See English →', () => setView('translate')];
  } else if (english && view === 'translate') {
    text = 'English matches your fixes';
    go = summarised ? ['See summary →', () => setView('summary')] : translate('Write the summary', 'summarize');
  } else if (english) {
    text = 'All done'; go = ['Export', openExport]; alt = translate('Run again');
  } else {
    kind = ''; text = fixed; go = translate('Translate & summarise →');
  }
  if (!working && failed && failed.message) { kind = 'stale'; text = failed.message; }
  if (!working && stageNotice) { kind = 'stale'; text = stageNotice; }
  if (caps && !gemini && !working && !english) text = HOSTED ? 'Translation and summaries are not enabled on this site.' : 'To translate, set GEMINI_API_KEY and restart the app (see the README).';
  dock.className = `dock${kind ? ` ${kind}` : ''}`;
  $('dock-note').textContent = text;
  const track = $('dock-track'), progress = working ? working.progress : null;
  track.hidden = !working; track.classList.toggle('indeterminate', Boolean(working) && progress == null);
  $('dock-fill').style.width = working && progress != null ? `${progress}%` : '';
  dockAction = go && go[1]; dockAltAction = alt && alt[1];
  $('dock-go').hidden = !go; $('dock-go').textContent = go ? go[0] : '';
  $('dock-alt').hidden = !alt; $('dock-alt').textContent = alt ? alt[0] : '';
  // Say where the text goes whenever a button here would start Gemini.
  $('llm-note').hidden = working || !gemini || ![go, alt].some(button => button && /Translate|Update|Run again|summary$|subtitles/.test(button[0]));
}
$('dock-go').addEventListener('click', () => { if (dockAction) dockAction(); });
$('dock-alt').addEventListener('click', () => { if (dockAltAction) dockAltAction(); });
async function startStage(path, body) {
  if (!current) return;
  const gujlish = path === 'gujlish';
  if (gujlish) gujlishNotice = ''; else stageNotice = '';
  for (const id of ['dock-go', 'dock-alt', 'gujlish-button', 'gujlish-alt']) $(id).disabled = true;
  try {
    const response = await postJson(`/api/jobs/${jobId}/${path}`, body);
    if (signedOut(response)) return;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { if (gujlish) gujlishNotice = data.error || 'Could not start this step.'; else stageNotice = data.error || 'Could not start this step.'; return; }
    pollFailures = 0; poll(); loadRuns();
  } catch { if (gujlish) gujlishNotice = `Could not reach ${SERVER}.`; else stageNotice = `Could not reach ${SERVER}.`; }
  finally {
    for (const id of ['dock-go', 'dock-alt', 'gujlish-button', 'gujlish-alt']) $(id).disabled = false;
    if (current) { updateGujlishBar(); updateDock(); }
  }
}

// ---- Copying and the export menu ----
function shownText() {
  if (!current) return '';
  if (view === 'translate') return current.translation_text;
  return script === 'lat' ? current.gujlish_text : current.text;
}
$('copy-button').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(shownText() || ''); $('copy-button').textContent = 'Copied ✓'; }
  catch {  // no clipboard access: select the text so it can be copied by hand
    const range = document.createRange(); range.selectNodeContents($('sheet'));
    window.getSelection().removeAllRanges(); window.getSelection().addRange(range); $('copy-button').textContent = 'Select & copy';
  }
  setTimeout(() => { $('copy-button').textContent = 'Copy'; }, 2000);
});
$('copy-summary').addEventListener('click', () => copyText($('copy-summary'), current && current.summary_markdown, 'Copy'));
$('copy-title').addEventListener('click', () => copyText($('copy-title'), current && current.youtube && current.youtube.title, 'Copy'));
$('copy-description').addEventListener('click', () => copyText($('copy-description'), current && current.youtube && current.youtube.description, 'Copy'));
function updateExports() {
  const has = { transcript: true, gujlish: Boolean(current.gujlish_tokens), translation: Boolean(current.translation_text), summary: Boolean(current.summary_markdown) };
  for (const files of document.querySelectorAll('#export .files')) {
    const row = files.dataset.row;
    files.replaceChildren(...DOWNLOADS[row].map(([kind, label]) => {
      const link = el('a', `file${has[row] ? '' : ' off'}`, `↓ ${label}`);
      if (has[row]) link.href = `/api/jobs/${jobId}/download/${kind}`; else link.setAttribute('aria-disabled', 'true');
      return link;
    }));
  }
}
function openExport() { $('export').hidden = false; $('export-btn').setAttribute('aria-expanded', 'true'); $('export-close').focus(); }
function closeExport() { $('export').hidden = true; $('export-btn').setAttribute('aria-expanded', 'false'); }
$('export-btn').addEventListener('click', () => { if ($('export').hidden) openExport(); else closeExport(); });
$('export-close').addEventListener('click', closeExport);
document.addEventListener('click', e => {
  if (!$('export').hidden && !e.target.closest('#export') && !e.target.closest('#export-btn') && !e.target.closest('#dock')) closeExport();
});
// A time in the summary opens that line of the talk.
$('summary').addEventListener('click', e => {
  const chip = e.target.closest('.time-chip');
  if (!chip || !current) return;
  const t = Number(chip.dataset.t);
  let index = 0;
  current.segments.forEach((segment, i) => { if (segment.start <= t + 0.5 && words(segment.text).length) index = i; });
  setView(current.translation_text ? 'translate' : 'fix');
  const line = $('sheet').querySelector(`.words[data-seg="${index}"]`);
  if (line) { line.closest('.line').scrollIntoView({ behavior: 'smooth', block: 'center' }); line.querySelector('.w')?.focus({ preventScroll: true }); }
});

// ---- Pause, resume, cancel and delete ----
async function act(path, body, id = jobId) {
  try {
    const response = await postJson(`/api/jobs/${id}/${path}`, body);
    if (signedOut(response)) return false;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { notice(data.error || 'That did not work. Try again.'); return false; }
    return true;
  } catch { notice(`Could not reach ${SERVER}.`); return false; }
  finally { if (jobId === id && path !== 'delete') { pollFailures = 0; poll(); } loadRuns(); }
}
$('pause-button').addEventListener('click', () => act('pause'));
$('resume-button').addEventListener('click', () => act('resume'));
$('delete-button').addEventListener('click', async () => {
  const job = shown || runs.find(run => run.id === jobId);
  if (!job) return;
  const finished = job.status === 'complete';
  const yes = await ask(finished ? 'Delete this recording?' : 'Cancel this recording?',
    finished ? `“${job.name}” and its transcript, Gujlish, English and summary are deleted. Fixes already in your dictionary stay.`
      : `“${job.name}” stops, and the recording and the progress so far are deleted.`,
    [{ label: finished ? 'Delete' : 'Cancel recording', value: true, kind: 'btn-accent danger' }, { label: 'Keep it', value: false }]);
  if (!yes) return;
  const id = jobId;
  if (await act('delete')) { runs = runs.filter(run => run.id !== id); if (jobId === id) select(null); }
});

// ---- Adding a recording: it uploads as soon as it is dropped or chosen, and carries on while you look at others ----
function describeOptions() {
  const model = $('model').hidden ? '' : `${$('model').selectedOptions[0].textContent.split(' · ')[0]} · `;
  $('options-text').textContent = model + ($('mode').hidden ? 'Gujarati script.' : $('mode').value === 'native' ? 'Everything comes out in Gujarati script.' : 'English words come out in English letters.');
}
$('model').addEventListener('change', describeOptions);
$('mode').addEventListener('change', describeOptions);
$('upload-form').addEventListener('submit', e => e.preventDefault());
$('audio-input').addEventListener('change', () => { const file = $('audio-input').files[0]; $('audio-input').value = ''; if (file) addRecording(file); });
let dragDepth = 0;
function dragging(on) { $('well').classList.toggle('drag', on && page === 'home'); $('drop-overlay').hidden = !on || page === 'home'; }
window.addEventListener('dragenter', e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); dragDepth++; dragging(true); } });
window.addEventListener('dragover', e => { if (e.dataTransfer.types.includes('Files')) e.preventDefault(); });
window.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; dragging(false); } });
window.addEventListener('drop', e => { if (!e.dataTransfer.files.length) return; e.preventDefault(); dragDepth = 0; dragging(false); addRecording(e.dataTransfer.files[0]); });

async function fingerprintOf(file) {  // SHA-256 of the file, as web/recordings.py computes it; null if unavailable
  try {
    const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
    return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  } catch { return null; }
}
async function uploadInPieces(file, signal) {
  // Sent in pieces because hosting platforms cap the size of one request; offsets make a resent piece harmless.
  const started = await fetch('/api/uploads', { method: 'POST', signal });
  if (signedOut(started)) return null;
  if (!started.ok) throw new Error('Could not start the upload.');
  const { id } = await started.json();
  let offset = 0;
  while (offset < file.size) {
    let response = null;
    for (let attempt = 0; ; attempt++) {
      try { response = await fetch(`/api/uploads/${id}?offset=${offset}`, { method: 'PUT', body: file.slice(offset, offset + CHUNK_BYTES), signal }); break; }
      catch (error) { if (signal.aborted) throw error; if (attempt >= 5) throw new Error(`Lost the connection to ${SERVER} during upload.`); await sleep(1500 * (attempt + 1)); }
    }
    if (signedOut(response)) return null;
    const data = await response.json().catch(() => ({}));
    if (response.status === 409 && Number.isInteger(data.received)) { offset = data.received; continue; }
    if (!response.ok) throw new Error(data.error || 'Upload failed. Please try again.');
    offset = data.received;
    upload.percent = Math.round(offset / file.size * 100);
    renderBusy();
  }
  return id;
}
async function addRecording(file) {
  if (upload && !upload.error) return ask('One upload at a time', `“${upload.name}” is still uploading. Add this one when it has finished.`, [{ label: 'OK', value: true, kind: 'btn-accent' }]);
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) return ask('This file is too large', `Files up to ${MAX_UPLOAD_MB} MB can be added here.`, [{ label: 'OK', value: true, kind: 'btn-accent' }]);
  if (page !== 'home') select(null);
  // The server knows recordings by a fingerprint of their audio, so a renamed copy is recognised too, and audio it
  // already has is never uploaded again.
  $('well-title').textContent = 'Checking whether you already have it…';
  const print = await fingerprintOf(file);
  let known = { stored: false, runs: [] };
  try {
    const response = print ? await fetch(`/api/recordings/${print}`) : null;
    if (response && signedOut(response)) return;
    if (response && response.ok) known = await response.json();
  } catch { /* checking is optional: upload as usual */ }
  finally { $('well-title').textContent = 'Drop audio or video here'; }
  const same = known.runs[0];
  if (same) {
    const others = known.runs.length > 1 ? ` (and ${known.runs.length - 1} more)` : '';
    const choice = await ask('You already have this recording',
      `“${same.name}”${same.started ? `, added ${when(same.started)},` : ''}${others} has exactly the same audio.`,
      [{ label: 'Open it', value: 'open', kind: 'btn-accent' }, { label: 'Transcribe again', value: 'again' }, { label: 'Cancel', value: null }]);
    if (choice === 'open') { select(same.id); return; }
    if (choice !== 'again') return;
  }
  const form = new FormData(); form.append('model', $('model').value); form.append('mode', $('mode').value); form.append('name', file.name);
  upload = { name: file.name, size: file.size, percent: 0, error: '', controller: new AbortController() };
  const mine = upload;
  renderBusy(); loadRuns();
  const start = () => fetch('/api/jobs', { method: 'POST', body: form, signal: mine.controller.signal });
  try {
    let response = null;
    if (known.stored) {  // the server has this audio already: start straight away
      form.append('fingerprint', print); mine.checking = true; renderBusy();
      response = await start();
      if (signedOut(response)) return;
      if (response.status === 409) { form.delete('fingerprint'); mine.checking = false; response = null; }  // gone since
    }
    if (!response) {
      const uploadId = await uploadInPieces(file, mine.controller.signal);
      if (!uploadId) return;
      form.append('upload_id', uploadId);
      mine.checking = true; renderBusy();
      response = await start();
      if (signedOut(response)) return;
    }
    const data = await response.json().catch(() => ({}));
    if (response.status !== 202) throw new Error(data.error || 'Upload failed. Please try again.');
    upload = null;
    await loadRuns();
  } catch (error) {
    if (mine.controller.signal.aborted || upload !== mine) return;
    upload.error = error.message || `Could not connect to ${SERVER}.`; upload.checking = false;
    renderBusy();
  }
}

// ---- The fixer: pick words in any script, type the right word, and save. The main box edits the script on screen;
// the other script sits beside it, filled in as you type (Gujlish suggests Gujarati, Gujarati suggests Gujlish).
function selectedText(latin) { return segmentWords(sel.seg, latin).slice(sel.start, sel.end).join(' '); }
function countMatches(heard) {
  const key = words(heard).map(wordKey);
  let count = 0;
  for (const segment of current.segments) {
    const keys = words(segment.text).map(wordKey);
    for (let i = 0; key.length && i + key.length <= keys.length;) {
      if (key.every((part, j) => keys[i + j] === part)) { count++; i += key.length; } else i++;
    }
  }
  return count;
}
function fillEditor(keepTyped) {
  const heard = selectedText(false), heardLatin = current.gujlish_tokens ? selectedText(true) : '';
  $('ed-heard').textContent = heard; $('ed-heard-latin').textContent = heardLatin;
  if (!keepTyped || !touched.gu) $('ed-gu').value = bare(heard);
  if (!keepTyped || !touched.latin) $('ed-latin').value = bare(heardLatin);
  const total = countMatches(heard);
  $('ed-all-row').hidden = total < 2; $('ed-count').textContent = total;
  $('ed-left').disabled = sel.start === 0;
  $('ed-right').disabled = sel.end >= segmentWords(sel.seg, false).length;
  stopWords(); $('ed-play-note').textContent = '';
  describeEdit(); markSelection(); placeEditor();
}
function openEditor() {
  touched = { gu: false, latin: false }; $('ed-suggest').replaceChildren(); $('ed-error').textContent = '';
  const latinFirst = script === 'lat' && Boolean(current.gujlish_tokens);
  const main = latinFirst ? $('ed-latin') : $('ed-gu'), other = latinFirst ? $('ed-gu') : $('ed-latin');
  $('ed-main').append(main); $('ed-other').append(other);
  main.placeholder = latinFirst ? 'Type in English letters' : 'Type in Gujarati or English letters';
  other.placeholder = latinFirst ? 'Gujarati' : 'Gujlish';
  $('ed-other-label').textContent = latinFirst ? 'ગુજરાતી' : 'Gujlish'; $('ed-other-label').htmlFor = other.id;
  $('ed-more').hidden = true; $('ed-more-toggle').setAttribute('aria-expanded', 'false');
  const hint = sel.suggestion;
  $('ed-all').checked = !hint; $('ed-remember').checked = true; $('ed-always').checked = false; $('ed-always-row').hidden = false;
  $('ed-suggestion').hidden = !hint;
  $('ed-save').textContent = hint ? 'Accept ↵' : 'Save ↵';
  if (hint) { $('ed-suggested').textContent = hint.correct; $('ed-gu').value = hint.correct; $('ed-latin').value = hint.correct_gujlish || ''; touched = { gu: true, latin: Boolean(hint.correct_gujlish) }; }
  fillEditor(Boolean(hint));
  if (hint && !hint.correct_gujlish) { $('ed-latin').value = ''; suggest('latin'); }  // never pair the heard spelling with the new word
  main.focus({ preventScroll: true }); main.select();
}
function closeEditor(refocus = false) {
  const was = sel, hadFocus = $('editor').contains(document.activeElement);
  stopWords(); sel = null;
  if ($('editor').parentElement !== $('editor-home')) $('editor-home').append($('editor'));
  markSelection();
  if (was && (refocus || hadFocus)) focusWord($('sheet').querySelector(`.words[data-seg="${was.seg}"] .w[data-i="${was.start}"]`));
}
// The fixer opens under the line it is fixing.
function placeEditor() {
  if (!sel) return;
  const line = $('sheet').querySelector(`.words[data-seg="${sel.seg}"]`);
  if (!line) return closeEditor();
  if ($('editor').parentElement !== line.parentElement) line.parentElement.append($('editor'));
}
function pick(seg, start, end, suggestion = null) {
  sel = { seg, start, end, suggestion }; openEditor();
  $('editor').scrollIntoView({ block: 'nearest' });  // clear of the dock (scroll-margin in the CSS)
}
function suggestionAt(seg, i) { return (current.segments[seg].suggestions || []).find(hint => i >= hint.start && i < hint.end) || null; }
function leaveSuggestion() {  // the words were changed by hand: an ordinary fix from here on
  if (!sel.suggestion) return;
  sel.suggestion = null; touched = { gu: false, latin: false };  // start from the words as heard, not the suggested ones
  $('ed-suggestion').hidden = true; $('ed-save').textContent = 'Save ↵';
}
$('ed-more-toggle').addEventListener('click', () => {
  const open = $('ed-more').hidden; $('ed-more').hidden = !open; $('ed-more-toggle').setAttribute('aria-expanded', String(open));
});
$('ed-left').addEventListener('click', () => { leaveSuggestion(); sel.start--; fillEditor(true); });
$('ed-right').addEventListener('click', () => { leaveSuggestion(); sel.end++; fillEditor(true); });
$('ed-remember').addEventListener('change', () => { $('ed-always-row').hidden = !$('ed-remember').checked; });
$('ed-dismiss').addEventListener('click', async () => {
  const hint = sel && sel.suggestion;
  if (!hint) return;
  $('ed-dismiss').disabled = true; $('ed-error').textContent = '';
  try {
    const response = await postJson(`/api/jobs/${jobId}/dismiss`, { segment: sel.seg, start: hint.start, end: hint.end, rule: hint.rule });
    if (signedOut(response)) return;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { $('ed-error').textContent = data.error || 'Could not dismiss this suggestion.'; return; }
    closeEditor();
    note('Kept as heard · your dictionary notes this place as one where the fix was wrong');
    pollFailures = 0; await poll(); loadDictionary();
  } catch { $('ed-error').textContent = `Could not reach ${SERVER}.`; }
  finally { $('ed-dismiss').disabled = false; }
});
// ▶ in the fixer: hear just the chosen words, cut from the recording where they are said.
let wordAudio = null, wordTicket = 0;
function stopWords() {
  wordTicket++;
  if (wordAudio) { wordAudio.pause(); URL.revokeObjectURL(wordAudio.src); wordAudio = null; }
  $('ed-play').classList.remove('busy', 'on');
}
$('ed-play').addEventListener('click', async () => {
  if (!sel || !jobId) return;
  const playingNow = Boolean(wordAudio);
  stopWords();
  if (playingNow) return;  // a second click stops it
  const ticket = wordTicket, button = $('ed-play');
  button.classList.add('busy'); $('ed-play-note').textContent = 'Finding the words in the recording…';
  try {
    const params = new URLSearchParams({ segment: sel.seg, start: sel.start, end: sel.end });
    const response = await fetch(`/api/jobs/${jobId}/word-audio?${params}`);
    if (signedOut(response) || ticket !== wordTicket) return;
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      $('ed-play-note').textContent = data.error || 'Could not play this part.'; return;
    }
    const rough = response.headers.get('X-Word-Timing') === 'estimated', blob = await response.blob();
    if (ticket !== wordTicket) return;
    wordAudio = new Audio(URL.createObjectURL(blob));
    wordAudio.addEventListener('ended', () => { if (ticket === wordTicket) stopWords(); });
    $('ed-play-note').textContent = rough ? 'Couldn’t pin these words down exactly, so you’ll hear a little around them.' : '';
    button.classList.add('on');
    await wordAudio.play();
  } catch { if (ticket === wordTicket) { stopWords(); $('ed-play-note').textContent = 'Could not play this part.'; } }
  finally { button.classList.remove('busy'); }
});
$('ed-cancel').addEventListener('click', closeEditor);
function chips(options) {
  $('ed-suggest').replaceChildren(...options.map(option => {
    const chip = el('button', 'chip', option); chip.type = 'button'; chip.lang = 'gu';
    chip.classList.toggle('chosen', option === $('ed-gu').value);
    chip.onclick = () => { $('ed-gu').value = option; touched.gu = false; describeEdit(); for (const c of $('ed-suggest').children) c.classList.toggle('chosen', c === chip); };
    return chip;
  }));
}
function suggest(to) {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(async () => {
    const text = to === 'gu' ? $('ed-latin').value : $('ed-gu').value, ticket = ++suggestTicket;
    if (!text.trim()) { if (to === 'gu') $('ed-suggest').replaceChildren(); return; }
    try {
      const response = await fetch(`/api/transliterate?to=${to}&text=${encodeURIComponent(text)}`);
      if (signedOut(response) || !response.ok || ticket !== suggestTicket || !sel) return;
      const { options } = await response.json();
      if (to === 'gu') {
        // A respelled Gujlish word usually still means the same Gujarati: keep it when the engine agrees.
        const heard = bare(selectedText(false));
        if (!touched.gu && options[0]) $('ed-gu').value = options.includes(heard) ? heard : options[0];
        chips(options);
      } else if (!touched.latin && options[0]) $('ed-latin').value = options[0];
      describeEdit();
    } catch { /* suggestions are optional */ }
  }, 200);
}
function describeEdit() {
  const heard = bare(selectedText(false)), correct = $('ed-gu').value.trim(), latin = $('ed-latin').value.trim();
  const heardLatin = current.gujlish_tokens ? bare(selectedText(true)) : '';
  $('ed-effect').textContent = correct && correct !== heard ? `Replaces “${heard}” with “${correct}”`
    : latin && latin !== heardLatin ? `Keeps the Gujarati and changes only the Gujlish spelling to “${latin}”` : '';
}
$('ed-latin').addEventListener('input', () => { touched.latin = true; describeEdit(); suggest('gu'); });
$('ed-gu').addEventListener('input', () => { touched.gu = true; $('ed-suggest').replaceChildren(); describeEdit(); suggest('latin'); });
$('editor').addEventListener('submit', e => { e.preventDefault(); saveEdit(); });
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!$('export').hidden) { closeExport(); $('export-btn').focus(); }
  else if (sel) closeEditor(true);
});
async function saveEdit() {
  const correct = $('ed-gu').value.trim(), latin = $('ed-latin').value.trim(), heard = selectedText(false);
  if (!correct) { $('ed-error').textContent = 'Type the correct word.'; return; }
  const heardLatin = current.gujlish_tokens ? bare(selectedText(true)) : '';
  if (correct === bare(heard) && (!latin || latin === heardLatin)) { closeEditor(); return; }
  const body = {
    segment: sel.seg, start: sel.start, end: sel.end, heard, correct, correct_gujlish: latin,
    everywhere: !$('ed-all-row').hidden && $('ed-all').checked, remember: $('ed-remember').checked,
    always: $('ed-remember').checked && $('ed-always').checked,
  };
  const accepted = Boolean(sel.suggestion);
  $('ed-save').disabled = true; $('ed-error').textContent = '';
  try {
    const response = await postJson(`/api/jobs/${jobId}/edit`, body);
    if (signedOut(response)) return;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { $('ed-error').textContent = data.error || 'Could not save this fix.'; return; }
    const { seg, start } = sel;
    closeEditor();
    note(`${accepted ? 'Accepted' : 'Fixed'} ${data.replaced === 1 ? '1 place' : `${data.replaced} places`}${data.remembered ? ' · saved to your dictionary' : ''}`);
    pollFailures = 0; await poll(); loadDictionary();
    if (!sel) focusWord($('sheet').querySelector(`.words[data-seg="${seg}"] .w[data-i="${start}"]`));  // the sheet was redrawn
  } catch { $('ed-error').textContent = `Could not reach ${SERVER}.`; }
  finally { $('ed-save').disabled = false; }
}

// ---- The dictionary ----
async function loadDictionary() {
  try {
    const response = await fetch('/api/dictionary');
    if (signedOut(response) || !response.ok) return;
    const data = await response.json();
    const spellings = Object.entries(data.gujlish);
    $('dict-count').textContent = data.corrections.length + spellings.length || '';
    const rows = data.corrections.map(rule => {
      const detail = [rule.heard_gujlish && rule.correct_gujlish ? `${rule.heard_gujlish} → ${rule.correct_gujlish}` : '',
        rule.rejected ? `wrong in ${rule.rejected} place${rule.rejected === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · ');
      return dictionaryRow(`${rule.heard} → ${rule.correct}`, detail, { correction: rule.id }, rule);
    });
    if (!rows.length) rows.push(el('p', 'muted', 'No fixes yet. Double-click a word in a transcript to fix it.'));
    $('dictionary').replaceChildren(...rows);
    $('spellings').hidden = !spellings.length; $('spelling-count').textContent = spellings.length;
    $('spelling-list').replaceChildren(...spellings.map(([gujarati, latin]) => dictionaryRow(`${gujarati} = ${latin}`, '', { gujlish: gujarati })));
  } catch { /* the list refreshes on the next fix */ }
}
function modeButton(rule) {
  const always = rule.mode === 'always';
  const button = el('button', always ? 'mode always' : 'mode', always ? 'Always' : 'Asks'); button.type = 'button';
  button.title = always ? 'Replaced in new transcripts without asking. Click to be asked each time instead.'
    : 'Marked in new transcripts for you to accept or reject. Click to replace it without asking.';
  button.onclick = async () => {
    try { const response = await postJson('/api/dictionary/mode', { correction: rule.id, mode: always ? 'ask' : 'always' }); if (!signedOut(response)) loadDictionary(); } catch { /* retry by clicking again */ }
  };
  return button;
}
function dictionaryRow(text, detail, forget, rule = null) {
  const row = el('div', 'dict-item'), label = el('div');
  const main = el('span', '', text); main.lang = 'gu'; label.append(main);
  if (detail) label.append(el('small', '', detail));
  const remove = el('button', 'forget', '×'); remove.type = 'button';
  remove.title = 'Forget'; remove.setAttribute('aria-label', `Forget ${text}`);
  remove.onclick = async () => {
    if (!await ask(`Forget “${text}”?`, 'Future recordings will no longer get this fix.', [{ label: 'Forget', value: true, kind: 'btn-accent danger' }, { label: 'Keep', value: false }])) return;
    try { const response = await postJson('/api/dictionary/forget', forget); if (!signedOut(response)) loadDictionary(); } catch { /* retry by clicking again */ }
  };
  const actions = el('div', 'dict-actions');
  if (rule) actions.append(modeButton(rule));
  actions.append(remove); row.append(label, actions);
  return row;
}

// Coming back to the tab or window: refresh at once, since nothing re-checks while the page is idle or hidden.
let lastReturn = 0;
function welcomeBack() {
  if (document.hidden || Date.now() - lastReturn < 2000) return;  // switching tabs fires both events
  lastReturn = Date.now(); lastActivity = Date.now();
  if (napping) { napping = false; $('run-notice').textContent = $('home-notice').textContent = ''; }
  loadRuns(); loadUsage();
  if (jobId) { pollFailures = 0; poll(); }
}
document.addEventListener('visibilitychange', welcomeBack);
window.addEventListener('focus', welcomeBack);
// A click or key press counts as being here; after a pause it refreshes at once.
for (const type of ['pointerdown', 'keydown']) {
  document.addEventListener(type, () => { lastActivity = Date.now(); if (napping) welcomeBack(); }, { passive: true });
}
describeOptions();
loadUsage();
loadDictionary();
loadRuns().then(() => { const saved = sessionStorage.getItem('asr-job'); if (saved && runs.some(run => run.id === saved)) select(saved); });
