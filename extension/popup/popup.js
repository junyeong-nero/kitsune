// Toolbar popup: closes the tabs agents opened, and sets the idle limit background.js applies
// (docs/api-reference.md#agent-tab-cleanup).
const IDLE_KEY = 'agentTabIdleMinutes';
const countLine = document.getElementById('count');
const closeButton = document.getElementById('close-all');
const resultLine = document.getElementById('result');
const idleSelect = document.getElementById('idle');

async function showCount() {
  const { count } = await browser.runtime.sendMessage({ action: 'agentTabCount' });
  countLine.textContent = String(count);
  closeButton.disabled = count === 0;
}

closeButton.addEventListener('click', async () => {
  closeButton.disabled = true;
  const { closed } = await browser.runtime.sendMessage({ action: 'closeAgentTabs' });
  resultLine.hidden = false;
  resultLine.textContent = `Closed ${closed} tab${closed === 1 ? '' : 's'}. Recently closed tabs can be reopened from History.`;
  await showCount();
});

idleSelect.addEventListener('change', () => {
  browser.storage.local.set({ [IDLE_KEY]: Number(idleSelect.value) });
});

browser.storage.local.get(IDLE_KEY).then(stored => {
  idleSelect.value = String(stored[IDLE_KEY] || 0);
});
showCount();

// Untrusted client/page labels are text only, never HTML or links.
async function showHandoffs() {
  const state = await browser.runtime.sendMessage({ action: 'handoffs' });
  const root = document.getElementById('handoffs');
  root.replaceChildren();
  for (const [kind, records] of [['Request', state.requests], ['Handed off', state.tabs]]) {
    for (const record of records) {
      const section = document.createElement('section');
      const label = document.createElement('strong');
      label.textContent = `${kind}: ${record.clientName}`;
      section.append(label);
      for (const text of [record.title, record.url, `Reason: ${record.reason}`]) {
        const line = document.createElement('p');
        line.textContent = text;
        line.style.overflowWrap = 'anywhere';
        section.append(line);
      }
      for (const decision of kind === 'Request' ? ['approve', 'decline'] : ['take back']) {
        const button = document.createElement('button');
        button.textContent = decision === 'approve' ? 'Approve handoff' : decision === 'decline' ? 'Decline' : 'Take back';
        button.addEventListener('click', async () => {
          button.disabled = true;
          await browser.runtime.sendMessage(decision === 'take back'
            ? { action: 'takeBackHandoff', tabId: record.tabId }
            : { action: 'handoffDecision', id: record.id, decision });
          await showHandoffs();
          await showCount();
        });
        section.append(button);
      }
      root.append(section);
    }
  }
}
showHandoffs();
setInterval(showHandoffs, 1000);
