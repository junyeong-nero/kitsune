// Lightweight child-frame input observation; no command engine, dialog hooks or page observers.
(() => {
  'use strict';
  if (window === window.top || window.__kitsuneFrameInputInstalled) return;
  window.__kitsuneFrameInputInstalled = true;
  const documentToken = Array.from(crypto.getRandomValues(new Uint32Array(4))).join('-');
  const modifiers = new Set(['Alt', 'Control', 'Meta', 'Shift']);
  let userTypedField = null;
  let inputSequence = 0, typedSequence = 0;
  const pending = new Set();
  for (const type of ['keydown', 'mousedown', 'pointerdown', 'touchstart', 'wheel', 'paste']) {
    window.addEventListener(type, event => {
      if (!event.isTrusted) return;
      const target = event.target, sequence = ++inputSequence;
      // Only modifier names cross the boundary, never typed characters or field values.
      // The background excludes announced OS input even in newly loaded frames.
      const report = browser.runtime.sendMessage({ action: 'trustedTabInput', at: Date.now(), type: event.type,
        documentToken,
        ...(event.type === 'keydown' && modifiers.has(event.key) ? { modifier: event.key } : {}),
      }).then(result => {
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
    if (msg.action !== 'frameUserActivity') return undefined;
    return Promise.all([...pending]).then(() => {
      if (msg.documentToken !== documentToken) return { active: false };
      const field = userTypedField;
      // hasFocus includes focus in the containing documents, unlike activeElement alone.
      const hasContent = field && (typeof field.value === 'string' ? field.value.length > 0
        : field.isContentEditable ? (field.textContent || '').trim().length > 0 : true);
      return field?.isConnected && field === document.activeElement && document.hasFocus() && hasContent
        ? { active: true, state: { reason: 'typing', field: field.tagName.toLowerCase() } }
        : { active: false };
    });
  });
})();
