// Chrome build only (scripts/build_extension.py chrome, #589): the Firefox `browser` API the
// extension is written against, over Chrome's `chrome`. Loaded before every other script in the
// service worker, the content scripts and the extension pages.

'use strict';

(() => {
  if (typeof globalThis.browser !== 'undefined') return; // Firefox, or already shimmed
  const api = globalThis.chrome;
  globalThis.browser = api;

  // A Firefox onMessage listener answers with the promise it returns; Chrome only answers through
  // sendResponse, after the listener returned true. A rejection travels as KITSUNE_ERROR so that
  // sendMessage rejects as it does in Firefox.
  const KITSUNE_ERROR = '__kitsuneError';
  const onMessage = api.runtime.onMessage;
  const addListener = onMessage.addListener.bind(onMessage);
  const removeListener = onMessage.removeListener.bind(onMessage);
  const wrappers = new WeakMap();
  onMessage.addListener = listener => {
    const wrapper = (message, sender, sendResponse) => {
      const result = listener(message, sender, sendResponse);
      if (!result || typeof result.then !== 'function') return result === true;
      result.then(sendResponse, error => sendResponse({ [KITSUNE_ERROR]: String(error?.message ?? error) }));
      return true;
    };
    wrappers.set(listener, wrapper);
    addListener(wrapper);
  };
  onMessage.removeListener = listener => removeListener(wrappers.get(listener) || listener);

  const unwrap = value => {
    if (value && typeof value === 'object' && KITSUNE_ERROR in value) throw new Error(value[KITSUNE_ERROR]);
    return value;
  };
  for (const [namespace, name] of [[api.runtime, 'sendMessage'], [api.tabs, 'sendMessage']]) {
    const send = namespace?.[name]?.bind(namespace);
    if (send) namespace[name] = (...args) => send(...args).then(unwrap);
  }

  // MV3 has no tabs.executeScript; Firefox's file form maps to scripting.executeScript. The compat
  // shim goes first so the injected script finds `browser`.
  if (api.scripting && api.tabs && !api.tabs.executeScript) {
    api.tabs.executeScript = (tabId, { file, allFrames, frameId } = {}) => api.scripting.executeScript({
      target: { tabId, ...(allFrames ? { allFrames: true } : frameId ? { frameIds: [frameId] } : {}) },
      files: ['/chrome/compat.js', file],
    }).then(results => results.map(entry => entry.result));
  }
})();
