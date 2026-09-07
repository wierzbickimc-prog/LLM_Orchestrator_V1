// Model Deck mobile UI -- vanilla JS, no dependencies
const API = '';
let currentTab = 'deck';
let sseSource = null;
let telemetryTimer = null;
let loopAlertTimer = null;
let pendingQuestion = { phase: '', payload: '' };

// --- Tab switching ---
function switchTab(name) {
  currentTab = name;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
}

// --- State / Deck ---
async function loadState() {
  try {
    const res = await fetch(API + '/api/state');
    const state = await res.json();
    document.getElementById('activeBadge').textContent = 'ACTIVE · ' + (state.active?.phase || '—').toUpperCase();
    // Key status
    const keyRes = await fetch(API + '/api/secrets/openai/status');
    const keyData = await keyRes.json();
    document.getElementById('keyStatus').textContent = keyData.has_key ? '● Key stored' : '○ No API key stored';
    // Planner fields
    document.getElementById('plannerKind').value = state.planner?.kind || 'openai';
    document.getElementById('plannerModel').value = state.planner?.model || '';
    document.getElementById('plannerEffort').value = state.planner?.reasoning_effort || 'high';
    // Roles
    renderRoles(state.roles);
  } catch (e) { console.error(e); }
}

function renderRoles(roles) {
  const container = document.getElementById('rolesContainer');
  container.innerHTML = '';
  for (const [phase, role] of Object.entries(roles)) {
    const div = document.createElement('div');
    div.style.cssText = 'margin-bottom:12px;padding:10px;border:1px solid var(--border);border-radius:8px;';
    div.innerHTML = `
      <strong style="font-size:13px;">${phase}</strong>
      <label>Model</label><input id="role_${phase}_model" value="${role.model||''}" readonly style="opacity:0.7;">
      <div class="row">
        <div><label>Context</label><input id="role_${phase}_context" type="number" value="${role.context_window||131072}"></div>
        <div><label>Depth</label><input id="role_${phase}_depth" type="number" min="1" max="8" value="${role.depth||3}"></div>
      </div>
      <div class="row">
        <div><label>Temp</label><input id="role_${phase}_temp" type="number" step="0.05" value="${role.temperature||0.7}"></div>
        <div><label>Top-p</label><input id="role_${phase}_topp" type="number" step="0.05" value="${role.top_p||0.9}"></div>
        <div><label>Top-k</label><input id="role_${phase}_topk" type="number" value="${role.top_k||40}"></div>
      </div>
      <button class="primary" style="margin-top:6px;" onclick="saveRole('${phase}')">Save ${phase}</button>
    `;
    container.appendChild(div);
  }
}

async function saveRole(phase) {
  const body = {
    context_window: parseInt(document.getElementById(`role_${phase}_context`).value),
    depth: parseInt(document.getElementById(`role_${phase}_depth`).value),
    temperature: parseFloat(document.getElementById(`role_${phase}_temp`).value),
    top_p: parseFloat(document.getElementById(`role_${phase}_topp`).value),
    top_k: parseInt(document.getElementById(`role_${phase}_topk`).value),
  };
  await fetch(API + `/api/state/roles/${phase}`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  alert(`${phase} saved`);
}

async function setActive(phase) {
  await fetch(API + '/api/state/active', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({phase}) });
  loadState();
}

async function savePlanner() {
  const body = {
    kind: document.getElementById('plannerKind').value,
    model: document.getElementById('plannerModel').value,
    reasoning_effort: document.getElementById('plannerEffort').value,
  };
  await fetch(API + '/api/state/planner', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  alert('Planner saved');
}

async function saveKey() {
  const key = document.getElementById('apiKeyInput').value.trim();
  if (!key) return;
  await fetch(API + '/api/secrets/openai', { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({key}) });
  document.getElementById('apiKeyInput').value = '';
  loadState();
}

// --- Pipeline ---
async function startPipeline(mode) {
  const body = {
    mode,
    path: document.getElementById('pipePath').value.trim(),
    task: document.getElementById('pipeTask').value.trim(),
    start_phase: document.getElementById('pipeStart').value,
  };
  const res = await fetch(API + '/api/pipeline/start', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  if (!res.ok) {
    const err = await res.json();
    alert(err.detail || 'Failed to start');
    return;
  }
  setButtonsRunning(true);
  connectSSE();
  updateChips();
}

async function stopPipeline() {
  await fetch(API + '/api/pipeline/stop', { method: 'POST' });
  setButtonsRunning(false);
}

async function continuePipeline() {
  const res = await fetch(API + '/api/pipeline/status');
  const status = await res.json();
  if (status.queue && status.queue.length) {
    setButtonsRunning(true);
  }
}

function setButtonsRunning(running) {
  document.getElementById('btnFull').disabled = running;
  document.getElementById('btnStep').disabled = running;
  document.getElementById('btnContinue').disabled = true;
  document.getElementById('btnStop').disabled = !running;
}

function updateChips() {
  const phases = ['scout','planner','builder','auditor','renovator'];
  const container = document.getElementById('phaseChips');
  container.innerHTML = phases.map(p => `<span class="chip" id="chip-${p}">${p}: pending</span>`).join('');
}

function setChip(phase, text, cls) {
  const el = document.getElementById('chip-' + phase);
  if (el) { el.textContent = `${phase}: ${text}`; el.className = 'chip ' + (cls||''); }
}

// --- SSE ---
function connectSSE() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource(API + '/api/pipeline/events');
  sseSource.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      handlePipelineEvent(event);
    } catch {}
  };
  sseSource.onerror = () => {};
}

function handlePipelineEvent(event) {
  const { type, phase, content, success, payload, status, message } = event;
  const out = document.getElementById('output');
  if (type === 'chunk') {
    out.textContent += content;
    out.scrollTop = out.scrollHeight;
  } else if (type === 'finished') {
    const label = success ? 'FINISHED' : 'FAILED';
    out.textContent += `\n\n--- ${phase.toUpperCase()} ${label} ---\n${success ? content : 'Failed:\n' + content}\n`;
    out.scrollTop = out.scrollHeight;
    setChip(phase, success ? 'done' : 'failed', success ? 'done' : 'failed');
    if (success) {
      // Check if more phases queued
      setTimeout(async () => {
        const res = await fetch(API + '/api/pipeline/status');
        const st = await res.json();
        if (st.queue && st.queue.length > 0) {
          if (st.current_phase) setChip(st.current_phase, 'preparing', 'running');
        } else {
          setButtonsRunning(false);
          if (sseSource) { sseSource.close(); sseSource = null; }
        }
      }, 500);
    } else {
      setButtonsRunning(false);
      if (sseSource) { sseSource.close(); sseSource = null; }
    }
  } else if (type === 'ask_question') {
    showAskModal(phase, payload);
  } else if (type === 'phase_status') {
    const cls = status.includes('skipped') ? 'skipped' : status.includes('run') ? 'running' : '';
    setChip(phase, status, cls);
  } else if (type === 'notification') {
    out.textContent += '\n' + message + '\n';
    out.scrollTop = out.scrollHeight;
  }
}

// --- Ask Question Modal ---
function showAskModal(phase, payloadStr) {
  pendingQuestion = { phase, payload: payloadStr };
  let data = {};
  try { data = JSON.parse(payloadStr); } catch { data = { question: payloadStr }; }
  document.getElementById('askTitle').textContent = `${phase} needs a decision`;
  document.getElementById('askText').textContent = data.question || '(no question)';
  const optsDiv = document.getElementById('askOptions');
  optsDiv.innerHTML = '';
  if (Array.isArray(data.options) && data.options.length) {
    data.options.forEach(opt => {
      const btn = document.createElement('button');
      btn.textContent = opt;
      btn.style.marginRight = '6px';
      btn.onclick = () => { document.getElementById('askAnswer').value = opt; };
      optsDiv.appendChild(btn);
    });
  }
  document.getElementById('askModal').classList.add('show');
}

async function submitAnswer() {
  const answer = document.getElementById('askAnswer').value;
  document.getElementById('askModal').classList.remove('show');
  document.getElementById('askAnswer').value = '';
  await fetch(API + '/api/pipeline/answer', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({phase: pendingQuestion.phase, answer}) });
}

// --- Telemetry ---
async function pollTelemetry() {
  try {
    const res = await fetch(API + '/api/telemetry');
    const data = await res.json();
    const active = data.active || {};
    document.getElementById('tStatus').textContent = active.kind === 'local' ? active.phase : 'cloud';
    if (data.metrics?.latest) {
      const used = data.metrics.latest.context_len || 0;
      const max = data.health?.context_window || 0;
      document.getElementById('tContext').textContent = max ? `${used}/${max} (${(used/max*100).toFixed(0)}%)` : '—';
    } else {
      document.getElementById('tContext').textContent = '—';
    }
    if (data.flight?.active?.[0]) {
      const req = data.flight.active[0];
      document.getElementById('tDecode').textContent = req.tps_now ? req.tps_now.toFixed(1) + ' tok/s' : '—';
      document.getElementById('tElapsed').textContent = req.elapsed_s ? req.elapsed_s.toFixed(1) + 's' : '—';
    } else {
      document.getElementById('tDecode').textContent = '—';
      document.getElementById('tElapsed').textContent = '—';
    }
  } catch {}
}

// --- Loop Alert ---
async function pollLoopAlert() {
  try {
    const res = await fetch(API + '/loop-alert');
    const info = await res.json();
    const banner = document.getElementById('loopAlert');
    if (info.should_alert) {
      banner.classList.add('show');
      banner.dataset.id = info.id;
      banner.dataset.seq = info.seq;
    } else {
      banner.classList.remove('show');
    }
  } catch {}
}

async function stopLoopAlert() {
  const banner = document.getElementById('loopAlert');
  await fetch(API + '/loop-alert/stop', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({id: parseInt(banner.dataset.id)}) });
  banner.classList.remove('show');
}

async function ackLoopAlert() {
  const banner = document.getElementById('loopAlert');
  await fetch(API + '/loop-alert/ack', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({id: parseInt(banner.dataset.id), seq: parseInt(banner.dataset.seq)}) });
  banner.classList.remove('show');
}

// --- Admin / Prompts ---
async function loadPrompts() {
  try {
    const res = await fetch(API + '/api/prompts');
    const prompts = await res.json();
    const container = document.getElementById('promptsContainer');
    container.innerHTML = '';
    for (const [phase, data] of Object.entries(prompts)) {
      const div = document.createElement('card');
      div.innerHTML = `
        <h3>${phase} <small style="color:var(--muted);">(${data.is_override ? 'edited' : 'default'})</small></h3>
        <textarea id="prompt_${phase}" rows="6">${data.is_override ? data.override : data.default}</textarea>
        <div class="row" style="margin-top:8px;">
          <button class="primary" onclick="savePrompt('${phase}')">Save</button>
          <button onclick="resetPrompt('${phase}')">Reset</button>
        </div>
      `;
      container.appendChild(div);
    }
  } catch {}
}

async function savePrompt(phase) {
  const text = document.getElementById('prompt_' + phase).value;
  await fetch(API + `/api/prompts/${phase}`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({text}) });
  loadPrompts();
}

async function resetPrompt(phase) {
  await fetch(API + `/api/prompts/${phase}`, { method: 'DELETE' });
  loadPrompts();
}

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
  loadState();
  loadPrompts();
  updateChips();
  telemetryTimer = setInterval(pollTelemetry, 2000);
  loopAlertTimer = setInterval(pollLoopAlert, 3000);
});
