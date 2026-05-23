(function () {
  const form = document.getElementById('run-form');
  const runBtn = document.getElementById('run-btn');
  const dryBtn = document.getElementById('dry-btn');
  const msg = document.getElementById('msg');
  const output = document.getElementById('output');
  const outputError = document.getElementById('output-error');
  const statusBadge = document.getElementById('status-badge');
  const statusText = document.getElementById('status-text');
  const historyBody = document.getElementById('history-body');
  const historyCount = document.getElementById('history-count');
  const clearOutputBtn = document.getElementById('clear-output');
  const logLines = document.getElementById('log-lines');
  const logLinesVal = document.getElementById('log-lines-val');
  const logFilter = document.getElementById('log-filter');
  const logBlocks = document.getElementById('log-blocks');
  const capApi = document.getElementById('cap-api');
  const capGmail = document.getElementById('cap-gmail');
  const formTitle = document.getElementById('form-title');
  const formDesc = document.getElementById('form-desc');
  const fieldsPaint = document.getElementById('fields-paint');
  const fieldsGmail = document.getElementById('fields-gmail');
  const paintSuggestionsWrap = document.getElementById('paint-suggestions-wrap');
  const paintSuggestions = document.getElementById('paint-suggestions');
  const toolsTitle = document.getElementById('tools-title');
  const toolsDesc = document.getElementById('tools-desc');
  const toolsList = document.getElementById('tools-list');
  const outputWrap = document.getElementById('output-wrap');
  const agentGrid = document.getElementById('agent-grid');

  let pollTimers = [];
  let statusInFlight = false;
  let localRunning = false;
  let runBaseline = null;
  let runStartedAt = 0;
  let activeTab = 'tools';
  let currentAgent = 'paint';
  let agentsMeta = {};
  let statusSnapshot = null;
  let logsLayoutKey = '';
  let wasRunning = false;
  let lastToastSeq = 0;
  let logPollId = null;

  const STORAGE_KEY = 'talk2mcp_selected_agent';
  const DEFAULT_PAINT_QUESTION = 'I love India';

  const AGENT_UI = {
    paint: {
      title: 'Run Paint',
      desc: 'Text goes inside Paint — Gemini calls the tools',
      toolsTitle: 'Paint MCP Tools',
      toolsDesc: 'LLM call order',
    },
    gmail: {
      title: 'Run Gmail',
      desc: 'LLM sends email via send-email',
      toolsTitle: 'Gmail MCP Tools',
      toolsDesc: 'Required tool',
    },
  };

  // Block explicit MCP tool names / paint automation commands — not plain phrases.
  const MANUAL_PAINT_PATTERNS = [
    /\bopen_paint\b/i,
    /\bdraw_rectangle\b/i,
    /\badd_text_in_paint\b/i,
    /\bopen\s+paint\b/i,
    /\bdraw\s+rectangle\b/i,
    /\bmspaint\b/i,
    /\bdraw\s+[a-z]\s*letter\b/i,
    /\bdraw\s+letter\b/i,
    /\badd\s+text\s+['"]/i,
    /\bopen\s+paint\s*,/i,
  ];

  function paintInputState(text) {
    const t = (text || '').trim();
    if (!t) return 'empty';
    return MANUAL_PAINT_PATTERNS.some((re) => re.test(t)) ? 'blocked' : 'ok';
  }

  function looksLikeManualPaintCommand(text) {
    return paintInputState(text) === 'blocked';
  }

  function normalizePaintInput(text) {
    const t = (text || '').trim();
    if (!t || !looksLikeManualPaintCommand(t)) return t;
    const quoted = t.match(/['"]([^'"]{1,500})['"]/);
    if (quoted) return quoted[1].trim();
    const addText = t.match(/\badd\s+text\s+(.+)$/i);
    if (addText) return addText[1].replace(/^['"]|['"]$/g, '').trim();
    return t;
  }

  // --- Tabs ---
  const tabs = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.tab-panel');

  function switchTab(name) {
    activeTab = name;
    tabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    panels.forEach((p) => p.classList.toggle('active', p.id === 'panel-' + name));
    if (name === 'logs') refreshLogs(true);
  }

  tabs.forEach((tab) => tab.addEventListener('click', () => switchTab(tab.dataset.tab)));

  // --- Helpers ---
  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatError(detail) {
    if (!detail) return 'Request failed';
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
    }
    return JSON.stringify(detail);
  }

  function showAlert(text, type) {
    msg.textContent = text;
    msg.className = 'alert alert-' + type;
    msg.classList.remove('alert-hidden');
  }

  function hideAlert() {
    msg.classList.add('alert-hidden');
  }

  function initPaintSuggestions() {
    if (!paintSuggestions) return;
    paintSuggestions.querySelectorAll('.suggest-chip').forEach((chip) => {
      const text = chip.dataset.q || chip.textContent.trim();
      chip.dataset.q = text;
      chip.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        fillPaintQuestion(text);
      });
    });
  }

  function fillPaintQuestion(text) {
    if (statusSnapshot && statusSnapshot.running) return;
    if (localRunning && statusSnapshot && !statusSnapshot.running) {
      clearLocalRun();
      setRunningUI(false, null);
    }
    if (localRunning) return;
    applyAgentUI('paint');
    try {
      sessionStorage.setItem(STORAGE_KEY, 'paint');
    } catch (_) {
      /* ignore */
    }
    const q = document.getElementById('question');
    if (!q) return;
    q.value = text;
    q.focus();
    if (paintSuggestions) {
      paintSuggestions.querySelectorAll('.suggest-chip').forEach((chip) => {
        chip.classList.toggle('active', chip.dataset.q === text);
      });
    }
    syncFormState();
    hideAlert();
  }

  function isAppRunning() {
    if (statusSnapshot && statusSnapshot.running) return true;
    if (localRunning && statusSnapshot && !statusSnapshot.running) {
      clearLocalRun();
      return false;
    }
    if (localRunning) return true;
    return false;
  }

  function resultAgent(data) {
    if (!data) return null;
    if (data.last_result && data.last_result.agent) return data.last_result.agent;
    return data.last_agent || null;
  }

  function syncFormState() {
    if (isAppRunning()) {
      return;
    }
    if (currentAgent === 'paint') {
      const q = document.getElementById('question');
      if (!q) return;
      const normalized = normalizePaintInput(q.value);
      const blocked = paintInputState(normalized) === 'blocked';
      const empty = !normalized.trim();
      runBtn.disabled = blocked || empty;
      dryBtn.disabled = blocked || empty;
      return;
    }
    const to = (document.getElementById('email-to') || {}).value || '';
    const subject = (document.getElementById('email-subject') || {}).value || '';
    const body = (document.getElementById('email-body') || {}).value || '';
    const gmailReady = to.trim() && subject.trim() && body.trim();
    runBtn.disabled = !gmailReady;
    dryBtn.disabled = !gmailReady;
  }

  function snapshotRunState(data) {
    const hist = (data && data.run_history) || [];
    return {
      historySeq: (data && data.history_seq) || 0,
      historyId: hist[0]?.id || 0,
      result: JSON.stringify((data && data.last_result) || null),
      error: (data && data.last_error) || null,
    };
  }

  function runFinished(data) {
    if (!runBaseline) return !data.running;
    if (data.running) return false;
    const seq = data.history_seq || 0;
    if (seq > runBaseline.historySeq) return true;
    const topId = (data.run_history && data.run_history[0] && data.run_history[0].id) || 0;
    if (topId > runBaseline.historyId) return true;
    return runStateChanged(data);
  }

  function runStateChanged(data) {
    if (!runBaseline) return false;
    const snap = snapshotRunState(data);
    return snap.result !== runBaseline.result || snap.error !== runBaseline.error;
  }

  function resolveRunningState(data) {
    if (data.running) {
      return { running: true, agent: data.running_agent };
    }
    if (localRunning) {
      clearLocalRun();
    }
    return { running: false, agent: null };
  }

  function clearLocalRun() {
    localRunning = false;
    runBaseline = null;
    runStartedAt = 0;
  }

  function setCapPill(el, ok, okText, badText) {
    if (!el) return;
    el.textContent = ok ? okText : badText;
    el.className = 'cap-pill ' + (ok ? 'cap-ok' : 'cap-bad');
  }

  function setGmailPill(setup) {
    if (!capGmail) return;
    if (!setup) {
      setCapPill(capGmail, false, 'Gmail ✓', 'Gmail setup');
    } else if (setup.ready) {
      setCapPill(capGmail, true, 'Gmail ✓', 'Gmail setup');
    } else if (setup.creds_ok && !setup.token_ok) {
      capGmail.textContent = 'OAuth needed';
      capGmail.className = 'cap-pill cap-warn';
    } else {
      setCapPill(capGmail, false, 'Gmail ✓', 'Gmail setup');
    }
  }

  function applyAgentUI(agent) {
    currentAgent = agent;
    document.querySelectorAll('.agent-card').forEach((c) => {
      c.classList.toggle('active', c.dataset.agent === agent);
    });
    if (fieldsPaint) fieldsPaint.classList.toggle('hidden', agent !== 'paint');
    if (fieldsGmail) fieldsGmail.classList.toggle('hidden', agent !== 'gmail');
    if (paintSuggestionsWrap) paintSuggestionsWrap.classList.toggle('hidden', agent !== 'paint');
    const ui = AGENT_UI[agent];
    if (ui && formTitle) formTitle.textContent = ui.title;
    if (ui && formDesc) formDesc.textContent = ui.desc;
    if (ui && toolsTitle) toolsTitle.textContent = ui.toolsTitle;
    if (ui && toolsDesc) toolsDesc.textContent = ui.toolsDesc;
    if (logFilter) {
      logFilter.value = agent === 'gmail' ? 'gmail' : 'paint';
    }
    renderToolsPanel();
    if (activeTab === 'logs') refreshLogs(true);
  }

  function selectAgent(agent) {
    if (!AGENT_UI[agent]) return;
    if (isAppRunning()) return;
    applyAgentUI(agent);
    if (agent === 'paint' || agent === 'gmail') syncFormState();
    try {
      sessionStorage.setItem(STORAGE_KEY, agent);
    } catch (_) {
      /* ignore */
    }
  }

  function markRunningAgent(running, runningAgent) {
    document.querySelectorAll('.agent-card').forEach((card) => {
      const busy = running && card.dataset.agent === runningAgent;
      card.classList.toggle('agent-busy', busy);
      let tag = card.querySelector('.agent-busy-tag');
      if (busy) {
        if (!tag) {
          tag = document.createElement('span');
          tag.className = 'agent-busy-tag';
          card.appendChild(tag);
        }
        tag.textContent = 'Running…';
      } else if (tag) {
        tag.remove();
      }
    });
  }

  function restoreSelectedAgent() {
    try {
      const saved = sessionStorage.getItem(STORAGE_KEY);
      if (saved && AGENT_UI[saved]) {
        applyAgentUI(saved);
        return;
      }
    } catch (_) {
      /* ignore */
    }
    applyAgentUI('paint');
  }

  function initDefaultPaintQuestion() {
    const q = document.getElementById('question');
    if (!q || q.value.trim()) return;
    q.value = DEFAULT_PAINT_QUESTION;
    if (paintSuggestions) {
      paintSuggestions.querySelectorAll('.suggest-chip').forEach((chip) => {
        chip.classList.toggle('active', chip.dataset.q === DEFAULT_PAINT_QUESTION);
      });
    }
  }

  if (agentGrid) {
    agentGrid.querySelectorAll('.agent-card').forEach((card) => {
      card.addEventListener('click', () => selectAgent(card.dataset.agent));
    });
  }

  function renderToolsPanel() {
    const meta = agentsMeta[currentAgent];
    if (!meta || !toolsList) return;
    toolsList.innerHTML = (meta.tools || [])
      .map(
        (tool, i) => `
      <li class="step">
        <span class="step-num">${i + 1}</span>
        <div class="step-body">
          <div class="step-title"><code>${escapeHtml(tool)}</code></div>
        </div>
      </li>`
      )
      .join('');
  }

  async function loadAgents() {
    try {
      const res = await fetch('/api/agents');
      const data = await res.json();
      (data.agents || []).forEach((a) => {
        agentsMeta[a.id] = a;
      });
      setCapPill(capApi, data.api_key_valid, 'API key ✓', 'API key ✗');
      setGmailPill(data.gmail_setup);
      if (agentsMeta.gmail) {
        agentsMeta.gmail.configured = !!(data.gmail_setup && data.gmail_setup.ready);
      }
      renderToolsPanel();
    } catch {
      setCapPill(capApi, false, 'API …', 'API offline');
    }
  }

  function setRunningUI(running, runningAgent) {
    const disabled = !!running;
    if (!disabled) {
      syncFormState();
    } else {
      runBtn.disabled = disabled;
      dryBtn.disabled = disabled;
    }
    document.querySelectorAll('.agent-card').forEach((c) => {
      c.disabled = disabled;
      c.classList.toggle('disabled', disabled);
    });

    if (running) {
      statusBadge.className = 'badge badge-warning badge-running';
      const label = runningAgent === 'gmail' ? 'Gmail' : 'Paint';
      statusText.innerHTML = '<span class="badge-dot"></span> Running · ' + label;
      if (outputWrap) outputWrap.classList.add('running');
      if (output.dataset.cleared !== '1' && !output.dataset.cleared) {
        output.textContent = 'Running…';
        output.classList.remove('waiting');
      }
    } else {
      statusBadge.className = 'badge badge-success';
      statusText.innerHTML = '<span class="badge-dot"></span> Idle';
      if (outputWrap) outputWrap.classList.remove('running');
      markRunningAgent(false, null);
    }
  }

  function showOutputError(text) {
    if (!text) {
      outputError.classList.add('hidden');
      outputError.textContent = '';
      return;
    }
    outputError.textContent = text;
    outputError.classList.remove('hidden');
  }

  function renderTranscript(data, runningOverride) {
    const userCleared = output.dataset.cleared === '1';
    const isRunning = runningOverride !== undefined ? runningOverride : !!data.running;

    if (!userCleared) {
      showOutputError(data.last_error || '');
    } else {
      showOutputError('');
    }

    if (isRunning && !userCleared) {
      if (outputWrap) outputWrap.classList.add('running');
      output.textContent = 'Running…';
      output.classList.remove('waiting');
      return;
    }

    if (userCleared) {
      return;
    }

    const agentForResult = resultAgent(data);
    if (!isRunning && data.last_result && agentForResult && agentForResult !== currentAgent) {
      output.classList.remove('waiting');
      output.classList.add('output-hint');
      const label = agentForResult === 'gmail' ? 'Gmail' : 'Paint';
      output.textContent =
        'Latest run was on ' + label + '. Switch to ' + label + ' MCP to view the transcript.';
      if (outputWrap) outputWrap.classList.remove('running');
      return;
    }

    output.classList.remove('output-hint');

    if (!data.last_result) {
      if (!userCleared) {
        output.textContent = 'Run an agent to see the MCP transcript here.';
        output.classList.add('waiting');
      }
      return;
    }

    output.classList.remove('waiting');
    if (outputWrap) outputWrap.classList.remove('running');
    const r = data.last_result;
    const lines = [];
    if (r.agent === 'gmail' || r.recipient) {
      lines.push('To: ' + (r.recipient || ''));
      lines.push('Subject: ' + (r.subject || ''));
    } else {
      lines.push('Question: ' + (r.question || ''));
    }
    lines.push('Model: ' + (r.model || ''));
    if (r.dry_run) lines.push('dry-run');
    if (r.complete === false) lines.push('INCOMPLETE');
    if (r.tools_called) {
      lines.push('Tools called: ' + (r.tools_called.join(', ') || '(none)'));
    }
    lines.push('');
    for (const item of r.transcript || []) {
      if (item.type === 'tool_call') {
        lines.push('[turn ' + item.turn + '] LLM → ' + item.name + '(' + JSON.stringify(item.args) + ')');
      } else if (item.type === 'tool_result') {
        lines.push('[turn ' + item.turn + '] MCP ← ' + item.name + ': ' + item.result);
      } else if (item.type === 'tool_error') {
        lines.push('[turn ' + item.turn + '] ERROR ' + item.name + ': ' + item.error);
      } else if (item.type === 'order_error') {
        lines.push('[turn ' + item.turn + '] ORDER ERROR ' + item.name + ': ' + item.error);
      } else if (item.type === 'nudge') {
        lines.push('[turn ' + item.turn + '] NUDGE: ' + item.text);
      } else if (item.type === 'final') {
        lines.push('');
        lines.push('Final: ' + item.text);
      }
    }
    output.textContent = lines.join('\n');
  }

  function agentBadge(agent) {
    if (agent === 'gmail') return '<span class="badge badge-gmail">gmail</span>';
    return '<span class="badge badge-paint">paint</span>';
  }

  function renderHistory(history) {
    const rows = history || [];
    historyCount.textContent = String(rows.length);
    if (!rows.length) {
      historyBody.innerHTML = '<tr class="empty-row"><td colspan="6">No runs yet</td></tr>';
      return;
    }
    historyBody.innerHTML = rows
      .map(
        (r) => `
      <tr>
        <td>${r.id}</td>
        <td>${agentBadge(r.agent || 'paint')}</td>
        <td>${escapeHtml(r.time)}</td>
        <td><span class="badge ${r.mode === 'live' ? 'badge-live' : 'badge-dry'}">${escapeHtml(r.mode)}</span></td>
        <td title="${escapeHtml(r.label || r.question || '')}">${escapeHtml(r.label || r.question || '')}</td>
        <td class="${r.success ? 'status-ok' : 'status-fail'}">${r.success ? '✓' : '✗'}</td>
      </tr>`
      )
      .join('');
  }

  function ensureLogBlocks(filter) {
    if (!logBlocks) return;
    const key = filter + ':' + (logLines ? logLines.value : '200');
    if (key === logsLayoutKey) return;
    logsLayoutKey = key;
    const blocks = [];
    if (filter === 'all' || filter === 'paint') {
      blocks.push({ id: 'log-paint-agent', label: 'talk2mcp.log', key: 'paint_agent' });
      blocks.push({ id: 'log-paint-server', label: 'paint_mcp_server.log', key: 'paint_server' });
    }
    if (filter === 'all' || filter === 'gmail') {
      blocks.push({ id: 'log-gmail-agent', label: 'talk2gmail.log', key: 'gmail_agent' });
    }
    logBlocks.innerHTML = blocks
      .map(
        (b) => `
      <div class="log-block">
        <div class="log-label">${escapeHtml(b.label)}</div>
        <pre id="${b.id}" class="log-pre waiting" data-key="${b.key}">Loading…</pre>
      </div>`
      )
      .join('');
  }

  async function refreshLogs(forceRebuild) {
    if (!logBlocks) return;
    const lines = logLines ? parseInt(logLines.value, 10) : 200;
    const filter = logFilter ? logFilter.value : 'all';
    if (forceRebuild) logsLayoutKey = '';
    ensureLogBlocks(filter);
    try {
      const res = await fetch('/api/logs?lines=' + lines + '&agent=' + filter + '&_=' + Date.now());
      const data = await res.json();
      logBlocks.querySelectorAll('.log-pre').forEach((pre) => {
        const logKey = pre.dataset.key;
        const text = data[logKey] || '(empty)';
        const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
        pre.textContent = text;
        pre.classList.remove('waiting');
        if (atBottom || text.endsWith('(empty)') === false) {
          pre.scrollTop = pre.scrollHeight;
        }
      });
    } catch (err) {
      if (forceRebuild) {
        logBlocks.innerHTML =
          '<p class="muted-text">Could not load logs: ' + escapeHtml(err.message) + '</p>';
        logsLayoutKey = '';
      }
    }
  }

  function startLogPolling() {
    stopLogPolling();
    refreshLogs(false);
    logPollId = setInterval(() => refreshLogs(false), 800);
  }

  function stopLogPolling() {
    if (logPollId) {
      clearInterval(logPollId);
      logPollId = null;
    }
  }

  if (logLines) {
    logLines.addEventListener('input', () => {
      if (logLinesVal) logLinesVal.textContent = logLines.value;
      if (activeTab === 'logs') refreshLogs(true);
    });
  }
  if (logFilter) {
    logFilter.addEventListener('change', () => {
      if (activeTab === 'logs') refreshLogs(true);
    });
  }

  async function refreshStatus() {
    if (statusInFlight) return;
    statusInFlight = true;
    try {
      const res = await fetch('/api/status');
      const data = await res.json();
      statusSnapshot = data;
      const runState = resolveRunningState(data);

      if (wasRunning && !runState.running) {
        output.dataset.cleared = '';
        const seq = data.history_seq || 0;
        if (seq > lastToastSeq) {
          lastToastSeq = seq;
          const ok = !data.last_error && data.last_result && data.last_result.complete !== false;
          showAlert(ok ? 'Run finished successfully.' : 'Run finished with issues — see output.', ok ? 'success' : 'error');
        }
      }
      wasRunning = runState.running;

      markRunningAgent(runState.running, runState.agent);
      setRunningUI(runState.running, runState.agent);
      setCapPill(capApi, data.api_key_valid, 'API key ✓', 'API key ✗');
      setGmailPill(data.gmail_setup || (data.gmail_configured ? { ready: true } : null));
      renderTranscript(data, runState.running);
      renderHistory(data.run_history);
      if (runState.running) {
        refreshLogs(false);
      } else if (activeTab === 'logs') {
        refreshLogs(false);
      }
      if (runState.running && !logPollId) {
        startLogPolling();
      } else if (!runState.running && logPollId) {
        stopLogPolling();
        refreshLogs(false);
      }
    } catch (err) {
      if (output.dataset.cleared !== '1') {
        output.textContent = 'Could not reach API: ' + err.message;
        output.classList.remove('waiting');
      }
    } finally {
      statusInFlight = false;
    }
  }

  function schedulePoll(ms) {
    const id = setTimeout(refreshStatus, ms);
    pollTimers.push(id);
  }

  function clearPollTimers() {
    pollTimers.forEach(clearTimeout);
    pollTimers = [];
  }

  function buildPayload(dryRun) {
    const payload = { agent: currentAgent, dry_run: dryRun };
    if (currentAgent === 'paint') {
      const qEl = document.getElementById('question');
      const question = normalizePaintInput(qEl ? qEl.value : '');
      if (qEl && question !== qEl.value.trim()) {
        qEl.value = question;
        syncFormState();
      }
      payload.question = question;
      if (!payload.question) return null;
    } else {
      payload.to = document.getElementById('email-to').value.trim();
      payload.subject = document.getElementById('email-subject').value.trim();
      payload.body = document.getElementById('email-body').value.trim();
      if (!payload.to || !payload.subject || !payload.body) return null;
    }
    return payload;
  }

  async function startRun(dryRun) {
    const payload = buildPayload(dryRun);
    if (!payload) return;

    if (currentAgent === 'paint' && looksLikeManualPaintCommand(payload.question)) {
      showAlert('Type plain text only — the LLM calls Paint tools for you.', 'error');
      syncFormState();
      return;
    }

    output.dataset.cleared = '';
    showOutputError('');
    hideAlert();
    localRunning = true;
    runStartedAt = Date.now();
    runBaseline = snapshotRunState(statusSnapshot);
    setRunningUI(true, currentAgent);
    markRunningAgent(true, currentAgent);

    try {
      const res = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        clearLocalRun();
        stopLogPolling();
        setRunningUI(false);
        markRunningAgent(false, null);
        showAlert(formatError(data.detail), 'error');
        return;
      }
      hideAlert();
      clearPollTimers();
      showAlert(data.message || 'Run started.', 'info');
      switchTab('logs');
      startLogPolling();
      [200, 500, 1000, 1500, 2500, 4000, 6000, 10000].forEach(schedulePoll);
    } catch (err) {
      clearLocalRun();
      stopLogPolling();
      setRunningUI(false);
      markRunningAgent(false, null);
      showAlert('Network error: ' + err.message, 'error');
    }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    startRun(false);
  });
  dryBtn.addEventListener('click', () => startRun(true));

  if (clearOutputBtn) {
    clearOutputBtn.addEventListener('click', () => {
      output.textContent = '';
      output.classList.add('waiting');
      output.dataset.cleared = '1';
      showOutputError('');
    });
  }

  hideAlert();
  initPaintSuggestions();
  const questionEl = document.getElementById('question');
  if (questionEl) {
    questionEl.addEventListener('input', () => {
      if (paintSuggestions) {
        paintSuggestions.querySelectorAll('.suggest-chip').forEach((c) => c.classList.remove('active'));
      }
      syncFormState();
      hideAlert();
    });
  }
  ['email-to', 'email-subject', 'email-body'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', syncFormState);
  });
  restoreSelectedAgent();
  initDefaultPaintQuestion();
  syncFormState();
  if (logLinesVal && logLines) logLinesVal.textContent = logLines.value;
  loadAgents();
  setInterval(refreshStatus, 2000);
  refreshStatus();
})();
