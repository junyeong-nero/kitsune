// Toolbar popup: closes the tabs agents opened, and sets the idle limit background.js applies
// (docs/api-reference.md#agent-tab-cleanup).
const IDLE_KEY = 'agentTabIdleMinutes';
const countLine = document.getElementById('count');
const closeButton = document.getElementById('close-all');
const resultLine = document.getElementById('result');
const idleSelect = document.getElementById('idle');
let closingTabs = false;

async function showCount() {
  const { count } = await browser.runtime.sendMessage({ action: 'agentTabCount' });
  countLine.textContent = String(count);
  closeButton.disabled = closingTabs || count === 0;
}

closeButton.addEventListener('click', async () => {
  closingTabs = true;
  closeButton.disabled = true;
  try {
    const { closed } = await browser.runtime.sendMessage({ action: 'closeAgentTabs' });
    resultLine.textContent = `Closed ${closed} tab${closed === 1 ? '' : 's'}. Recently closed tabs can be reopened from History.`;
  } catch {
    resultLine.textContent = 'Could not close agent tabs. Try again.';
  } finally {
    closingTabs = false;
    resultLine.hidden = false;
  }
  await showCount();
});

idleSelect.addEventListener('change', () => {
  browser.storage.local.set({ [IDLE_KEY]: Number(idleSelect.value) });
});

browser.storage.local.get(IDLE_KEY).then(stored => {
  idleSelect.value = String(stored[IDLE_KEY] ?? 5);
});
showCount();

async function showUsage() {
  const { connected, calls, lastActivity } = await browser.runtime.sendMessage({ action: 'popupStatus' });
  const connection = document.getElementById('connection');
  connection.textContent = connected ? 'Connected' : 'Disconnected';
  connection.className = `connection ${connected ? 'connected' : 'disconnected'}`;
  document.getElementById('calls').textContent = connected && calls != null ? calls.toLocaleString() : '—';
  const seconds = lastActivity == null ? null : Math.max(0, Math.floor((Date.now() - lastActivity) / 1000));
  document.getElementById('last-active').textContent = !connected || seconds == null ? '—'
    : seconds < 10 ? 'Just now' : seconds < 60 ? `${seconds}s ago`
    : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 3600)}h ago`;
  document.getElementById('usage-note').textContent = !connected ? 'Waiting for the local bridge'
    : calls == null ? 'Update the MCP server to see usage'
    : 'Since bridge connected · all clients';
}

// Untrusted client/page labels are text only, never HTML or links.
let handoffState = '';
let handoffBusy = 0;
async function showHandoffs() {
  const state = await browser.runtime.sendMessage({ action: 'handoffs' });
  const serialized = JSON.stringify(state);
  // Polling must not replace a focused button or re-announce unchanged cards.
  if (handoffBusy || serialized === handoffState) return;
  handoffState = serialized;
  const root = document.getElementById('handoffs');
  root.replaceChildren();
  for (const [kind, records] of [['Request', state.requests], ['Handed off', state.tabs]]) {
    for (const record of records) {
      const section = document.createElement('section');
      section.className = 'handoff';
      for (const [tag, className, text] of [
        ['p', 'handoff-label', kind === 'Request' ? 'Tab access request' : 'Handed off'],
        ['p', 'handoff-client', record.clientName],
        ['h3', '', record.title],
        ['p', 'handoff-url', record.url],
        ['p', 'handoff-reason', record.reason],
      ]) {
        const line = document.createElement(tag);
        line.className = className;
        line.textContent = text;
        section.append(line);
      }
      const actions = document.createElement('div');
      actions.className = 'actions';
      for (const decision of kind === 'Request' ? ['decline', 'approve'] : ['take back']) {
        const button = document.createElement('button');
        button.className = `button${decision === 'decline' ? ' button-text' : ''}`;
        button.textContent = decision === 'approve' ? 'Approve handoff' : decision === 'decline' ? 'Decline' : 'Take back';
        button.addEventListener('click', async () => {
          handoffBusy += 1;
          for (const action of actions.children) action.disabled = true;
          try {
            await browser.runtime.sendMessage(decision === 'take back'
              ? { action: 'takeBackHandoff', tabId: record.tabId }
              : { action: 'handoffDecision', id: record.id, decision });
          } catch {
            resultLine.hidden = false;
            resultLine.textContent = 'Could not update tab access. Try again.';
          } finally {
            handoffBusy -= 1;
            for (const action of actions.children) action.disabled = false;
          }
          await showHandoffs();
          await showCount();
        });
        actions.append(button);
      }
      section.append(actions);
      root.append(section);
    }
  }
}
// Per-tool approvals (#525): a tool the user marked in the options page waits for an answer here.
// Agent-supplied names and arguments are text only.
let approvalState = '';
let approvalBusy = 0;
async function showToolApprovals() {
  const requests = await browser.runtime.sendMessage({ action: 'toolApprovals' });
  const serialized = JSON.stringify(requests);
  if (approvalBusy || serialized === approvalState) return;
  approvalState = serialized;
  const root = document.getElementById('tool-approvals');
  root.replaceChildren();
  for (const record of requests) {
    const section = document.createElement('section');
    section.className = 'handoff';
    for (const [tag, className, text] of [
      ['p', 'handoff-label', 'Tool approval'],
      ['p', 'handoff-client', record.clientName],
      ['h3', '', record.tool],
      ['p', 'handoff-reason', record.summary && record.summary !== '{}' ? record.summary : ''],
    ]) {
      const line = document.createElement(tag);
      line.className = className;
      line.textContent = text;
      section.append(line);
    }
    const actions = document.createElement('div');
    actions.className = 'actions';
    for (const [decision, label] of [['decline', 'Decline'], ['session', 'Allow for this session'], ['once', 'Allow once']]) {
      const button = document.createElement('button');
      button.className = `button${decision === 'decline' ? ' button-text' : ''}`;
      button.textContent = label;
      button.addEventListener('click', async () => {
        approvalBusy += 1;
        for (const action of actions.children) action.disabled = true;
        try {
          await browser.runtime.sendMessage({ action: 'toolApprovalDecision', id: record.id, decision });
        } catch {
          resultLine.hidden = false;
          resultLine.textContent = 'Could not answer the approval. Try again.';
        } finally {
          approvalBusy -= 1;
        }
        await showToolApprovals();
      });
      actions.append(button);
    }
    section.append(actions);
    root.append(section);
  }
}
showToolApprovals();
showHandoffs();
showUsage();
setInterval(() => { showToolApprovals(); showHandoffs(); showUsage(); showCount(); }, 1000);
