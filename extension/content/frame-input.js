// Ownership-only input observation in child frames, even before any frame command is sent.
// Do not install the full command engine here: it also changes dialogs and observes page state.
(() => {
  'use strict';
  if (window === window.top || window.__kitsuneFrameInputInstalled) return;
  window.__kitsuneFrameInputInstalled = true;
  const modifiers = new Set(['Alt', 'Control', 'Meta', 'Shift']);
  for (const type of ['keydown', 'mousedown', 'pointerdown', 'touchstart', 'wheel', 'paste']) {
    window.addEventListener(type, event => {
      if (!event.isTrusted) return;
      // Only modifier names cross the boundary, never typed characters or field values.
      // The background excludes announced OS input across frames, including newly loaded ones.
      browser.runtime.sendMessage({ action: 'trustedTabInput', at: Date.now(), type: event.type,
        ...(event.type === 'keydown' && modifiers.has(event.key) ? { modifier: event.key } : {}),
      }).catch(() => {});
    }, { capture: true, passive: true });
  }
})();
