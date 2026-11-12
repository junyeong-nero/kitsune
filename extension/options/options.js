// The only place Kitsune asks for its optional history/bookmarks permissions (#140). The browser
// shows its permission prompt only for a request made during a user gesture, so
// permissions.request runs directly in the click handler; agents can never trigger it.
const HISTORY_PERMISSIONS = { permissions: ['history', 'bookmarks'] };
const grantButton = document.getElementById('grant');
const revokeButton = document.getElementById('revoke');
const errorLine = document.getElementById('error');

async function showPermissions() {
  const granted = {};
  for (const permission of HISTORY_PERMISSIONS.permissions) {
    granted[permission] = await browser.permissions.contains({ permissions: [permission] });
    document.getElementById(`${permission}-status`).textContent = granted[permission] ? 'allowed' : 'not allowed';
  }
  grantButton.disabled = Object.values(granted).every(Boolean);
  revokeButton.disabled = !Object.values(granted).some(Boolean);
}

function showError(e) {
  errorLine.hidden = false;
  errorLine.textContent = `Could not change the permission: ${e.message}`;
}

grantButton.addEventListener('click', () => {
  errorLine.hidden = true;
  browser.permissions.request(HISTORY_PERMISSIONS).then(showPermissions, showError);
});

revokeButton.addEventListener('click', () => {
  errorLine.hidden = true;
  browser.permissions.remove(HISTORY_PERMISSIONS).then(showPermissions, showError);
});

// Per-tool approvals (#525): the checked tools wait for the user; background.js sends the list to the
// bridge whenever it changes. Tool names and groups come from tool-approvals.js.
const approvalRoot = document.getElementById('approval-tools');
const savedLine = document.getElementById('approval-saved');

async function saveApprovals(tools) {
  await browser.storage.local.set({ [APPROVAL_TOOLS_KEY]: tools });
  savedLine.textContent = 'Saved.';
  await showApprovals();
}

async function showApprovals() {
  const selected = new Set(await approvalTools());
  approvalRoot.replaceChildren();
  for (const [group, names] of Object.entries(KITSUNE_TOOL_GROUPS)) {
    const fieldset = document.createElement('fieldset');
    const legend = document.createElement('legend');
    legend.textContent = group;
    fieldset.append(legend);
    for (const name of names) {
      const label = document.createElement('label');
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = selected.has(name);
      box.addEventListener('change', async () => {
        const tools = new Set(await approvalTools());
        if (box.checked) tools.add(name); else tools.delete(name);
        await saveApprovals([...tools].sort());
      });
      label.append(box, ` ${name}`);
      fieldset.append(label);
    }
    approvalRoot.append(fieldset);
  }
}

document.getElementById('approval-defaults').addEventListener('click', () => saveApprovals([...DEFAULT_APPROVAL_TOOLS]));
document.getElementById('approval-none').addEventListener('click', () => saveApprovals([]));
showApprovals();

// Also reflect changes made in about:addons → Kitsune → Permissions.
browser.permissions.onAdded.addListener(showPermissions);
browser.permissions.onRemoved.addListener(showPermissions);
showPermissions();
