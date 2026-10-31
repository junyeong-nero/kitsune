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
