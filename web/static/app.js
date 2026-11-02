const $ = id => document.getElementById(id);
// runs: the sidebar list. jobId: the run on screen ('upload' while a file is uploading). current: its finished transcript.
let runs = [], jobId = null, shown = null, current = null, upload = null;
let pollTimer = null, runsTimer = null, pollFailures = 0, noticeTimer = null;
let renderedFor = null, renderedRev = null, caps = null, tab = 'transcript', stageNotice = '';
let sel = null, touched = { gu: false, latin: false }, suggestTimer = null, suggestTicket = 0, noteTimer = null;
const TABS = ['transcript', 'gujlish', 'translation', 'summary'];
const WORD_TABS = ['transcript', 'gujlish'];
const STAGE_KEYS = ['gujlish', 'translation', 'summary'];
const ACTIVE = ['queued', 'loading', 'transcribing'];
const STOPPED = 'Stopped. Start it again to continue where it left off.';  // web.server.STOPPED
const HOSTED = document.body.dataset.hosted === '1';
const CHUNK_BYTES = Number(document.body.dataset.chunkBytes) || 8 * 1024 * 1024;
const MAX_UPLOAD_MB = Number(document.body.dataset.maxUploadMb) || 4096;
const SERVER = HOSTED ? 'the server' : 'the local server';
const EDGE = /^[.,!?;:"'()[\]{}…।॥]+|[.,!?;:"'()[\]{}…।॥]+$/g;  // asr.postprocess.EDGE_PUNCTUATION
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
const narrow = () => window.matchMedia('(max-width: 760px)').matches;

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
      const button = document.createElement('button'); button.type = 'button'; button.className = kind || 'secondary'; button.textContent = label;
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

// Summary text comes from a model, so it is rendered with DOM nodes (never innerHTML).
function inline(parent, text) {
  for (const part of text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/)) {
    if (!part) continue;
    let node;
    if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) { node = document.createElement('strong'); node.textContent = part.slice(2, -2); }
    else if (part.length > 2 && part.startsWith('*') && part.endsWith('*')) { node = document.createElement('em'); node.textContent = part.slice(1, -1); }
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
      if (!list) { list = document.createElement('ul'); element.append(list); }
      const item = document.createElement('li'); inline(item, line.slice(2)); list.append(item); continue;
    }
    list = null;
    const heading = line.match(/^(#{1,3}) (.*)$/);
    if (heading) { const h = document.createElement(`h${heading[1].length + 2}`); inline(h, heading[2]); element.append(h); }
    else if (line.startsWith('> ')) { const q = document.createElement('blockquote'); inline(q, line.slice(2)); element.append(q); }
    else if (line.trim()) { const p = document.createElement('p'); inline(p, line); element.append(p); }
  }
}

// ---- The sidebar: every run with what it is doing now ----
function runBadge(run) {
  const percent = run.progress != null ? ` ${run.progress}%` : '';
  if (run.status === 'queued') return ['Waiting', 'busy'];
  if (run.status === 'loading') return ['Starting', 'busy'];
  if (run.status === 'transcribing') return [`Transcribing${percent}`, 'busy'];
  if (run.status === 'paused') return ['Paused', 'paused'];
  if (run.status === 'error') return ['Stopped', 'error'];
  for (const [key, verb] of [['translation', 'Translating'], ['summary', 'Summarizing'], ['gujlish', 'Gujlish']]) {
    const state = run[key];
    if (running(state)) return [state.status === 'queued' ? `${verb} soon` : `${verb}${state.progress != null ? ` ${state.progress}%` : ''}`, 'busy'];
  }
  return null;
}
function runItem({ id, name, detail, badge, progress, selected }) {
  const item = document.createElement('button'); item.type = 'button'; item.className = 'run-item';
  item.classList.toggle('selected', selected); item.setAttribute('aria-current', selected ? 'true' : 'false');
  const title = document.createElement('span'); title.className = 'run-name'; title.textContent = name;
  const meta = document.createElement('span'); meta.className = 'run-detail'; meta.textContent = detail;
  item.append(title, meta);
  if (badge) { const tag = document.createElement('span'); tag.className = `badge ${badge[1]}`; tag.textContent = badge[0]; item.append(tag); }
  if (progress != null) { const bar = document.createElement('span'); bar.className = 'mini-bar'; const fill = document.createElement('i'); fill.style.width = `${progress}%`; bar.append(fill); item.append(bar); }
  item.onclick = () => { select(id); if (narrow()) $('main').scrollIntoView({ behavior: 'smooth' }); };
  return item;
}
function renderRuns() {
  const items = [];
  if (upload) items.push(runItem({ id: 'upload', name: upload.name, detail: upload.error ? 'Upload failed' : `Uploading · ${mb(upload.size)}`,
    badge: upload.error ? ['Failed', 'error'] : [`${upload.percent}%`, 'busy'], progress: upload.error ? null : upload.percent, selected: jobId === 'upload' }));
  for (const run of runs) {
    const badge = runBadge(run);
    items.push(runItem({ id: run.id, name: run.name, detail: [when(run.started), minutes(run.duration)].filter(Boolean).join(' · '),
      badge, progress: run.status === 'transcribing' ? run.progress : null, selected: run.id === jobId }));
  }
  if (!items.length) { const p = document.createElement('p'); p.className = 'muted'; p.textContent = 'No recordings yet. Add one above.'; items.push(p); }
  $('history').replaceChildren(...items);
  $('runs-count').textContent = runs.length || '';
}
async function loadRuns() {
  clearTimeout(runsTimer);
  try {
    const response = await fetch('/api/history');
    if (signedOut(response)) return;
    if (response.ok) { runs = await response.json(); renderRuns(); }
  } catch { /* the server may be restarting: try again shortly */ }
  runsTimer = setTimeout(loadRuns, upload || runs.some(busyRun) ? 3000 : 20000);
}
function mergeRun(job) {
  const run = runs.find(item => item.id === job.id);
  if (!run) return;
  for (const key of ['status', 'progress', 'message', 'duration', ...STAGE_KEYS]) run[key] = job[key];
  renderRuns();
}
function sideTab(name) {
  $('side-runs').setAttribute('aria-selected', String(name === 'runs')); $('side-dict').setAttribute('aria-selected', String(name === 'dict'));
  $('runs-pane').hidden = name !== 'runs'; $('dict-pane').hidden = name !== 'dict';
}
$('side-runs').addEventListener('click', () => sideTab('runs'));
$('side-dict').addEventListener('click', () => sideTab('dict'));

// ---- The run on screen ----
function view(name) {  // 'empty', 'status' or 'result'
  $('empty-state').hidden = name !== 'empty';
  $('run-view').hidden = name === 'empty';
  $('processing').hidden = name !== 'status';
  $('result').hidden = name !== 'result';
}
function select(id) {
  clearTimeout(pollTimer); closeEditor(); notice('');
  jobId = id; shown = null; current = null; renderedFor = null; renderedRev = null; stageNotice = '';
  if (id && id !== 'upload') sessionStorage.setItem('asr-job', id); else sessionStorage.removeItem('asr-job');
  renderRuns();
  if (!id) return view('empty');
  if (id === 'upload') return renderUpload();
  const run = runs.find(item => item.id === id);
  renderHead(run || { name: 'Opening…', status: 'loading' });
  view('status'); $('status-label').textContent = 'Opening…'; $('status-detail').textContent = ''; progress(null);
  $('progress-description').textContent = ''; $('spinner').hidden = false;
  pollFailures = 0; poll();
}
function progress(percent, still) {
  $('progress-fill').classList.toggle('indeterminate', percent == null && !still);
  $('progress-fill').style.width = percent == null ? (still ? '0' : '') : `${percent}%`;
  $('progress-percent').textContent = percent == null ? '' : `${percent}%`;
}
function stateLabel(job) {
  const badge = runBadge(job);
  if (job.status === 'complete') return badge || ['Ready', 'ready'];
  return badge || [job.status, 'busy'];
}
function renderHead(job) {
  $('run-title').textContent = job.name || '';
  const script = job.mode === 'native' ? 'Gujarati script' : job.mode === 'mixed' ? 'English words in English letters' : '';
  $('run-meta').textContent = [when(job.started), minutes(job.duration), script].filter(Boolean).join(' · ');
  const [label, kind] = stateLabel(job);
  $('run-state').textContent = label; $('run-state').className = `state ${kind}`;
  const active = ACTIVE.includes(job.status), stopped = job.status === 'paused' || job.status === 'error';
  $('pause-button').hidden = !active;
  $('resume-button').hidden = !stopped;
  $('delete-button').hidden = false;
  $('delete-button').textContent = job.status === 'complete' ? 'Delete' : 'Cancel';
}
function renderStatus(job) {
  view('status');
  const active = ACTIVE.includes(job.status);
  $('spinner').hidden = !active;
  $('processing').classList.toggle('stopped', !active);
  $('status-label').textContent = {
    queued: job.ahead ? `Waiting · ${job.ahead} recording${job.ahead === 1 ? '' : 's'} ahead` : 'Waiting to start…',
    loading: 'Preparing transcription…', transcribing: 'Transcribing…', paused: 'Paused', error: 'Stopped',
  }[job.status] || job.message;
  $('status-detail').textContent = job.message + (active && job.device ? ` · ${job.device.toUpperCase()}` : '');
  const processed = job.processed ? `${time(job.processed)}${job.duration ? ` / ${time(job.duration)}` : ''} audio done` : '';
  $('progress-description').textContent = job.status === 'transcribing' || !active ? processed : 'Progress begins when the model is ready';
  progress(job.status === 'transcribing' || !active ? (job.progress ?? (active ? null : 0)) : null, !active);
}
function renderUpload() {
  if (!upload) return select(null);
  renderHead({ name: upload.name, status: 'loading' });
  $('run-meta').textContent = mb(upload.size);
  $('run-state').textContent = upload.error ? 'Failed' : 'Uploading'; $('run-state').className = `state ${upload.error ? 'error' : 'busy'}`;
  $('pause-button').hidden = true; $('resume-button').hidden = true;
  $('delete-button').textContent = upload.error ? 'Dismiss' : 'Cancel upload';
  view('status');
  $('spinner').hidden = Boolean(upload.error); $('processing').classList.toggle('stopped', Boolean(upload.error));
  $('status-label').textContent = upload.error ? 'Upload failed' : upload.checking ? 'Checking the audio…' : 'Uploading recording…';
  $('status-detail').textContent = upload.error || (HOSTED ? 'Sending your recording to the server. You can open other recordings meanwhile.' : 'Saving your recording to the local workspace.');
  $('progress-description').textContent = 'Uploaded';
  progress(upload.checking ? null : upload.percent, Boolean(upload.error));
}
async function poll() {
  clearTimeout(pollTimer);
  const id = jobId;
  if (!id || id === 'upload') return;
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
    mergeRun(job); renderHead(job);
    if (job.status === 'complete') {
      renderResult(job);
      if (STAGE_KEYS.some(key => running(job[key]))) pollTimer = setTimeout(poll, 1500);
      return;
    }
    current = null; renderStatus(job);
    if (ACTIVE.includes(job.status)) pollTimer = setTimeout(poll, 1000);
  } catch {
    if (jobId !== id) return;
    // Cloud Run sometimes swaps the server's machine; the new one picks the run up again within seconds.
    pollFailures++;
    if (pollFailures > 2) notice(`Reconnecting to ${SERVER}…`);
    pollTimer = setTimeout(poll, Math.min(10000, 1000 * pollFailures));
  }
}
function renderResult(job) {
  const first = renderedFor !== job.id;
  current = job; view('result');
  if (pollFailures === 0 && $('run-notice').textContent.startsWith('Reconnecting')) notice('');
  if (first || job.rev !== renderedRev) {
    renderedRev = job.rev;
    renderWords($('transcript'), false); renderWords($('gujlish'), true); markSelection();
  }
  $('translation').textContent = job.translation_text || '';
  if (job.summary_markdown) renderMarkdown($('summary'), job.summary_markdown); else $('summary').replaceChildren();
  markTabs();
  if (first) { renderedFor = job.id; setTab('transcript'); loadCaps(); }
  else { updateExports(); updateStageBox(); }
}

// ---- Pause, resume, cancel and delete ----
async function act(path, body) {
  const id = jobId;
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
  if (jobId === 'upload') {
    if (upload && !upload.error && !await ask('Cancel this upload?', `“${upload.name}” stops uploading and is not transcribed.`,
      [{ label: 'Cancel upload', value: true, kind: 'primary danger' }, { label: 'Keep uploading', value: false }])) return;
    if (upload) upload.controller.abort();
    upload = null; select(null); return;
  }
  const job = shown || runs.find(run => run.id === jobId);
  if (!job) return;
  const finished = job.status === 'complete';
  const yes = await ask(finished ? 'Delete this recording?' : 'Cancel this recording?',
    finished ? `“${job.name}” and its transcript, Gujlish, English and summary are deleted. Fixes already in your dictionary stay.`
      : `“${job.name}” stops, and the recording and the progress so far are deleted.`,
    [{ label: finished ? 'Delete' : 'Cancel recording', value: true, kind: 'primary danger' }, { label: 'Keep it', value: false }]);
  if (!yes) return;
  const id = jobId;
  if (await act('delete')) { runs = runs.filter(run => run.id !== id); if (jobId === id) select(null); }
});

// ---- Adding a recording: uploads carry on in the background while you look at other runs ----
function chosen() {
  const file = $('audio-input').files[0];
  $('file-name').textContent = file ? file.name : 'New recording';
  $('file-detail').textContent = file ? `${mb(file.size)} · ready to transcribe` : 'Drop audio here or click to choose';
  $('start-row').hidden = !file; $('dropzone').classList.toggle('chosen', Boolean(file));
}
function clearFile() { $('audio-input').value = ''; chosen(); }
function takeFile(file) {
  if (!file) return;
  const transfer = new DataTransfer(); transfer.items.add(file); $('audio-input').files = transfer.files; chosen();
  sideTab('runs'); $('start-button').focus();
}
$('audio-input').addEventListener('change', chosen);
$('clear-file').addEventListener('click', clearFile);
let dragDepth = 0;
window.addEventListener('dragenter', e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); dragDepth++; $('drop-overlay').hidden = false; } });
window.addEventListener('dragover', e => { if (e.dataTransfer.types.includes('Files')) e.preventDefault(); });
window.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; $('drop-overlay').hidden = true; } });
window.addEventListener('drop', e => { if (!e.dataTransfer.files.length) return; e.preventDefault(); dragDepth = 0; $('drop-overlay').hidden = true; takeFile(e.dataTransfer.files[0]); });

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
    renderRuns(); if (jobId === 'upload') renderUpload();
  }
  return id;
}
$('upload-form').addEventListener('submit', async e => {
  e.preventDefault();
  const file = $('audio-input').files[0];
  if (!file) return;
  if (upload && !upload.error) return ask('One upload at a time', `“${upload.name}” is still uploading. Add this one when it has finished.`, [{ label: 'OK', value: true, kind: 'primary' }]);
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) return ask('This file is too large', `Files up to ${MAX_UPLOAD_MB} MB can be added here.`, [{ label: 'OK', value: true, kind: 'primary' }]);
  // The server knows recordings by a fingerprint of their audio, so a renamed copy is recognised too, and audio it
  // already has is never uploaded again.
  $('start-button').disabled = true; $('file-detail').textContent = 'Checking whether you already have it…';
  const print = await fingerprintOf(file);
  let known = { stored: false, runs: [] };
  try {
    const response = print ? await fetch(`/api/recordings/${print}`) : null;
    if (response && signedOut(response)) return;
    if (response && response.ok) known = await response.json();
  } catch { /* checking is optional: upload as usual */ }
  finally { $('start-button').disabled = false; chosen(); }
  const same = known.runs[0];
  if (same) {
    const others = known.runs.length > 1 ? ` (and ${known.runs.length - 1} more)` : '';
    const choice = await ask('You already have this recording',
      `“${same.name}”${same.started ? `, added ${when(same.started)},` : ''}${others} has exactly the same audio.`,
      [{ label: 'Open it', value: 'open', kind: 'primary' }, { label: 'Transcribe again', value: 'again' }, { label: 'Cancel', value: null }]);
    if (choice === 'open') { clearFile(); select(same.id); return; }
    if (choice !== 'again') return;
  }
  const form = new FormData(); form.append('model', $('model').value); form.append('mode', $('mode').value); form.append('name', file.name);
  clearFile();  // so a second click cannot send it again
  upload = { name: file.name, size: file.size, percent: 0, error: '', controller: new AbortController() };
  const mine = upload;
  select('upload'); loadRuns();
  const start = () => fetch('/api/jobs', { method: 'POST', body: form, signal: mine.controller.signal });
  try {
    let response = null;
    if (known.stored) {  // the server has this audio already: start straight away
      form.append('fingerprint', print); mine.checking = true; if (jobId === 'upload') renderUpload();
      response = await start();
      if (signedOut(response)) return;
      if (response.status === 409) { form.delete('fingerprint'); mine.checking = false; response = null; }  // gone since
    }
    if (!response) {
      const uploadId = await uploadInPieces(file, mine.controller.signal);
      if (!uploadId) return;
      form.append('upload_id', uploadId);
      mine.checking = true; if (jobId === 'upload') renderUpload();
      response = await start();
      if (signedOut(response)) return;
    }
    const data = await response.json().catch(() => ({}));
    if (response.status !== 202) throw new Error(data.error || 'Upload failed. Please try again.');
    const watching = jobId === 'upload';
    upload = null;
    await loadRuns();
    if (watching) select(data.id); else renderRuns();
  } catch (error) {
    if (mine.controller.signal.aborted || upload !== mine) return;
    upload.error = error.message || `Could not connect to ${SERVER}.`; upload.checking = false;
    renderRuns(); if (jobId === 'upload') renderUpload();
  }
});

// ---- Result views ----
// Gujarati and Gujlish are shown one segment per line, each word its own element, so a click picks a word and
// word N of a line is the same word in both views.
function segmentWords(index, latin) {
  return latin ? ((current.gujlish_tokens || [])[index] || []) : words(current.segments[index].text);
}
function renderWords(panel, latin) {
  const rows = [];
  if (!latin || current.gujlish_tokens) current.segments.forEach((segment, index) => {
    const list = segmentWords(index, latin);
    if (!list.length) return;
    const fixed = new Map();
    for (const fix of segment.fixes || []) for (let i = fix.start; i < fix.end; i++) fixed.set(i, fix.heard);
    const row = document.createElement('div'); row.className = 'line';
    const stamp = document.createElement('span'); stamp.className = 'stamp'; stamp.textContent = time(segment.start);
    const line = document.createElement('p'); line.className = 'words'; line.dataset.seg = index;
    if (!latin) line.lang = 'gu';
    list.forEach((word, i) => {
      if (i) line.append(' ');
      const span = document.createElement('span'); span.className = 'w'; span.dataset.i = i; span.textContent = word;
      if (fixed.has(i)) { span.classList.add('fixed'); span.title = `Fixed by your dictionary · heard “${fixed.get(i)}”`; }
      line.append(span);
    });
    row.append(stamp, line); rows.push(row);
  });
  panel.replaceChildren(...rows);
}
function markSelection() {
  for (const span of document.querySelectorAll('.w.selected')) span.classList.remove('selected');
  if (!sel) return;
  for (const id of WORD_TABS) {
    for (const span of $(id).querySelectorAll(`.words[data-seg="${sel.seg}"] .w`)) {
      const i = Number(span.dataset.i);
      if (i >= sel.start && i < sel.end) span.classList.add('selected');
    }
  }
}

async function loadCaps() {
  try { const response = await fetch('/api/capabilities'); caps = response.ok ? await response.json() : null; }
  catch { caps = null; }
  updateStageBox();
}
function stageState(key) { return (current && current[key]) || { status: 'idle' }; }
function stageBoxKey() { return tab === 'gujlish' ? 'gujlish' : (tab === 'translation' || tab === 'summary') ? 'english' : null; }
function updateStageBox() {
  const key = stageBoxKey(), box = $('stage-box');
  $('edit-hint').hidden = !current || !WORD_TABS.includes(tab) || (tab === 'gujlish' && !current.gujlish_tokens);
  box.hidden = !current || !key;
  if (box.hidden) return;
  const gemini = Boolean(caps && caps.gemini);
  let state, explain, label = null, alt = false, unavailable = '';
  if (key === 'gujlish') {
    state = stageState('gujlish');
    const stale = state.status === 'complete' && !current.gujlish_tokens;
    explain = stale ? 'The Gujlish no longer lines up with the Gujarati. Write it again.'
      : 'Gujarati in Latin letters, word for word. Fix a word here or in the Gujarati: the Gujlish spellings you type are remembered too.';
    if (['idle', 'error'].includes(state.status) || stale) label = 'Write Gujlish';
    alt = gemini && !running(state);
  } else {
    const translation = stageState('translation'), summary = stageState('summary');
    state = running(translation) || translation.status === 'error' ? translation : summary;
    const stale = Boolean(current.english_stale || current.summary_stale);
    const done = translation.status === 'complete' && summary.status === 'complete';
    explain = stale ? 'You’ve fixed words since this English was made. Updating re-translates only the lines you changed, then rewrites the summary.'
      : done ? 'English and summary are up to date with your fixes.'
      : 'Fix the words first. Then this translates the Gujarati into natural spoken English that keeps the speaker’s tone, and summarizes it.';
    label = stale ? 'Update English & summary' : done ? 'Run again' : 'Translate & summarize';
    if (!gemini) { label = null; unavailable = HOSTED ? 'Translation and summaries are not enabled on this site.' : 'To translate and summarize, set GEMINI_API_KEY in the terminal and restart the app (see the README).'; }
  }
  const busy = running(state) || (key === 'english' && (running(stageState('translation')) || running(stageState('summary'))));
  box.classList.toggle('compact', !busy && key === 'english' && Boolean(current.translation_text) && !current.english_stale && !current.summary_stale);
  $('stage-explain').textContent = explain;
  $('llm-note').hidden = key !== 'english' || !gemini || busy;
  $('stage-button').hidden = !label || busy; $('stage-button').textContent = label || '';
  $('stage-stop').hidden = !busy;
  $('stage-alt').hidden = !alt || busy;
  $('stage-track').hidden = !busy;
  $('stage-fill').classList.toggle('indeterminate', busy && state.progress == null);
  $('stage-fill').style.width = busy && state.progress != null ? `${state.progress}%` : '';
  let status = '';
  if (stageNotice) status = stageNotice;
  else if (busy || state.status === 'error' || state.message === STOPPED) status = state.message || '';
  else if (unavailable) status = unavailable;
  $('stage-status').textContent = status;
}
function tabText(name) {
  if (!current) return '';
  return { transcript: current.text, gujlish: current.gujlish_text, translation: current.translation_text, summary: current.summary_markdown }[name] || '';
}
function updateExports() {
  if (!current) return;
  const links = {
    transcript: [['txt', 'TXT'], ['docx', 'DOCX'], ['srt', 'SRT']],
    gujlish: [['gujlish-txt', 'TXT'], ['gujlish-docx', 'DOCX'], ['gujlish-srt', 'SRT']],
    translation: [['en-txt', 'TXT'], ['en-docx', 'DOCX'], ['en-srt', 'SRT']],
    summary: [['summary-md', 'MD'], ['summary-docx', 'DOCX']],
  }[tab];
  const has = Boolean(tabText(tab));
  $('copy-button').hidden = !has;
  ['txt-download', 'docx-download', 'srt-download'].forEach((id, index) => {
    $(id).hidden = !has || !links[index];
    if ($(id).hidden) return;
    $(id).href = `/api/jobs/${jobId}/download/${links[index][0]}`;
    $(id).textContent = `↓ ${links[index][1]}`;
  });
  $('copy-button').textContent = 'Copy text';
}
function setTab(name) {
  tab = name; stageNotice = ''; closeEditor();
  for (const id of TABS) { $(`tab-${id}`).setAttribute('aria-selected', String(id === name)); $(id).hidden = id !== name; }
  updateExports(); updateStageBox();
  if (name !== 'transcript') loadCaps();
}
for (const id of TABS) $(`tab-${id}`).addEventListener('click', () => setTab(id));
function markTabs() {
  const tabs = [
    ['gujlish', 'Gujlish', Boolean(current.gujlish_tokens), false],
    ['translation', 'English', Boolean(current.translation_text), current.english_stale],
    ['summary', 'Summary', Boolean(current.summary_markdown), current.summary_stale],
  ];
  for (const [id, name, has, stale] of tabs) {
    const label = $(`tab-${id}`), state = current[id]; label.replaceChildren(name);
    if (running(state)) {
      const work = document.createElement('span'); work.className = 'tab-work';
      work.textContent = state.status === 'running' && state.progress != null ? `${state.progress}%` : '…';
      label.append(work);
    } else if (has) { const dot = document.createElement('span'); dot.className = stale ? 'dot stale' : 'dot'; dot.title = stale ? 'Made before your latest fixes' : ''; label.append(dot); }
  }
}
async function startStage(path, body) {
  if (!current) return;
  stageNotice = ''; $('stage-button').disabled = true; $('stage-alt').disabled = true;
  try {
    const response = await postJson(`/api/jobs/${jobId}/${path}`, body);
    if (signedOut(response)) return;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { stageNotice = data.error || 'Could not start this step.'; updateStageBox(); return; }
    pollFailures = 0; poll(); loadRuns();
  } catch { stageNotice = `Could not reach ${SERVER}.`; updateStageBox(); }
  finally { $('stage-button').disabled = false; $('stage-alt').disabled = false; }
}
$('stage-button').addEventListener('click', () => startStage(stageBoxKey() === 'gujlish' ? 'gujlish' : 'english'));
$('stage-alt').addEventListener('click', () => startStage('gujlish', { engine: 'gemini' }));
$('stage-stop').addEventListener('click', () => act('stop', { stage: stageBoxKey() === 'gujlish' ? 'gujlish' : 'english' }));
$('copy-button').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(tabText(tab)); $('copy-button').textContent = 'Copied ✓'; setTimeout(() => $('copy-button').textContent = 'Copy text', 2000); }
  catch { const selection = window.getSelection(); const range = document.createRange(); range.selectNodeContents($(tab)); selection.removeAllRanges(); selection.addRange(range); $('copy-button').textContent = 'Select & copy'; }
});

// ---- The word editor: pick words in the Gujarati or Gujlish, type the right word in either script, and save.
// Typing Gujlish fills in the Gujarati (and suggests spellings); typing Gujarati fills in the Gujlish.
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
  $('ed-heard').textContent = heard; $('ed-heard-latin').textContent = heardLatin ? `· ${heardLatin}` : '';
  if (!keepTyped || !touched.gu) $('ed-gu').value = bare(heard);
  if (!keepTyped || !touched.latin) $('ed-latin').value = bare(heardLatin);
  const others = countMatches(heard) - 1;
  $('ed-all-row').hidden = others < 1; $('ed-count').textContent = others === 1 ? '1 place' : `${others} places`;
  $('ed-left').disabled = sel.start === 0;
  $('ed-right').disabled = sel.end >= segmentWords(sel.seg, false).length;
  describeEdit(); markSelection(); placeEditor();
}
function openEditor() {
  touched = { gu: false, latin: false }; $('ed-suggest').replaceChildren(); $('ed-error').textContent = '';
  $('ed-all').checked = true; $('ed-remember').checked = true; $('editor').hidden = false;
  fillEditor(false);
  const input = tab === 'gujlish' ? $('ed-latin') : $('ed-gu');
  input.focus(); input.select();
}
function closeEditor() { $('editor').hidden = true; sel = null; markSelection(); }
function placeEditor() {
  const editor = $('editor');
  if (editor.hidden || !sel || narrow()) return;  // phones: a bottom sheet
  const last = $(tab).querySelector(`.words[data-seg="${sel.seg}"] .w[data-i="${sel.end - 1}"]`);
  if (!last) return;
  const box = last.getBoundingClientRect(), right = document.documentElement.clientWidth - editor.offsetWidth - 8;
  const below = box.bottom + 8 + editor.offsetHeight < window.innerHeight ? box.bottom + 8 : Math.max(8, box.top - editor.offsetHeight - 8);
  editor.style.left = `${window.scrollX + Math.max(8, Math.min(box.left, right))}px`;
  editor.style.top = `${window.scrollY + below}px`;
}
function pick(seg, start, end) { sel = { seg, start, end }; openEditor(); }
for (const id of WORD_TABS) {
  $(id).addEventListener('click', e => {
    const word = e.target.closest('.w');
    if (!word || !current || !window.getSelection().isCollapsed) return;  // a drag is handled on mouseup
    const seg = Number(word.parentElement.dataset.seg), i = Number(word.dataset.i);
    if (e.shiftKey && sel && sel.seg === seg) pick(seg, Math.min(sel.start, i), Math.max(sel.end, i + 1));
    else pick(seg, i, i + 1);
  });
  $(id).addEventListener('mouseup', () => {
    const selection = window.getSelection();
    if (selection.isCollapsed || !selection.rangeCount) return;
    const range = selection.getRangeAt(0), node = range.commonAncestorContainer;
    const line = (node.nodeType === 1 ? node : node.parentElement).closest('.words');
    if (!line) return;  // across lines: left as an ordinary text selection
    const hit = [...line.querySelectorAll('.w')].filter(span => range.intersectsNode(span)).map(span => Number(span.dataset.i));
    if (!hit.length) return;
    selection.removeAllRanges();
    pick(Number(line.dataset.seg), Math.min(...hit), Math.max(...hit) + 1);
  });
  $(id).addEventListener('scroll', placeEditor);
}
window.addEventListener('resize', placeEditor);
document.addEventListener('mousedown', e => {
  if (!$('editor').hidden && !e.target.closest('#editor') && !e.target.closest('.w')) closeEditor();
});
$('ed-left').addEventListener('click', () => { sel.start--; fillEditor(true); });
$('ed-right').addEventListener('click', () => { sel.end++; fillEditor(true); });
$('ed-close').addEventListener('click', closeEditor);
$('ed-cancel').addEventListener('click', closeEditor);
function chips(options) {
  $('ed-suggest').replaceChildren(...options.map(option => {
    const chip = document.createElement('button'); chip.type = 'button'; chip.className = 'chip'; chip.lang = 'gu'; chip.textContent = option;
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
      if (signedOut(response) || !response.ok || ticket !== suggestTicket || $('editor').hidden) return;
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
for (const id of ['ed-gu', 'ed-latin']) $(id).addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); saveEdit(); } });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('editor').hidden) closeEditor(); });
async function saveEdit() {
  const correct = $('ed-gu').value.trim(), latin = $('ed-latin').value.trim(), heard = selectedText(false);
  if (!correct) { $('ed-error').textContent = 'Type the correct word.'; return; }
  const heardLatin = current.gujlish_tokens ? bare(selectedText(true)) : '';
  if (correct === bare(heard) && (!latin || latin === heardLatin)) { closeEditor(); return; }
  const body = {
    segment: sel.seg, start: sel.start, end: sel.end, heard, correct, correct_gujlish: latin,
    everywhere: !$('ed-all-row').hidden && $('ed-all').checked, remember: $('ed-remember').checked,
  };
  $('ed-save').disabled = true; $('ed-error').textContent = '';
  try {
    const response = await postJson(`/api/jobs/${jobId}/edit`, body);
    if (signedOut(response)) return;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { $('ed-error').textContent = data.error || 'Could not save this fix.'; return; }
    closeEditor();
    note(`Fixed ${data.replaced === 1 ? '1 place' : `${data.replaced} places`}${data.remembered ? ' · saved to your dictionary' : ''}`);
    pollFailures = 0; await poll(); loadDictionary();
  } catch { $('ed-error').textContent = `Could not reach ${SERVER}.`; }
  finally { $('ed-save').disabled = false; }
}
$('ed-save').addEventListener('click', saveEdit);

async function loadDictionary() {
  try {
    const response = await fetch('/api/dictionary');
    if (signedOut(response) || !response.ok) return;
    const data = await response.json();
    $('dict-count').textContent = data.corrections.length || '';
    const rows = data.corrections.map(rule => dictionaryRow(`${rule.heard} → ${rule.correct}`,
      rule.heard_gujlish && rule.correct_gujlish ? `${rule.heard_gujlish} → ${rule.correct_gujlish}` : '', { correction: rule.id }));
    if (!rows.length) { const p = document.createElement('p'); p.className = 'muted'; p.textContent = 'No fixes yet. Click a word in a transcript to fix it.'; rows.push(p); }
    $('dictionary').replaceChildren(...rows);
    const spellings = Object.entries(data.gujlish);
    $('spellings').hidden = !spellings.length; $('spelling-count').textContent = spellings.length;
    $('spelling-list').replaceChildren(...spellings.map(([gujarati, latin]) => dictionaryRow(`${gujarati} = ${latin}`, '', { gujlish: gujarati })));
  } catch { /* the list refreshes on the next fix */ }
}
function dictionaryRow(text, detail, forget) {
  const row = document.createElement('div'); row.className = 'dict-item';
  const label = document.createElement('div');
  const main = document.createElement('span'); main.lang = 'gu'; main.textContent = text; label.append(main);
  if (detail) { const small = document.createElement('small'); small.textContent = detail; label.append(small); }
  const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'forget'; remove.textContent = '×';
  remove.title = 'Forget'; remove.setAttribute('aria-label', `Forget ${text}`);
  remove.onclick = async () => {
    if (!await ask(`Forget “${text}”?`, 'Future recordings will no longer get this fix.', [{ label: 'Forget', value: true, kind: 'primary danger' }, { label: 'Keep', value: false }])) return;
    try { const response = await postJson('/api/dictionary/forget', forget); if (!signedOut(response)) loadDictionary(); } catch { /* retry by clicking again */ }
  };
  row.append(label, remove);
  return row;
}

// Coming back to the tab: refresh at once rather than waiting for the next timer.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  loadRuns();
  if (jobId && jobId !== 'upload') { pollFailures = 0; poll(); }
});
loadDictionary();
loadRuns().then(() => { const saved = sessionStorage.getItem('asr-job'); select(saved && runs.some(run => run.id === saved) ? saved : null); });
