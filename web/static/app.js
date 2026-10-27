const $ = id => document.getElementById(id);
let jobId = null, pollTimer = null, pollFailures = 0;
let current = null, renderedFor = null, caps = null, tab = 'transcript', stageNotice = '';
const TABS = ['transcript', 'translation', 'summary'];
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
function fillModels() {
  const select = $('llm-model'), models = (caps && caps.local_models) || [], signature = models.join('|');
  if (select.dataset.sig === signature) return;
  const previous = select.value;
  select.replaceChildren();
  for (const name of models) { const option = document.createElement('option'); option.value = name; option.textContent = name; select.append(option); }
  if (models.includes(previous)) select.value = previous;
  select.dataset.sig = signature;
}
function updateStageBox() {
  const key = stageKey(), box = $('stage-box');
  box.hidden = !current || !key;
  if (box.hidden) return;
  const state = current[key] || { status: 'idle' };
  const busy = state.status === 'queued' || state.status === 'running';
  const translated = (current.translation || {}).status === 'complete';
  for (const [value, name, variable, company] of [['claude', 'Claude', 'ANTHROPIC_API_KEY', 'Anthropic'], ['gemini', 'Gemini', 'GEMINI_API_KEY', 'Google']]) {
    const option = $('llm').querySelector(`option[value=${value}]`);
    option.disabled = !(caps && caps[value]);
    option.textContent = option.disabled ? `${name} API · set ${variable} to enable` : `${name} API · sends text (not audio) to ${company}`;
    if ($('llm').value === value && option.disabled) $('llm').value = 'local';
  }
  const local = $('llm').value === 'local', models = (caps && caps.local_models) || [];
  fillModels();
  $('llm-model').hidden = !local || !models.length;
  $('llm').disabled = busy; $('llm-model').disabled = busy;
  const noServer = local && !models.length;
  $('stage-explain').textContent = key === 'translation'
    ? 'Translate the transcript into natural spoken English that keeps the speaker’s tone, not a word-for-word version.'
    : (translated ? 'Summarize the English into an overview, main points by theme, key stories and quotes, each with a time.' : 'Translate the transcript first. The summary is written from the English.');
  const button = $('stage-button');
  button.textContent = state.status === 'complete' ? 'Run again' : (key === 'translation' ? 'Translate to English' : 'Summarize');
  button.disabled = busy || noServer || (key === 'summary' && !translated);
  $('stage-track').hidden = !busy;
  $('stage-fill').classList.toggle('indeterminate', busy && state.progress == null);
  $('stage-fill').style.width = busy && state.progress != null ? `${state.progress}%` : '';
  let status = '';
  if (stageNotice) status = stageNotice;
  else if (busy || state.status === 'error') status = state.message || '';
  else if (noServer) status = `No local model server found at ${caps ? caps.local_url : 'the default address'}. Start Ollama or LM Studio and load a model, or choose Claude or Gemini.`;
  $('stage-status').textContent = status;
}
function updateExports() {
  if (!current) return;
  const views = {
    transcript: { text: current.text, links: ['txt', 'docx'], names: ['TXT', 'DOCX'] },
    translation: { text: current.translation_text, links: ['en-txt', 'en-docx'], names: ['TXT', 'DOCX'] },
    summary: { text: current.summary_markdown, links: ['summary-md', 'summary-docx'], names: ['MD', 'DOCX'] },
  };
  const view = views[tab], has = Boolean(view.text);
  $('copy-button').hidden = !has;
  for (const [id, index] of [['txt-download', 0], ['docx-download', 1]]) {
    $(id).hidden = !has;
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

$('upload-form').addEventListener('submit', e => {
  e.preventDefault(); clearTimeout(pollTimer); jobId = null; current = null; renderedFor = null; sessionStorage.removeItem('asr-job');
  const data = new FormData($('upload-form')); // collect before disabling fields
  setBusy(true); show('processing'); $('status-label').textContent = 'Uploading recording…';
  $('status-detail').textContent = 'Saving your recording to the local workspace.';
  $('progress-description').textContent = 'Uploading'; progress(0); $('result-meta').textContent = '';
  const xhr = new XMLHttpRequest(); xhr.open('POST', '/api/jobs'); xhr.responseType = 'json';
  xhr.upload.onprogress = event => { if (event.lengthComputable) progress(Math.round(event.loaded / event.total * 100)); };
  xhr.onerror = () => fail('Could not connect to the local server. Check that python app.py is running.');
  xhr.onload = () => {
    if (xhr.status !== 202) return fail(xhr.response?.error || 'Upload failed. Please try again.');
    jobId = xhr.response.id; sessionStorage.setItem('asr-job', jobId); pollFailures = 0; poll();
  };
  xhr.send(data);
});
async function poll() {
  clearTimeout(pollTimer);
  const currentId = jobId;
  try {
    const response = await fetch(`/api/jobs/${currentId}`);
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
    $('status-label').textContent = {queued:'Waiting for the current run…',loading:'Preparing transcription…',transcribing:'Transcribing your recording…'}[job.status] || job.message;
    $('status-detail').textContent = job.message + (job.device ? ` · ${job.device.toUpperCase()}` : '');
    $('progress-description').textContent = job.status === 'transcribing' ? `${time(job.processed)}${job.duration ? ' / '+time(job.duration) : ''} audio processed` : 'Progress begins when the model is ready';
    progress(job.status === 'transcribing' ? job.progress : null);
    pollTimer = setTimeout(poll, 1000);
  } catch(error) {
    if (jobId !== currentId) return;
    if (++pollFailures <= 4) { $('status-detail').textContent = 'Reconnecting to the local server…'; pollTimer = setTimeout(poll, 1500); }
    else fail(error.message);
  }
}
$('retry-button').addEventListener('click', async () => {
  try { const response = await fetch(`/api/jobs/${jobId}/retry`, {method:'POST'}); if (!response.ok) throw new Error('Could not resume this run.'); pollFailures=0; poll(); } catch(error) { fail(error.message, true); }
});
$('stage-button').addEventListener('click', async () => {
  const key = stageKey(); if (!key || !current) return;
  const body = { llm: $('llm').value };
  if (body.llm === 'local' && $('llm-model').value) body.model = $('llm-model').value;
  stageNotice = ''; $('stage-button').disabled = true;
  try {
    const response = await fetch(`/api/jobs/${jobId}/${key === 'translation' ? 'translate' : 'summarize'}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { stageNotice = data.error || 'Could not start this step.'; updateStageBox(); return; }
    pollFailures = 0; poll();
  } catch { stageNotice = 'Could not reach the local server.'; updateStageBox(); }
});
$('llm').addEventListener('change', () => { stageNotice = ''; updateStageBox(); });
$('copy-button').addEventListener('click', async () => {
  const text = { transcript: current && current.text, translation: current && current.translation_text, summary: current && current.summary_markdown }[tab] || '';
  try { await navigator.clipboard.writeText(text); $('copy-button').textContent = 'Copied ✓'; setTimeout(() => $('copy-button').textContent='Copy text', 2000); }
  catch { const selection = window.getSelection(); const range = document.createRange(); range.selectNodeContents($(tab)); selection.removeAllRanges(); selection.addRange(range); $('copy-button').textContent='Select & copy'; }
});
async function loadHistory() {
  try {
    const response = await fetch('/api/history'); const runs = await response.json();
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
