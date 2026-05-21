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
  const testCasesList = document.getElementById('test-cases-list');
  const testsAgentBadge = document.getElementById('tests-agent-badge');
  const clearOutputBtn = document.getElementById('clear-output');
  const helpShortcut = document.getElementById('help-shortcut');
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
  const toolsTitle = document.getElementById('tools-title');
  const toolsDesc = document.getElementById('tools-desc');
  const toolsList = document.getElementById('tools-list');
  const toolsMeta = document.getElementById('tools-meta');
  const agentGrid = document.getElementById('agent-grid');

  let pollTimers = [];
  let statusInFlight = false;
  let localRunning = false;
  let runBaseline = null;
  let activeTab = 'tools';
  let currentAgent = 'paint';
  let agentsMeta = {};
  let statusSnapshot = null;
  let logsLayoutKey = '';

  const PAINT_SUGGESTIONS = [
    'What is the capital of France?',
    'What is MCP?',
    'How does an LLM agent work?',
    'What is the speed of light?',
    'Can Paint be controlled without an API?',
  ];

  const GMAIL_SUGGESTIONS = [
    {
      label: 'What is MCP?',
      subject: 'What is MCP?',
      body: 'This email was sent by a Gemini LLM agent using Gmail MCP — not manually.',
    },
    {
      label: 'Week 4 complete',
      subject: 'Talk2MCP Week 4 complete',
      body: 'Paint was controlled without an API. This email proves the same MCP pattern works for Gmail.',
    },
    {
      label: 'Quick MCP test',
      subject: 'MCP agent test',
      body: 'Hello from Talk2Gmail — LLM chose send-email via MCP.',
    },
  ];

  const STORAGE_KEY = 'talk2mcp_selected_agent';

  // Block explicit tool/command phrasing, not normal questions mentioning Paint.
  const MANUAL_PAINT_PATTERNS = [
    /\bopen_paint\b/i,
    /\bdraw_rectangle\b/i,
    /\badd_text_in_paint\b/i,
    /\bopen\s+paint\b/i,
    /\bdraw\s+rectangle\b/i,
    /\bmspaint\b/i,
    /\bdraw\s+[a-z]\s*letter\b/i,
    /\bdraw\s+letter\b/i,
    /\bopen\s+paint\s+draw\b/i,
  ];

  function paintInputState(text) {
    const t = (text || '').trim();
    if (!t) return 'empty';
    return MANUAL_PAINT_PATTERNS.some((re) => re.test(t)) ? 'blocked' : 'ok';
  }

  function looksLikeManualPaintCommand(text) {
    return paintInputState(text) === 'blocked';
  }

  const AGENT_UI = {
    paint: {
      formTitle: 'Run Paint Agent',
      formDesc: 'Enter text to write in Paint — Gemini calls open_paint → draw_rectangle → add_text_in_paint.',
      runHint: '<strong>Run Live</strong> = Gemini + Paint &nbsp;|&nbsp; <strong>Dry Run</strong> = simulate 3 tools',
    },
    gmail: {
      formTitle: 'Run Gmail Agent',
      formDesc: 'Fill email fields — the LLM calls send-email via Gmail MCP.',
      runHint: '<strong>Run Live</strong> = Gemini + Gmail MCP &nbsp;|&nbsp; <strong>Dry Run</strong> = simulate send-email',
    },
  };

  // --- Tabs ---
  const tabs = document.querySelectorAll('.tab');
  const panels = document.querySelectorAll('.tab-panel');

  function switchTab(name) {
    activeTab = name;
    tabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    panels.forEach((p) => p.classList.toggle('active', p.id === 'panel-' + name));
    if (name === 'logs') refreshLogs(true);
    if (name === 'tests') loadTestCases();
  }

  tabs.forEach((tab) => tab.addEventListener('click', () => switchTab(tab.dataset.tab)));
  if (helpShortcut) helpShortcut.addEventListener('click', () => switchTab('guide'));

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
    const container = document.getElementById('paint-suggestions');
    if (!container) return;
    container.innerHTML = PAINT_SUGGESTIONS.map(
      (q) =>
        `<button type="button" class="suggest-chip" data-q="${encodeURIComponent(q)}">${escapeHtml(q)}</button>`
    ).join('');
    container.querySelectorAll('.suggest-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        selectAgent('paint');
        document.getElementById('question').value = decodeURIComponent(chip.dataset.q);
        document.getElementById('question').focus();
        updatePaintWarn();
        hideAlert();
        showAlert('Suggestion filled — click Run Live or Dry Run.', 'info');
      });
    });
  }

  function updatePaintWarn() {
    const warn = document.getElementById('paint-warn');
    const q = document.getElementById('question');
    if (!warn || !q) return;
    const state = paintInputState(q.value);
    warn.classList.remove('suggest-warn-bad', 'suggest-warn-ok');
    if (state === 'blocked') {
      warn.classList.add('suggest-warn-bad');
      warn.innerHTML =
        '<strong>Looks like a manual paint command.</strong> Enter a question instead — ' +
        'e.g. “What is MCP?” — and let Gemini call the 3 MCP tools.';
    } else if (state === 'ok') {
      warn.classList.add('suggest-warn-ok');
      warn.innerHTML =
        '<strong>Ready.</strong> Gemini will call ' +
        '<code>open_paint</code> → <code>draw_rectangle</code> → <code>add_text_in_paint</code> for you.';
    } else {
      warn.innerHTML =
        '<strong>Tip:</strong> Ask a question like “What is MCP?” — not a command like “open paint draw S letter”.';
    }
  }

  function snapshotRunState(data) {
    return {
      result: JSON.stringify((data && data.last_result) || null),
      error: (data && data.last_error) || null,
    };
  }

  function runStateChanged(data) {
    if (!runBaseline) return false;
    const snap = snapshotRunState(data);
    return snap.result !== runBaseline.result || snap.error !== runBaseline.error;
  }

  function clearLocalRun() {
    localRunning = false;
    runBaseline = null;
  }

  function setCapPill(el, ok, okText, badText) {
    if (!el) return;
    el.textContent = ok ? okText : badText;
    el.className = 'cap-pill ' + (ok ? 'cap-ok' : 'cap-bad');
  }

  function initGmailSuggestions() {
    const container = document.getElementById('gmail-suggestions');
    if (!container) return;
    container.innerHTML = GMAIL_SUGGESTIONS.map(
      (s, i) =>
        `<button type="button" class="suggest-chip suggest-chip-gmail" data-idx="${i}">${escapeHtml(s.label)}</button>`
    ).join('');
    container.querySelectorAll('.suggest-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        const s = GMAIL_SUGGESTIONS[parseInt(chip.dataset.idx, 10)];
        document.getElementById('email-subject').value = s.subject;
        document.getElementById('email-body').value = s.body;
        selectAgent('gmail');
        showAlert('Email template filled — add your address in To, then Run.', 'info');
      });
    });
  }

  function applyAgentUI(agent) {
    currentAgent = agent;
    document.querySelectorAll('.agent-card').forEach((c) => {
      c.classList.toggle('active', c.dataset.agent === agent);
    });
    fieldsPaint.classList.toggle('hidden', agent !== 'paint');
    fieldsGmail.classList.toggle('hidden', agent !== 'gmail');
    const ui = AGENT_UI[agent];
    if (ui) {
      formTitle.textContent = ui.formTitle;
      formDesc.textContent = ui.formDesc;
      document.getElementById('run-hint').innerHTML = ui.runHint;
    }
    if (testsAgentBadge) testsAgentBadge.textContent = agent;
    if (logFilter) {
      logFilter.value = agent === 'gmail' ? 'gmail' : 'paint';
    }
    renderToolsPanel();
    if (activeTab === 'tests') loadTestCases();
  }

  function selectAgent(agent) {
    if (!AGENT_UI[agent]) return;
    if (localRunning || (statusSnapshot && statusSnapshot.running)) return;
    applyAgentUI(agent);
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

  if (agentGrid) {
    agentGrid.querySelectorAll('.agent-card').forEach((card) => {
      card.addEventListener('click', () => selectAgent(card.dataset.agent));
    });
  }

  function renderToolsPanel() {
    const meta = agentsMeta[currentAgent];
    if (!meta) return;
    toolsTitle.textContent = meta.name + ' Tools';
    toolsDesc.textContent =
      currentAgent === 'paint' ? 'LLM must call in this order' : 'LLM must call this tool';
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
    toolsMeta.innerHTML = `
      <li><span class="meta-label">Log</span> <code>logs/${escapeHtml(meta.log_file)}</code></li>
      <li><span class="meta-label">CLI</span> <code>${escapeHtml(meta.cli)}</code></li>
      <li><span class="meta-label">Ready</span> ${meta.configured ? '✓ configured' : '⚠ setup needed'}</li>`;
  }

  async function loadAgents() {
    try {
      const res = await fetch('/api/agents');
      const data = await res.json();
      (data.agents || []).forEach((a) => {
        agentsMeta[a.id] = a;
      });
      setCapPill(capApi, data.api_key_valid, 'API key ✓', 'API key ✗');
      const gmail = agentsMeta.gmail;
      setCapPill(capGmail, gmail && gmail.configured, 'Gmail ✓', 'Gmail setup');
      renderToolsPanel();
    } catch {
      setCapPill(capApi, false, 'API …', 'API offline');
    }
  }

  function setRunningUI(running, runningAgent) {
    const disabled = !!running;
    runBtn.disabled = disabled;
    dryBtn.disabled = disabled;
    document.querySelectorAll('.agent-card').forEach((c) => {
      c.disabled = disabled;
      c.classList.toggle('disabled', disabled);
    });
    document.querySelectorAll('.tc-run, .tc-dry, .tc-fill').forEach((el) => {
      el.disabled = disabled;
    });

    if (running) {
      statusBadge.className = 'badge badge-warning';
      const label = runningAgent === 'gmail' ? 'Gmail' : 'Paint';
      statusText.innerHTML = '<span class="spinner"></span> Running ' + label;
      if (output.dataset.cleared !== '1' && !output.dataset.cleared) {
        output.textContent = label + ' agent running… waiting for LLM tool calls…';
        output.classList.remove('waiting');
      }
    } else {
      statusBadge.className = 'badge badge-success';
      statusText.innerHTML = '<span class="badge-dot"></span> Idle';
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

  function renderTranscript(data) {
    const userCleared = output.dataset.cleared === '1';

    if (!userCleared) {
      showOutputError(data.last_error || '');
    } else {
      showOutputError('');
    }

    if (data.running && !userCleared) {
      const label = data.running_agent === 'gmail' ? 'Gmail' : 'Paint';
      output.textContent = label + ' agent running… waiting for LLM tool calls…';
      output.classList.remove('waiting');
      return;
    }

    if (userCleared) {
      return;
    }

    if (!data.last_result) {
      if (!userCleared) {
        output.textContent = '';
        output.classList.add('waiting');
      }
      return;
    }

    output.classList.remove('waiting');
    const r = data.last_result;
    const lines = [];
    lines.push('Agent: ' + (r.agent || data.last_agent || 'paint'));
    if (r.agent === 'gmail' || r.recipient) {
      lines.push('To: ' + (r.recipient || ''));
      lines.push('Subject: ' + (r.subject || ''));
    } else {
      lines.push('Question: ' + (r.question || ''));
    }
    lines.push('Model: ' + (r.model || ''));
    if (r.dry_run) lines.push('Mode: dry-run (simulated)');
    if (r.complete === false) lines.push('Status: INCOMPLETE — not all required tools ran');
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
    historyCount.textContent = rows.length + ' run' + (rows.length === 1 ? '' : 's');
    if (!rows.length) {
      historyBody.innerHTML = '<tr class="empty-row"><td colspan="6">No runs yet.</td></tr>';
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

  function fillPaintCase(tc) {
    document.getElementById('question').value = tc.question || '';
    updatePaintWarn();
    selectAgent('paint');
  }

  function fillGmailCase(tc) {
    document.getElementById('email-to').value = tc.to || '';
    document.getElementById('email-subject').value = tc.subject || '';
    document.getElementById('email-body').value = tc.body || '';
    selectAgent('gmail');
  }

  async function loadTestCases() {
    try {
      const res = await fetch('/api/test-cases?agent=' + currentAgent);
      const data = await res.json();
      const cases = data.cases || [];

      if (currentAgent === 'paint') {
        testCasesList.innerHTML = cases
          .map(
            (tc) => `
          <div class="test-case-card">
            <span class="test-case-title">${escapeHtml(tc.title)}</span>
            <div class="test-case-q">${escapeHtml(tc.question)}</div>
            <div class="test-case-hint">${escapeHtml(tc.hint)}</div>
            <div class="test-case-actions">
              <button type="button" class="btn btn-primary tc-run" data-id="${escapeHtml(tc.id)}">▶ Live</button>
              <button type="button" class="btn btn-outline tc-dry" data-id="${escapeHtml(tc.id)}">⚡ Dry</button>
              <button type="button" class="btn btn-ghost tc-fill" data-id="${escapeHtml(tc.id)}">Fill</button>
            </div>
          </div>`
          )
          .join('');
        testCasesList.querySelectorAll('.tc-run').forEach((el, i) => {
          el.addEventListener('click', () => {
            fillPaintCase(cases[i]);
            startRun(false);
          });
        });
        testCasesList.querySelectorAll('.tc-dry').forEach((el, i) => {
          el.addEventListener('click', () => {
            fillPaintCase(cases[i]);
            startRun(true);
          });
        });
        testCasesList.querySelectorAll('.tc-fill').forEach((el, i) => {
          el.addEventListener('click', () => {
            fillPaintCase(cases[i]);
            showAlert('Question filled.', 'info');
          });
        });
      } else {
        testCasesList.innerHTML = cases
          .map(
            (tc) => `
          <div class="test-case-card">
            <span class="test-case-title">${escapeHtml(tc.title)}</span>
            <div class="test-case-q">${escapeHtml(tc.subject)}</div>
            <div class="test-case-hint">${escapeHtml(tc.hint)}</div>
            <div class="test-case-actions">
              <button type="button" class="btn btn-primary tc-run" data-id="${escapeHtml(tc.id)}">▶ Live</button>
              <button type="button" class="btn btn-outline tc-dry" data-id="${escapeHtml(tc.id)}">⚡ Dry</button>
              <button type="button" class="btn btn-ghost tc-fill" data-id="${escapeHtml(tc.id)}">Fill</button>
            </div>
          </div>`
          )
          .join('');
        testCasesList.querySelectorAll('.tc-run').forEach((el, i) => {
          el.addEventListener('click', () => {
            fillGmailCase(cases[i]);
            startRun(false);
          });
        });
        testCasesList.querySelectorAll('.tc-dry').forEach((el, i) => {
          el.addEventListener('click', () => {
            fillGmailCase(cases[i]);
            startRun(true);
          });
        });
        testCasesList.querySelectorAll('.tc-fill').forEach((el, i) => {
          el.addEventListener('click', () => {
            fillGmailCase(cases[i]);
            showAlert('Email fields filled — add To address for live run.', 'info');
          });
        });
      }
    } catch {
      testCasesList.innerHTML = '<p class="muted-text">Could not load test cases.</p>';
    }
  }

  function ensureLogBlocks(filter) {
    if (!logBlocks) return;
    const key = filter + ':' + (logLines ? logLines.value : '200');
    if (key === logsLayoutKey) return;
    logsLayoutKey = key;
    const blocks = [];
    if (filter === 'all' || filter === 'paint') {
      blocks.push({ id: 'log-paint-agent', label: 'Paint agent (talk2mcp.log)', key: 'paint_agent' });
      blocks.push({ id: 'log-paint-server', label: 'Paint MCP server', key: 'paint_server' });
    }
    if (filter === 'all' || filter === 'gmail') {
      blocks.push({ id: 'log-gmail-agent', label: 'Gmail agent (talk2gmail.log)', key: 'gmail_agent' });
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
      const res = await fetch('/api/logs?lines=' + lines + '&agent=' + filter);
      const data = await res.json();
      logBlocks.querySelectorAll('.log-pre').forEach((pre) => {
        const logKey = pre.dataset.key;
        pre.textContent = data[logKey] || '(empty)';
        pre.classList.remove('waiting');
      });
    } catch (err) {
      logBlocks.innerHTML =
        '<p class="muted-text">Could not load logs: ' + escapeHtml(err.message) + '</p>';
      logsLayoutKey = '';
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
      if (data.running) {
        clearLocalRun();
        markRunningAgent(true, data.running_agent);
      } else if (localRunning) {
        if (runStateChanged(data)) {
          clearLocalRun();
          markRunningAgent(false, null);
        } else {
          markRunningAgent(true, currentAgent);
        }
      } else {
        markRunningAgent(false, null);
      }
      const running = !!data.running || localRunning;
      const runningAgent = data.running
        ? data.running_agent
        : localRunning
          ? currentAgent
          : null;
      setRunningUI(running, runningAgent);
      setCapPill(capApi, data.api_key_valid, 'API key ✓', 'API key ✗');
      setCapPill(capGmail, data.gmail_configured, 'Gmail ✓', 'Gmail setup');
      renderTranscript(data);
      renderHistory(data.run_history);
      if (activeTab === 'logs') refreshLogs(false);
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
      payload.question = document.getElementById('question').value.trim();
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
    if (!payload) {
      showAlert(
        currentAgent === 'paint'
          ? 'Please enter a question.'
          : 'Please fill To, Subject, and Message.',
        'error'
      );
      return;
    }

    if (currentAgent === 'paint' && looksLikeManualPaintCommand(payload.question)) {
      showAlert(
        'That looks like a manual paint command. Enter a question or phrase instead — ' +
          'e.g. "What is MCP?" — and Gemini will call the Paint tools automatically.',
        'error'
      );
      return;
    }

    output.dataset.cleared = '';
    showOutputError('');
    localRunning = true;
    runBaseline = snapshotRunState(statusSnapshot);
    setRunningUI(true, currentAgent);
    markRunningAgent(true, currentAgent);
    showAlert(dryRun ? 'Starting dry-run…' : 'Starting live agent…', 'info');

    try {
      const res = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        clearLocalRun();
        setRunningUI(false);
        markRunningAgent(false, null);
        showAlert(formatError(data.detail), 'error');
        return;
      }
      showAlert(data.message, 'success');
      clearPollTimers();
      [300, 800, 1500, 3000, 6000, 10000].forEach(schedulePoll);
    } catch (err) {
      clearLocalRun();
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
  initGmailSuggestions();
  const questionEl = document.getElementById('question');
  if (questionEl) {
    questionEl.addEventListener('input', () => {
      updatePaintWarn();
      if (paintInputState(questionEl.value) !== 'blocked') hideAlert();
    });
  }
  restoreSelectedAgent();
  updatePaintWarn();
  if (logLinesVal && logLines) logLinesVal.textContent = logLines.value;
  loadAgents();
  loadTestCases();
  setInterval(refreshStatus, 2000);
  refreshStatus();
})();
