const $ = id => document.getElementById(id);
let jobId = null, pollTimer = null, pollFailures = 0;
let current = null, renderedFor = null, renderedRev = null, caps = null, tab = 'transcript', stageNotice = '';
let sel = null, touched = { gu: false, latin: false }, suggestTimer = null, suggestTicket = 0, noteTimer = null;
const TABS = ['transcript', 'gujlish', 'translation', 'summary'];
const WORD_TABS = ['transcript', 'gujlish'];
const STAGE_KEYS = ['gujlish', 'translation', 'summary'];
const HOSTED = document.body.dataset.hosted === '1';
const CHUNK_BYTES = Number(document.body.dataset.chunkBytes) || 8 * 1024 * 1024;
const MAX_UPLOAD_MB = Number(document.body.dataset.maxUploadMb) || 4096;
const SERVER = HOSTED ? 'the server' : 'the local server';
const EDGE = /^[.,!?;:"'()[\]{}…।॥]+|[.,!?;:"'()[\]{}…।॥]+$/g;  // asr.postprocess.EDGE_PUNCTUATION
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
// A 401 means the site's password session ended: go back to the sign-in page.
const signedOut = response => { if (response.status === 401) { location.href = '/login'; return true; } return false; };
const time = seconds => `${Math.floor((seconds || 0) / 60)}:${String(Math.floor((seconds || 0) % 60)).padStart(2, '0')}`;
const running = state => Boolean(state) && (state.status === 'queued' || state.status === 'running');
const words = text => (text || '').split(/\s+/).filter(Boolean);
const bare = text => words(text).map(word => word.replace(EDGE, '')).filter(Boolean).join(' ');
const wordKey = word => word.normalize('NFC').toLowerCase().replace(EDGE, '');
const postJson = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
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
function note(text) {
  clearTimeout(noteTimer); $('edit-note').textContent = text;
  noteTimer = setTimeout(() => { $('edit-note').textContent = ''; }, 6000);
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
  $('stage-explain').textContent = explain;
  $('llm-note').hidden = key !== 'english' || !gemini;
  $('stage-button').hidden = !label; $('stage-button').textContent = label || '';
  $('stage-button').disabled = busy;
  $('stage-alt').hidden = !alt; $('stage-alt').disabled = busy;
  $('stage-track').hidden = !busy;
  $('stage-fill').classList.toggle('indeterminate', busy && state.progress == null);
  $('stage-fill').style.width = busy && state.progress != null ? `${state.progress}%` : '';
  let status = '';
  if (stageNotice) status = stageNotice;
  else if (busy || state.status === 'error') status = state.message || '';
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
    const label = $(`tab-${id}`); label.replaceChildren(name);
    if (has) { const dot = document.createElement('span'); dot.className = stale ? 'dot stale' : 'dot'; dot.title = stale ? 'Made before your latest fixes' : ''; label.append(dot); }
  }
}

// The word editor: pick words in the Gujarati or Gujlish, type the right word in either script, and save. Typing
// Gujlish fills in the Gujarati (and suggests spellings); typing Gujarati fills in the Gujlish.
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
  if (editor.hidden || !sel || window.matchMedia('(max-width: 760px)').matches) return;  // phones: a bottom sheet
  const last = $(tab).querySelector(`.words[data-seg="${sel.seg}"] .w[data-i="${sel.end - 1}"]`);
  if (!last) return;
  const box = last.getBoundingClientRect(), right = document.documentElement.clientWidth - editor.offsetWidth - 8;
  editor.style.left = `${window.scrollX + Math.max(8, Math.min(box.left, right))}px`;
  editor.style.top = `${window.scrollY + box.bottom + 8}px`;
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
    $('dict-count').textContent = data.corrections.length ? `· ${data.corrections.length}` : '';
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
    if (!confirm(`Forget “${text}”? Future recordings will no longer get this fix.`)) return;
    try { const response = await postJson('/api/dictionary/forget', forget); if (!signedOut(response)) loadDictionary(); } catch { /* retry by clicking again */ }
  };
  row.append(label, remove);
  return row;
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
  e.preventDefault(); clearTimeout(pollTimer); jobId = null; current = null; renderedFor = null; closeEditor(); sessionStorage.removeItem('asr-job');
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
      if (first || job.rev !== renderedRev) {
        renderedRev = job.rev;
        renderWords($('transcript'), false); renderWords($('gujlish'), true); markSelection();
      }
      $('translation').textContent = job.translation_text || '';
      if (job.summary_markdown) renderMarkdown($('summary'), job.summary_markdown); else $('summary').replaceChildren();
      markTabs();
      if (first) { renderedFor = currentId; setTab('transcript'); loadHistory(); loadCaps(); loadDictionary(); }
      else { updateExports(); updateStageBox(); }
      if (STAGE_KEYS.some(key => running(job[key]))) pollTimer = setTimeout(poll, 1000);
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
async function startStage(path, body) {
  if (!current) return;
  stageNotice = ''; $('stage-button').disabled = true; $('stage-alt').disabled = true;
  try {
    const response = await postJson(`/api/jobs/${jobId}/${path}`, body);
    if (signedOut(response)) return;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { stageNotice = data.error || 'Could not start this step.'; updateStageBox(); return; }
    pollFailures = 0; poll();
  } catch { stageNotice = `Could not reach ${SERVER}.`; updateStageBox(); }
}
$('stage-button').addEventListener('click', () => startStage(stageBoxKey() === 'gujlish' ? 'gujlish' : 'english'));
$('stage-alt').addEventListener('click', () => startStage('gujlish', { engine: 'gemini' }));
$('copy-button').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(tabText(tab)); $('copy-button').textContent = 'Copied ✓'; setTimeout(() => $('copy-button').textContent='Copy text', 2000); }
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
      button.append(name,detail); button.onclick=()=>{ if ($('start-button').disabled) return; closeEditor(); jobId=run.id; sessionStorage.setItem('asr-job',jobId); pollFailures=0; poll(); }; $('history').append(button);
    }
  } catch { $('history').textContent='Saved runs are temporarily unavailable.'; }
}
loadDictionary();
loadHistory().then(() => { const saved=sessionStorage.getItem('asr-job'); if (saved) {jobId=saved;show('processing');poll();} });
