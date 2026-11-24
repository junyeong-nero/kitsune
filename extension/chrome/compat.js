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

  // Chrome has no session tab values, which keep the agent's tabs (background.js, #376) across a
  // service worker restart. storage.session lasts as long as Chrome's tab ids do, until the browser
  // quits, and only the extension itself reads it. A value goes with its tab. Unlike Firefox,
  // a duplicated or reopened tab gets no copy: it is the user's either way.
  if (api.storage?.session && api.sessions && !api.sessions.getTabValue) {
    const store = api.storage.session;
    const prefix = tabId => `kitsune.tab.${tabId}.`;
    api.sessions.getTabValue = (tabId, key) => store.get(prefix(tabId) + key).then(found => found[prefix(tabId) + key]);
    api.sessions.setTabValue = (tabId, key, value) => store.set({ [prefix(tabId) + key]: value });
    api.sessions.removeTabValue = (tabId, key) => store.remove(prefix(tabId) + key);
    api.tabs.onRemoved.addListener(tabId => store.get(null)
      .then(all => store.remove(Object.keys(all).filter(key => key.startsWith(prefix(tabId)))))
      .catch(() => {}));
  }

  // Chrome's back button, and tabs.goBack/goForward, skip history entries that a page added without
  // user activation, such as those of the agent's synthetic clicks. Firefox goes to them, and so does
  // the page's own history.
  for (const [name, step, can] of [['goBack', 'back', 'canGoBack'], ['goForward', 'forward', 'canGoForward']]) {
    const go = api.scripting && api.tabs?.[name]?.bind(api.tabs);
    if (!go) continue;
    api.tabs[name] = (tabId, ...rest) => go(tabId, ...rest).catch(async error => {
      const [{ result } = {}] = await api.scripting.executeScript({ target: { tabId },
        func: (step, can) => (navigation[can] ? (history[step](), true) : false), args: [step, can] })
        .catch(() => []);
      if (result !== true) throw error;
    });
  }

  // MV3 has no tabs.executeScript; Firefox's file form maps to scripting.executeScript. The compat
  // shim goes first so the injected script finds `browser`. content.js also goes into the page's
  // main world, as the manifest has it (as page-world.js, its copy in the build).
  if (api.scripting && api.tabs && !api.tabs.executeScript) {
    api.tabs.executeScript = async (tabId, { file, allFrames, frameId } = {}) => {
      const target = { tabId, ...(allFrames ? { allFrames: true } : frameId ? { frameIds: [frameId] } : {}) };
      if (file === '/content/content.js') {
        await api.scripting.executeScript({ target, files: ['/chrome/page-world.js'], world: 'MAIN' }).catch(() => {});
      }
      const results = await api.scripting.executeScript({ target, files: ['/chrome/compat.js', file] });
      return results.map(entry => entry.result);
    };
  }
})();
