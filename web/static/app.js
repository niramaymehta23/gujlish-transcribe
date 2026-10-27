const $ = id => document.getElementById(id);
let jobId = null, pollTimer = null, pollFailures = 0;
let current = null, renderedFor = null, caps = null, tab = 'transcript', stageNotice = '';
const TABS = ['transcript', 'translation', 'summary'];
const HOSTED = document.body.dataset.hosted === '1';
const CHUNK_BYTES = Number(document.body.dataset.chunkBytes) || 8 * 1024 * 1024;
const MAX_UPLOAD_MB = Number(document.body.dataset.maxUploadMb) || 4096;
const SERVER = HOSTED ? 'the server' : 'the local server';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
// A 401 means the site's password session ended: go back to the sign-in page.
const signedOut = response => { if (response.status === 401) { location.href = '/login'; return true; } return false; };
const time = seconds => `${Math.floor((seconds || 0) / 60)}:${String(Math.floor((seconds || 0) % 60)).padStart(2, '0')}`;
function show(state) {
  for (const id of ['empty-state','processing','error-state','result']) $(id).hidden = id !== state;
}
function setBusy(busy) {
  $('start-button').disabled = busy;
  $('model').disabled = busy;
  $('mode').disabled = busy;
  $('audio-input').disabled = busy;
}
function progress(percent) {
  $('progress-fill').classList.toggle('indeterminate', percent === null);
  $('progress-fill').style.width = percent === null ? '' : `${percent}%`;
  $('progress-percent').textContent = percent === null ? '' : `${percent}%`;
}
function selectedFile() {
  const file = $('audio-input').files[0];
  $('file-name').textContent = file ? file.name : 'Drop an audio file here';
  $('file-detail').textContent = file ? `${(file.size / 1024 / 1024).toFixed(1)} MB · ready to transcribe` : 'or click to choose a file';
}
$('audio-input').addEventListener('change', selectedFile);
for (const event of ['dragenter','dragover']) $('dropzone').addEventListener(event, e => { e.preventDefault(); $('dropzone').classList.add('dragging'); });
for (const event of ['dragleave','drop']) $('dropzone').addEventListener(event, e => { e.preventDefault(); $('dropzone').classList.remove('dragging'); });
$('dropzone').addEventListener('drop', e => { if ($('audio-input').disabled) return; if (e.dataTransfer.files.length) { const transfer = new DataTransfer(); transfer.items.add(e.dataTransfer.files[0]); $('audio-input').files = transfer.files; selectedFile(); } });
$('model').addEventListener('change', () => { $('mode-field').hidden = $('model').value === 'gujlish'; if ($('mode-field').hidden) $('mode').value = 'mixed'; });
function fail(message, retry = false) {
  show('error-state'); $('error-message').textContent = message;
  $('retry-button').hidden = !retry; setBusy(false);
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

async function loadCaps() {
  try { const response = await fetch('/api/capabilities'); caps = response.ok ? await response.json() : null; }
  catch { caps = null; }
  updateStageBox();
}
function stageKey() { return tab === 'translation' || tab === 'summary' ? tab : null; }
function updateStageBox() {
  const key = stageKey(), box = $('stage-box');
  box.hidden = !current || !key;
  if (box.hidden) return;
  const state = current[key] || { status: 'idle' };
  const busy = state.status === 'queued' || state.status === 'running';
  const translated = (current.translation || {}).status === 'complete';
  const unavailable = Boolean(caps && !caps.gemini);
  $('llm-note').hidden = unavailable;
  $('stage-explain').textContent = key === 'translation'
    ? 'Translate the transcript into natural spoken English that keeps the speaker’s tone, not a word-for-word version.'
    : (translated ? 'Summarize the English into an overview, main points by theme, key stories and quotes, each with a time.' : 'Translate the transcript first. The summary is written from the English.');
  const button = $('stage-button');
  button.textContent = state.status === 'complete' ? 'Run again' : (key === 'translation' ? 'Translate to English' : 'Summarize');
  button.disabled = busy || unavailable || (key === 'summary' && !translated);
  button.hidden = unavailable;
  $('stage-track').hidden = !busy;
  $('stage-fill').classList.toggle('indeterminate', busy && state.progress == null);
  $('stage-fill').style.width = busy && state.progress != null ? `${state.progress}%` : '';
  let status = '';
  if (stageNotice) status = stageNotice;
  else if (busy || state.status === 'error') status = state.message || '';
  else if (unavailable) status = HOSTED ? 'Translation and summaries are not enabled on this site.' : 'To translate and summarize, set GEMINI_API_KEY in the terminal and restart the app (see the README).';
  $('stage-status').textContent = status;
}
function updateExports() {
  if (!current) return;
  const views = {
    transcript: { text: current.text, links: ['txt', 'docx', 'srt'], names: ['TXT', 'DOCX', 'SRT'] },
    translation: { text: current.translation_text, links: ['en-txt', 'en-docx', 'en-srt'], names: ['TXT', 'DOCX', 'SRT'] },
    summary: { text: current.summary_markdown, links: ['summary-md', 'summary-docx'], names: ['MD', 'DOCX'] },
  };
  const view = views[tab], has = Boolean(view.text);
  $('copy-button').hidden = !has;
  for (const [id, index] of [['txt-download', 0], ['docx-download', 1], ['srt-download', 2]]) {
    $(id).hidden = !has || !view.links[index];
    if ($(id).hidden) continue;
    $(id).href = `/api/jobs/${jobId}/download/${view.links[index]}`;
    $(id).textContent = `↓ ${view.names[index]}`;
  }
  $('copy-button').textContent = 'Copy text';
}
function setTab(name) {
  tab = name; stageNotice = '';
  for (const id of TABS) { $(`tab-${id}`).setAttribute('aria-selected', String(id === name)); $(id).hidden = id !== name; }
  updateExports(); updateStageBox();
  if (name !== 'transcript') loadCaps();
}
for (const id of TABS) $(`tab-${id}`).addEventListener('click', () => setTab(id));
function markTabs() {
  for (const [id, has] of [['translation', Boolean(current.translation_text)], ['summary', Boolean(current.summary_markdown)]]) {
    const label = $(`tab-${id}`); label.replaceChildren(id === 'translation' ? 'English' : 'Summary');
    if (has) { const dot = document.createElement('span'); dot.className = 'dot'; label.append(dot); }
  }
}

async function uploadInPieces(file) {
  // Sent in pieces because hosting platforms cap the size of one request; offsets make a resent piece harmless.
  const started = await fetch('/api/uploads', { method: 'POST' });
  if (signedOut(started)) return null;
  if (!started.ok) throw new Error('Could not start the upload.');
  const { id } = await started.json();
  let offset = 0;
  while (offset < file.size) {
    let response = null;
    for (let attempt = 0; ; attempt++) {
      try { response = await fetch(`/api/uploads/${id}?offset=${offset}`, { method: 'PUT', body: file.slice(offset, offset + CHUNK_BYTES) }); break; }
      catch (error) { if (attempt >= 3) throw new Error(`Lost the connection to ${SERVER} during upload.`); await sleep(1500 * (attempt + 1)); }
    }
    if (signedOut(response)) return null;
    const data = await response.json().catch(() => ({}));
    if (response.status === 409 && Number.isInteger(data.received)) { offset = data.received; continue; }
    if (!response.ok) throw new Error(data.error || 'Upload failed. Please try again.');
    offset = data.received;
    progress(Math.round(offset / file.size * 100));
  }
  return id;
}
$('upload-form').addEventListener('submit', async e => {
  e.preventDefault(); clearTimeout(pollTimer); jobId = null; current = null; renderedFor = null; sessionStorage.removeItem('asr-job');
  const file = $('audio-input').files[0];
  if (!file) return;
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) return fail(`This file is larger than the ${MAX_UPLOAD_MB} MB upload limit.`);
  const form = new FormData(); form.append('model', $('model').value); form.append('mode', $('mode').value); form.append('name', file.name);
  setBusy(true); show('processing'); $('status-label').textContent = 'Uploading recording…';
  $('status-detail').textContent = HOSTED ? 'Sending your recording to the server.' : 'Saving your recording to the local workspace.';
  $('progress-description').textContent = 'Uploading'; progress(0); $('result-meta').textContent = '';
  try {
    const uploadId = await uploadInPieces(file);
    if (!uploadId) return;
    form.append('upload_id', uploadId);
    $('status-detail').textContent = 'Checking the audio…'; progress(null);
    const response = await fetch('/api/jobs', { method: 'POST', body: form });
    if (signedOut(response)) return;
    const data = await response.json().catch(() => ({}));
    if (response.status !== 202) return fail(data.error || 'Upload failed. Please try again.');
    jobId = data.id; sessionStorage.setItem('asr-job', jobId); pollFailures = 0; poll();
  } catch (error) {
    fail(error.message || `Could not connect to ${SERVER}.`);
  }
});
async function poll() {
  clearTimeout(pollTimer);
  const currentId = jobId;
  try {
    const response = await fetch(`/api/jobs/${currentId}`);
    if (signedOut(response)) return;
    if (!response.ok) throw new Error(response.status === 404 ? 'This run is no longer active. Open a saved transcript under Previous runs.' : 'Could not read progress.');
    const job = await response.json(); if (jobId !== currentId) return;
    pollFailures = 0; $('result-meta').textContent = job.name;
    if (job.status === 'complete') {
      const first = renderedFor !== currentId;
      current = job; show('result'); setBusy(false);
      $('transcript').textContent = job.text;
      $('translation').textContent = job.translation_text || '';
      if (job.summary_markdown) renderMarkdown($('summary'), job.summary_markdown); else $('summary').replaceChildren();
      markTabs();
      if (first) { renderedFor = currentId; setTab('transcript'); loadHistory(); loadCaps(); }
      else { updateExports(); updateStageBox(); }
      if (['translation', 'summary'].some(key => ['queued', 'running'].includes((job[key] || {}).status))) pollTimer = setTimeout(poll, 1000);
      return;
    }
    if (job.status === 'error') return fail(job.message, true);
    show('processing'); setBusy(true);
    $('status-label').textContent = job.status === 'queued' && job.ahead ? `Waiting · ${job.ahead} recording${job.ahead === 1 ? '' : 's'} ahead of yours` : {queued:'Waiting for the current run…',loading:'Preparing transcription…',transcribing:'Transcribing your recording…'}[job.status] || job.message;
    $('status-detail').textContent = job.message + (job.device ? ` · ${job.device.toUpperCase()}` : '');
    $('progress-description').textContent = job.status === 'transcribing' ? `${time(job.processed)}${job.duration ? ' / '+time(job.duration) : ''} audio processed` : 'Progress begins when the model is ready';
    progress(job.status === 'transcribing' ? job.progress : null);
    pollTimer = setTimeout(poll, 1000);
  } catch(error) {
    if (jobId !== currentId) return;
    if (++pollFailures <= 4) { $('status-detail').textContent = `Reconnecting to ${SERVER}…`; pollTimer = setTimeout(poll, 1500); }
    else fail(error.message);
  }
}
$('retry-button').addEventListener('click', async () => {
  try { const response = await fetch(`/api/jobs/${jobId}/retry`, {method:'POST'}); if (!response.ok) throw new Error('Could not resume this run.'); pollFailures=0; poll(); } catch(error) { fail(error.message, true); }
});
$('stage-button').addEventListener('click', async () => {
  const key = stageKey(); if (!key || !current) return;
  stageNotice = ''; $('stage-button').disabled = true;
  try {
    const response = await fetch(`/api/jobs/${jobId}/${key === 'translation' ? 'translate' : 'summarize'}`, { method: 'POST' });
    if (signedOut(response)) return;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { stageNotice = data.error || 'Could not start this step.'; updateStageBox(); return; }
    pollFailures = 0; poll();
  } catch { stageNotice = `Could not reach ${SERVER}.`; updateStageBox(); }
});
$('copy-button').addEventListener('click', async () => {
  const text = { transcript: current && current.text, translation: current && current.translation_text, summary: current && current.summary_markdown }[tab] || '';
  try { await navigator.clipboard.writeText(text); $('copy-button').textContent = 'Copied ✓'; setTimeout(() => $('copy-button').textContent='Copy text', 2000); }
  catch { const selection = window.getSelection(); const range = document.createRange(); range.selectNodeContents($(tab)); selection.removeAllRanges(); selection.addRange(range); $('copy-button').textContent='Select & copy'; }
});
async function loadHistory() {
  try {
    const response = await fetch('/api/history'); if (signedOut(response)) return; const runs = await response.json();
    $('history').replaceChildren();
    if (!runs.length) { const p=document.createElement('p'); p.className='muted'; p.textContent='No saved transcripts yet.'; $('history').append(p); }
    for (const run of runs) {
      const button = document.createElement('button'); button.type='button'; button.className='history-item';
      const name = document.createElement('span'); name.textContent=run.name;
      // run folders are named YYYYMMDD-HHMMSS-ffffff (asr/storage.new_run)
      const detail = document.createElement('small'); detail.textContent=`${run.model}${run.mode ? ' · '+run.mode : ''} · ${run.run.slice(0,8)} ${run.run.slice(9,15)}`;
      button.append(name,detail); button.onclick=()=>{ if ($('start-button').disabled) return; jobId=run.id; sessionStorage.setItem('asr-job',jobId); pollFailures=0; poll(); }; $('history').append(button);
    }
  } catch { $('history').textContent='Saved runs are temporarily unavailable.'; }
}
loadHistory().then(() => { const saved=sessionStorage.getItem('asr-job'); if (saved) {jobId=saved;show('processing');poll();} });
