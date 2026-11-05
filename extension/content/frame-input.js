// Lightweight child-frame input observation; no command engine, dialog hooks or page observers.
(() => {
  'use strict';
  if (window === window.top || window.__kitsuneFrameInputInstalled) return;
  window.__kitsuneFrameInputInstalled = true;
  const documentToken = Array.from(crypto.getRandomValues(new Uint32Array(4))).join('-');
  const modifiers = new Set(['Alt', 'Control', 'Meta', 'Shift']);
  // content.js USER_TYPING_IDLE_MS: a draft left this long without user input no longer holds the tab (#456).
  const USER_TYPING_IDLE_MS = 10000;
  let userTypedField = null, lastUserInputAt = -Infinity;
  let inputSequence = 0, typedSequence = 0;
  const pending = new Set();
  for (const type of ['keydown', 'mousedown', 'pointerdown', 'touchstart', 'wheel', 'paste']) {
    window.addEventListener(type, event => {
      if (!event.isTrusted) return;
      const target = event.target, sequence = ++inputSequence, at = Date.now();
      // Only modifier names cross the boundary, never typed characters or field values.
      // The background excludes announced OS input even in newly loaded frames.
      const report = browser.runtime.sendMessage({ action: 'trustedTabInput', at, type: event.type,
        documentToken,
        ...(event.type === 'keydown' && modifiers.has(event.key) ? { modifier: event.key } : {}),
      }).then(result => {
        if (result?.userInput) lastUserInputAt = Math.max(lastUserInputAt, at);
        if (result?.userInput && sequence > typedSequence && (type === 'keydown' || type === 'paste') &&
            (['INPUT', 'TEXTAREA'].includes(target?.tagName) || target?.isContentEditable ||
             target?.tagName?.includes('-'))) {
          typedSequence = sequence;
          userTypedField = target;
        }
      }).catch(() => {}).finally(() => pending.delete(report));
      pending.add(report);
    }, { capture: true, passive: true });
  }
  browser.runtime.onMessage.addListener(msg => {
    // The user approved handing this tab to an agent (#456). Reports still in flight are older
    // than the approval, so they cannot make their field the draft again.
    if (msg.action === 'releaseUserDraft') {
      typedSequence = inputSequence;
      userTypedField = null;
      return Promise.resolve({ ok: true });
    }
    if (msg.action !== 'frameUserActivity') return undefined;
    return Promise.all([...pending]).then(() => {
      if (msg.documentToken !== documentToken) return { active: false };
      const field = userTypedField;
      // hasFocus includes focus in the containing documents, unlike activeElement alone.
      const hasContent = field && (typeof field.value === 'string' ? field.value.length > 0
        : field.isContentEditable ? (field.textContent || '').trim().length > 0 : true);
      const idleMs = Date.now() - lastUserInputAt;
      // An inactive matching document is still queryable after focus returns, without new input.
      if (field?.isConnected && field === document.activeElement && document.hasFocus() && hasContent &&
          idleMs < USER_TYPING_IDLE_MS) {
        return { active: true, state: { reason: 'typing', field: field.tagName.toLowerCase(), sinceMs: idleMs,
          windowMs: USER_TYPING_IDLE_MS } };
      }
      // Released in a focused document (also once idle), or after focus moved elsewhere in its
      // focused page (the background's pageFocused, from the top frame): the agent's own later focus
      // or text there is no user draft. Only focus leaving the whole page keeps the field for when
      // focus returns.
      if (field && (document.hasFocus() || msg.pageFocused === true)) userTypedField = null;
      return { active: false, documentToken };
    });
  });
})();
