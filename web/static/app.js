const $ = id => document.getElementById(id);
let jobId = null, pollTimer = null, pollFailures = 0;
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
$('upload-form').addEventListener('submit', e => {
  e.preventDefault(); clearTimeout(pollTimer); jobId = null; sessionStorage.removeItem('asr-job');
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
      show('result'); setBusy(false); $('transcript').textContent = job.text;
      $('copy-button').textContent = 'Copy text';
      $('txt-download').href = `/api/jobs/${currentId}/download/txt`;
      $('docx-download').href = `/api/jobs/${currentId}/download/docx`;
      loadHistory(); return;
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
$('copy-button').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('transcript').textContent); $('copy-button').textContent = 'Copied ✓'; setTimeout(() => $('copy-button').textContent='Copy text', 2000); }
  catch { const selection = window.getSelection(); const range = document.createRange(); range.selectNodeContents($('transcript')); selection.removeAllRanges(); selection.addRange(range); $('copy-button').textContent='Select & copy'; }
});
async function loadHistory() {
  try {
    const response = await fetch('/api/history'); const runs = await response.json();
    $('history').replaceChildren();
    if (!runs.length) { const p=document.createElement('p'); p.className='muted'; p.textContent='No saved transcripts yet.'; $('history').append(p); }
    for (const run of runs) {
      const button = document.createElement('button'); button.type='button'; button.className='history-item';
      const name = document.createElement('span'); name.textContent=run.name;
      const detail = document.createElement('small'); detail.textContent=`${run.model}${run.mode ? ' · '+run.mode : ''} · ${run.run.slice(0,8)} ${run.run.slice(9,15)}`;
      button.append(name,detail); button.onclick=()=>{ if ($('start-button').disabled) return; jobId=run.id; sessionStorage.setItem('asr-job',jobId); pollFailures=0; poll(); }; $('history').append(button);
    }
  } catch { $('history').textContent='Saved runs are temporarily unavailable.'; }
}
loadHistory().then(() => { const saved=sessionStorage.getItem('asr-job'); if (saved) {jobId=saved;show('processing');poll();} });
