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
// The bottom pill (islandOpen: expanded; islandParts: its drawn rings and texts), the summary's Source note (shown in
// Details), and the activity log.
let islandOpen = false, islandKey = '', islandParts = null, sourceLine = '', logReady = false;
// Keyed `${id}:${stage}`: each step's last seen phase, steps just started here (shown before the server says so), and
// steps you asked to stop.
const seen = new Map(), starting = new Map(), stopAsked = new Set();
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

// ---- What each step is called, and its icon in the bottom pill ----
const svg = body => `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
const ICONS = {
  upload: svg('<path d="M8 10V2.6M4.9 5.6 8 2.6l3.1 3M2.5 9.5v3.8h11V9.5"/>'),
  transcribe: svg('<path d="M2 7v2M5 4.5v7M8 2.5v11M11 5v6M14 7v2"/>'),
  gujlish: svg('<path d="M1.6 12.8 4.6 3.4h.8l3 9.4M2.7 9.6h4.6M14 7.6v5.2M11.7 7.6a2.3 2.6 0 1 0 0 5.2 2.3 2.6 0 1 0 0-5.2"/>'),
  translate: svg('<path d="M1.6 3.6h6.8M5 2v1.6M7 3.6c-.5 2.8-2.4 5-5 6.2M3.4 6c.8 1.7 2.2 3 3.9 3.7M8.6 14l2.7-6.4h.5L14.5 14M9.7 11.8h3.7"/>'),
  captions: svg('<rect x="1.6" y="3" width="12.8" height="10" rx="2.2"/><path d="M4.2 7.8h4.2M10.4 7.8h1.4M4.2 10.4h2M8.2 10.4h3.6"/>'),
  summary: svg('<path d="M4 1.8h5.3l2.9 2.9v9.5H4z"/><path d="M9.2 1.8v3h3M6.2 7.6h4M6.2 9.9h4M6.2 12.2h2.4"/>'),
};
const LEGEND = [
  ['upload', 'Uploading a recording'], ['transcribe', 'Transcribing the speech'], ['gujlish', 'Writing Gujlish (Gujarati in Latin letters)'],
  ['translate', 'Translating into English'], ['captions', 'Timing the English subtitles'], ['summary', 'Writing the summary and YouTube details'],
];
const STAGE_INFO = { gujlish: ['gujlish', 'Writing Gujlish', 'Gujlish'], translation: ['translate', 'Translating', 'English'], captions: ['captions', 'Timing subtitles', 'Subtitles'], summary: ['summary', 'Summarising', 'Summary'] };
const STEP_NAMES = { asr: 'Transcription', gujlish: 'Gujlish', translation: 'English translation', captions: 'Subtitle timing', summary: 'Summary and YouTube details' };
const STAGES_OF = { english: ['translation', 'captions', 'summary'], translate: ['translation', 'captions'], captions: ['captions'], summarize: ['summary'], gujlish: ['gujlish'] };
const UPLOAD_PHASES = {
  reading: ['Reading the file', file => `${mb(file.size)} · reading it on this device`],
  checking: ['Checking for a copy', () => 'Seeing whether you already have this recording'],
  uploading: ['Uploading', file => HOSTED ? `${mb(file.size)} · sending your recording to the server` : `${mb(file.size)} · saving to the local workspace`],
  starting: ['Starting', () => 'Handing it to the transcriber'],
};
// An icon in a ring: the ring fills with `percent`, spins while there is no measure yet, and is dotted while waiting.
function ring(icon, percent = null, waiting = false) {
  const box = el('span', 'ring');
  box.innerHTML = `<svg viewBox="0 0 36 36" aria-hidden="true"><circle class="track" cx="18" cy="18" r="16" pathLength="100"/><circle class="arc" cx="18" cy="18" r="16" pathLength="100"/></svg>${ICONS[icon]}`;
  return setRing(box, percent, waiting);
}
function setRing(box, percent, waiting) {
  const cls = `ring${waiting ? ' waiting' : percent == null ? ' spinning' : ''}`;
  const arc = waiting ? 100 : percent == null ? 28 : Math.max(2, Math.min(100, Number(percent) || 0));
  const circle = box.querySelector('.arc'), dash = `${arc} 100`;
  if (box.className !== cls) box.className = cls;
  if (circle.getAttribute('stroke-dasharray') !== dash) circle.setAttribute('stroke-dasharray', dash);
  return box;
}
const put = (node, text) => { if (node.textContent !== text) node.textContent = text; };

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
    box.textContent = `GPU this month: ${used}${data.cap_hours ? ` of ${data.cap_hours} h` : ''} · ≈ £${data.cost.toFixed(2)}`;
    box.classList.toggle('over', data.over_cap);
    box.hidden = false;
    $('info-dot').hidden = !data.over_cap;
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
// Waits until no popup question is open, so a second question never replaces one still waiting for an answer.
async function modalClosed() {
  while ($('modal').open) await new Promise(resolve => $('modal').addEventListener('close', resolve, { once: true }));
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
  closePops(); islandOpen = false;
  if (name !== 'run') $('export-btn').hidden = true;
  updateDock();
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
  for (const [key, state] of starting) {  // a step started here shows as starting until the server says it is queued
    const [id, stage] = key.split(':'), run = runs.find(item => item.id === id);
    if (run && !running(run[stage])) run[stage] = { ...state };
  }
  watchRuns();
  const rows = runs.map(talkRow);
  if (!rows.length) rows.push(el('p', 'muted', 'No talks yet. Drop one above.'));
  $('history').replaceChildren(...rows);
  $('runs-count').textContent = runs.length || '';
  renderBusy(); updateDock();
}
// The card on the home page: the upload in progress, or else the talk being transcribed now.
function renderBusy() {
  const card = $('busy'), action = $('busy-action');
  const active = runs.find(run => run.status === 'transcribing') || runs.find(run => ACTIVE.includes(run.status));
  card.hidden = !upload && !active;
  renderPhases();
  if (card.hidden) return;
  if (upload) {
    const [label, detail] = UPLOAD_PHASES[upload.phase];
    $('busy-label').textContent = upload.error ? 'Upload failed' : label;
    $('busy-name').textContent = upload.name;
    $('busy-detail').textContent = upload.error || detail(upload);
    $('busy-percent').textContent = !upload.error && upload.phase === 'uploading' ? `${upload.percent}%` : '';
    action.textContent = upload.error ? 'Dismiss' : 'Cancel upload'; action.hidden = false;
    card.dataset.id = '';
    drawWave($('busy-wave'), upload.phase === 'uploading' ? upload.percent : null, Boolean(upload.error));
    return;
  }
  const label = { queued: 'Waiting', loading: 'Starting', transcribing: 'Transcribing' }[active.status];
  $('busy-label').textContent = label; $('busy-name').textContent = active.name;
  $('busy-detail').textContent = 'You can open other talks while this runs.';
  $('busy-percent').textContent = active.status === 'transcribing' && active.progress != null ? `${active.progress}%` : '';
  action.textContent = 'Pause'; action.hidden = false; card.dataset.id = active.id;
  drawWave($('busy-wave'), active.status === 'transcribing' ? active.progress : null, false);
}
// Reading · Checking · Uploading N% · Starting, under the upload card.
function renderPhases() {
  const box = $('busy-phases');
  box.hidden = !upload;
  if (!upload) return;
  const order = Object.keys(UPLOAD_PHASES), at = order.indexOf(upload.phase);
  box.replaceChildren(...order.map((phase, i) => {
    const skipped = phase === 'uploading' && upload.stored;
    const label = skipped ? 'Already uploaded' : phase === 'uploading' && i === at ? `Uploading ${upload.percent}%` : phase[0].toUpperCase() + phase.slice(1);
    const state = i < at || skipped ? 'done' : i === at ? (upload.error ? 'failed' : 'current') : '';
    const item = el('li', state, state === 'done' ? `✓ ${label}` : label);
    if (i === at) item.setAttribute('aria-current', 'step');
    return item;
  }));
}
$('busy').addEventListener('click', e => { if (!e.target.closest('button') && $('busy').dataset.id) select($('busy').dataset.id); });
$('busy-action').addEventListener('click', () => {
  if (!upload) { const id = $('busy').dataset.id; if (id) act('pause', null, id, $('busy-action')); return; }
  cancelUpload();
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

// ---- The activity log: uploads and steps started, finished, failed or stopped, kept in this tab ----
const KINDS = ['start', 'done', 'fail', 'info'];
const EVENT_KINDS = { started: 'start', resumed: 'start', finished: 'done', failed: 'fail', stopped: 'info', paused: 'info' };
const feed = loadFeed();
function loadFeed() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('vaani-activity'));
    if (saved && Array.isArray(saved.items) && Number.isInteger(saved.unread)) {
      const items = saved.items.filter(item => item && KINDS.includes(item.kind) && typeof item.text === 'string'
        && Number.isFinite(item.at) && (item.id == null || typeof item.id === 'string')).slice(0, 50);
      return { items, unread: Math.max(0, Math.min(saved.unread, items.length)) };
    }
  } catch { /* nothing saved, or storage is off */ }
  return { items: [], unread: 0 };
}
function saveFeed() { try { sessionStorage.setItem('vaani-activity', JSON.stringify(feed)); } catch { /* the log is only kept while the page is open */ } }
function logActivity(kind, text, id = null) {
  feed.items.unshift({ kind, text, id, at: Date.now() });
  feed.items.length = Math.min(feed.items.length, 50);
  if ($('activity').hidden) feed.unread++;
  saveFeed(); renderActivity();
}
function renderActivity(read) {
  if (read && feed.unread) { feed.unread = 0; saveFeed(); }
  $('bell-dot').hidden = !feed.unread;
  $('bell-btn').setAttribute('aria-label', feed.unread ? `Activity, ${feed.unread} new` : 'Activity');
  const rows = feed.items.map(item => {
    const row = el('li', `activity-item ${item.kind}`), body = el(item.id ? 'button' : 'div', 'activity-row');
    if (item.id) { body.type = 'button'; body.title = 'Open this talk'; body.onclick = () => { closePop('activity'); select(item.id); }; }
    const at = new Date(item.at), stamp = el('time', '', at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
    stamp.dateTime = at.toISOString();
    const dot = el('i', 'kind-dot'); dot.setAttribute('aria-hidden', 'true');
    body.append(dot, el('span', 'activity-text', item.text), stamp);
    row.append(body);
    return row;
  });
  if (!rows.length) rows.push(el('li', 'activity-empty', 'Nothing yet. Uploads and steps you start show up here.'));
  $('activity-list').replaceChildren(...rows);
}
$('activity-clear').addEventListener('click', () => { feed.items = []; feed.unread = 0; saveFeed(); renderActivity(); });
// Each talk's transcription and steps, as phases; a change between two looks at the list is logged.
function asrPhase(run, before) {
  const phase = { queued: 'wait', loading: 'wait', transcribing: 'run', paused: 'pause', error: 'fail', complete: 'done' }[run.status] || 'rest';
  return phase === 'wait' && (before === 'pause' || before === 'resume') ? 'resume' : phase;
}
function asrEvent(before, now) {
  if (now === 'run') return before === 'pause' ? 'resumed' : before === 'resume' || before === 'run' ? '' : 'started';
  if (now === 'resume') return before === 'pause' ? 'resumed' : '';
  if (now === 'done') return ['wait', 'run', 'resume'].includes(before) ? 'finished' : '';
  if (now === 'fail' && before !== 'fail') return 'failed';
  if (now === 'pause' && before !== 'pause') return 'paused';
  return '';
}
function stagePhase(state) {
  if (!state) return 'rest';
  if (state.status === 'running') return 'run';
  if (state.status === 'queued') return 'wait';
  if (state.status === 'error') return 'fail';
  if (state.message === STOPPED) return 'stop';
  return state.status === 'complete' ? 'done' : 'rest';  // the talk list leaves out most finished steps: also 'rest'
}
function stageEvent(before, now, asked) {
  if (now === 'run') return before === 'run' ? '' : 'started';
  if (before !== 'run' && before !== 'wait') return now === 'fail' && before !== 'fail' ? 'failed' : '';
  if (now === 'wait') return '';
  if (now === 'fail') return 'failed';
  if (now === 'stop' || asked) return before === 'run' ? 'stopped' : '';  // steps that were only queued just drop away
  return now === 'done' || before === 'run' ? 'finished' : '';
}
function watchRuns() {
  const present = new Set();
  const track = (run, step, now, event) => {
    const key = `${run.id}:${step}`;
    present.add(key);
    if (starting.has(key)) return;
    const before = seen.get(key);
    seen.set(key, now);
    const what = logReady && before !== undefined ? event(before, now, key) : '';
    if (what) logActivity(EVENT_KINDS[what], `${STEP_NAMES[step]} ${what} · “${run.name}”`, run.id);
  };
  for (const run of runs) {
    track(run, 'asr', asrPhase(run, seen.get(`${run.id}:asr`)), asrEvent);
    for (const step of STAGE_KEYS) {
      track(run, step, stagePhase(run[step]), (before, now, key) => {
        const asked = stopAsked.has(key);
        if (asked && now !== 'run' && now !== 'wait') stopAsked.delete(key);
        return stageEvent(before, now, asked);
      });
    }
  }
  for (const key of [...seen.keys()]) if (!present.has(key)) seen.delete(key);
}

// ---- One talk ----
function select(id) {
  clearTimeout(pollTimer); closeEditor(); notice('');
  jobId = id; shown = null; current = null; renderedFor = null; sheetKey = null; summaryShown = null; stageNotice = ''; gujlishNotice = ''; sourceLine = '';
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
  if (name !== 'result') $('export-btn').hidden = true;
  updateDock();
}
function renderHead(job) {
  const name = job.name || '';
  $('run-title').textContent = name; $('run-title').classList.toggle('long', name.length > 48);
  $('run-meta').textContent = [minutes(job.duration), when(job.started)].filter(Boolean).join(' · ');
  const [label, kind] = job.status === 'complete' && !runBadge(job) ? ['', ''] : runPill(job);
  $('run-state').textContent = label; $('run-state').className = `status-pill ${kind}`;
  const active = ACTIVE.includes(job.status), stopped = job.status === 'paused' || job.status === 'error';
  $('pause-button').hidden = !active;
  $('resume-button').hidden = !stopped;
  $('delete-label').textContent = job.status === 'complete' ? 'Delete' : 'Cancel';
  $('delete-button').title = job.status === 'complete' ? 'Delete this talk' : 'Cancel this recording';
  renderSteps(job);
  renderInfo();
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
    for (const key of STAGE_KEYS) if (starting.has(`${id}:${key}`) && !running(job[key])) job[key] = { ...starting.get(`${id}:${key}`) };
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
    summaryShown = current.summary_markdown; sourceLine = '';
    if (summaryShown) {
      renderMarkdown($('summary'), summaryShown);
      // The "Source: … machine-generated" note moves to Details, beside the models that made the summary.
      const source = [...$('summary').querySelectorAll('blockquote')].find(quote => quote.textContent.trim().startsWith('Source:'));
      if (source) { sourceLine = source.textContent.trim(); source.remove(); }
    } else $('summary').replaceChildren();
    renderInfo();
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
  // No Stop until the server has the step you just clicked, so a double-click can't stop it again.
  $('gujlish-alt').hidden = !alt; $('gujlish-stop').hidden = !busy || starting.has(`${current.id}:gujlish`);
  $('gujlish-status').textContent = gujlishNotice || (busy || state.status === 'error' || state.message === STOPPED ? state.message || '' : '');
}
$('gujlish-button').addEventListener('click', () => startStage('gujlish'));
$('gujlish-alt').addEventListener('click', () => startStage('gujlish', { engine: 'gemini' }));
$('gujlish-stop').addEventListener('click', () => act('stop', { stage: 'gujlish' }, jobId, $('gujlish-stop')));

// ---- The bottom pill (an island): the main thing running, or the talk's next step; other running steps sit beside
// it as circles; tap it to see everything running, stop a step, or read what the icons mean ----
// Everything in progress: the upload, transcriptions, and each talk's queued or running steps.
function activities() {
  const list = [];
  if (upload && !upload.error) {
    list.push({ id: null, group: 'upload', icon: 'upload', label: UPLOAD_PHASES[upload.phase][0], sub: upload.name,
      percent: upload.phase === 'uploading' ? upload.percent : null, waiting: false, stop: ['Cancel', cancelUpload] });
  }
  for (const run of runs) {
    if (ACTIVE.includes(run.status)) {
      list.push({ id: run.id, group: 'asr', icon: 'transcribe', label: { queued: 'Waiting', loading: 'Starting', transcribing: 'Transcribing' }[run.status],
        sub: run.name, percent: run.status === 'transcribing' ? run.progress ?? null : null, waiting: run.status === 'queued',
        stop: ['Pause', () => act('pause', null, run.id)] });
    }
    let englishStop = false;
    STAGE_KEYS.forEach((key, index) => {
      const state = run[key];
      if (!running(state)) return;
      const [icon, verb, noun] = STAGE_INFO[key], group = key === 'gujlish' ? 'gujlish' : 'english';
      const queued = state.status === 'queued';
      const behind = group === 'english' && STAGE_KEYS.slice(1, index).some(other => running(run[other]));  // the English steps run in turn
      const item = { id: run.id, group, icon, label: queued ? `${noun} · ${behind ? 'next' : 'starting'}` : verb, sub: run.name,
        percent: queued ? null : state.progress ?? null, waiting: queued && behind };
      if (starting.has(`${run.id}:${key}`)) { /* just clicked: no Stop until the server has it, so a double-click can't stop it */ }
      else if (group === 'gujlish') item.stop = ['Stop', () => act('stop', { stage: 'gujlish' }, run.id)];
      else if (!englishStop) { englishStop = true; item.stop = ['Stop', () => act('stop', { stage: 'english' }, run.id)]; }
      list.push(item);
    });
  }
  return list;
}
// The talk's next step when nothing of its English is running: a note and up to two buttons.
function nextStep() {
  const gemini = Boolean(caps && caps.gemini);
  const translation = stageState('translation'), captions = stageState('captions'), summary = stageState('summary');
  const failed = [translation, captions, summary].find(s => s.status === 'error' || s.message === STOPPED);
  const english = translation.status === 'complete', summarised = summary.status === 'complete';
  const stale = Boolean(current.english_stale || current.summary_stale);
  const captionsOnly = !stale && english && summarised && Boolean(current.captions_stale);
  const fixes = current.segments.reduce((total, segment) => total + (segment.fixes || []).length, 0);
  const fixed = fixes ? `${fixes} word${fixes === 1 ? '' : 's'} fixed` : 'Fix any misheard words first';
  const translate = (label, path = 'english') => gemini ? [label, () => startStage(path)] : null;
  let kind = 'ok', text, go = null, alt = null;
  if (english && current.youtube_missing && !current.english_stale) {
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
  if (failed && failed.message) { kind = 'stale'; text = failed.message; }
  if (stageNotice) { kind = 'stale'; text = stageNotice; }
  if (caps && !gemini && !english) text = HOSTED ? 'Translation and summaries are not enabled on this site.' : 'To translate, set GEMINI_API_KEY and restart the app (see the README).';
  return { kind, text, go, alt, gemini };
}
function updateDock() {
  const wrap = $('dock-wrap'), dock = $('dock'), list = activities();
  const idle = page === 'run' && Boolean(current);  // a finished transcript on screen: its English, or else its next step
  const live = idle ? list.find(item => item.id === jobId && item.group === 'english') : list.find(item => item.id === jobId) || list[0];
  const step = !live && idle ? nextStep() : null;
  wrap.hidden = !live && !step;
  if (wrap.hidden) { islandOpen = false; islandKey = ''; islandParts = null; return; }
  const groups = new Set(live ? [`${live.id}:${live.group}`] : []);
  const others = list.filter(item => { const group = `${item.id}:${item.group}`; if (groups.has(group)) return false; groups.add(group); return true; });
  const dots = others.length > 2 ? others.slice(0, 1) : others;  // circles for the other things running: two, or one and a count
  const mine = item => live && item.id === live.id && item.group === live.group;  // expanded, the pill's own steps come first
  const rows = islandOpen ? [...list.filter(mine), ...list.filter(item => !mine(item))] : [];
  dock.className = `dock${live ? ' live' : step.kind ? ` ${step.kind}` : ''}${islandOpen ? ' expanded' : ''}`;
  const go = step && step.go, alt = step && step.alt;
  dockAction = go && go[1]; dockAltAction = alt && alt[1];
  $('dock-go').hidden = !go; put($('dock-go'), go ? go[0] : '');
  $('dock-alt').hidden = !alt; put($('dock-alt'), alt ? alt[0] : '');
  // Say where the text goes whenever a button here would start Gemini.
  $('llm-note').hidden = !step || !step.gemini || ![go, alt].some(button => button && /Translate|Update|Run again|summary$|subtitles/.test(button[0]));
  $('island-tap').setAttribute('aria-expanded', String(islandOpen));
  // Redraw rings, circles and rows only when what is running changes. Progress only moves them, so a button you are
  // on or pressing stays where it is.
  const ident = item => [item.id, item.group, item.icon];
  const key = JSON.stringify([islandOpen, live && ident(live), Boolean(step), dots.map(ident), others.length, rows.map(item => [...ident(item), item.stop && item.stop[0]])]);
  if (key !== islandKey) { islandKey = key; drawIsland(live, step, dots, others.length, rows); }
  const parts = islandParts;
  if (live) setRing(parts.icon, live.percent, live.waiting);
  put($('dock-note'), live ? live.label : step.text);
  put($('island-pct'), live && live.percent != null ? `${live.percent}%` : '');
  $('island-tap').title = live ? `${live.label} · ${live.sub}` : step.text;
  dots.forEach((item, index) => {
    const { dot, box } = parts.dots[index], here = item.id === jobId && page === (item.id ? 'run' : 'home');
    setRing(box, item.percent, item.waiting);
    dot.title = `${item.label}${item.percent != null ? ` ${item.percent}%` : ''} · ${item.sub}`;
    dot.setAttribute('aria-label', `${dot.title}${here ? '' : ' · open'}`);
  });
  if (parts.text) put(parts.text, step.text);
  rows.forEach((item, index) => {
    const row = parts.rows[index];
    setRing(row.box, item.percent, item.waiting);
    put(row.label, item.label); put(row.sub, item.sub); put(row.pct, item.percent != null ? `${item.percent}%` : '');
    if (row.button) row.button.setAttribute('aria-label', `${item.stop[0]} · ${item.label} · ${item.sub}`);
  });
}
// Builds the pill's rings, circles and (expanded) rows; updateDock fills in their progress and text.
function drawIsland(live, step, dots, count, rows) {
  const parts = islandParts = { icon: live ? ring(live.icon) : null, dots: [], rows: [], text: null };
  $('island-icon').hidden = !live;
  $('island-icon').replaceChildren(...(live ? [parts.icon] : []));
  $('island-dots').hidden = islandOpen || !count;
  $('island-dots').replaceChildren(...dots.map(item => {
    const dot = el('button', 'island-dot'), box = ring(item.icon); dot.type = 'button';
    dot.append(box);
    dot.onclick = () => { if (item.id === jobId && page === (item.id ? 'run' : 'home')) setIsland(true); else select(item.id); };
    parts.dots.push({ dot, box });
    return dot;
  }), ...(count > 2 ? [moreDot(count - 1)] : []));
  // Expanded: what is running (or the next step in full), each with its own Stop, and a "?" for the icons.
  const more = $('island-more');
  more.hidden = !islandOpen;
  if (!islandOpen) { more.replaceChildren(); return; }
  const head = el('div', 'island-head'), help = el('button', 'island-help', '?');
  help.type = 'button'; help.title = 'What the icons mean'; help.setAttribute('aria-label', 'What the icons mean');
  help.onclick = () => { setIsland(false); openPop('info', 'legend'); };
  head.append(el('p', 'island-title', live ? 'Running now' : 'Next step'), help);
  const nodes = [head];
  if (step) nodes.push(parts.text = el('p', 'island-text'));
  if (step && rows.length) nodes.push(el('p', 'island-title', 'Running now'));
  for (const item of rows) { const row = islandRow(item); parts.rows.push(row); nodes.push(row.node); }
  more.replaceChildren(...nodes);
}
function moreDot(count) {
  const dot = el('button', 'island-dot more', `+${count}`); dot.type = 'button';
  dot.title = `${count} more running`; dot.setAttribute('aria-label', `${count} more running · show all`);
  dot.onclick = () => setIsland(true);
  return dot;
}
function islandRow(item) {
  const node = el('div', 'island-item'), text = el('span', 'island-row-text');
  const row = { node, box: ring(item.icon), label: el('span', 'island-label'), sub: el('span', 'island-sub'), pct: el('span', 'island-pct'), button: null };
  text.append(row.label, row.sub);
  node.append(row.box, text, row.pct);
  if (item.stop) {
    const button = row.button = el('button', 'btn btn-ghost btn-sm', item.stop[0]); button.type = 'button';
    button.onclick = () => { button.disabled = true; Promise.resolve(item.stop[1]()).finally(() => { button.disabled = false; }); };
    node.append(button);
  }
  return row;
}
function setIsland(open) {
  if (islandOpen === open) return;
  islandOpen = open; updateDock();
}
$('island-tap').addEventListener('click', () => setIsland(!islandOpen));
$('dock-go').addEventListener('click', () => { if (dockAction) dockAction(); });
$('dock-alt').addEventListener('click', () => { if (dockAltAction) dockAltAction(); });
// A step shows as starting the moment you click; if the server says no, it goes back to how it was.
async function startStage(path, body) {
  if (!current) return;
  const id = jobId, job = current, gujlish = path === 'gujlish', keys = STAGES_OF[path];
  const run = runs.find(item => item.id === id), buttons = ['dock-go', 'dock-alt', 'gujlish-button', 'gujlish-alt'];
  if (gujlish) gujlishNotice = ''; else stageNotice = '';
  const before = keys.map(key => [key, job[key], run && run[key]]);
  for (const key of keys) {
    const state = { status: 'queued', progress: null, message: 'Starting…' };
    job[key] = { ...state }; if (run) run[key] = { ...state };
    starting.set(`${id}:${key}`, state);
  }
  for (const button of buttons) $(button).disabled = true;
  renderRuns(); updateGujlishBar(); updateDock();
  let failure = '';
  try {
    const response = await postJson(`/api/jobs/${id}/${path}`, body);
    if (signedOut(response)) return;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) failure = data.error || 'Could not start this step.';
    else { pollFailures = 0; await Promise.all([poll(), loadRuns()]); }
  } catch { failure = `Could not reach ${SERVER}.`; }
  finally {
    for (const key of keys) starting.delete(`${id}:${key}`);
    for (const button of buttons) $(button).disabled = false;
    if (current) updateGujlishBar();
    updateDock();  // Stop can show now
  }
  if (!failure) return;
  // Put back how it was, on these objects and on any newer copies a poll or list refresh brought in meanwhile.
  const now = runs.find(item => item.id === id), shownNow = current && current.id === id ? current : null;
  for (const [key, jobState, runState] of before) {
    job[key] = jobState; if (run) run[key] = runState;
    if (shownNow && shownNow !== job) shownNow[key] = jobState;
    if (now && now !== run) now[key] = runState;
  }
  if (gujlish) gujlishNotice = failure; else stageNotice = failure;
  logActivity('fail', `Couldn’t start ${STEP_NAMES[keys[0]]} · “${job.name || (run && run.name) || 'this talk'}”`, id);
  renderRuns();
  if (current) updateGujlishBar();
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

// ---- Popovers from the top bar: Export, Details (with the icon legend) and Activity; one open at a time ----
const POPS = { export: 'export-btn', activity: 'bell-btn', info: 'info-btn' };
function openPop(name, scrollTo) {
  for (const other of Object.keys(POPS)) if (other !== name) closePop(other);
  if (name === 'info') renderInfo();
  if (name === 'activity') renderActivity(true);
  $(name).hidden = false; $(POPS[name]).setAttribute('aria-expanded', 'true');
  if (scrollTo) $(scrollTo).scrollIntoView({ block: 'nearest' });
  $(name).querySelector('.pop-close').focus({ preventScroll: Boolean(scrollTo) });
}
function closePop(name) { $(name).hidden = true; $(POPS[name]).setAttribute('aria-expanded', 'false'); }
function closePops() { for (const name of Object.keys(POPS)) closePop(name); }
function togglePop(name) { if ($(name).hidden) openPop(name); else closePop(name); }
function openExport() { openPop('export'); }
for (const [name, button] of Object.entries(POPS)) $(button).addEventListener('click', () => togglePop(name));
for (const button of document.querySelectorAll('.pop-close')) {
  button.addEventListener('click', () => { const pop = button.closest('.pop'); closePop(pop.id); $(POPS[pop.id]).focus(); });
}
// A click elsewhere closes an open popover and folds the pill back. The click's path is read as it was when clicked,
// since the pill may redraw what was clicked.
document.addEventListener('click', e => {
  const path = e.composedPath();
  for (const [name, button] of Object.entries(POPS)) {
    if (!$(name).hidden && !path.includes($(name)) && !path.includes($(button)) && !path.includes($('island'))) closePop(name);
  }
  if (islandOpen && !path.includes($('island'))) setIsland(false);
});
// Details: this talk's models and the summary's Source note, the workspace, and what the pill's icons mean.
function renderInfo() {
  const job = jobId && (shown || runs.find(run => run.id === jobId));
  $('info-talk').hidden = !job;
  if (!job) return;
  const rows = [];
  if (job.model) rows.push(['Transcribed with', job.model[0].toUpperCase() + job.model.slice(1)]);
  const script = job.mode === 'native' ? 'Everything in Gujarati script' : job.mode === 'mixed' ? 'English words in English letters' : '';
  if (script) rows.push(['Script', script]);
  const made = sourceLine.match(/English translation by (.+?); summary by (.+?)\.(\s|$)/);
  if (made) rows.push(['English by', made[1]], ['Summary by', made[2]]);
  $('info-list').replaceChildren(...rows.flatMap(([term, value]) => [el('dt', '', term), el('dd', '', value)]));
  $('info-source').textContent = sourceLine; $('info-source').hidden = !sourceLine;
}
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
async function act(path, body, id = jobId, button = null) {
  if (button) button.disabled = true;
  if (id === jobId && (path === 'pause' || path === 'resume')) {
    $('run-state').textContent = path === 'pause' ? 'Pausing…' : 'Resuming…'; $('run-state').className = 'status-pill busy';
  }
  // Note a stop before asking for it, so a step that ends while the request is out is logged as stopped, not finished.
  const asked = path !== 'stop' ? [] : (body.stage === 'gujlish' ? ['gujlish'] : STAGES_OF.english)
    .map(key => `${id}:${key}`).filter(key => !stopAsked.has(key));
  for (const key of asked) stopAsked.add(key);
  const failed = message => {
    for (const key of asked) stopAsked.delete(key);
    if (!message) return false;
    notice(message);
    // The notice shows only on that talk's page; anywhere else, the activity log keeps it.
    if (page !== 'run' || id !== jobId) logActivity('fail', `Couldn’t ${path} “${(runs.find(run => run.id === id) || {}).name || 'a talk'}” · ${message}`, id);
    return false;
  };
  try {
    const response = await postJson(`/api/jobs/${id}/${path}`, body);
    if (signedOut(response)) return failed('');
    const data = await response.json().catch(() => ({}));
    if (!response.ok) return failed(data.error || 'That did not work. Try again.');
    return true;
  } catch { return failed(`Could not reach ${SERVER}.`); }
  finally {
    if (button) button.disabled = false;
    if (jobId === id && path !== 'delete') { pollFailures = 0; poll(); }
    loadRuns();
  }
}
$('pause-button').addEventListener('click', () => act('pause', null, jobId, $('pause-button')));
$('resume-button').addEventListener('click', () => act('resume', null, jobId, $('resume-button')));
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
  if (await act('delete', null, id, $('delete-button'))) {
    logActivity('info', `${finished ? 'Deleted' : 'Cancelled'} “${job.name}”`);
    runs = runs.filter(run => run.id !== id); if (jobId === id) select(null);
  }
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

const uploadChanged = () => { renderBusy(); updateDock(); };
async function cancelUpload() {
  const mine = upload;
  if (!mine) return;
  if (!mine.error && !await ask('Cancel this upload?', `“${mine.name}” stops uploading and is not transcribed.`,
    [{ label: 'Cancel upload', value: true, kind: 'btn-accent danger' }, { label: 'Keep uploading', value: false }])) return;
  if (upload !== mine) return;
  mine.controller.abort(); upload = null;
  if (!mine.error) logActivity('info', `Upload cancelled · “${mine.name}”`);
  uploadChanged();
}
async function fingerprintOf(file) {  // SHA-256 of the file, as web/recordings.py computes it; null if unavailable
  try {
    const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
    return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  } catch { return null; }
}
async function uploadInPieces(file, mine) {
  // Sent in pieces because hosting platforms cap the size of one request; offsets make a resent piece harmless.
  const signal = mine.controller.signal;
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
    mine.percent = Math.round(offset / file.size * 100);
    uploadChanged();
  }
  return id;
}
async function addRecording(file) {
  if (upload && !upload.error) return ask('One upload at a time', `“${upload.name}” is still uploading. Add this one when it has finished.`, [{ label: 'OK', value: true, kind: 'btn-accent' }]);
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) return ask('This file is too large', `Files up to ${MAX_UPLOAD_MB} MB can be added here.`, [{ label: 'OK', value: true, kind: 'btn-accent' }]);
  if (page !== 'home') select(null);
  // The card shows from the moment of the drop: Reading → Checking → Uploading % → Starting.
  const controller = new AbortController();
  upload = { name: file.name, size: file.size, phase: 'reading', percent: 0, error: '', controller };
  const mine = upload, alive = () => upload === mine;
  logActivity('start', `Upload started · “${file.name}”`);
  uploadChanged(); loadRuns();
  // The server knows recordings by a fingerprint of their audio, so a renamed copy is recognised too, and audio it
  // already has is never uploaded again.
  const print = await fingerprintOf(file);
  if (!alive()) return;
  mine.phase = 'checking'; uploadChanged();
  let known = { stored: false, runs: [] };
  try {
    const response = print ? await fetch(`/api/recordings/${print}`, { signal: controller.signal }) : null;
    if (response && signedOut(response)) return;
    if (response && response.ok) known = await response.json();
  } catch { /* checking is optional: upload as usual */ }
  if (!alive()) return;
  const same = known.runs[0];
  if (same) {
    await modalClosed();  // "Cancel this upload?" may still be open
    if (!alive()) return;
    upload = null; uploadChanged();  // nothing runs while you decide, so the page stops checking for news
    const others = known.runs.length > 1 ? ` (and ${known.runs.length - 1} more)` : '';
    const choice = await ask('You already have this recording',
      `“${same.name}”${same.started ? `, added ${when(same.started)},` : ''}${others} has exactly the same audio.`,
      [{ label: 'Open it', value: 'open', kind: 'btn-accent' }, { label: 'Transcribe again', value: 'again' }, { label: 'Cancel', value: null }]);
    if (choice !== 'again') {
      logActivity('info', `Not uploaded · you already have “${same.name}”`, same.id);
      if (choice === 'open') select(same.id);
      return;
    }
    if (upload) { logActivity('info', `Not uploaded · “${upload.name}” started first`); return; }  // added meanwhile
    upload = mine; uploadChanged(); loadRuns();
  }
  const form = new FormData(); form.append('model', $('model').value); form.append('mode', $('mode').value); form.append('name', file.name);
  const start = () => fetch('/api/jobs', { method: 'POST', body: form, signal: controller.signal });
  try {
    let response = null;
    if (known.stored) {  // the server has this audio already: start straight away
      form.append('fingerprint', print); mine.stored = true; mine.phase = 'starting'; uploadChanged();
      response = await start();
      if (signedOut(response)) return;
      if (response.status === 409) { form.delete('fingerprint'); mine.stored = false; response = null; }  // gone since
    }
    if (!response) {
      mine.phase = 'uploading'; mine.percent = 0; uploadChanged();
      const uploadId = await uploadInPieces(file, mine);
      if (!uploadId) return;
      form.append('upload_id', uploadId);
      mine.phase = 'starting'; uploadChanged();
      response = await start();
      if (signedOut(response)) return;
    }
    const data = await response.json().catch(() => ({}));
    if (response.status !== 202) throw new Error(data.error || 'Upload failed. Please try again.');
    if (alive()) upload = null;
    logActivity('done', `Upload finished · “${file.name}”`, data.id || null);
    uploadChanged();
    await loadRuns();
  } catch (error) {
    if (controller.signal.aborted || !alive()) return;
    upload.error = error.message || `Could not connect to ${SERVER}.`;
    logActivity('fail', `Upload failed · “${file.name}”`);
    uploadChanged();
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
// Escape closes an open popover, else folds the pill, else closes the fixer. An open dialog handles its own.
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || $('modal').open) return;
  const open = Object.keys(POPS).find(name => !$(name).hidden);
  if (open) { closePop(open); $(POPS[open]).focus(); }
  else if (islandOpen) { setIsland(false); $('island-tap').focus(); }
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
$('legend-list').replaceChildren(...LEGEND.map(([icon, text]) => {
  const item = el('li'), mark = el('i'); mark.innerHTML = ICONS[icon];
  item.append(mark, el('span', '', text));
  return item;
}));
renderActivity();
describeOptions();
loadUsage();
loadDictionary();
// Steps already running or finished when the page opens are not news: the log starts with what changes from here.
loadRuns().then(() => { logReady = true; const saved = sessionStorage.getItem('asr-job'); if (saved && runs.some(run => run.id === saved)) select(saved); });
