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

// Also reflect changes made in about:addons → Kitsune → Permissions.
browser.permissions.onAdded.addListener(showPermissions);
browser.permissions.onRemoved.addListener(showPermissions);
showPermissions();
