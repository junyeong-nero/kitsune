// Kitsune - Content Script
// Handles all DOM interaction on web pages

(function() {
  'use strict';
  const CONTENT_VERSION = 42;
  if (window.__claudeBridgeVersion >= CONTENT_VERSION) return;
  window.__claudeBridgeVersion = CONTENT_VERSION;

  // scripts/build_extension.py sets 'signed' and strips @kitsune-dev-only regions:
  // Mozilla policy forbids running code that is not part of the signed package.
  const KITSUNE_BUILD = 'dev';
  const DEV_BUILD_ACTIONS = new Set(['executeJS', 'waitForResult']);
  const DEV_BUILD_REQUIRED = 'Agent-supplied JavaScript needs the Kitsune developer extension build; ' +
    'the signed build cannot run it (Mozilla add-on policy). See docs/installation.md#developer-build';

  // Install page-world functions before page scripts run. Firefox exports also work
  // under strict page CSP, which blocks the script element.
  function injectPageScript(code) {
    if (!document.documentElement) return;
    const script = document.createElement('script');
    script.textContent = code;
    document.documentElement.appendChild(script);
    script.remove();
  }

  function installDialogs(W, expose) {
    if (W.__kitsuneDialogSnapshot) return;
    const nativeDialogs = { alert: W.alert, confirm: W.confirm, prompt: W.prompt };
    let savedBeforeUnload = null;
    const suppressUnloadHandler = () => {
      if (W.onbeforeunload) savedBeforeUnload = W.onbeforeunload;
      W.onbeforeunload = null;
    };
    const state = { policy: { accept: null, prompt_text: null }, dialogs: [] };
    const record = (type, message, defaultValue, response) => {
      state.dialogs.push({ type, message: String(message ?? '').slice(0, 10000),
        default_value: defaultValue, timestamp: Date.now(), response, pending: false });
      if (state.dialogs.length > 100) state.dialogs.shift();
    };
    W.__kitsuneDialogSnapshot = expose(() => JSON.stringify(state));
    W.__kitsuneDialogPolicy = expose((json) => {
      const previousAccept = state.policy.accept;
      state.policy = JSON.parse(json);
      // Same semantics as close_tab(force=True), while allowing native mode again.
      if (state.policy.accept === true) suppressUnloadHandler();
      else if (previousAccept === true && W.onbeforeunload == null && savedBeforeUnload) {
        W.onbeforeunload = savedBeforeUnload;
        savedBeforeUnload = null;
      }
    });
    W.alert = expose((...args) => {
      // Preserve native argument conversion and blocking behavior in unmanaged tabs.
      const answer = state.policy.accept == null ? nativeDialogs.alert.apply(W, args) : undefined;
      record('alert', args[0], null, null);
      return answer;
    });
    W.confirm = expose((...args) => {
      const answer = state.policy.accept == null
        ? nativeDialogs.confirm.apply(W, args) : state.policy.accept === true;
      record('confirm', args[0], null, answer);
      return answer;
    });
    W.prompt = expose((...args) => {
      const defaultValue = args.length > 1 ? String(args[1]) : '';
      let answer = null;
      if (state.policy.accept == null) {
        answer = nativeDialogs.prompt.apply(W, args);
      } else if (state.policy.accept === true) {
        answer = String(state.policy.prompt_text ?? defaultValue);
      }
      record('prompt', args[0], defaultValue, answer);
      return answer;
    });
    W.addEventListener('beforeunload', expose(() => {
      record('beforeunload', '', null, state.policy.accept === true ? 'leave' : 'native');
      if (state.policy.accept === true) suppressUnloadHandler();
    }), true);
    const proto = W.BeforeUnloadEvent?.prototype;
    if (proto) {
      const descriptor = Object.getOwnPropertyDescriptor(proto, 'returnValue');
      if (descriptor?.configurable && descriptor.get && descriptor.set) {
        Object.defineProperty(proto, 'returnValue', { configurable: true,
          get: expose(function() { return descriptor.get.call(this); }),
          set: expose(function(value) {
            if (state.policy.accept !== true) descriptor.set.call(this, value);
          }) });
      }
      const preventDefault = W.Event.prototype.preventDefault;
      W.Event.prototype.preventDefault = expose(function() {
        if (this.type === 'beforeunload' && state.policy.accept === true) return;
        return preventDefault.call(this);
      });
    }
  }

  const pageWindow = window.wrappedJSObject || window;
  injectPageScript('(' + installDialogs.toString() + ')(window, f => f);');
  if (!pageWindow.__kitsuneDialogSnapshot && typeof exportFunction === 'function') {
    installDialogs(pageWindow, fn => exportFunction(fn, pageWindow));
  }
  function dialogInfo() {
    if (!pageWindow.__kitsuneDialogSnapshot) return { error: 'Page-world dialog capture unavailable' };
    return JSON.parse(pageWindow.__kitsuneDialogSnapshot());
  }
  function handleDialog(accept, promptText) {
    if (!pageWindow.__kitsuneDialogPolicy) return dialogInfo();
    pageWindow.__kitsuneDialogPolicy(JSON.stringify({ accept: accept ?? null, prompt_text: accept == null ? null : promptText ?? null }));
    return { ok: true, ...dialogInfo() };
  }
  // Restore the tab policy after navigation; native dialogs apply until this reply arrives.
  browser.runtime.sendMessage({ action: 'getDialogPolicy' }).then(policy => {
    if (policy) handleDialog(policy.accept, policy.prompt_text);
  }).catch(() => {});

  // Issued refs live for this document while their nodes stay attached.
  const elementRefs = new WeakMap();
  const refMap = {};
  // IDs only: preserve element_removed after pruning without retaining detached nodes.
  // Like refMap, this belongs to this document/injection, never a later generation.
  const removedRefs = new Set();
  let refCounter = 0;
  let lastActedElement = null;
  // The field type, fill, form_fill, select_option or check last wrote, or focus last
  // focused; a submit without a target submits its form.
  let lastFilledField = null;
  // Persist across reinjection; seed per document so navigation cannot reuse refs.
  let refGeneration = window.__kitsuneRefGeneration
    ? (BigInt(window.__kitsuneRefGeneration) + 1n).toString()
    : `${Date.now()}${crypto.getRandomValues(new Uint32Array(1))[0]}`;
  window.__kitsuneRefGeneration = refGeneration;
  // ── Action errors ──
  // `error` keeps its historical text; code/retryable/hint/state are the stable contract.
  // Content-script failures happen before any mutation, so repeating them is always safe.
  class ActionError extends Error {
    constructor(message, details) {
      super(message);
      this.details = details;
    }
  }

  function actionError(message, code, hint, state) {
    return { error: message, code, retryable: true, hint, ...(state ? { state } : {}) };
  }

  // Nothing was dispatched, but the same command cannot succeed: it names several targets.
  // state.candidates lists the first few, each with a new ref.
  const AMBIGUOUS_CANDIDATE_LIMIT = 5;
  function ambiguousTarget(message, hint, state) {
    return { ...actionError(message, 'ambiguous_target', hint, state), retryable: false };
  }

  function errorResult(e) {
    return e instanceof ActionError ? { error: e.message, ...e.details } : { error: e.message };
  }

  function notFound(message = 'Element not found') {
    return actionError(message, 'not_found',
      'No element matches; take a new browser_snapshot, or browser_wait_for the element if the page is still updating.');
  }

  // A scoped read whose selector matched nothing.
  function selectorNotFound(selector) {
    return { ...notFound(`Selector not found: ${selector}`), state: { selector } };
  }

  const STALE_REF_HINTS = {
    element_removed: 'The element was removed from the page; take a new browser_snapshot and use its refs.',
    not_in_snapshot: 'The latest snapshot did not issue this ref; take a new browser_snapshot and use its refs.',
    other_document: 'The ref belongs to another document: the tab navigated or a different tab is targeted. ' +
      'Pass tab_id and take a new browser_snapshot.',
  };

  function staleRef(ref, el) {
    const generation = /^s(\d+)e\d+$/.exec(ref)?.[1];
    const reason = generation && generation !== refGeneration ? 'other_document' :
      el || removedRefs.has(ref) ? 'element_removed' : 'not_in_snapshot';
    const { error, ...details } = actionError('stale ref, call browser_snapshot again', 'stale_ref',
      STALE_REF_HINTS[reason], { reason, ref });
    return new ActionError(error, details);
  }

  // Zero-size and transparent elements count as rendered (custom checkbox inputs).
  function isRendered(el) {
    return !isHidden(el) && (typeof el.getClientRects !== 'function' || el.getClientRects().length > 0);
  }

  // Synthetic events still reach an unrendered target, so pointer actions report it instead of refusing.
  function notRenderedWarning(el) {
    if (isRendered(el)) return {};
    return { warning: { code: 'hidden_or_covered', state: { reason: 'not_rendered' },
      hint: 'Events were dispatched to a hidden element a user could not click; ' +
        'prefer its label or visible control from a new browser_snapshot.' } };
  }

  // ── Actionability wait ──
  // Opt-in (msg.wait): poll before dispatch until every target resolves and is connected.
  // Pointer actions also need it rendered; value actions do not, so label-driven hidden
  // native checkboxes/selects keep working. Stale refs fail at once: they cannot come back.
  const POINTER_WAIT_ACTIONS = new Set(['click', 'doubleClick', 'hover', 'drag']);
  const VALUE_WAIT_ACTIONS = new Set(['type', 'fill', 'setEditableContent', 'selectOption', 'checkBox', 'keypress']);
  const ACTION_WAIT_TIMEOUT = 10000;

  function waitForActionable(msg) {
    const timeout = msg.timeout ?? ACTION_WAIT_TIMEOUT;
    if (!Number.isFinite(timeout) || timeout < 0) {
      return Promise.resolve({ error: 'timeout must be non-negative milliseconds' });
    }
    // Coordinate clicks and keypresses on the active element have no target to wait for.
    const targets = msg.action === 'drag' ? [msg.from, msg.to] : [msg.selector];
    const pointer = POINTER_WAIT_ACTIONS.has(msg.action);
    const start = Date.now();
    return new Promise((resolve, reject) => {
      function poll() {
        let pending = -1, el = null;
        try {
          pending = targets.findIndex(selector => {
            if (!selector) return false;
            el = resolveTarget(selector, null, { first: msg.first, pointer });
            return !el || !el.isConnected || (pointer && !isRendered(el));
          });
        } catch (e) {
          reject(e);
          return;
        }
        const elapsed = Date.now() - start;
        if (pending < 0) resolve(null);
        else if (elapsed >= timeout) resolve(actionWaitTimeout(msg.action, pending, el, timeout, elapsed));
        else setTimeout(poll, Math.min(100, timeout - elapsed));
      }
      poll();
    });
  }

  // The last observed state: the target never resolved, or resolved but never rendered.
  function actionWaitTimeout(action, index, el, timeoutMs, elapsed) {
    const state = { timeoutMs, elapsed };
    if (!el || !el.isConnected) {
      const message = action === 'drag' ? `${index ? 'to' : 'from'} element not found`
        : action === 'keypress' ? 'No target' : undefined;
      return { ...notFound(message), state };
    }
    return actionError(`Timed out after ${timeoutMs}ms waiting for the target to render`, 'not_actionable',
      'The target stayed hidden; act on its label or visible control from a new browser_snapshot, ' +
        'or repeat without wait to dispatch to the hidden element anyway.',
      { reason: 'not_rendered', ...state });
  }

  // A node in a retired frame document can still report isConnected=true.
  function refAttached(el) {
    try {
      if (!el.isConnected) return false;
      for (let doc = el.ownerDocument; doc && doc !== document;) {
        const frame = hostFrame(doc);
        if (!frame?.isConnected || frame.contentDocument !== doc) return false;
        doc = frame.ownerDocument;
      }
      return true;
    } catch {
      // Firefox revokes wrappers from a destroyed frame ("can't access dead object").
      return false;
    }
  }

  function pruneRefMap() {
    for (const [ref, el] of Object.entries(refMap)) {
      if (refAttached(el)) continue;
      delete refMap[ref];
      elementRefs.delete(el);
      removedRefs.add(ref);
    }
  }

  // ── User activity (docs/api-reference.md#user-activity) ──
  // The user shares the browser with the agent, so agent writes back off from a page the user is
  // using. Only trusted input counts: Kitsune's own DOM events are synthetic (isTrusted false),
  // and the trusted OS click of browser_trusted_click follows its screenPoint, which opens a
  // window for its pointer events. Typing counts while the field the user typed into keeps focus
  // in a focused document and is not empty. Values are never read out.
  const USER_INPUT_EVENTS = ['keydown', 'mousedown', 'pointerdown', 'touchstart', 'wheel', 'paste'];
  const USER_INPUT_WINDOW_MS = 3000;
  const AGENT_TRUSTED_CLICK_MS = 2000;
  let lastUserInput = null;   // { at, type }
  let userTypedField = null;  // the editable element (as the document sees it) the user last typed into
  let agentTrustedClickUntil = 0;

  function noteUserInput(event) {
    if (!event.isTrusted) return;
    const now = Date.now();
    if (/^(mouse|pointer|touch)/.test(event.type) && now < agentTrustedClickUntil) return;
    lastUserInput = { at: now, type: event.type };
    if ((event.type === 'keydown' || event.type === 'paste') && isTypingTarget(event.target)) {
      userTypedField = event.target;
    }
  }
  // A field in a shadow root shows as its host, a custom element.
  const isTypingTarget = el => ['INPUT', 'TEXTAREA'].includes(el?.tagName) || !!el?.isContentEditable ||
    !!el?.tagName?.includes('-');
  // Listeners on the window see input before page handlers can stop it (absent in vm test harnesses).
  for (const type of USER_INPUT_EVENTS) window.addEventListener?.(type, noteUserInput, { capture: true, passive: true });

  function hasUserContent(el) {
    if (typeof el.value === 'string') return el.value.length > 0;
    return el.isContentEditable ? (el.textContent || '').trim().length > 0 : true;
  }

  function userActivity() {
    const now = Date.now();
    if (lastUserInput && now - lastUserInput.at < USER_INPUT_WINDOW_MS) {
      return { active: true, state: { reason: 'input', input: lastUserInput.type, sinceMs: now - lastUserInput.at,
        windowMs: USER_INPUT_WINDOW_MS } };
    }
    const field = userTypedField;
    if (field?.isConnected && field === document.activeElement && document.hasFocus() && hasUserContent(field)) {
      return { active: true, state: { reason: 'typing', field: field.tagName.toLowerCase() } };
    }
    return { active: false };
  }

  // ── Message Handler ──
  browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    const handlers = {
      'ping':                  () => ({ ok: true, version: CONTENT_VERSION }),
      'getPageInfo':           () => getPageInfo(),
      'handleDialog':          () => handleDialog(msg.accept, msg.prompt_text),
      'waitForText':           () => waitForText(msg.text, msg.text_gone, msg.timeout, msg.diagnose === true),
      'getPageText':           () => getPageText(msg.selector),
      'getSnapshot':           () => getSnapshot(msg.maxChars, msg.focusLastAction, msg.ref || msg.selector, msg.maxDepth, msg.text ?? (msg.observation ? false : undefined),
                                     msg.observation, msg.delta ? 'delta' : 'full', !!msg.boxes, msg.collapse !== false),
      'getAccessibilityTree':  () => getAccessibilityTree(msg.depth ?? 6, msg.ref || msg.selector),
      'getFormFields':         () => getFormFields(),
      'click':                 () => clickElement(msg.selector, msg.coords, msg.first, msg.button, msg.modifiers),
      'type':                  () => typeText(msg.selector, msg.text, msg.clear, msg.first),
      'setEditableContent':    () => setEditableContent(msg.selector, msg.value, msg.format || 'text', msg.clear !== false,
                                     msg.first),
      'scroll':                () => scrollPage(msg.direction, msg.amount, msg.selector, msg.first),
      'hover':                 () => hoverElement(msg.selector, msg.coords, msg.first),
      'fill':                  () => fillFieldSafe(msg.selector, msg.value, msg.first),
      'find':                  () => findElements(msg.query),
      // @kitsune-dev-only:start
      'executeJS':             () => executeJS(msg.code, msg.selector),
      'waitForResult':         () => waitForResult(msg.code, msg.timeout, msg.pollInterval),
      // @kitsune-dev-only:end
      'highlight':             () => highlightElement(msg.selector),
      'clearHighlight':        () => clearHighlight(),
      'waitForElement':        () => waitForElement(msg.selector, msg.timeout, msg.pollInterval),
      // Fixed operations the bridge and background used to send as code strings.
      'keepAlive':             () => ({ ok: true }),
      'neuterUnload':          () => neuterUnload(),
      'screenPoint':           () => screenPoint(msg.selector, msg.x, msg.y, msg.click, msg.first),
      'userActivity':          () => userActivity(),
      'captureMeasure':        () => captureMeasure(msg.selector),
      'captureScrollIntoView': () => captureScrollIntoView(msg.selector, msg.block),
      'captureScroll':         () => captureScroll(msg.to, msg.by),
      'hideFixed':             () => hideFixed(msg.selector),
      'restoreFixed':          () => restoreFixed(),
      // ── 1.4.0 additions ──
      'query':                 () => queryElements(msg.selector, msg.fields, msg.limit),
      'getHTML':               () => getHTML(msg.selector),
      'getLinks':              () => getLinks(msg.internalOnly),
      'getImages':             () => getImages(),
      'getMeta':               () => getMeta(),
      'getStructuredData':     () => getStructuredData(),
      'markScreenshotTarget':  () => markScreenshotTarget(msg.selector),
      'unmarkScreenshotTarget': () => unmarkScreenshotTarget(msg.attribute),
      'getBounds':             () => getBounds(msg.selector),
      'getComputedStyle':      () => getComputedStyleFor(msg.selector, msg.properties),
      'getReadability':        () => getReadability(),
      'getMarkdown':           () => getMarkdown(msg.selector),
      'selectOption':          () => selectOption(msg.selector, msg.value, msg.byText, msg.first, msg.values),
      'checkBox':              () => checkBox(msg.selector, msg.checked, msg.first),
      'focusElement':          () => focusEl(msg.selector, msg.first),
      'blurElement':           () => blurEl(msg.selector, msg.first),
      'keypress':              () => keypress(msg.selector, msg.key, msg.modifiers, msg.text, msg.first),
      'doubleClick':           () => doubleClick(msg.selector, msg.first, msg.modifiers),
      'submitForm':            () => submitForm(msg.selector, msg.first),
      'describeTarget':        () => describeTarget(msg.selector, msg.coords, msg.key, msg.first, msg.fieldKeys, msg.draftFill),
      'formFill':              () => formFill(msg.fields, msg.first),
      'media':                 () => controlMedia(msg.selector, msg.op, msg.time, msg.first),
      'drag':                  () => dragElement(msg.from, msg.to, msg.first),
      'waitForUrl':            () => waitForUrl(msg.pattern, msg.timeout),
      'waitForTitle':          () => waitForTitle(msg.pattern, msg.timeout),
      'waitForNetworkIdle':    () => waitForNetworkIdle(msg.idleMs, msg.timeout),
      'captureNetwork':        () => captureNetwork(msg.op, msg.since),
      'watchConsole':          () => watchConsole(msg.enabled),
      'consoleLogs':           () => consoleLogs(msg.since, msg.level),
      'storageOp':             () => storageOp(msg.kind, msg.op, msg.key, msg.value),
      'getIframes':            () => getIframes(),
      'explainSelector':       () => explainSelector(msg.selector),
      'fullPageMetrics':       () => fullPageMetrics(),
      // ── 2.1.0 file upload ──
      'uploadChunk':           () => uploadChunk(msg.uploadId, msg.index, msg.data),
      'uploadCommit':          () => uploadCommit(msg.uploadId, msg.selector, msg.files, msg.mode, msg.first, msg.data),
      'uploadAbort':           () => uploadAbort(msg.uploadId),
    };

    // Only respond if we're the latest version
    if (window.__claudeBridgeVersion !== CONTENT_VERSION) return false;
    const handler = handlers[msg.action];
    if (!handler) {
      if (DEV_BUILD_ACTIONS.has(msg.action)) sendResponse({ error: DEV_BUILD_REQUIRED, devBuildRequired: true });
      return false;
    }

    try {
      if (['click', 'type', 'fill', 'selectOption', 'checkBox', 'keypress', 'submitForm'].includes(msg.action)) {
        lastActedElement = null;
      }
      // Found/not-found on any selector-taking tool is a value oracle too, so
      // every caller-supplied selector is checked here (find handles its own).
      const selectors = [msg.selector, msg.from, msg.to, ...Object.keys(msg.fields || {})];
      if (selectors.some(s => typeof s === 'string' && valueSelectorBlocked(s))) {
        sendResponse({ error: VALUE_SELECTOR_ERROR });
        return false;
      }
      // A delta post-action snapshot is diffed against the page right before the action runs.
      const run = msg.deltaBaseline ? () => { takeActionBaseline(!!msg.snapshotText); return handler(); } : handler;
      if (msg.wait && (POINTER_WAIT_ACTIONS.has(msg.action) || VALUE_WAIT_ACTIONS.has(msg.action))) {
        waitForActionable(msg).then(failure => failure || run())
          .then(r => sendResponse(r)).catch(e => sendResponse(errorResult(e)));
        return true;
      }
      const result = REDACTION_CACHED_READS.has(msg.action) ? withRedactionCache(handler) : run();
      if (result && typeof result.then === 'function') {
        // Async handler — keep message channel open
        result.then(r => sendResponse(r)).catch(e => sendResponse(errorResult(e)));
        return true;
      }
      sendResponse(result);
    } catch (e) {
      sendResponse(errorResult(e));
    }
    return false;
  });

  // ── File upload (2.1.0) ──
  // The bridge reads the file from disk and streams it here as independently
  // decodable base64 chunks; commit rebuilds real File objects and attaches
  // them to an <input type=file> (or drops them on a drop zone).
  const __zlUploads = {};
  function uploadChunk(id, index, data) {
    (__zlUploads[id] = __zlUploads[id] || [])[index] = data;
    return { ok: true, index };
  }
  function uploadAbort(id) { delete __zlUploads[id]; return { ok: true }; }
  function uploadCommit(id, selector, files, mode, first, data) {
    // A drop of data alone (browser_drop) sends no chunks.
    const parts = __zlUploads[id] || (files.length ? null : []);
    delete __zlUploads[id];
    if (!parts) return { error: 'upload ' + id + ' not found (chunks never arrived)' };
    if (!first) {
      if (selector) assertOneTarget(selector, false);
      else if (mode !== 'drop') assertOneTarget('input[type=file]', false,
        Array.from(document.querySelectorAll('input[type=file]')));
    }
    return __zlAttachFiles(parts, selector, files, mode, data);
  }
  const __zlAttachFiles = function(parts, selector, files, mode, data) {
    var toBytes = function (b64) { var bin = atob(b64), a = new Uint8Array(bin.length); for (var i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i); return a; };
    // 2.1.0: build the File + DataTransfer with the PAGE's own constructors (wrappedJSObject + cloneInto).
    // Content-script Files were silently dropped by some uploaders (YouTube thumbnails, TikTok/Instagram covers).
    var W = (typeof cloneInto === 'function' && window.wrappedJSObject) ? window.wrappedJSObject : null;
    var objs = files.map(function (f) {
      var bytes = parts.slice(f.start, f.end).map(toBytes), opts = { type: f.type || 'application/octet-stream', lastModified: Date.now() };
      return W ? new W.File(cloneInto(bytes, W), f.name, cloneInto(opts, W)) : new File(bytes, f.name, opts);
    });
    var target = null;
    if (selector) { target = resolveElement(selector); if (!target) return { error: 'No element matches: ' + selector }; }
    var input = null;
    if (mode !== 'drop') {
      if (target) input = target.matches('input[type=file]') ? target : target.querySelector('input[type=file]');
      if (!input && (!target || mode === 'input')) input = document.querySelector('input[type=file]');
      if (!input && mode === 'input') return { error: 'No input[type=file] found' };
    }
    var dt = W ? new W.DataTransfer() : new DataTransfer();
    objs.forEach(function (f) { dt.items.add(f); });
    Object.entries(data || {}).forEach(function ([type, value]) { dt.setData(type, String(value)); });
    var info = objs.map(function (f) { return { name: f.name, size: f.size, type: f.type }; });
    if (input) {
      if (!input.multiple && objs.length > 1) return { error: 'This input takes one file; got ' + objs.length };
      if (W) input.wrappedJSObject.files = dt.files; else input.files = dt.files;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return { ok: true, mode: 'input', files: info, accept: input.accept || null, pageFiles: !!W };
    }
    var zone = target || document.body;
    ['dragenter', 'dragover', 'drop'].forEach(function (t) {
      if (W) {
        var init = new W.Object(); init.bubbles = true; init.cancelable = true; init.dataTransfer = dt;
        zone.wrappedJSObject.dispatchEvent(new W.DragEvent(t, init));
      } else {
        zone.dispatchEvent(new DragEvent(t, { bubbles: true, cancelable: true, dataTransfer: dt }));
      }
    });
    return { ok: true, mode: 'drop', files: info, pageFiles: !!W };
  };

  // ── Page Info ──
  function getPageInfo() {
    return {
      ...dialogInfo(),
      url: location.href,
      title: document.title,
      readyState: document.readyState,
      lang: document.documentElement.lang || 'unknown',
      width: document.documentElement.scrollWidth,
      height: document.documentElement.scrollHeight,
      viewportW: window.innerWidth,
      viewportH: window.innerHeight,
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      forms: document.forms.length,
      links: document.links.length,
      images: document.images.length,
    };
  }

  // ── Page Text ──
  const PAGE_TEXT_LIMIT = 20000;

  function truncateText(raw) {
    const truncated = raw.length > PAGE_TEXT_LIMIT;
    return {
      text: truncated ? raw.slice(0, PAGE_TEXT_LIMIT) : raw,
      ...(truncated && { truncated: true, fullLength: raw.length }),
    };
  }

  function getPageText(selector) {
    if (selector) {
      const el = document.querySelector(selector);
      if (!el) return { error: 'Selector not found: ' + selector };
      return truncateText(el.innerText);
    }
    const selectors = ['article', '[role="main"]', 'main', '.content', '.post-content', '.article-body', '.entry-content'];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.innerText.length > 100) {
        return truncateText(el.innerText);
      }
    }
    return truncateText(document.body.innerText);
  }

  // ── Accessibility Tree ──
  const DOM_TREE_LIMIT = 20000;
  const DOM_TRUNCATED_HINT = 'Tree cut at 20,000 chars; scope it with selector, ref or max_depth, ' +
    'or use browser_snapshot.';

  function getAccessibilityTree(maxDepth, selector) {
    // Resolve the scope before pruning detached refs.
    const root = selector ? resolveElement(selector) : document.body;
    if (!root) return selectorNotFound(selector);
    pruneRefMap();
    let refs = 0;
    let size = 0;
    let truncated = false;

    function buildNode(el, depth) {
      if (truncated || depth > maxDepth || !el || el.nodeType !== 1) return null;
      if (isHidden(el)) return null;

      const tag = el.tagName.toLowerCase();
      // Skip noise
      if (['script', 'style', 'noscript', 'svg', 'path', 'br', 'hr'].includes(tag)) return null;

      const rect = viewportRect(el);
      const isInteractive = isInteractiveElement(el);
      const text = directText(el);
      const frame = FRAME_TAGS.has(el.tagName);

      // Skip non-interactive elements with no text and small size
      if (!isInteractive && !text && rect.width < 5 && rect.height < 5) return null;

      const ref = elementRefs.get(el) || `s${refGeneration}e${refCounter}`;
      const node = { ref, tag };
      
      // Only include useful properties
      if (el.id) node.id = el.id;
      if (isInteractive) {
        node.interactive = true;
        const role = el.getAttribute('role') || inferRole(el);
        if (role) node.role = role;
      }
      if (text) node.text = text;
      if (el.name) node.name = el.name;
      if (el.type && el.tagName === 'INPUT') node.type = el.type;
      if (isSensitiveField(el)) Object.assign(node, redactedValue(el));
      else if (el.value && isInteractive) node.value = String(el.value).slice(0, 80);
      if (el.placeholder) node.placeholder = el.placeholder;
      if (el.href) node.href = el.href.slice(0, 200);
      if (el.ariaLabel) node.label = el.ariaLabel;
      if (el.disabled) node.disabled = true;
      if (el.checked !== undefined) node.checked = el.checked;
      if (frame) {
        if (el.title) node.title = el.title;
        const { reason } = frameAccess(el);
        if (reason) Object.assign(node, { content: reason, src: frameSource(el) });
      }

      // Bounds only for interactive or visible elements
      if (isInteractive || text || frame) {
        node.bounds = [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)];
      }

      // Upper bound of the node's share of the JSON: itself, a comma and its children array.
      // The first node past the cap ends the read, so the tree is a document-order prefix.
      const cost = JSON.stringify(node).length + ',"children":[],'.length;
      if (size + cost > DOM_TREE_LIMIT) { truncated = true; return null; }
      size += cost;
      issueRef(el);
      refs++;

      // Recurse children, and the body of a same-origin frame
      const children = [];
      for (const child of childrenAcrossFrames(el)) {
        const childNode = buildNode(child, depth + 1);
        if (childNode) children.push(childNode);
        if (truncated) break;
      }
      if (children.length) node.children = children;

      return node;
    }

    const tree = buildNode(root, 0);
    return { tree, refs, ...(truncated && { truncated: true, hint: DOM_TRUNCATED_HINT }) };
  }

  // Explicit text=true is the bounded text mode. Omitted text is readable but has no
  // new document-prefix cap; text=false remains the compact full-toolset observation.
  const TEXT_SNAPSHOT_LIMIT = 12000;
  const TEXT_LINE_LIMIT = 1000;
  const TEXT_TRUNCATED_LINE = '- … truncated; scope with selector, ref or max_depth';
  // A loaded tab is not loaded content (#259): an empty readable snapshot may be a page that
  // still renders asynchronously or one that is empty, and readyState alone cannot tell.
  const EMPTY_SNAPSHOT_HINT = 'The page shows no content yet. If it should, wait for text you ' +
    'expect with browser_wait_for (page text, not a field\'s placeholder or label), then take a ' +
    'new snapshot; a loaded page can also be empty.';
  // A bounded snapshot never ends inside a block (an element's lines below the root, such as
  // a product card or a short section) of at most this many characters or a quarter of its
  // budget (#262): the block is kept or left out whole.
  const SMALL_BLOCK_LIMIT = 2000;
  // Inline content joins its block's text line; other elements start their own.
  const INLINE_TAGS = new Set(['A', 'ABBR', 'B', 'BDI', 'BDO', 'CITE', 'CODE', 'DATA', 'DFN', 'EM',
    'I', 'KBD', 'LABEL', 'MARK', 'Q', 'S', 'SAMP', 'SMALL', 'SPAN', 'STRONG', 'SUB', 'SUP',
    'TIME', 'U', 'VAR']);
  const FIELD_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'OPTION']);
  // A run of similar siblings shows its first COLLAPSED_ITEM_LIMIT items. A snapshot scoped to
  // their container shows up to SCOPED_ITEM_LIMIT of its own; browser_find searches the rest.
  const COLLAPSED_ITEM_LIMIT = 5;
  const SCOPED_ITEM_LIMIT = 100;

  // What a delta snapshot is diffed against: the page's snapshot lines (without refs) per
  // element, url and title, recorded right before an action (takeActionBaseline).
  let actionBaseline = null;

  function takeActionBaseline(withText) {
    actionBaseline = null;
    try {
      getSnapshot(undefined, false, null, null, withText, undefined, 'baseline');
    } catch {}
  }

  // Compact accessibility-style view (DOM semantics, not a platform AX tree).
  // observation: the tool whose call took this snapshot (an action's post-action snapshot).
  // mode: 'full'; 'baseline' records actionBaseline and returns no snapshot; 'delta' lists only
  // the target and the lines that changed since the baseline, adding refs to the current
  // document's refs. A delta without a baseline (another document) is full.
  // collapse=false lists every item of repeated sibling runs (core browser_find searches them).
  // Viewport-relative CSS-pixel bounding box as x,y,width,height.
  function boxOf(el) {
    const rect = el.getBoundingClientRect();
    return [rect.x, rect.y, rect.width, rect.height].map(Math.round).join(',');
  }

  function getSnapshot(maxChars, focusLastAction, selector, maxDepth, textMode,
      observation = 'browser_snapshot', mode = 'full', boxes = false, collapse = true) {
    const withText = textMode !== false;
    if (maxChars != null && (!Number.isFinite(maxChars) || maxChars < 0)) {
      return { error: 'maxChars must be a finite non-negative number' };
    }
    if (maxDepth != null && (!Number.isInteger(maxDepth) || maxDepth < 0)) {
      return { error: 'max_depth must be a non-negative integer' };
    }
    // Observations share refs; scoping never invalidates an earlier observation.
    const root = selector ? resolveElement(selector) : document.body;
    if (!root) return selectorNotFound(selector);
    const baseline = mode === 'delta' ? actionBaseline : null;
    const full = mode === 'full' || (mode === 'delta' && !baseline);
    let target = null;
    if (mode !== 'baseline') {
      actionBaseline = null;
      // Remember the actual node even when this snapshot omits it.
      target = focusLastAction && lastActedElement?.isConnected ? lastActedElement : null;
      lastActedElement = null;
    }
    pruneRefMap();
    // Lines whose ref is assigned once a delta is selected are measured with this stand-in.
    const pendingRef = `s${refGeneration}e${refCounter}`;
    const entries = [];
    let limit = Number.isFinite(maxChars) ? Math.max(0, maxChars) : Infinity;
    if (textMode === true) limit = Math.min(limit, TEXT_SNAPSHOT_LIMIT);
    // Reserve marker space for bounded readable observations. The background reapplies
    // the public budget after URL redaction and alias serialization, which can expand it.
    if (withText && Number.isFinite(limit)) limit = Math.max(0, limit - TEXT_TRUNCATED_LINE.length - 1);
    let size = 0;
    let truncated = false;
    const landmarks = { MAIN: 'main', NAV: 'navigation', HEADER: 'banner',
      FOOTER: 'contentinfo', ASIDE: 'complementary', FORM: 'form' };
    const semanticRoles = new Set(['heading', 'main', 'navigation', 'banner',
      'contentinfo', 'complementary', 'region', 'form', 'search']);
    const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
    const clean = value => normalize(value).slice(0, 160);
    const nativeTags = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'DETAILS', 'SUMMARY']);
    const metadata = new WeakMap();
    const signatures = new Map();
    const targetAncestors = new Set();
    for (let el = target; el; el = parentAcrossFrames(el)) targetAncestors.add(el);
    function ignored(el) {
      return !el || el.nodeType !== 1 || isHidden(el) ||
        el.getAttribute('aria-hidden') === 'true' ||
        ['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG'].includes(el.tagName);
    }
    const hasOwnText = el => flatChildNodes(el).some(node => node.nodeType === 3 && node.textContent.trim());
    // An <img> is content unless role=presentation/none marks it decorative; one without a
    // name (alt="", no alt) is left out too. Its src is never listed.
    const isImage = el => el.tagName === 'IMG' && !['presentation', 'none'].includes(el.getAttribute('role'));
    // [all, plain] text of el's inline content; plain omits controls and labels, which
    // already appear as control names. Form fields and sensitive elements never contribute.
    function inlineText(el) {
      let all = '', plain = '';
      const sensitive = holdsSensitiveText(el);
      for (const node of flatChildNodes(el)) {
        if (node.nodeType === 3) {
          all += sensitive ? ' ' : node.textContent;
          plain += sensitive ? ' ' : node.textContent;
        } else if (ignored(node) || !INLINE_TAGS.has(node.tagName) || FIELD_TAGS.has(node.tagName) ||
            isSensitiveField(node)) {
          all += ' ';
          plain += ' ';
        } else {
          const [childAll, childPlain] = inlineText(node);
          all += childAll;
          plain += isInteractiveElement(node) || node.tagName === 'LABEL' ? ' ' : childPlain;
        }
      }
      return [all, plain];
    }
    // Bottom-up metadata avoids repeatedly scanning large clickable ancestor trees.
    function measure(el, domDepth = 0) {
      if (ignored(el)) return null;
      // A media element's children are sources, tracks and fallback content, not page structure.
      const media = MEDIA_TAGS.has(el.tagName);
      const children = media ? [] :
        childrenAcrossFrames(el).map(child => measure(child, domDepth + 1)).filter(Boolean);
      const shape = JSON.stringify([el.tagName, el.getAttribute('role'), el.type || '',
        children.map(child => child.signature)]);
      if (!signatures.has(shape)) signatures.set(shape, signatures.size);
      const data = {
        signature: signatures.get(shape),
        nodes: 1 + children.reduce((sum, child) => sum + child.nodes, 0),
        interactive: children.reduce((sum, child) => sum + child.interactive, 0),
        native: children.reduce((sum, child) => sum + child.native, 0),
      };
      // One native descendant or two custom controls makes a generic click handler
      // a delegation wrapper, not a separate button. Explicit roles remain intact.
      data.wrapper = !nativeTags.has(el.tagName) && !el.getAttribute('role') &&
        !el.isContentEditable && el.contentEditable !== 'true' &&
        (data.native > 0 || data.interactive >= 2);
      const interactive = isSnapshotControl(el);
      const role = el.getAttribute('role') || landmarks[el.tagName] ||
        (/^H[1-6]$/.test(el.tagName) ? 'heading' : inferRole(el));
      data.image = !interactive && isImage(el) && !!nameOf(el, false);
      // Rendered frames are listed even when their content is unavailable (cross-origin).
      data.useful = (maxDepth == null || domDepth <= maxDepth) &&
        (children.some(child => child.useful) || (interactive && !data.wrapper) || media || data.image ||
          semanticRoles.has(role) || el === target || (withText && hasOwnText(el)) ||
          (FRAME_TAGS.has(el.tagName) && hasArea(el)));
      data.interactive += interactive ? 1 : 0;
      data.native += nativeTags.has(el.tagName) ? 1 : 0;
      metadata.set(el, data);
      return data;
    }
    measure(root);
    function nameOf(el, interactive) {
      const labelledBy = el.getAttribute('aria-labelledby');
      if (labelledBy) {
        const label = labelledBy.split(/\s+/).map(id => {
          const labelEl = elementByIdNear(el, id);
          return labelEl ? redactedText(labelEl) : '';
        }).join(' ');
        if (clean(label)) return clean(label);
      }
      if (MEDIA_TAGS.has(el.tagName)) return clean(el.getAttribute('aria-label') || el.title);
      if (!interactive && isImage(el)) {
        return clean(alternativeName(el));
      }
      const explicitRole = el.getAttribute('role');
      const isLandmark = landmarks[el.tagName] ||
        (semanticRoles.has(explicitRole) && explicitRole !== 'heading');
      const isField = ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
      if (interactive && !nativeTags.has(el.tagName)) {
        // A clickable image keeps the name role= selectors match it by.
        const name = clean(ALTERNATIVE_NAMED_TAGS.has(el.tagName.toUpperCase()) ? alternativeName(el)
          : el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || directText(el));
        if (name) return name;
        const text = metadata.get(el).nodes <= 5 ? normalize(redactedText(el)) : '';
        return text.length <= 80 ? text : '';
      }
      let fallback;
      if (isField) fallback = el.placeholder || el.title;
      else if (isLandmark) fallback = el.title;
      else fallback = redactedText(el) || el.title;
      return clean(el.getAttribute('aria-label') || findLabel(el) ||
        (el.type === 'image' ? el.alt : '') ||
        (['button', 'submit', 'reset'].includes(el.type) && !isSensitiveField(el) ? el.value : '') || fallback);
    }
    function append(entry) {
      size += entry.line.length + (entries.length ? 1 : 0);
      entries.push(entry);
    }
    // named: an ancestor control or heading already shows this text as its name.
    function visit(el, depth, parent = -1, domDepth = 0, named = false) {
      if (!metadata.has(el) || (maxDepth != null && domDepth > maxDepth)) return;
      const frame = FRAME_TAGS.has(el.tagName);
      const image = metadata.get(el).image;
      let role = el.getAttribute('role') || landmarks[el.tagName] ||
        (/^H[1-6]$/.test(el.tagName) ? 'heading' : inferRole(el)) || (frame ? 'iframe' : '') || (image ? 'img' : '');
      const media = MEDIA_TAGS.has(el.tagName);
      const interactive = (isSnapshotControl(el) && !metadata.get(el).wrapper) || media;
      const include = interactive || semanticRoles.has(role) || el === target || frame || image;
      let ref = null;
      if (include) {
        if (!role) {
          if (isEditingHost(el)) role = 'textbox';
          else if (el === target) role = el.tagName.toLowerCase();
          else role = 'button';
        }
        ref = full ? elementRefs.get(el) || `s${refGeneration}e${refCounter++}` : null;
        const states = [];
        if (role === 'heading') states.push(`level=${el.getAttribute('aria-level') || el.tagName.slice(1)}`);
        if ('value' in el && !isSensitiveField(el)) states.push(`value=${JSON.stringify(clean(el.value))}`);
        if (el.type === 'checkbox' || el.type === 'radio' || el.hasAttribute('aria-checked')) {
          states.push(`checked=${el.getAttribute('aria-checked') || !!el.checked}`);
        }
        if (el.disabled || el.getAttribute('aria-disabled') === 'true') states.push('disabled');
        for (const state of ['expanded', 'selected', 'pressed']) {
          if (el.hasAttribute(`aria-${state}`)) states.push(`${state}=${el.getAttribute(`aria-${state}`)}`);
        }
        const frameReason = frame && frameAccess(el).reason;
        if (frameReason) {
          const src = clean(frameSource(el));
          states.push(`content=${frameReason}`, ...(src ? [`src=${JSON.stringify(src)}`] : []));
        }
        if (media) {
          const { src, duration, currentTime, paused, muted, controls } = mediaState(el);
          states.push(`src=${JSON.stringify(src)}`, `duration=${duration}`, `currentTime=${currentTime}`,
            `paused=${paused}`, `muted=${muted}`, `controls=${controls}`);
        }
        const label = `${role} ${JSON.stringify(nameOf(el, interactive || el === target))}`;
        const head = `${'  '.repeat(Math.min(depth, 12))}- ${label} [ref=`;
        const stateTail = `]${states.length ? ' [' + states.join(' ') + ']' : ''}`;
        // Boxes stay out of the signature, so repeated siblings still group.
        const tail = stateTail + (boxes ? ` [box=${boxOf(el)}]` : '');
        append({ el, ref, line: head + (ref ?? pendingRef) + tail, parent, interactive,
          head, tail, label, signature: label + stateTail });
        parent = entries.length - 1;
        // Only web links are exposed: other schemes can hold executable code or local data.
        if (role === 'link' && el.href) {
          let url;
          try { url = new URL(el.href); } catch {}
          if (url && ['http:', 'https:'].includes(url.protocol)) {
            const signature = `/url: ${JSON.stringify(url.href)}`;
            append({ line: `${'  '.repeat(Math.min(depth + 1, 12))}- ${signature}`,
              parent, interactive: false, source: el, signature });
          }
        }
      }
      const childDepth = depth + (include ? 1 : 0);
      named ||= interactive || role === 'heading';
      if (withText && !named && (el === root || !INLINE_TAGS.has(el.tagName)) &&
          !FIELD_TAGS.has(el.tagName) && !isSensitiveField(el)) {
        const [all, plain] = inlineText(el);
        const text = normalize(all);
        if (normalize(plain)) {
          const clipped = text.length > TEXT_LINE_LIMIT ? text.slice(0, TEXT_LINE_LIMIT) + '…' : text;
          const signature = `text ${JSON.stringify(clipped)}`;
          append({ line: `${'  '.repeat(Math.min(childDepth, 12))}- ${signature}`,
            parent, interactive: false, source: el, signature });
        }
      }
      if (maxDepth != null && domDepth >= maxDepth) return;
      const children = childrenAcrossFrames(el).filter(child => metadata.get(child)?.useful);
      // Only composite children / list items collapse: a toolbar of leaf controls
      // should remain fully discoverable. Text, IDs and classes are not structure.
      // In text mode, siblings with only inline content are prose, not cards.
      const repeatable = child => (flatChildren(child).length > 0 || child.tagName === 'LI' ||
        child.getAttribute('role') === 'listitem') &&
        !(withText && flatChildren(child).every(c => INLINE_TAGS.has(c.tagName)));
      const scopedRoot = selector && el === root;
      const shown = scopedRoot ? SCOPED_ITEM_LIMIT : COLLAPSED_ITEM_LIMIT;
      for (let start = 0; start < children.length;) {
        let end = start + 1;
        // Entries from here on belong to this run; a delta keeps its marker with them.
        const run = entries.length;
        if (collapse && repeatable(children[start])) {
          while (end < children.length &&
              metadata.get(children[end]).signature === metadata.get(children[start]).signature) end++;
        }
        let omitted = 0;
        function flushOmitted() {
          if (!omitted) return;
          const signature = `… ${omitted} more similar items`;
          const indent = '  '.repeat(Math.min(childDepth, 12));
          if (scopedRoot) {
            append({ line: `${indent}- ${signature}; use browser_find to locate a specific one`,
              parent, interactive: false, source: el, run, signature });
            omitted = 0;
            return;
          }
          // The marker names the container by ref: a snapshot scoped to it lists its items.
          if (full) ref ??= elementRefs.get(el) || `s${refGeneration}e${refCounter++}`;
          const head = `${indent}- ${signature}; snapshot `;
          const tail = ' to list all';
          append({ line: head + (ref ?? pendingRef) + tail, parent, interactive: false, source: el,
            ref, head, tail, run, signature });
          omitted = 0;
        }
        for (let i = start; i < end; i++) {
          if (i - start < shown || targetAncestors.has(children[i])) {
            flushOmitted();
            visit(children[i], childDepth, parent, domDepth + 1, named);
          } else omitted++;
        }
        flushOmitted();
        start = end;
      }
    }
    visit(root, 0);

    if (mode === 'baseline') {
      const signatures = new Map();
      for (const entry of entries) {
        const el = entry.el || entry.source;
        if (!el) continue;
        if (!signatures.has(el)) signatures.set(el, new Set());
        signatures.get(el).add(entry.signature);
      }
      actionBaseline = { signatures, url: window.location?.href, title: document.title };
      return { ok: true };
    }

    const targetIndex = entries.findIndex(entry => entry.el === target);
    // Where a bounded snapshot without a target may end: before entry i unless a small block
    // (SMALL_BLOCK_LIMIT) has lines on both sides of it.
    let cuts = null;
    if (!baseline && targetIndex < 0 && Number.isFinite(limit)) {
      const blockLimit = Math.min(SMALL_BLOCK_LIMIT, limit / 4);
      const blocks = new Map();
      entries.forEach((entry, i) => {
        const cost = entry.line.length + 1;
        for (let el = entry.el || entry.source; el && el !== root; el = parentAcrossFrames(el)) {
          const block = blocks.get(el);
          if (block) Object.assign(block, { last: i, cost: block.cost + cost });
          else blocks.set(el, { first: i, last: i, cost });
        }
      });
      const inside = new Array(entries.length + 1).fill(false);
      for (const { first, last, cost } of blocks.values()) {
        if (cost <= blockLimit) for (let i = first + 1; i <= last; i++) inside[i] = true;
      }
      cuts = inside.flatMap((split, i) => split ? [] : [i]);
    }
    let selected = entries;
    const kept = new Set();
    let used = 0;
    function add(index, withAncestors = true) {
      const pending = [];
      for (let i = index; i >= 0 && !kept.has(i); i = withAncestors ? entries[i].parent : -1) {
        pending.push(i);
      }
      const cost = pending.reduce((sum, i) => sum + entries[i].line.length + 1, 0);
      if (used + cost > limit + 1) return;
      for (const i of pending) kept.add(i);
      used += cost;
    }
    function keepTarget() {
      // The target's complete line wins even if an unusually small cap cannot fit it.
      kept.add(targetIndex);
      used = entries[targetIndex].line.length + 1;
      const ancestors = [];
      for (let i = entries[targetIndex].parent; i >= 0; i = entries[i].parent) ancestors.push(i);
      for (const i of ancestors.reverse()) add(i, false);
    }
    if (baseline) {
      // The target, then lines the baseline lacks (new or newly shown elements, changed names,
      // states or text) in document order, each under its ancestors. Removals are not listed.
      if (targetIndex >= 0) keepTarget();
      const changed = [];
      entries.forEach((entry, i) => {
        const el = entry.el || entry.source;
        if (el && !baseline.signatures.get(el)?.has(entry.signature)) changed.push(i);
      });
      for (const i of changed) add(i);
      // A collapse marker follows the listed items of its run.
      entries.forEach((entry, i) => {
        if (entry.run === undefined || kept.has(i)) return;
        for (let j = entry.run; j < i; j++) if (kept.has(j)) return add(i);
      });
      selected = entries.filter((_, i) => kept.has(i));
      truncated = changed.some(i => !kept.has(i));
      // Refs add to the current generation; an element that already has one keeps it.
      // A collapse marker's ref names its container (source).
      for (const entry of selected) {
        if (!entry.head) continue;
        entry.ref = issueRef(entry.el || entry.source);
        entry.line = entry.head + entry.ref + entry.tail;
      }
    } else if (size > limit) {
      if (targetIndex >= 0) {
        keepTarget();
        // Prefer immediate siblings and nearby controls on either side of the target.
        for (const predicate of [
          entry => entry.parent === entries[targetIndex].parent,
          entry => entry.interactive,
        ]) {
          for (const direction of [-1, 1]) {
            let count = 0;
            for (let i = targetIndex + direction; i >= 0 && i < entries.length; i += direction) {
              if (predicate(entries[i])) {
                add(i);
                if (++count === 2) break;
              }
            }
          }
        }
        for (let i = 0; i < entries.length; i++) add(i);
      } else {
        // With no surviving target this is the longest document-order prefix ending at a cut.
        for (let i = 0, c = 0; c < cuts.length; c++) {
          let cost = used;
          for (let j = i; j < cuts[c]; j++) cost += entries[j].line.length + 1;
          if (cost > limit + 1) break;
          for (; i < cuts[c]; i++) kept.add(i);
          used = cost;
        }
      }
      selected = entries.filter((_, i) => kept.has(i));
      truncated = selected.length < entries.length;
    }
    for (const entry of selected) {
      if (entry.ref) {
        refMap[entry.ref] = entry.el || entry.source;
        elementRefs.set(entry.el || entry.source, entry.ref);
      }
    }
    let snapshot = selected.map(entry => entry.line).join('\n');
    if (withText && truncated) snapshot += (snapshot ? '\n' : '') + TEXT_TRUNCATED_LINE;
    // An explicit readable snapshot reports readiness; a compact one omits prose, so its
    // emptiness says nothing about content.
    const explicit = withText && !baseline && observation === 'browser_snapshot';
    return {
      snapshot,
      ...(truncated ? { truncated: true } : {}),
      ...(targetIndex >= 0 ? { target_ref: entries[targetIndex].ref } : {}),
      // For boundPublicSnapshot, which can cut again after aliases and URL redaction.
      ...(cuts ? { cuts: cuts.filter(cut => cut <= selected.length) } : {}),
      ...(withText && !baseline ? { url: window.location?.href ?? globalThis.location?.href, title: document.title } : {}),
      ...(explicit ? { readyState: document.readyState } : {}),
      ...(explicit && !snapshot && !truncated ? { empty: true, hint: EMPTY_SNAPSHOT_HINT } : {}),
      ...(baseline ? {
        delta: true,
        ...(window.location?.href !== baseline.url ? { url: window.location?.href } : {}),
        ...(document.title !== baseline.title ? { title: document.title } : {}),
      } : {}),
    };
  }

  // ── Find Elements ──
  const FIND_LIMIT = 20;
  const FIND_NO_MATCH_HINT = 'No element matches this query; try a shorter phrase from the element\'s ' +
    'visible label, or browser_snapshot(text=True).';
  // Filler words in descriptive queries ("the button in the filters") that match almost anything.
  const FIND_STOP_WORDS = new Set(['a', 'an', 'the', 'in', 'on', 'of', 'to', 'for', 'or', 'and',
    'with', 'at', 'by', 'from', 'is', 'that', 'this', 'its']);
  // Words a query uses for a control's kind, matched against its role.
  const FIND_ROLE_WORDS = {
    button: 'button', link: 'link', checkbox: 'checkbox', radio: 'radio', tab: 'tab',
    combobox: 'combobox dropdown select', listbox: 'listbox dropdown select list',
    textbox: 'textbox input field box', searchbox: 'searchbox search input field box bar',
    menuitem: 'menuitem menu item', option: 'option', slider: 'slider', switch: 'switch toggle',
    heading: 'heading title', img: 'image img picture',
  };
  // Longer text belongs to a container; it can match the whole query but not single terms.
  const FIND_TEXT_TERM_LIMIT = 200;

  // Lowercase words, with camelCase and kebab/snake-case ids split apart.
  function findWords(value) {
    return String(value || '').replace(/(\p{Ll})(\p{Lu})/gu, '$1 $2').toLowerCase()
      .match(/[\p{L}\p{N}]+/gu) || [];
  }

  // Terms are the query's words without filler; phrases are quoted parts and the whole query.
  function parseFindQuery(query) {
    const words = findWords(query);
    const content = words.filter(word => !FIND_STOP_WORDS.has(word));
    const terms = [...new Set(content.length ? content : words)];
    const phrases = new Set(words.length > 1 ? [words.join(' ')] : []);
    for (const [, double, single] of query.matchAll(/["\u201c\u201d]([^"\u201c\u201d]+)["\u201c\u201d]|(?:^|\s)['\u2018]([^'\u2018\u2019]+)['\u2019](?=\s|$|[.,;:!?])/g)) {
      const phrase = findWords(double || single).join(' ');
      if (phrase.includes(' ')) phrases.add(phrase);
    }
    return { terms, phrases: [...phrases] };
  }

  // A field word matches a term it starts with, or a slightly shorter form of it
  // ("reviews" finds "review", "버튼을" finds "버튼").
  function wordMatchesTerm(word, term) {
    return word.startsWith(term) ||
      (word.length >= 2 && term.startsWith(word) && term.length - word.length <= 3);
  }

  function findElements(query) {
    pruneRefMap();
    const results = [];

    // Try as CSS selector
    try {
      if (valueSelectorBlocked(query)) throw new Error(VALUE_SELECTOR_ERROR);
      const matches = deepQuerySelectorAll(query);
      if (matches.length > 0 && matches.length < 50) {
        matches.forEach(el => {
          if (!isHidden(el)) results.push(makeResult(el));
        });
        if (results.length) return { results: results.slice(0, FIND_LIMIT) };
      }
    } catch {}

    // Text/attribute search
    const parsed = parseFindQuery(query);
    if (!parsed.terms.length) return { results: [], hint: FIND_NO_MATCH_HINT };
    const scored = [];
    const candidates = deepQuerySelectorAll('a, button, input, select, textarea, [role], [onclick], label, h1, h2, h3, h4, h5, h6, p, span, div, li, td, th, img');
    for (const el of candidates) {
      if (isHidden(el)) continue;
      const match = scoreMatch(el, parsed);
      if (match) scored.push({ el, ...match });
    }
    if (!scored.length) return { results: [], hint: FIND_NO_MATCH_HINT };

    // Drop weak partial matches: keep elements matching at least half as many terms as the best.
    const bestMatched = Math.max(...scored.map(entry => entry.matched));
    const kept = scored.filter(entry => entry.matched * 2 >= bestMatched);
    // Between equal scores, the element with less text is the more specific one.
    kept.sort((a, b) => b.score - a.score || a.textLength - b.textLength);
    return {
      results: kept.slice(0, FIND_LIMIT).map(entry => ({ ...makeResult(entry.el), score: entry.score })),
    };
  }

  function makeResult(el) {
    const ref = issueRef(el);
    const rect = viewportRect(el);
    return {
      ref,
      tag: el.tagName.toLowerCase(),
      text: redactedText(el).trim().slice(0, 120),
      selector: buildSelector(el),
      bounds: [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)],
      interactive: isInteractiveElement(el)
    };
  }

  // Scores el against a parsed query, or returns null when el does not match it.
  // Each term counts once, at the weight of the best field it matches.
  function scoreMatch(el, { terms, phrases }) {
    const text = redactedText(el).replace(/\s+/g, ' ').trim();
    const role = el.getAttribute('role') || inferRole(el) ||
      (/^H[1-6]$/.test(el.tagName) ? 'heading' : el.tagName === 'IMG' ? 'img' : '');
    const fields = [
      [text, 2],
      [el.getAttribute('aria-label'), 5],
      [el.placeholder, 4],
      [el.title, 3],
      [el.alt, 3],
      [el.id, 3],
      [el.name, 2],
      // Matching on a sensitive value would let queries probe it.
      [isSensitiveField(el) ? '' : el.value, 2],
    ].filter(([value]) => value && typeof value === 'string').map(([value, weight]) => {
      const words = findWords(value);
      const joined = ` ${words.join(' ')} `;
      const phrase = phrases.some(p => joined.includes(` ${p} `)) ? weight : 0;
      return { words, weight, phrase, long: value === text && text.length > FIND_TEXT_TERM_LIMIT && !phrase };
    });

    let termScore = 0;
    let matched = 0;
    let matchedContent = 0;
    const roleWords = findWords(FIND_ROLE_WORDS[role] || role);
    for (const term of terms) {
      let best = 0;
      for (const field of fields) {
        if (field.weight > best && !field.long && field.words.some(word => wordMatchesTerm(word, term))) {
          best = field.weight;
        }
      }
      if (best) matchedContent++;
      else if (roleWords.includes(term)) best = 1;
      if (best) { matched++; termScore += best; }
    }
    // A role alone ("xyzzy button") is no match unless the query names nothing but roles ("button").
    if (!matched || (!matchedContent && matched < terms.length)) return null;

    let score = termScore * (matched / terms.length);
    if (matched === terms.length) score += 4;
    score += Math.max(0, ...fields.map(field => field.phrase)) * 3;
    if (isInteractiveElement(el)) score += 1;
    return { score: Math.round(score * 100) / 100, matched, textLength: text.length };
  }

  // ── Click ──
  const DISABLED_CONTROLS = 'button:disabled, input:disabled, select:disabled, textarea:disabled';

  function dispatchPointerOrMouse(target, type, init) {
    // Browsers dispatch a mouse click as a PointerEvent where they support one.
    const pointer = type.startsWith('pointer') || type === 'click';
    const EventCtor = pointer && window.PointerEvent ? PointerEvent : MouseEvent;
    return target.dispatchEvent(new EventCtor(type, {
      ...init, pointerId: 1, pointerType: 'mouse', isPrimary: true,
    }));
  }

  // MouseEvent.button and the held-buttons bit of left, middle and right.
  const MOUSE_BUTTONS = { left: [0, 1], middle: [1, 4], right: [2, 2] };

  // Modifier flags for mouse events; ControlOrMeta is Meta on macOS, else Control.
  function modifierFlags(modifiers) {
    const names = new Set(modifiers || []);
    const meta = names.has('Meta') || (names.has('ControlOrMeta') && /Mac/.test(navigator.platform));
    const ctrl = names.has('Control') || (names.has('ControlOrMeta') && !meta);
    return { altKey: names.has('Alt'), ctrlKey: ctrl, metaKey: meta, shiftKey: names.has('Shift') };
  }

  function clickElement(selector, coords, first, button, modifiers) {
    button ??= 'left';
    if (!Object.prototype.hasOwnProperty.call(MOUSE_BUTTONS, button)) {
      return { error: 'button must be left, middle or right' };
    }
    const [buttonIndex, buttonBit] = MOUSE_BUTTONS[button];
    const flags = modifierFlags(modifiers);
    // A user's middle or Ctrl/Cmd click opens a link in a new tab; a synthetic one never does,
    // so newTabLink asks the background to, unless the page cancelled the click (#223).
    const newTab = button === 'middle' || (button === 'left' && (flags.ctrlKey || flags.metaKey));
    let el = resolveTarget(selector, coords, { first, pointer: true });
    if (!el) return notFound();

    // Coordinates resolve to a rendered element by construction.
    const warning = selector ? notRenderedWarning(el) : {};
    if (coords) {
      el = nearestInteractiveAncestor(el) || el;
    } else {
      el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
    }

    lastActedElement = el;
    const rect = el.getBoundingClientRect();
    // Events carry the point in the viewport of the target's own (frame) document.
    const point = coords ? elementAtPoint(coords.x, coords.y) : null;
    const x = coords ? point.x : rect.x + rect.width / 2;
    const y = coords ? point.y : rect.y + rect.height / 2;
    const eventInit = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: el.ownerDocument?.defaultView || window,
      clientX: x,
      clientY: y,
      button: buttonIndex,
      buttons: buttonBit,
      ...flags,
    };
    // No button is held once it is released.
    const released = { ...eventInit, buttons: 0 };

    for (const type of ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
      const proceed = dispatchPointerOrMouse(el, type, type.endsWith('up') ? released : eventInit);
      // As a real mousedown does, unless the page cancelled it. The background has already
      // refused a tab the user is using (user_active), so this never takes focus from them.
      if (type === 'mousedown' && proceed) focusForClick(el, coords && { x, y });
    }
    // Other buttons fire auxclick (middle) or contextmenu (right) in place of click.
    if (button !== 'left') {
      const proceed = dispatchPointerOrMouse(el, button === 'right' ? 'contextmenu' : 'auxclick',
        { ...released, detail: 1 });
      const link = newTab ? followedLink(el) : null;
      return { ok: true, tag: el.tagName.toLowerCase(), text: redactedText(el).trim().slice(0, 60), button,
        ...warning, ...(link ? { newTabLink: { url: link.href, prevented: !proceed } } : {}) };
    }

    const link = newTab ? followedLink(el) : blankTargetLink(el);
    const media = MEDIA_TAGS.has(el.tagName) ? el : null;
    const wasPaused = media?.paused;
    let clickEvent = null;
    const onClick = event => { clickEvent = event; };
    if (link || media) el.addEventListener('click', onClick);
    // A dispatched click runs native activation exactly once, as el.click() does, and also
    // carries the point. Like el.click(), it skips disabled form controls.
    if (typeof el.click === 'function' && !el.matches?.(DISABLED_CONTROLS)) {
      dispatchPointerOrMouse(el, 'click', { ...released, detail: 1 });
    }
    if (link || media) el.removeEventListener('click', onClick);
    // A page handler that cancelled the click took the link over, including any tab it opens.
    const prevented = !clickEvent || clickEvent.defaultPrevented;
    let linkTab = {};
    if (link && newTab) linkTab = { newTabLink: { url: link.href, prevented } };
    else if (link && !prevented) {
      linkTab = { targetBlankLink: { url: link.href, opener: link.relList.contains('opener') } };
    }

    // A media element's text is its fallback content, which the browser does not show.
    const result = { ok: true, tag: el.tagName.toLowerCase(),
      text: media ? '' : redactedText(el).trim().slice(0, 60), ...warning, ...linkTab };
    // Synthetic clicks cannot press the browser's native media controls (they only answer
    // trusted input), so a click the page neither cancelled nor handled toggles playback.
    if (media && clickEvent && !clickEvent.defaultPrevented && media.paused === wasPaused) {
      if (!wasPaused) media.pause();
      return (wasPaused ? playMedia(media) : Promise.resolve(null))
        .then(failure => ({ ...result, media: { ...mediaState(media), ...failure } }));
    }
    return result;
  }

  // ── Media ──
  // <video> and <audio> are driven through HTMLMediaElement: play() needs no user
  // activation for muted media, while unmuted playback may be refused (NotAllowedError).
  const MEDIA_OPS = new Set(['play', 'pause', 'seek', 'mute', 'unmute']);
  // How long play() or a seek may take to settle; a slow source keeps loading afterwards.
  const MEDIA_SETTLE_MS = 2000;

  function mediaState(el) {
    const seconds = value => Number.isFinite(value) ? Math.round(value * 100) / 100 : null;
    return { src: String(el.currentSrc || el.src || '').slice(0, 160), duration: seconds(el.duration),
      currentTime: seconds(el.currentTime), paused: !!el.paused, muted: !!el.muted, controls: !!el.controls };
  }

  // Resolves to null once playback starts (or is still loading), else to { error, reason }.
  // Firefox holds play() pending in a tab that has never been shown
  // (media.block-autoplay-until-in-foreground), and agent tabs open in the background.
  // Showing the tab is the caller's explicit choice, so the pending play is cancelled.
  async function playMedia(el) {
    let timer;
    const pending = new Promise(resolve => { timer = setTimeout(resolve, MEDIA_SETTLE_MS, 'pending'); });
    const started = Promise.resolve(el.play()).then(() => null, e => ({
      reason: e.name === 'NotAllowedError' ? 'autoplay_blocked' : 'refused',
      error: `play() was refused (${e.name}: ${e.message})` + (e.name === 'NotAllowedError'
        ? '; use the media command mute then play, browser_trusted_click where available, or ask the user'
        : '') }));
    const outcome = await Promise.race([started, pending]).finally(() => clearTimeout(timer));
    if (outcome !== 'pending') return outcome;
    if (!document.hidden) return null;
    el.pause();
    return { reason: 'background_tab', error: 'Playback did not start: Firefox delays media in a tab ' +
      'that has not been shown yet. Show it with browser_tabs select, then play again' };
  }

  // Resolves once the frame at time is shown, so a screenshot taken next shows it.
  function seekMedia(el, time) {
    return new Promise(resolve => {
      const done = () => {
        clearTimeout(timer);
        el.removeEventListener('seeked', done);
        resolve();
      };
      const timer = setTimeout(done, MEDIA_SETTLE_MS);
      el.addEventListener('seeked', done);
      el.currentTime = time;
    });
  }

  async function controlMedia(selector, op, time, first) {
    if (!MEDIA_OPS.has(op)) return { error: 'op must be play, pause, seek, mute or unmute' };
    if (op === 'seek' && !(Number.isFinite(time) && time >= 0)) {
      return { error: 'seek needs time: a non-negative number of seconds' };
    }
    const el = resolveTarget(selector, null, { first });
    if (!el) return notFound();
    if (!MEDIA_TAGS.has(el.tagName)) return { error: `Not a video or audio element: ${el.tagName.toLowerCase()}` };
    let failure = null;
    if (op === 'play') failure = await playMedia(el);
    else if (op === 'pause') el.pause();
    else if (op === 'seek') await seekMedia(el, time);
    else el.muted = op === 'mute';
    const media = mediaState(el);
    return failure ? { ...failure, media } : { ok: true, tag: el.tagName.toLowerCase(), media };
  }

  // Focus what a click on el focuses; in an editor, put the caret at the click point, or at
  // the end of el for a ref or selector click.
  function focusForClick(el, coords) {
    if (isEditableRegion(el)) {
      placeEditableCaret(el, coords && caretRangeAt(el, coords));
      return;
    }
    el.closest?.(FOCUSABLE_SELECTOR)?.focus?.({ preventScroll: true });
  }

  const FOCUSABLE_SELECTOR = 'a[href], area[href], button, input, select, textarea, iframe, summary, [tabindex]';

  // The collapsed range at a viewport point, if it falls inside el.
  function caretRangeAt(el, { x, y }) {
    let range = null;
    if (docOf(el).caretPositionFromPoint) {
      const position = docOf(el).caretPositionFromPoint(x, y);
      if (position?.offsetNode) {
        range = docOf(el).createRange();
        range.setStart(position.offsetNode, position.offset);
      }
    } else if (docOf(el).caretRangeFromPoint) {
      range = docOf(el).caretRangeFromPoint(x, y);
    }
    return range && el.contains(range.startContainer) ? range : null;
  }

  // The editing host el belongs to: the element that takes focus for it.
  function editingHost(el) {
    let host = el;
    while (host.parentElement?.isContentEditable) host = host.parentElement;
    return host;
  }

  function selectionInside(el) {
    const selection = (docOf(el).defaultView || window).getSelection();
    if (!selection?.rangeCount) return null;
    const range = selection.getRangeAt(0);
    return el.contains(range.startContainer) && el.contains(range.endContainer) ? range.cloneRange() : null;
  }

  // Focus el's editing host and select range, by default a caret at the end of el. Focusing
  // the host alone leaves Firefox's caret at the start of the editor.
  function placeEditableCaret(el, range) {
    if (!range) {
      range = docOf(el).createRange();
      range.selectNodeContents(el);
      range.collapse(false);
    }
    editingHost(el).focus({ preventScroll: true });
    const selection = (docOf(el).defaultView || window).getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  // Replace the selection with text as typing does, firing beforeinput and input. False when
  // the page cancelled beforeinput.
  function insertEditableText(el, text) {
    if (docOf(el).execCommand('insertText', false, text)) return true;
    const init = { bubbles: true, composed: true, inputType: 'insertText', data: text };
    if (!el.dispatchEvent(new InputEvent('beforeinput', { ...init, cancelable: true }))) return false;
    const selection = (docOf(el).defaultView || window).getSelection();
    const range = selection.getRangeAt(0);
    range.deleteContents();
    const node = docOf(el).createTextNode(text);
    range.insertNode(node);
    range.setStartAfter(node);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    el.dispatchEvent(new InputEvent('input', init));
    return true;
  }

  // The http(s) link a click on el follows, unless it downloads.
  function followedLink(el) {
    const link = el.closest('a[href], area[href]');
    return link && !link.hasAttribute('download') && /^https?:$/.test(link.protocol) ? link : null;
  }

  // The http(s) link a click on el follows into a new tab (target=_blank, or <base target>).
  // The popup blocker can drop that tab for a synthetic click, so the background checks it.
  function blankTargetLink(el) {
    const link = followedLink(el);
    if (!link) return null;
    const target = link.getAttribute('target') ?? docOf(link).querySelector('base[target]')?.getAttribute('target');
    return target?.toLowerCase() === '_blank' ? link : null;
  }

  function nativeValueSetter(el) {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    return Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  }

  // ── Type ──
  function typeText(selector, text, clear, first) {
    const host = resolveTarget(selector, null, { first });
    if (!host) return notFound();
    // A web-component host types into the field in its (nested) open or closed shadow roots.
    const el = ['INPUT', 'TEXTAREA'].includes(host.tagName) ? host : shadowField(host, 'input, textarea') || host;
    if (!['INPUT', 'TEXTAREA'].includes(el.tagName) && !isEditableRegion(el)) {
      return notFillable(el, 'input or textarea');
    }
    lastActedElement = lastFilledField = el;

    // In an editor, type at the caret if it is in el, else at the end; clear replaces el's content.
    if (isEditableRegion(el)) {
      const range = clear ? docOf(el).createRange() : selectionInside(el);
      if (clear) range.selectNodeContents(el);
      placeEditableCaret(el, range);
      insertEditableText(el, text);
      return { ok: true };
    }

    el.focus();
    
    // Use native value setter to properly trigger React/Vue state
    const setter = nativeValueSetter(el);
    if (setter) setter.call(el, clear ? text : el.value + text);
    else if (clear) el.value = text;
    else el.value += text;

    // Fire all the events React/Vue/Angular listen for
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));

    return { ok: true };
  }

  // ── Set Editable Content ──
  function setEditableContent(selector, value, format, clear, first) {
    if (format === 'html' && KITSUNE_BUILD !== 'dev') {
      return { error: DEV_BUILD_REQUIRED, devBuildRequired: true };
    }
    const el = resolveTarget(selector, null, { first });
    if (!el) return notFound();

    const target = getEditableTarget(el);
    if (!target) return { error: 'Element is not editable' };
    lastFilledField = target;

    // Read before focus(), which can move the caret to the start of the editor.
    const caret = isEditableRegion(target) ? selectionInside(target) : null;
    target.focus();
    const text = String(value ?? '');

    if (target.isContentEditable || target.contentEditable === 'true') {
      if (clear) {
        // @kitsune-dev-only:start
        if (format === 'html') target.innerHTML = text; else
        // @kitsune-dev-only:end
        target.innerText = text;
      } else {
        placeEditableCaret(target, caret);
        docOf(target).execCommand(format === 'html' ? 'insertHTML' : 'insertText', false, text);
      }
      target.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        inputType: clear ? 'insertReplacementText' : 'insertText',
        data: format === 'text' ? text : null,
      }));
      target.dispatchEvent(new Event('change', { bubbles: true }));
      return {
        ok: true,
        tag: target.tagName.toLowerCase(),
        contentEditable: true,
        length: text.length,
      };
    }

    if (['INPUT', 'TEXTAREA'].includes(target.tagName)) {
      const setter = nativeValueSetter(target);
      const next = clear ? text : `${target.value || ''}${text}`;
      if (setter) setter.call(target, next);
      else target.value = next;
      target.dispatchEvent(new Event('input', { bubbles: true }));
      target.dispatchEvent(new Event('change', { bubbles: true }));
      return { ok: true, tag: target.tagName.toLowerCase(), length: text.length };
    }

    return { error: 'Element is not editable' };
  }

  // ── Scroll ──
  function scrollPage(direction, amount, selector, first) {
    let target = (selector ? resolveTarget(selector, null, { first }) : null) || window;
    // amount is pixels; keep this aligned with the MCP tool contract.
    const parsed = Number(amount);
    const dist = Number.isFinite(parsed) ? Math.round(parsed) : 500;
    const horizontal = direction === 'left' || direction === 'right';
    const opts = { behavior: 'instant' };
    const position = el => {
      if (el === window) return horizontal ? window.scrollX : window.scrollY;
      return horizontal ? el.scrollLeft : el.scrollTop;
    };
    const scroll = el => {
      const before = position(el);
      if (direction === 'top') {
        el.scrollTo({ top: 0, ...opts });
      } else if (direction === 'bottom') {
        const height = el === window
          ? Math.max(document.documentElement?.scrollHeight || 0, document.body?.scrollHeight || 0)
          : el.scrollHeight;
        el.scrollTo({ top: height, ...opts });
      } else {
        const deltas = { up: { top: -dist }, down: { top: dist }, left: { left: -dist }, right: { left: dist } };
        el.scrollBy({ ...deltas[direction || 'down'], ...opts });
      }
      return position(el) - before;
    };

    let scrolledBy = scroll(target);
    // A page may scroll an inner container instead of the document. Try the
    // nearest scrollable ancestor that can actually move in this direction.
    if (target === window && !selector && scrolledBy === 0 && (dist !== 0 || direction === 'top' || direction === 'bottom')) {
      let el = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
      for (; el; el = el.parentElement) {
        if (el === document.scrollingElement) continue;
        const style = window.getComputedStyle(el);
        const overflow = horizontal ? style.overflowX : style.overflowY;
        const hasRange = horizontal ? el.scrollWidth > el.clientWidth : el.scrollHeight > el.clientHeight;
        if (!/^(auto|scroll)$/.test(overflow) || !hasRange) continue;
        scrolledBy = scroll(el);
        if (scrolledBy !== 0) {
          target = el;
          break;
        }
      }
    }

    return { ok: true, scrollY: window.scrollY, scrollX: window.scrollX,
      scrolled_by: scrolledBy, scrolledPx: scrolledBy,
      target: target === window ? 'window' : buildSelector(target) };
  }

  // ── Hover ──
  function hoverElement(selector, coords, first) {
    const el = resolveTarget(selector, coords, { first, pointer: true });
    if (!el) return notFound();

    // As a pointer reaching it would, bring the target into view first (#220). scrollIntoView
    // scrolls every scrolling ancestor, so an inner list that loads more at its end sees it;
    // the point is read after the scroll.
    if (!coords) el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
    const rect = el.getBoundingClientRect();
    const x = rect.x + rect.width / 2;
    const y = rect.y + rect.height / 2;

    for (const type of ['mouseenter', 'mouseover', 'mousemove']) {
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, clientX: x, clientY: y }));
    }

    return { ok: true, tag: el.tagName.toLowerCase(), ...(selector ? notRenderedWarning(el) : {}) };
  }

  // ── Fill Form ──
  function fillFieldSafe(selector, value, first) {
    try { return fillField(selector, value, first); } catch(e) { return errorResult(e); }
  }

  const FILL_TAGS = ['INPUT', 'TEXTAREA', 'SELECT'];

  // fields: what the action looks for, e.g. 'input, textarea or select'.
  // state.reason: 'no_field', or 'shadow_depth' when shadow roots nest deeper than shadowField searches.
  function notFillable(el, fields) {
    const tooDeep = hasDeeperShadowRoot(el);
    const reason = tooDeep
      ? `no ${fields} within ${SHADOW_FIELD_DEPTH} nested shadow roots, and its shadow roots nest deeper`
      : `no ${fields} in it or in its open or closed shadow roots`;
    return actionError(`Element is not a fillable field: ${reason}`,
      'not_fillable', 'Target the field or its web-component host from a new browser_snapshot. ' +
        (tooDeep ? 'A field nested deeper is not reached; use browser_evaluate for it. ' : '') +
        'A contenteditable editor is filled through its textbox ref.',
      { tag: el.tagName.toLowerCase(), reason: tooDeep ? 'shadow_depth' : 'no_field' });
  }

  function fillField(selector, value, first) {
    const el = resolveTarget(selector, null, { first });
    if (!el) return notFound();

    const target = fillDestination(el);
    if (!FILL_TAGS.includes(target.tagName)) {
      if (isEditableRegion(target)) return fillEditable(target, value, target !== el);
      return notFillable(el, 'input, textarea, select or editor');
    }
    lastActedElement = lastFilledField = target;

    if (target.type === 'checkbox' || target.type === 'radio') {
      target.checked = !!value;
      target.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (target.tagName === 'SELECT') {
      target.value = value;
      target.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      // Use native setter to trigger React/Vue/custom element state
      target.focus();
      const setter = nativeValueSetter(target);
      if (setter) {
        setter.call(target, String(value));
      } else {
        target.value = String(value);
      }
      target.dispatchEvent(new Event('input', { bubbles: true }));
      target.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return { ok: true, pierced: target !== el, tag: target.tagName.toLowerCase() };
  }

  const EDITOR_SELECTOR = '[contenteditable="true"], [contenteditable=""], [contenteditable="plaintext-only"]';

  // What fill writes to: el's field (piercing shadow roots), else the editing host of el or of
  // an editor inside it, else el itself.
  function fillDestination(el) {
    if (isEditableRegion(el)) return editingHost(el);
    const field = getFillTarget(el);
    if (FILL_TAGS.includes(field.tagName)) return field;
    const editor = shadowField(el, EDITOR_SELECTOR) || el.querySelector?.(EDITOR_SELECTOR);
    return editor ? editingHost(editor) : el;
  }

  // Replace an editing host's content as typing over a full selection does, so the editor
  // sees beforeinput and input (#255).
  function fillEditable(host, value, pierced) {
    lastActedElement = lastFilledField = host;
    const range = docOf(host).createRange();
    range.selectNodeContents(host);
    placeEditableCaret(host, range);
    const text = String(value ?? '');
    if (text) insertEditableText(host, text);
    else docOf(host).execCommand('delete', false);
    return { ok: true, pierced, tag: host.tagName.toLowerCase(), contentEditable: true };
  }

  // Shadow roots searched for a host's field: its own, and those of hosts nested in it.
  const SHADOW_FIELD_DEPTH = 8;

  // The first match in el's shadow root, else in the shadow roots of hosts inside it,
  // at most `depth` roots deep. Firefox content scripts can also reach closed roots
  // through openOrClosedShadowRoot.
  function shadowField(el, selector, depth = SHADOW_FIELD_DEPTH) {
    const root = el.openOrClosedShadowRoot || el.shadowRoot;
    if (!root || depth < 1) return null;
    const field = root.querySelector(selector);
    if (field) return field;
    for (const host of root.querySelectorAll('*')) {
      const nested = shadowField(host, selector, depth - 1);
      if (nested) return nested;
    }
    return null;
  }

  // Whether el has shadow roots nested deeper than shadowField searches.
  function hasDeeperShadowRoot(el, depth = SHADOW_FIELD_DEPTH) {
    const root = el.openOrClosedShadowRoot || el.shadowRoot;
    if (!root) return false;
    return depth < 1 || Array.from(root.querySelectorAll('*')).some(host => hasDeeperShadowRoot(host, depth - 1));
  }

  function getFillTarget(el) {
    const tag = el.tagName.toLowerCase();
    if (['input', 'textarea', 'select'].includes(tag)) return el;

    // Nested open or closed shadow roots
    const inner = shadowField(el, 'input, textarea, select');
    if (inner) return inner;

    // Regular children
    const child = el.querySelector('input, textarea, select');
    if (child) return child;

    return el;
  }

  function getEditableTarget(el) {
    if (el.isContentEditable || el.contentEditable === 'true') return el;
    if (['INPUT', 'TEXTAREA'].includes(el.tagName)) return el;

    const inner = shadowField(el, '[contenteditable="true"], textarea, input');
    if (inner) return inner;

    const child = el.querySelector?.('[contenteditable="true"], textarea, input');
    if (child) return child;

    return null;
  }

  // ── Get Form Fields ──
  // Fields in same-origin frames name their frame; unreadable frames are listed.
  function getFormFields() {
    const fields = [];
    deepQuerySelectorAll('input, select, textarea').forEach(el => {
      if (isHidden(el)) return;
      const rect = viewportRect(el);
      if (rect.width < 2 || rect.height < 2) return;

      const ref = issueRef(el);
      const frame = hostFrame(el.ownerDocument);

      fields.push({
        ref,
        tag: el.tagName.toLowerCase(),
        type: el.type || 'text',
        name: el.name || '',
        id: el.id || '',
        placeholder: el.placeholder || '',
        ...(isSensitiveField(el) ? redactedValue(el) : { value: (el.value || '').slice(0, 100) }),
        label: findLabel(el),
        required: el.required,
        selector: buildSelector(el),
        ...(frame ? { frame: frame.title || frame.getAttribute('name') || frameSource(frame) } : {}),
        bounds: [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)]
      });
    });
    const frames = unavailableFrames();
    return { fields, ...(frames.length ? { unavailableFrames: frames } : {}) };
  }

  // ── Execute JS ──
  const JS_RESULT_LIMIT = 8000000; // 2.1.0: bridge WebSocket now takes up to 256 MB (was 50 KB under the old 1 MB socket limit)

  // @kitsune-dev-only:start
  // A selector or ref binds the element the code sees as __kitsuneElement (browser_evaluate);
  // a promise result is awaited.
  function executeJS(code, selector) {
    const format = result => {
      const str = String(result ?? '');
      const truncated = str.length > JS_RESULT_LIMIT;
      return {
        result: truncated ? str.slice(0, JS_RESULT_LIMIT) : str,
        ...(truncated && { truncated: true, fullLength: str.length }),
      };
    };
    try {
      // Read only by the evaluated code, which direct eval lets see this scope.
      const __kitsuneElement = selector ? resolveElement(selector) : undefined;
      if (selector && !__kitsuneElement) return notFound();
      const result = eval(code);
      if (result && typeof result.then === 'function') {
        return Promise.resolve(result).then(format, e => ({ error: e?.message || String(e) }));
      }
      return format(result);
    } catch (e) {
      return { error: e.message };
    }
  }
  // @kitsune-dev-only:end

  // Names a snapshot shows but innerText lacks (#275): placeholders and aria labels.
  const TEXT_NAME_SOURCES = ['aria-label', 'aria-labelledby', 'placeholder'];
  const TEXT_NAME_MATCH_LIMIT = 3;
  function textNameSource(el, text) {
    return TEXT_NAME_SOURCES.find(attr => normalizeSelectorText(
      attr === 'aria-labelledby' ? labelledByText(el) : el.getAttribute(attr)).includes(text));
  }

  // A timed-out text wait whose text is a visible element's name, not page text: the
  // elements and a hint. Only an unmet wait asks, so a met wait costs nothing.
  function textNameDiagnosis(text) {
    const matches = [];
    for (const el of deepQuerySelectorAll('[aria-label], [aria-labelledby], [placeholder]')) {
      if (matches.length >= TEXT_NAME_MATCH_LIMIT) break;
      if (isHidden(el) || !hasArea(el)) continue;
      const source = textNameSource(el, text);
      if (source) matches.push({ ...targetSummary(el), source });
    }
    if (!matches.length) return {};
    const [first] = matches;
    return { matched_as: 'accessible_name', name_matches: matches,
      hint: `Found as the name of ${first.role} ${JSON.stringify(first.name)} (${first.source}), not page ` +
        'text: text waits match only rendered page text (innerText). Act on its ref from ' +
        'name_matches, or wait for text the page shows.' };
  }

  function waitForText(text, textGone, timeout = 10000, diagnose = false) {
    if ((text == null && textGone == null) ||
        [text, textGone].some(t => t != null && (typeof t !== 'string' || !t.length)) ||
        !Number.isFinite(timeout) || timeout < 0) {
      return { error: 'Provide non-empty text and/or text_gone and a non-negative timeout' };
    }
    const start = Date.now();
    return new Promise(resolve => {
      function poll() {
        // innerText follows rendered layout, excluding display:none and hidden text.
        const body = document.body;
        const rendered = body && window.getComputedStyle(body).display !== 'none' &&
          window.getComputedStyle(document.documentElement).display !== 'none';
        const visibleText = rendered ? body.innerText : '';
        const appeared = text == null || visibleText.includes(text);
        const disappeared = textGone == null || !visibleText.includes(textGone);
        const elapsed = Date.now() - start;
        if (appeared && disappeared) resolve({ ok: true, text, text_gone: textGone, elapsed });
        else if (elapsed >= timeout) resolve({ ok: false, text, text_gone: textGone, elapsed,
          error: `Timed out after ${timeout}ms waiting for visible text`,
          ...(diagnose && !appeared ? textNameDiagnosis(text) : {}) });
        else setTimeout(poll, Math.min(100, timeout - elapsed));
      }
      poll();
    });
  }

  // ── Wait For Element ──
  function waitForElement(selector, timeout, pollInterval) {
    // A 0 ms timeout is a single check, as in waitForText.
    const maxWait = timeout ?? 10000;
    const interval = pollInterval || 200;
    const start = Date.now();

    return new Promise((resolve) => {
      function poll() {
        let el;
        try {
          // Preserve native CSS selector errors while resolving snapshot refs.
          el = REF_SELECTOR.test(selector) ? resolveElement(selector) : deepQuerySelector(selector);
        } catch (e) {
          resolve(errorResult(e));
          return;
        }
        if (el && !isHidden(el)) {
          const elapsed = Date.now() - start;
          const ref = issueRef(el);
          resolve({
            ok: true,
            found: true,
            selector,
            elapsed,
            ref,
            tag: el.tagName.toLowerCase(),
            text: redactedText(el).trim().slice(0, 120),
          });
        } else if (Date.now() - start >= maxWait) {
          resolve({
            ok: false,
            found: false,
            selector,
            elapsed: Date.now() - start,
            error: `Timed out after ${maxWait}ms waiting for "${selector}"`,
          });
        } else {
          setTimeout(poll, interval);
        }
      }
      poll();
    });
  }

  // ── Wait For Result (poll JS expression until non-empty) ──
  // @kitsune-dev-only:start
  function waitForResult(code, timeout, pollInterval) {
    const maxWait = timeout || 15000;
    const interval = pollInterval || 500;
    const start = Date.now();

    return new Promise((resolve) => {
      function poll() {
        try {
          const raw = eval(code);
          const result = String(raw ?? '');
          // Treat non-empty, non-null, non-undefined, non-"[]", non-"null" as success
          if (result && result !== 'undefined' && result !== 'null' && result !== '[]' && result !== '""') {
            resolve({
              ok: true,
              result: result.slice(0, JS_RESULT_LIMIT),
              elapsed: Date.now() - start,
              polls: Math.floor((Date.now() - start) / interval) + 1,
            });
            return;
          }
        } catch (e) {
          // Expression threw — keep polling (page may still be hydrating)
        }

        if (Date.now() - start >= maxWait) {
          resolve({
            ok: false,
            result: null,
            elapsed: Date.now() - start,
            error: `Timed out after ${maxWait}ms — expression never returned non-empty result`,
          });
        } else {
          setTimeout(poll, interval);
        }
      }
      poll();
    });
  }
  // @kitsune-dev-only:end

  // ── Fixed page operations (replace bridge/background code strings) ──
  function neuterUnload() {
    // Same effect as the former NEUTER_UNLOAD_JS: stop "Leave page?" prompts before a forced close.
    try {
      const W = window.wrappedJSObject || window;
      W.onbeforeunload = null;
      window.onbeforeunload = null;
      if (typeof exportFunction === 'function' && W.BeforeUnloadEvent) {
        Object.defineProperty(W.BeforeUnloadEvent.prototype, 'returnValue', { configurable: true,
          get: exportFunction(function () { return ''; }, W), set: exportFunction(function () {}, W) });
        const EP = W.Event.prototype, orig = EP.preventDefault;
        EP.preventDefault = exportFunction(function () {
          if (this.type === 'beforeunload') return;
          return orig.call(this);
        }, W);
      }
      return { ok: true };
    } catch (e) {
      return { error: String(e) };
    }
  }

  function screenPoint(selector, x, y, click, first) {
    // Viewport point (element centre or x/y) plus its desktop position for OS-level clicks.
    if (selector) {
      const el = click ? resolveTarget(selector, null, { first, pointer: true }) : resolveElement(selector);
      if (!el) return notFound();
      el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
      const rect = viewportRect(el);
      x = rect.left + rect.width / 2;
      y = rect.top + rect.height / 2;
    }
    if (x == null || y == null) return { error: 'No target point' };
    if (click) agentTrustedClickUntil = Date.now() + AGENT_TRUSTED_CLICK_MS;
    const viewportLeft = Number.isFinite(window.mozInnerScreenX)
      ? window.mozInnerScreenX
      : window.screenX + Math.max(0, (window.outerWidth - window.innerWidth) / 2);
    const viewportTop = Number.isFinite(window.mozInnerScreenY)
      ? window.mozInnerScreenY
      : window.screenY + Math.max(0, window.outerHeight - window.innerHeight);
    return {
      title: document.title,
      viewportX: x,
      viewportY: y,
      screenX: viewportLeft + x,
      screenY: viewportTop + y,
      devicePixelRatio: window.devicePixelRatio || 1,
    };
  }

  // Markers resolve to the exact connected ref target, including inside closed shadow roots.
  const screenshotTargets = new Map();

  function screenshotTarget(selector) {
    const marker = /^\[(data-kitsune-capture-\d+)\]$/.exec(selector);
    if (!marker) return document.querySelector(selector);
    const el = screenshotTargets.get(marker[1]);
    return el?.isConnected ? el : null;
  }

  function captureMeasure(selector) {
    const el = screenshotTarget(selector);
    if (!el) return { missing: true };
    const b = viewportRect(el);
    return { x: b.left, y: b.top, w: b.width, h: b.height, vw: innerWidth, vh: innerHeight,
      sy: scrollY, dpr: devicePixelRatio || 1 };
  }

  function captureScrollIntoView(selector, block) {
    const el = screenshotTarget(selector);
    if (!el) return notFound();
    el.scrollIntoView({ block: block === 'start' ? 'start' : 'center', inline: 'center', behavior: 'instant' });
    return { ok: true };
  }

  function captureScroll(to, by) {
    if (Number.isFinite(to)) window.scrollTo(0, to);
    else if (Number.isFinite(by)) window.scrollBy(0, by);
    return { sy: scrollY };
  }

  // Hide position:fixed/sticky elements (headers, cookie bars) so they don't repeat in every
  // stitched screenshot segment. Elements containing the capture target are left alone.
  function hideFixed(selector) {
    const target = selector ? screenshotTarget(selector) : null;
    const ancestors = new Set();
    for (let el = target; el; el = flatParent(el)) ancestors.add(el);
    let hidden = 0;
    document.querySelectorAll('body *').forEach(el => {
      const style = getComputedStyle(el);
      if ((style.position === 'fixed' || style.position === 'sticky') && !ancestors.has(el) &&
          style.visibility !== 'hidden') {
        el.setAttribute('data-zl-fixed', el.style.visibility || '');
        el.style.visibility = 'hidden';
        hidden++;
      }
    });
    return { hidden };
  }

  function restoreFixed() {
    document.querySelectorAll('[data-zl-fixed]').forEach(el => {
      el.style.visibility = el.getAttribute('data-zl-fixed');
      el.removeAttribute('data-zl-fixed');
    });
    return { ok: true };
  }

  // ── Highlight ──
  let highlightOverlay = null;

  function highlightElement(selector) {
    clearHighlight();
    const el = resolveElement(selector);
    if (!el) return notFound();

    const rect = el.getBoundingClientRect();
    highlightOverlay = document.createElement('div');
    Object.assign(highlightOverlay.style, {
      position: 'fixed',
      left: rect.x + 'px', top: rect.y + 'px',
      width: rect.width + 'px', height: rect.height + 'px',
      border: '2px solid #e94560',
      background: 'rgba(233, 69, 96, 0.15)',
      pointerEvents: 'none',
      zIndex: '2147483647',
      borderRadius: '3px'
    });
    document.body.appendChild(highlightOverlay);
    return { ok: true };
  }

  function clearHighlight() {
    if (highlightOverlay) { highlightOverlay.remove(); highlightOverlay = null; }
    return { ok: true };
  }

  // ── Same-origin frames ──
  // The content script runs in the top document only. Reads and actions reach into
  // same-origin frames through contentDocument, at most FRAME_DEPTH frames deep;
  // cross-origin frames give content scripts no contentDocument and stay opaque.
  const FRAME_DEPTH = 3;
  const FRAME_TAGS = new Set(['IFRAME', 'FRAME']);

  // { doc } for a frame Kitsune reads into, else { reason: 'cross_origin' | 'too_deep' }.
  function frameAccess(frame) {
    let doc = null;
    try {
      doc = frame.contentDocument;
      if (doc && doc.defaultView?.origin !== window.origin) doc = null;
    } catch { doc = null; }
    if (!doc) return { reason: 'cross_origin' };
    let depth = 0;
    for (let owner = frame.ownerDocument; owner && owner !== document; owner = hostFrame(owner)?.ownerDocument) depth++;
    return depth < FRAME_DEPTH ? { doc } : { reason: 'too_deep' };
  }

  // The document inside el when el is a frame Kitsune reads into, else null.
  function frameDocument(el) {
    return FRAME_TAGS.has(el?.tagName) ? frameAccess(el).doc || null : null;
  }

  // The frame element holding doc; null for the top document.
  function hostFrame(doc) {
    if (!doc || doc === document) return null;
    try { return doc.defaultView?.frameElement || null; } catch { return null; }
  }

  const docOf = el => el?.ownerDocument || document;
  const styleOf = el => (el.ownerDocument?.defaultView || window).getComputedStyle(el);

  // el's parent, continuing from a frame's root element to the frame element.
  function parentAcrossFrames(el) {
    return flatParent(el) || hostFrame(el.ownerDocument);
  }

  // Element children, or a same-origin frame's body.
  function childrenAcrossFrames(el) {
    const doc = frameDocument(el);
    if (doc) return doc.body ? [doc.body] : [];
    return flatChildren(el);
  }

  // The top document, then each same-origin frame document after the document holding it.
  function searchedDocuments(doc = document, docs = []) {
    docs.push(doc);
    let frames = [];
    try { frames = queryTreeSelectorAll('iframe, frame', doc); } catch {}
    for (const frame of frames) {
      const inner = frameDocument(frame);
      if (inner) searchedDocuments(inner, docs);
    }
    return docs;
  }

  // Where a frame's viewport starts in its parent's viewport: inside its border and padding.
  function frameContentOffset(frame) {
    const rect = frame.getBoundingClientRect();
    const style = styleOf(frame);
    return { x: rect.left + (frame.clientLeft || 0) + (parseFloat(style.paddingLeft) || 0),
      y: rect.top + (frame.clientTop || 0) + (parseFloat(style.paddingTop) || 0) };
  }

  // el.getBoundingClientRect() in the top document's viewport, also for elements in frames.
  function viewportRect(el) {
    const rect = el.getBoundingClientRect();
    let frame = hostFrame(el.ownerDocument);
    if (!frame) return rect;
    let x = rect.left, y = rect.top;
    for (; frame; frame = hostFrame(frame.ownerDocument)) {
      const offset = frameContentOffset(frame);
      x += offset.x;
      y += offset.y;
    }
    return { x, y, width: rect.width, height: rect.height,
      left: x, top: y, right: x + rect.width, bottom: y + rect.height };
  }

  // The element at a point of the top viewport, inside same-origin frames too, and the
  // point in that element's own viewport.
  function elementAtPoint(x, y) {
    let el = deepElementFromPoint(x, y);
    for (let doc = frameDocument(el); doc; doc = frameDocument(el)) {
      const offset = frameContentOffset(el);
      const inner = deepElementFromPoint(x - offset.x, y - offset.y, doc);
      if (!inner) break;
      el = inner;
      x -= offset.x;
      y -= offset.y;
    }
    return { el, x, y };
  }

  // The focused element, inside a focused same-origin frame too.
  function activeElement() {
    let el = deepActiveElement();
    for (let doc = frameDocument(el); doc?.activeElement; doc = frameDocument(el)) el = deepActiveElement(doc);
    return el;
  }

  // A frame's src without query or fragment, which can carry tokens.
  function frameSource(frame) {
    const src = frame.getAttribute('src') || '';
    try {
      const url = new URL(src, frame.baseURI || location.href);
      if (/^https?:$/.test(url.protocol)) return url.origin + url.pathname;
    } catch {}
    return src.split(/[?#]/)[0].slice(0, 100);
  }

  const hasArea = el => { const r = el.getBoundingClientRect(); return r.width >= 2 && r.height >= 2; };

  // Rendered frames whose content Kitsune cannot read, as browser_forms lists them.
  function unavailableFrames() {
    const out = [];
    for (const doc of searchedDocuments()) {
      let frames = [];
      try { frames = queryTreeSelectorAll('iframe, frame', doc); } catch {}
      for (const frame of frames) {
        if (!FRAME_TAGS.has(frame.tagName)) continue;
        const { reason } = frameAccess(frame);
        if (!reason || isHidden(frame) || !hasArea(frame)) continue;
        out.push({ ref: issueRef(frame), title: frame.title || frame.getAttribute('name') || '',
          src: frameSource(frame), content: reason });
      }
    }
    return out;
  }

  // ── Helpers ──

  const REF_SELECTOR = /^(?:s\d+e\d+|r\d+)$/;

  // ── Shadow DOM ──
  // Reads and selector resolution see open and closed shadow roots, as the fill path does.
  function shadowRootOf(el) {
    return el?.openOrClosedShadowRoot || el?.shadowRoot || null;
  }

  // Every shadow root in scope, each followed by the roots nested in it.
  function shadowRootsIn(scope) {
    const roots = [];
    for (const el of scope.querySelectorAll('*')) {
      const root = shadowRootOf(el);
      if (root) roots.push(root, ...shadowRootsIn(root));
    }
    return roots;
  }

  // CSS matches within one tree; query each document and its shadow roots separately.
  function queryTreeSelectorAll(selector, scope) {
    return [scope, ...shadowRootsIn(scope)].flatMap(root => Array.from(root.querySelectorAll(selector)));
  }

  function deepQuerySelectorAll(selector) {
    return searchedDocuments().flatMap(doc => queryTreeSelectorAll(selector, doc));
  }

  function deepQuerySelector(selector) {
    // Preserve native querySelector syntax errors and avoid walking other trees when
    // the top document already has the first match.
    const match = document.querySelector(selector);
    if (match) return match;
    for (const doc of searchedDocuments()) {
      if (doc !== document) {
        const inner = doc.querySelector(selector);
        if (inner) return inner;
      }
      for (const root of shadowRootsIn(doc)) {
        const inner = root.querySelector(selector);
        if (inner) return inner;
      }
    }
    return null;
  }

  // Children as rendered (the flat tree): a host's shadow tree instead of its light children,
  // which appear where a slot takes them; a slot's assigned nodes, else its fallback content.
  function flatChildren(el) {
    const root = shadowRootOf(el);
    if (root) return Array.from(root.children);
    if (el.tagName === 'SLOT' && el.assignedElements) return Array.from(el.assignedElements({ flatten: true }));
    return Array.from(el.children);
  }

  function flatChildNodes(el) {
    const root = shadowRootOf(el);
    if (root) return Array.from(root.childNodes || []);
    if (el.tagName === 'SLOT' && el.assignedNodes) return Array.from(el.assignedNodes({ flatten: true }));
    return Array.from(el.childNodes);
  }

  function flatParent(el) {
    return el.openOrClosedAssignedSlot || el.assignedSlot || el.parentElement || el.parentNode?.host || null;
  }

  // IDs are scoped to their tree: a field in a shadow root names labels in that root.
  function elementByIdNear(el, id) {
    const root = el.getRootNode?.();
    return (root?.getElementById ? root : docOf(el)).getElementById?.(id);
  }

  // document.elementFromPoint stops at a shadow host; each shadow root hit-tests its own tree.
  function deepElementFromPoint(x, y, doc = document) {
    let el = doc.elementFromPoint(x, y);
    const seen = new Set([el]);
    for (let root = shadowRootOf(el); root?.elementFromPoint; root = shadowRootOf(el)) {
      const inner = root.elementFromPoint(x, y);
      if (!inner || seen.has(inner)) break;
      seen.add(inner);
      el = inner;
    }
    return el;
  }

  // document.activeElement is the host of a focused shadow-root element.
  function deepActiveElement(doc = document) {
    let el = doc.activeElement;
    for (let inner = shadowRootOf(el)?.activeElement; inner && inner !== el; inner = shadowRootOf(el)?.activeElement) {
      el = inner;
    }
    return el;
  }

  // ── Playwright selectors (#219) ──
  // Besides refs and CSS, targets may use the Playwright forms agents write most: text=
  // (also /regex/), role= (also [name=/regex/]), internal:text=, internal:role= and
  // internal:label=, the :has-text(), :text-is(), :text() and :visible pseudo-classes,
  // >> chains (also starting at a ref), nth= and xpath=. This is a subset, not Playwright's
  // engine. Text and names are read redacted, as snapshots show them. XPath reads text and
  // attributes (value) unredacted, so other than .. it runs only in documents holding no
  // redacted content. Anything else fails with unsupported_selector, which repeating cannot fix.
  const UNSUPPORTED_SELECTOR_HINT = 'Pass a ref from browser_snapshot (e12), a CSS selector, ' +
    'or one of the Playwright forms Kitsune resolves: text=… (or text=/regex/), role=…[name="…"] ' +
    '(or [name=/regex/]), internal:label="…", :has-text("…"), :text-is("…"), :text("…"), :visible, ' +
    '>> chains (e12 >> … too), nth=N and xpath=….';
  // Valid CSS never starts with an engine name and =, two dots, // or a quote.
  const SELECTOR_ENGINE = /^([a-z][\w-]*(?::[a-z][\w-]*)*)\s*=/i;
  const PLAYWRIGHT_PSEUDO =
    /:(has-text|text-is|text-matches|text|visible|nth-match|left-of|right-of|above|below|near)(?![\w-])/i;
  const PLAYWRIGHT_FILTERS = new Set(['has-text', 'text-is', 'text', 'visible']);
  // Never matched by text: their text is not page content.
  const TEXTLESS_TAGS = new Set(['HEAD', 'TITLE', 'SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  // Implicit roles beyond the ones snapshots infer (inferRole).
  const IMPLICIT_ROLES = { ARTICLE: 'article', ASIDE: 'complementary', DIALOG: 'dialog', FIELDSET: 'group',
    FOOTER: 'contentinfo', FORM: 'form', HEADER: 'banner', HR: 'separator', IMG: 'img', LI: 'listitem',
    MAIN: 'main', MENU: 'list', NAV: 'navigation', OL: 'list', OPTION: 'option', PROGRESS: 'progressbar',
    TABLE: 'table', TD: 'cell', TH: 'columnheader', TR: 'row', UL: 'list' };
  // A snapshot line's `role "name"`, passed as a target instead of its ref.
  const SNAPSHOT_LABEL = /^[a-z]+ "(?:[^"\\]|\\.)*"$/i;

  function unsupportedSelector(selector, reason) {
    return new ActionError(`Unsupported selector: ${reason}`, { code: 'unsupported_selector', retryable: false,
      hint: UNSUPPORTED_SELECTOR_HINT, state: { selector } });
  }

  // A native CSS failure: invalid syntax can never match, so it is not a retryable not_found.
  function cssSelectorError(selector, e) {
    if (e?.name !== 'SyntaxError') return null;
    return unsupportedSelector(selector, SNAPSHOT_LABEL.test(selector.trim())
      ? 'this is a snapshot line\'s role and name; pass that line\'s ref instead'
      : 'it is not valid CSS');
  }

  // The index after the string, (…) or […] group that opens at text[i]. As in CSS, the end of
  // the text closes whatever is still open. A role= [name=/regex/] skips the regex.
  function groupEnd(text, i) {
    const open = text[i];
    const close = open === '(' ? ')' : open === '[' ? ']' : open;
    for (let j = i + 1; j < text.length; j++) {
      const c = text[j];
      if (c === '\\') j++;
      else if (c === '/' && open === '[' && /^\s*name\s*[*^$]?=\s*$/i.test(text.slice(i + 1, j))) j = regexEnd(text, j) - 1;
      else if (c === close) return j + 1;
      else if (close !== '"' && close !== "'" && '"\'(['.includes(c)) j = groupEnd(text, j) - 1;
    }
    return text.length;
  }

  // The index after the /regex/ that opens at text[i]; a [class] may hold a /.
  function regexEnd(text, i) {
    for (let j = i + 1; j < text.length; j++) {
      if (text[j] === '\\') j++;
      else if (text[j] === '/') return j + 1;
      else if (text[j] === '[') {
        for (j++; j < text.length && text[j] !== ']'; j++) if (text[j] === '\\') j++;
      }
    }
    return text.length;
  }

  // A regex value opens at a / that follows text=, internal:text= or internal:label= at the
  // start of a >> part. Quotes and brackets inside a lax text= value that does not start with
  // a quote are plain characters, as in Playwright.
  const REGEX_VALUE_START = /^\s*(?:text|internal:text|internal:label)\s*=\s*$/i;
  const LAX_TEXT_VALUE = /^\s*text\s*=\s*[^\s"']/i;

  // text split at each separator outside strings and groups, and for >>, outside regex values.
  function splitTopLevel(text, separator) {
    const parts = [];
    let start = 0;
    const chain = separator === '>>';
    for (let i = 0; i < text.length; i++) {
      if (text[i] === '\\') i++;
      else if (chain && text[i] === '/' && REGEX_VALUE_START.test(text.slice(start, i))) i = regexEnd(text, i) - 1;
      else if ('"\'(['.includes(text[i]) && !(chain && LAX_TEXT_VALUE.test(text.slice(start, i)))) {
        i = groupEnd(text, i) - 1;
      } else if (text.startsWith(separator, i)) {
        parts.push(text.slice(start, i));
        i += separator.length - 1;
        start = i + 1;
      }
    }
    parts.push(text.slice(start));
    return parts;
  }

  const withoutStrings = text => text.replace(/"(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?/g, '""');
  const unescapeSelector = text => text.replace(/\\([\s\S])/g, '$1');
  const normalizeSelectorText = text => String(text || '').replace(/\s+/g, ' ').trim();
  // A pseudo-class argument: a quoted string, or the bare text.
  function selectorArgument(arg) {
    const quoted = /^(["'])([\s\S]*)\1$/.exec(arg.trim());
    return quoted ? unescapeSelector(quoted[2]) : arg.trim();
  }

  // Lax: a case-insensitive substring. Strict: the whole text, case-sensitive. Both after
  // whitespace is collapsed, as Playwright matches text.
  function textMatcher(value, strict) {
    const wanted = normalizeSelectorText(value);
    if (strict) return text => text === wanted;
    const lower = wanted.toLowerCase();
    return text => text.toLowerCase().includes(lower);
  }

  // Parts of a selector that needs this engine (separated by >>), or null for plain CSS.
  function parseTargetSelector(selector) {
    const text = selector.trim();
    const parts = splitTopLevel(text, '>>').map(part => part.trim());
    if (parts.length === 1 && !SELECTOR_ENGINE.test(text) && !/^(?:\.\.|\/\/|["'])/.test(text) &&
        !PLAYWRIGHT_PSEUDO.test(withoutStrings(text))) return null;
    if (parts.some(part => !part)) throw unsupportedSelector(selector, 'a >> chain has an empty part');
    const parsed = parts.map((part, index) => parseSelectorPart(part, index, selector));
    if (parsed[0].kind === 'nth') throw unsupportedSelector(selector, 'nth= must follow another selector');
    return parsed;
  }

  // A ref may start a chain (e12 >> xpath=..); the background expands its short form, so
  // one that reaches the page unexpanded is unknown (stale_ref).
  function parseSelectorPart(part, index, selector) {
    if (REF_SELECTOR.test(part) || /^e\d+$/.test(part)) {
      if (index === 0) return { kind: 'ref', ref: part };
      throw unsupportedSelector(selector, 'a snapshot ref can only start a >> chain');
    }
    const engine = SELECTOR_ENGINE.exec(part);
    if (!engine) {
      if (part.startsWith('//') || part.startsWith('..')) return xpathPart(part, index, selector);
      return part[0] === '"' || part[0] === "'" ? { kind: 'text', match: textMatcherFor(part, 'text', selector) }
        : cssPart(part, selector);
    }
    const name = engine[1].toLowerCase();
    const body = part.slice(engine[0].length).trim();
    if (name === 'css') return cssPart(body, selector);
    if (name === 'text' || name === 'internal:text') return { kind: 'text', match: textMatcherFor(body, name, selector) };
    if (name === 'role' || name === 'internal:role') return rolePart(body, name !== 'role', selector);
    if (name === 'internal:label') return { kind: 'label', match: textMatcherFor(body, name, selector) };
    if (name === 'nth' || name === 'internal:nth') {
      if (!/^-?\d+$/.test(body)) throw unsupportedSelector(selector, 'nth= takes an integer index');
      return { kind: 'nth', index: Number(body) };
    }
    if (name === 'xpath') {
      if (!body) throw unsupportedSelector(selector, 'xpath= needs an expression');
      return xpathPart(body, index, selector);
    }
    throw unsupportedSelector(selector, `the ${name}= selector engine`);
  }

  // As in Playwright, an absolute path after >> is made relative to the earlier match. The
  // expression compiles here, so an invalid one is unsupported even when an earlier part
  // matches nothing; compiling reads nothing from the page.
  function xpathPart(expression, index, selector) {
    if (expression === '..') return { kind: 'parent' };
    const relative = index > 0 && expression.startsWith('/') ? '.' + expression : expression;
    try { document.createExpression(relative); } catch {
      throw unsupportedSelector(selector, `"${expression}" is not an XPath expression that selects elements`);
    }
    return { kind: 'xpath', expression: relative, source: expression };
  }

  // A /regex/ value: the source up to the last /, when only flags follow it.
  const REGEX_VALUE = /^\/([\s\S]*)\/([dgimsuvy]*)$/;

  // text=foo is lax; a quoted text="foo" (or "foo"s) is strict and "foo"i is lax again.
  // /regex/flags tests the whitespace-collapsed text. internal:text= and internal:label=
  // (engine) take a quoted string or a regex.
  function textMatcherFor(body, engine, selector) {
    const quoted = /^(["'])((?:\\[\s\S]|(?!\1)[^\\])*)\1([is])?$/.exec(body);
    if (quoted) return textMatcher(unescapeSelector(quoted[2]), quoted[3] !== 'i');
    const regex = REGEX_VALUE.exec(body);
    if (regex) {
      const re = selectorRegex(regex[1], regex[2], selector);
      return text => { re.lastIndex = 0; return re.test(text); };
    }
    if (engine !== 'text') throw unsupportedSelector(selector, `${engine}= takes a quoted string or /regex/`);
    if (!normalizeSelectorText(body)) throw unsupportedSelector(selector, 'text= needs text');
    return textMatcher(body, false);
  }

  function selectorRegex(source, flags, selector) {
    try { return new RegExp(source, flags); } catch {
      throw unsupportedSelector(selector, `/${source}/${flags} is not a valid regular expression`);
    }
  }

  // [key], or [key op value] with a quoted string (and i/s), a number or boolean, or /regex/flags.
  const ROLE_ATTRIBUTE =
    /^\[\s*([\w-]+)\s*(?:([*^$]?=)\s*(?:(["'])((?:\\[\s\S]|(?!\3)[^\\])*)\3\s*([is])?|(-?\d+|true|false)|\/((?:\\[\s\S]|\[(?:\\[\s\S]|[^\]\\])*\]|[^\\/[])+)\/([a-z]*)))?\s*\]$/i;

  // role=button[name="Save"] matches the whole name ignoring case, [name="Save"s] exactly;
  // internal:role (getByRole) takes [name="Save"i] as a substring. [name=/Save/i] tests the
  // name with a regex. Also [level=N] and [include-hidden]; hidden elements are skipped otherwise.
  function rolePart(body, internal, selector) {
    const role = /^[a-z][\w-]*/i.exec(body)?.[0];
    if (!role) throw unsupportedSelector(selector, 'role= needs a role, e.g. role=button[name="Save"]');
    const part = { kind: 'role', role: role.toLowerCase(), includeHidden: false, level: null, name: null };
    let rest = body.slice(role.length).trim();
    while (rest) {
      if (rest[0] !== '[') throw unsupportedSelector(selector, `unexpected "${rest}" after role=${role}`);
      const end = groupEnd(rest, 0);
      const attr = ROLE_ATTRIBUTE.exec(rest.slice(0, end));
      const key = attr?.[1].toLowerCase();
      if (key === 'name' && attr[7] && attr[2] === '=') {
        part.name = { regex: selectorRegex(attr[7], attr[8], selector) };
      } else if (key === 'name' && attr[3]) {
        const caseSensitive = attr[5] === 's';
        const op = internal && attr[2] === '=' && !caseSensitive ? '*=' : attr[2];
        part.name = { op, caseSensitive, value: normalizeSelectorText(unescapeSelector(attr[4])) };
      } else if (key === 'level' && attr[2] === '=' && /^\d+$/.test(attr[6] || '')) {
        part.level = Number(attr[6]);
      } else if (key === 'include-hidden' && (!attr[2] || (attr[2] === '=' && /^(?:true|false)$/i.test(attr[6] || '')))) {
        part.includeHidden = !attr[2] || attr[6].toLowerCase() === 'true';
      } else {
        throw unsupportedSelector(selector, `the role= attribute ${rest.slice(0, end)}`);
      }
      rest = rest.slice(end).trim();
    }
    return part;
  }

  // CSS whose compounds may carry :has-text(), :text-is(), :text() or :visible. Each
  // alternative is a list of { combinator, css, filters }, matched right to left.
  function cssPart(css, selector) {
    if (!PLAYWRIGHT_PSEUDO.test(withoutStrings(css))) return { kind: 'css', css };
    return { kind: 'css', css, alternatives: splitTopLevel(css, ',').map(alt => complexSelector(alt.trim(), selector)) };
  }

  function complexSelector(text, selector) {
    const compounds = [];
    let current = { combinator: null, css: '', filters: [] };
    const finish = () => {
      if (!current.css && !current.filters.length) throw unsupportedSelector(selector, 'it is not valid CSS');
      compounds.push(current);
    };
    for (let i = 0; i < text.length;) {
      const c = text[i];
      if (/[\s>+~]/.test(c)) {
        let j = i, next = ' ';
        for (; j < text.length && /[\s>+~]/.test(text[j]); j++) {
          if (text[j].trim()) {
            if (next !== ' ') throw unsupportedSelector(selector, 'it is not valid CSS');
            next = text[j];
          }
        }
        finish();
        current = { combinator: next, css: '', filters: [] };
        i = j;
      } else if (c === '\\') {
        current.css += text.slice(i, i + 2);
        i += 2;
      } else if ('"\'(['.includes(c)) {
        const end = groupEnd(text, i);
        current.css += text.slice(i, end);
        i = end;
      } else if (c === ':' && text[i + 1] !== ':') {
        const name = /^:([\w-]+)/.exec(text.slice(i))?.[1] || '';
        const end = i + 1 + name.length;
        const argEnd = text[end] === '(' ? groupEnd(text, end) : end;
        const lower = name.toLowerCase();
        if (PLAYWRIGHT_FILTERS.has(lower)) {
          if ((lower === 'visible') !== (argEnd === end)) {
            throw unsupportedSelector(selector, `:${lower}${lower === 'visible' ? ' takes no argument' : '() needs text'}`);
          }
          current.filters.push(selectorFilter(lower, text.slice(end + 1, argEnd - 1)));
        } else if (PLAYWRIGHT_PSEUDO.test(`:${lower}`)) {
          throw unsupportedSelector(selector, `the :${lower}() pseudo-class`);
        } else if (PLAYWRIGHT_PSEUDO.test(withoutStrings(text.slice(end, argEnd)))) {
          throw unsupportedSelector(selector, `Playwright pseudo-classes inside :${lower}()`);
        } else {
          current.css += text.slice(i, argEnd);
        }
        i = argEnd;
      } else {
        current.css += c;
        i++;
      }
    }
    finish();
    return compounds;
  }

  // :has-text() is any element containing the text, :text() the smallest one, :text-is()
  // one whose whole text it is.
  function selectorFilter(name, arg) {
    if (name === 'visible') return el => isRendered(el);
    const value = selectorArgument(arg);
    const match = textMatcher(value, name === 'text-is');
    if (name === 'text') return (el, scan) => scan.smallest(el, match);
    return (el, scan) => match(scan.text(el));
  }

  // The text a selector matches: redacted text content without script or style text, or a
  // button input's value.
  function selectorText(el) {
    if (el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(String(el.type).toLowerCase())) {
      return isSensitiveField(el) ? '' : String(el.value || '');
    }
    return redactedText(el, true);
  }

  // The role snapshots and role= selectors give el.
  function targetRole(el) {
    const explicit = String(el.getAttribute('role') || '').trim().split(/\s+/)[0].toLowerCase();
    if (explicit) return explicit;
    if (/^H[1-6]$/.test(el.tagName)) return 'heading';
    if (el.tagName === 'A' || el.tagName === 'AREA') return el.hasAttribute('href') ? 'link' : '';
    if (el.tagName === 'SELECT' && (el.multiple || el.size > 1)) return 'listbox';
    return IMPLICIT_ROLES[el.tagName] || inferRole(el);
  }

  // Matches for each parsed part in turn; a later part searches inside the earlier matches.
  function matchSelectorParts(parts, selector) {
    const texts = new Map(), smallest = new Map(), redacted = new Map();
    const scan = {
      text: el => {
        if (!texts.has(el)) texts.set(el, normalizeSelectorText(selectorText(el)));
        return texts.get(el);
      },
      // el's text matches and no child element's does.
      smallest: (el, match) => {
        let byElement = smallest.get(match);
        if (!byElement) smallest.set(match, byElement = new Map());
        if (!byElement.has(el)) {
          byElement.set(el, !TEXTLESS_TAGS.has(el.tagName) && match(scan.text(el)) &&
            !Array.from(el.children || []).some(child => !TEXTLESS_TAGS.has(child.tagName) && match(scan.text(child))));
        }
        return byElement.get(el);
      },
      redacted: doc => {
        if (!redacted.has(doc)) redacted.set(doc, holdsRedactedContent(doc));
        return redacted.get(doc);
      },
    };
    let matches = null;
    for (const part of parts) {
      if (part.kind === 'ref') {
        matches = [refElement(part.ref)];
      } else if (part.kind === 'nth') {
        const el = matches.at(part.index);
        matches = el ? [el] : [];
      } else if (part.kind === 'parent') {
        matches = [...new Set((matches || [document.documentElement]).map(el => el.parentElement).filter(Boolean))];
      } else {
        const found = new Set();
        for (const scope of matches || [null]) {
          for (const el of partMatches(part, scope, scan, selector)) found.add(el);
        }
        matches = [...found];
      }
    }
    return matches;
  }

  function partMatches(part, scope, scan, selector) {
    const query = css => {
      try { return scope ? queryTreeSelectorAll(css, scope) : deepQuerySelectorAll(css); } catch (e) {
        throw cssSelectorError(selector, e) || e;
      }
    };
    if (part.kind === 'text') return query('*').filter(el => scan.smallest(el, part.match));
    if (part.kind === 'role') return query('*').filter(el => roleMatches(el, part));
    if (part.kind === 'label') {
      return query('*').filter(el => selectorLabels(el, scan).some(label => part.match(label)));
    }
    if (part.kind === 'xpath') return xpathMatches(part, scope, scan, selector);
    if (!part.alternatives) return query(part.css);
    const last = part.alternatives.map(compounds => compounds.at(-1).css || '*');
    return query(last.join(', ')).filter(el => part.alternatives.some(compounds => {
      try { return complexMatches(el, compounds, compounds.length - 1, scan); } catch (e) {
        throw cssSelectorError(selector, e) || e;
      }
    }));
  }

  function complexMatches(el, compounds, i, scan) {
    const { combinator, css, filters } = compounds[i];
    if ((css && !el.matches(css)) || !filters.every(filter => filter(el, scan))) return false;
    if (i === 0) return true;
    const step = combinator === '+' || combinator === '~' ? node => node.previousElementSibling
      : node => node.parentElement;
    for (let node = step(el); node; node = step(node)) {
      if (complexMatches(node, compounds, i - 1, scan)) return true;
      if (combinator === '>' || combinator === '+') break;
    }
    return false;
  }

  function roleMatches(el, part) {
    if (targetRole(el) !== part.role) return false;
    if (!part.includeHidden && (!isRendered(el) || el.closest('[aria-hidden="true"]'))) return false;
    if (part.level != null) {
      const level = Number(el.getAttribute('aria-level')) || Number(/^H([1-6])$/.exec(el.tagName)?.[1]);
      if (level !== part.level) return false;
    }
    if (!part.name) return true;
    const { op, caseSensitive, value, regex } = part.name;
    let name = normalizeSelectorText(accessibleName(el));
    if (regex) {
      regex.lastIndex = 0;
      return regex.test(name);
    }
    let wanted = value;
    if (!caseSensitive) [name, wanted] = [name.toLowerCase(), wanted.toLowerCase()];
    if (op === '*=') return name.includes(wanted);
    if (op === '^=') return name.startsWith(wanted);
    if (op === '$=') return name.endsWith(wanted);
    return name === wanted;
  }

  // The labels internal:label= (getByLabel) matches, as Playwright reads them: the
  // aria-labelledby elements, else aria-label, else a labelable element's <label>s.
  const LABELABLE_TAGS = new Set(['BUTTON', 'INPUT', 'METER', 'OUTPUT', 'PROGRESS', 'SELECT', 'TEXTAREA']);
  function selectorLabels(el, scan) {
    const labelledBy = String(el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => elementByIdNear(el, id)).filter(Boolean);
    if (labelledBy.length) return labelledBy.map(scan.text);
    const ariaLabel = normalizeSelectorText(el.getAttribute('aria-label'));
    if (ariaLabel) return [ariaLabel];
    if (!LABELABLE_TAGS.has(el.tagName) || (el.tagName === 'INPUT' && el.type === 'hidden')) return [];
    return Array.from(el.labels || [], scan.text);
  }

  // XPath sees what redaction hides from the other forms: a sensitive field's value
  // attribute and masked text. It runs only in documents that hold neither, in any of their
  // trees: a scope inside a shadow root evaluates in that root.
  function holdsRedactedContent(doc) {
    return withRedactionCache(() => queryTreeSelectorAll('*', doc).some(el =>
      (el.hasAttribute('value') && isSensitiveField(el)) ||
      (holdsSensitiveText(el) && String(el.textContent || '').trim() !== '')));
  }

  // Elements an XPath expression selects in each searched document, or within scope. XPath
  // does not cross into shadow roots or frames.
  function xpathMatches({ expression, source }, scope, scan, selector) {
    const docs = scope ? [docOf(scope)] : searchedDocuments();
    if (docs.some(scan.redacted)) {
      throw unsupportedSelector(selector, 'XPath on a page holding redacted text or sensitive values; ' +
        'use CSS, text= or role= instead');
    }
    const found = [];
    for (const doc of docs) {
      let result;
      try {
        result = doc.evaluate(expression, scope || doc, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      } catch {
        throw unsupportedSelector(selector, `"${source}" is not an XPath expression that selects elements`);
      }
      for (let i = 0; i < result.snapshotLength; i++) {
        const node = result.snapshotItem(i);
        if (node.nodeType === 1) found.push(node);
      }
    }
    return found;
  }

  // Every element a non-ref selector names, in document order per searched tree.
  function targetMatches(selector) {
    const parts = parseTargetSelector(selector);
    if (parts) return withRedactionCache(() => matchSelectorParts(parts, selector));
    try { return deepQuerySelectorAll(selector); } catch (e) {
      const error = cssSelectorError(selector, e);
      if (error) throw error;
      return [];
    }
  }

  // A type selector no HTML element has: a snapshot ref (e12) or a word that is not ASCII,
  // such as an element's text, passed as CSS.
  function impossibleTypeSelector(css) {
    if (css.includes('\\')) return null;
    let bare = '';
    for (let i = 0; i < css.length; i++) {
      if ('"\'(['.includes(css[i])) i = groupEnd(css, i) - 1;
      else bare += css[i];
    }
    for (const compound of bare.split(/[\s>+~,]+/)) {
      const type = /^[^.#:*|]+/.exec(compound)?.[0];
      if (type && (/^(?:s\d+)?e\d+$/i.test(type) || (/[^\x00-\x7f]/.test(type) && !type.includes('-')))) return type;
    }
    return null;
  }

  // A CSS selector that matched nothing: unsupported when it can never match.
  function cssMiss(selector) {
    const type = impossibleTypeSelector(selector);
    if (type) {
      throw unsupportedSelector(selector, `"${type}" is not an element name; it looks like a ref or element ` +
        'text (use the ref on its own, or text=…)');
    }
    return null;
  }

  // A ref's attached element; a stale ref throws.
  function refElement(ref) {
    const el = refMap[ref];
    if (!ref.startsWith(`s${refGeneration}e`) || !el || !refAttached(el)) {
      if (el && !refAttached(el)) {
        delete refMap[ref];
        elementRefs.delete(el);
        removedRefs.add(ref);
      }
      throw staleRef(ref, el);
    }
    return el;
  }

  function resolveElement(selector, coords) {
    if (!selector && coords) return elementAtPoint(coords.x, coords.y).el;
    if (!selector) return null;
    
    if (REF_SELECTOR.test(selector)) return refElement(selector);

    const parts = parseTargetSelector(selector);
    if (parts) return withRedactionCache(() => matchSelectorParts(parts, selector))[0] || null;
    // Try CSS selector
    try { return deepQuerySelector(selector) || cssMiss(selector); } catch (e) {
      if (e instanceof ActionError) throw e;
      const error = cssSelectorError(selector, e);
      if (error) throw error;
    }
    return null;
  }

  // Write actions: like resolveElement, but a selector matching several actionable elements
  // is ambiguous unless first is set; one actionable match among others is the target.
  // Refs and coordinates name one element.
  function resolveTarget(selector, coords, { first = false, pointer = false } = {}) {
    if (!selector || REF_SELECTOR.test(selector)) return resolveElement(selector, coords);
    const matches = targetMatches(selector);
    if (!matches.length) return parseTargetSelector(selector) ? null : resolveElement(selector, coords);
    if (first) return matches[0];
    const actionable = assertOneTarget(selector, pointer, matches);
    return actionable.length === 1 ? actionable[0] : matches[0];
  }

  // What a write action could act on: enabled and not <input type=hidden>; for pointer
  // actions also rendered, as their actionability wait requires.
  function isActionable(el, pointer) {
    if (el.matches?.(':disabled') || (el.tagName === 'INPUT' && el.type === 'hidden')) return false;
    return !pointer || isRendered(el);
  }

  // The actionable matches, unless there are several.
  function assertOneTarget(selector, pointer, matches = targetMatches(selector)) {
    const actionable = matches.filter(el => isActionable(el, pointer));
    if (actionable.length < 2) return actionable;
    const { error, ...details } = ambiguousTarget(
      `${actionable.length} elements match the selector; nothing was dispatched`,
      'Act on one element: pass its ref from state.candidates or a new browser_snapshot, or a more ' +
        'specific selector. first: true acts on the first match.',
      { selector, matches: actionable.length,
        candidates: actionable.slice(0, AMBIGUOUS_CANDIDATE_LIMIT).map(targetSummary) });
    throw new ActionError(error, details);
  }

  // Reuse one ref per attached node, across all observation tools.
  function issueRef(el) {
    const existing = elementRefs.get(el);
    if (existing && refMap[existing] === el) return existing;
    const ref = `s${refGeneration}e${refCounter++}`;
    refMap[ref] = el;
    elementRefs.set(el, ref);
    return ref;
  }

  // An ambiguous_target candidate: its ref, role and name (never a field value).
  function targetSummary(el) {
    return { ref: issueRef(el), role: el.getAttribute('role') || inferRole(el) || el.tagName.toLowerCase(),
      name: String(accessibleName(el)).trim().slice(0, 80) };
  }

  // The name ambiguous_target candidates and role= selectors use (never a field value).
  function accessibleName(el) {
    if (ALTERNATIVE_NAMED_TAGS.has(el.tagName.toUpperCase())) return alternativeName(el);
    const buttonInput = el.tagName === 'INPUT' && BUTTON_INPUT_TYPES.has(String(el.type).toLowerCase());
    return controlLabel(el, buttonInput) ||
      (FILL_TAGS.includes(el.tagName) ? findLabel(el) || el.placeholder || '' : '');
  }

  function nearestInteractiveAncestor(el) {
    let cur = el;
    while (cur && cur !== docOf(cur).body) {
      if (isInteractiveElement(cur)) return cur;
      cur = flatParent(cur);
    }
    return null;
  }

  function isHidden(el) {
    if (!el || el.hidden) return true;
    const s = styleOf(el);
    return s.display === 'none' || s.visibility === 'hidden';
  }

  const MEDIA_TAGS = new Set(['VIDEO', 'AUDIO']);

  function isInteractiveElement(el) {
    return isEditableRegion(el) || isControl(el);
  }

  // A snapshot lists an editor once, as its editing host: its descendants inherit
  // isContentEditable but are not separate textboxes (#256).
  function isSnapshotControl(el) {
    return isEditingHost(el) || isControl(el);
  }

  function isControl(el) {
    const interactiveTags = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'DETAILS', 'SUMMARY']);
    if (interactiveTags.has(el.tagName)) return true;
    if (el.onclick || el.hasAttribute('tabindex')) return true;
    const role = el.getAttribute('role');
    if (role && ['button', 'link', 'textbox', 'checkbox', 'radio', 'combobox', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'listbox', 'option', 'treeitem', 'spinbutton', 'searchbox', 'switch', 'slider'].includes(role)) return true;
    return false;
  }

  function inferRole(el) {
    const inputRole = { button: 'button', submit: 'button', reset: 'button',
      image: 'button', checkbox: 'checkbox', radio: 'radio', range: 'slider', number: 'spinbutton',
      search: 'searchbox' };
    const map = { A: 'link', BUTTON: 'button', INPUT: inputRole[el.type] || 'textbox',
      SELECT: 'combobox', TEXTAREA: 'textbox', SUMMARY: 'button', DETAILS: 'group',
      VIDEO: 'video', AUDIO: 'audio' };
    return map[el.tagName] || '';
  }

  // Structural reads never return sensitive values or lengths; browser_js is a
  // separate, unrestricted trust boundary.
  const SENSITIVE_AUTOCOMPLETE = new Set(['current-password', 'new-password', 'one-time-code',
    'cc-number', 'cc-csc']);
  const TEXT_ENTRY_TYPES = new Set(['text', 'search', 'tel', 'number']);
  // Matched against whole words of name/id/placeholder/aria-label, so a password
  // field whose show/hide toggle switched it to type=text stays redacted.
  const SECRET_NAME = /(?:^| )(?:pass(?:word|wd|code)?|pwd|secret|otp|totp|mfa|2fa|pin|cvv|cvc|csc|one time|two factor|security code|verification code|card number|cc num(?:ber)?|api key)(?: |$)/;
  const NUMERIC_CODE_NAME = /(?:^| )(?:code|digit)(?: |$)/;
  const ADDRESS_CODE_NAME = /(?:^| )(?:zip|postal)(?: |$)/;
  const AUTOFILL_SELECTORS = [':autofill', ':-webkit-autofill', ':-moz-autofill'];
  const TEXT_SECURITY_MASKS = new Set(['disc', 'circle', 'square']);

  function identifierWords(value) {
    return String(value || '').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase()
      .split(/[^a-z0-9]+/).filter(Boolean).join(' ');
  }

  function hasSensitiveAutocomplete(el) {
    return String(el.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/)
      .some(token => SENSITIVE_AUTOCOMPLETE.has(token));
  }

  function hasSecretName(el) {
    const names = [el.name, el.id, el.placeholder, el.getAttribute('aria-label')].map(identifierWords);
    if (names.some(name => SECRET_NAME.test(name))) return true;
    // Numeric one-time codes, e.g. <input inputmode="numeric" name="code">.
    return el.getAttribute('inputmode') === 'numeric' &&
      names.some(name => NUMERIC_CODE_NAME.test(name)) && !names.some(name => ADDRESS_CODE_NAME.test(name));
  }

  // Password-manager autofill; unsupported pseudo-classes throw and are skipped.
  function isAutofilled(el) {
    return AUTOFILL_SELECTORS.some(selector => {
      try { return el.matches(selector); } catch { return false; }
    });
  }

  // Text reads look at the same elements many times (names, labels, find scoring), so
  // within one synchronous read each element's mask and redacted text are computed once.
  // The cache never outlives the read, so page changes between reads are always seen.
  let redactionCache = null;
  // Synchronous reads that share one cache; action handlers are left out, because
  // page handlers they trigger can change the text they report afterwards.
  const REDACTION_CACHED_READS = new Set(['getSnapshot', 'getAccessibilityTree', 'getFormFields', 'find',
    'query', 'getHTML', 'getReadability', 'getMarkdown', 'explainSelector']);

  function withRedactionCache(read) {
    if (redactionCache) return read();
    redactionCache = { masked: new WeakMap(), region: new WeakMap(), text: new WeakMap(), pageText: new WeakMap() };
    try { return read(); } finally { redactionCache = null; }
  }

  function cachedFor(kind, el, compute) {
    const cache = redactionCache?.[kind];
    if (!cache) return compute();
    if (!cache.has(el)) cache.set(el, compute());
    return cache.get(el);
  }

  function isMasked(el) {
    return cachedFor('masked', el, () => {
      const style = styleOf(el);
      const security = style.webkitTextSecurity || style.getPropertyValue?.('-webkit-text-security');
      return TEXT_SECURITY_MASKS.has(security);
    });
  }

  // Editing hosts and their descendants (isContentEditable is inherited).
  function isEditableRegion(el) {
    return el.isContentEditable || el.contentEditable === 'true';
  }

  // The element that takes focus for an editor: editable, in a parent that is not.
  function isEditingHost(el) {
    return isEditableRegion(el) && !el.parentElement?.isContentEditable;
  }

  function isSensitiveField(el) {
    if (el.tagName === 'INPUT') {
      return el.type === 'password' || hasSensitiveAutocomplete(el) ||
        (TEXT_ENTRY_TYPES.has(el.type) && hasSecretName(el)) || isAutofilled(el) || isMasked(el);
    }
    // A masked textarea or editing region holds its secret as text content.
    if ((el.tagName === 'TEXTAREA' || isEditableRegion(el)) && isMasked(el)) return true;
    // Custom controls such as <x-input type="password"> expose their inner input's
    // value as a property (and sometimes a reflected attribute) on the host.
    // Xray wrappers hide page-defined properties, so custom element names count too.
    if (!el.tagName.includes('-') && !('value' in el)) return false;
    if (String(el.getAttribute('type')).toLowerCase() === 'password' || hasSensitiveAutocomplete(el)) return true;
    // Firefox content scripts can also see closed roots through openOrClosedShadowRoot.
    const root = el.openOrClosedShadowRoot || el.shadowRoot;
    return !!root && Array.from(root.querySelectorAll('*')).some(isSensitiveField);
  }

  function redactedValue(el) {
    // An editing region has no value property; its content is its value.
    const value = el.tagName !== 'TEXTAREA' && isEditableRegion(el) ? el.textContent : el.value;
    return { redacted: true, hasValue: !!value };
  }

  // Textareas and editing regions are the only fields whose content is text.
  function isSensitiveTextField(el) {
    return (el.tagName === 'TEXTAREA' || isEditableRegion(el)) && isSensitiveField(el);
  }

  // A masked editing region or anything inside one, including its contenteditable="false"
  // parts, which are not editable themselves and may opt out of the mask.
  function inMaskedEditingRegion(el) {
    return !!el && cachedFor('region', el, () =>
      (isEditableRegion(el) && isMasked(el)) || inMaskedEditingRegion(el.parentElement));
  }

  // Whether el's own text is sensitive: masked text (editable or static), a sensitive
  // textarea or editing region, or text inside a masked editing region.
  function holdsSensitiveText(el) {
    return isMasked(el) || isSensitiveTextField(el) || inMaskedEditingRegion(el.parentElement);
  }

  // el.textContent without sensitive text. A masked element's children that set
  // `-webkit-text-security: none` keep their text; sensitive fields lose all of it.
  // pageTextOnly also leaves out TEXTLESS_TAGS elements (scripts, styles), as selectors do.
  function redactedText(el, pageTextOnly = false) {
    return withRedactionCache(() => cachedFor(pageTextOnly ? 'pageText' : 'text', el, () => {
      if (isSensitiveTextField(el) || (pageTextOnly && TEXTLESS_TAGS.has(el.tagName))) return '';
      const sensitive = holdsSensitiveText(el);
      if (!el.firstElementChild) return sensitive ? '' : el.textContent || '';
      let text = '';
      for (const node of el.childNodes) {
        if (node.nodeType === 3) text += sensitive ? '' : node.textContent;
        else if (node.nodeType === 1) text += redactedText(node, pageTextOnly);
      }
      return text;
    }));
  }

  // Rendered text, but redacted text content when any of el's text is sensitive, so
  // masked text is left out rather than shown as mask characters.
  function renderedText(el) {
    return withRedactionCache(() => {
      const text = redactedText(el);
      return text === el.textContent ? el.innerText || text : text;
    });
  }

  // Frameworks such as React mirror the live value into the `value` attribute,
  // so markup is serialized from a clone with sensitive value attributes removed,
  // with sensitive textareas and editing regions emptied, and without other sensitive text.
  // Sensitivity is decided on the live elements (autofill, styles, shadow roots);
  // a clone that does not mirror the original tree loses every value attribute,
  // the content of every textarea and contenteditable element, and all of its text
  // if any of the original's text is sensitive.
  function redactedClone(el) {
    return withRedactionCache(() => {
      const clone = el.cloneNode(true);
      const originals = [el, ...el.querySelectorAll('*')];
      const copies = [clone, ...clone.querySelectorAll('*')];
      const aligned = copies.length === originals.length &&
        copies.every((copy, i) => copy.tagName === originals[i].tagName);
      const dropAllText = !aligned && originals.some(holdsSensitiveText);
      copies.forEach((copy, i) => {
        if (!aligned || isSensitiveField(originals[i])) copy.removeAttribute('value');
        if (aligned ? isSensitiveTextField(originals[i])
          : copy.tagName === 'TEXTAREA' || copy.hasAttribute('contenteditable')) copy.replaceChildren();
        else if (aligned ? holdsSensitiveText(originals[i]) : dropAllText) {
          for (const node of Array.from(copy.childNodes)) if (node.nodeType === 3) node.remove();
        }
      });
      return clone;
    });
  }

  function redactMarkup(el, outer) {
    const clone = redactedClone(el);
    return (outer ? clone.outerHTML : clone.innerHTML) || '';
  }

  // A `[value^="K"]` selector would let callers guess a sensitive value attribute
  // character by character, including through combinators and :has(). Escapes
  // and comments inside a selector with attribute blocks can spell `value`, so
  // they are treated the same way.
  function isValueAttributeSelector(selector) {
    selector = String(selector);
    return /\[\s*(?:(?:[\w-]+|\*)?\|)?\s*value/i.test(selector) ||
      (selector.includes('[') && (selector.includes('\\') || selector.includes('/*')));
  }

  function valueSelectorBlocked(selector) {
    if (!isValueAttributeSelector(selector)) return false;
    for (const el of deepQuerySelectorAll('[value]')) {
      if (isSensitiveField(el)) return true;
    }
    return false;
  }

  const VALUE_SELECTOR_ERROR = 'value-attribute selectors are disabled while a sensitive field has a value attribute';

  function directText(el) {
    if (holdsSensitiveText(el)) return '';
    let t = '';
    for (const n of el.childNodes) {
      if (n.nodeType === 3) t += n.textContent;
    }
    return t.trim().slice(0, 120);
  }

  function buildSelector(el) {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const parts = [];
    let cur = el;
    for (let i = 0; i < 4 && cur && cur !== docOf(cur).body; i++) {
      let s = cur.tagName.toLowerCase();
      if (cur.id) { parts.unshift(`#${CSS.escape(cur.id)}`); break; }
      if (cur.className && typeof cur.className === 'string') {
        const cls = cur.className.trim().split(/\s+/).slice(0, 2).map(c => CSS.escape(c)).join('.');
        if (cls) s += '.' + cls;
      }
      parts.unshift(s);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  function findLabel(el) {
    if (el.id) {
      const root = el.getRootNode?.();
      const lbl = (root?.querySelector ? root : docOf(el)).querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return redactedText(lbl).trim().slice(0, 80);
    }
    const parent = el.closest('label');
    return parent ? redactedText(parent).trim().slice(0, 80) : (el.ariaLabel || '');
  }

  // ══════════════════════════════════════════════════════════════
  // 1.4.0 additions
  // ══════════════════════════════════════════════════════════════

  // ── Multi-field extract ──
  const DEFAULT_FIELDS = ['text', 'href', 'value', 'src', 'alt', 'id', 'name'];
  const QUERY_LIMIT = 500;

  function queryElements(selector, fields, limit) {
    if (!selector) return { error: 'queryElements requires selector' };
    if (valueSelectorBlocked(selector)) return { error: VALUE_SELECTOR_ERROR };
    let nodes;
    try { nodes = document.querySelectorAll(selector); } catch (e) { return { error: 'Invalid selector: ' + e.message }; }
    const cap = Math.min(limit || QUERY_LIMIT, QUERY_LIMIT);
    const want = (fields && fields.length) ? fields : DEFAULT_FIELDS;
    const items = [];
    for (let i = 0; i < nodes.length && items.length < cap; i++) {
      const el = nodes[i];
      const row = { tag: el.tagName.toLowerCase() };
      const sensitive = isSensitiveField(el);
      for (const f of want) {
        const name = String(f).toLowerCase();
        try {
          if (f === 'text') row.text = renderedText(el).trim().slice(0, 400);
          else if (f === 'html') row.html = redactMarkup(el, false).slice(0, 4000);
          else if (f === 'href') row.href = el.href || el.getAttribute('href') || undefined;
          else if (name === 'value' && sensitive) Object.assign(row, redactedValue(el));
          else if (f === 'value') row.value = (el.value != null) ? String(el.value).slice(0, 400) : undefined;
          else if (f === 'src') row.src = el.src || el.getAttribute('src') || undefined;
          else if (f === 'alt') row.alt = el.alt || el.getAttribute('alt') || undefined;
          else if (f === 'id') row.id = el.id || undefined;
          else if (f === 'name') row.name = el.name || undefined;
          else if (f === 'bounds') {
            const r = el.getBoundingClientRect();
            row.bounds = [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
          } else if (f === 'attrs') {
            const a = {};
            for (const att of el.attributes) {
              if (sensitive && att.name.toLowerCase() === 'value') continue;
              a[att.name] = att.value.slice(0, 200);
            }
            row.attrs = a;
          } else {
            // arbitrary attribute name
            const v = el.getAttribute(f);
            if (v != null) row[f] = v.slice(0, 400);
          }
        } catch {}
      }
      // Strip undefined for compact output
      for (const k of Object.keys(row)) if (row[k] === undefined) delete row[k];
      items.push(row);
    }
    return { count: items.length, total: nodes.length, truncated: nodes.length > items.length, items };
  }

  // ── HTML / bounds / computed style ──
  function getHTML(selector) {
    const el = resolveElement(selector);
    if (!el) return notFound();
    const outer = redactMarkup(el, true);
    const truncated = outer.length > 100000;
    return { html: outer.slice(0, 100000), length: outer.length, ...(truncated && { truncated: true }) };
  }

  // Keep the exact target for capture and cleanup; document selectors cannot reach shadow roots.
  function markScreenshotTarget(ref) {
    const el = resolveElement(ref);
    if (!el) return notFound();
    let attribute;
    do { attribute = `data-kitsune-capture-${crypto.getRandomValues(new Uint32Array(1))[0]}`; }
    while (screenshotTargets.has(attribute));
    el.setAttribute(attribute, '');
    screenshotTargets.set(attribute, el);
    return { selector: `[${attribute}]`, attribute };
  }

  function unmarkScreenshotTarget(attribute) {
    if (/^data-kitsune-capture-\d+$/.test(attribute)) {
      screenshotTargets.get(attribute)?.removeAttribute(attribute);
      screenshotTargets.delete(attribute);
    }
    return { ok: true };
  }

  function getBounds(selector) {
    const el = resolveElement(selector);
    if (!el) return notFound();
    const r = viewportRect(el);
    return {
      x: r.x, y: r.y, width: r.width, height: r.height,
      top: r.top, right: r.right, bottom: r.bottom, left: r.left,
      viewport: { width: window.innerWidth, height: window.innerHeight, dpr: window.devicePixelRatio || 1 },
      page: { width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight },
      inView: r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth,
    };
  }

  function getComputedStyleFor(selector, properties) {
    const el = resolveElement(selector);
    if (!el) return notFound();
    const cs = window.getComputedStyle(el);
    // No props specified → return commonly-needed ones
    const common = ['display','position','visibility','opacity','width','height','color','background-color','font-size','font-family','font-weight','margin','padding','border','z-index','overflow','cursor'];
    const requested = properties && properties.length ? properties : common;
    const out = {};
    for (const property of requested) out[property] = cs.getPropertyValue(property);
    return { computed: out };
  }

  // ── Links / images / meta / structured data ──
  function getLinks(internalOnly) {
    const origin = location.origin;
    const out = [];
    for (const a of document.querySelectorAll('a[href]')) {
      const href = a.href;
      const isInternal = href.startsWith(origin);
      if (internalOnly && !isInternal) continue;
      out.push({
        href,
        text: (a.innerText || '').trim().slice(0, 200),
        rel: a.rel || undefined,
        target: a.target || undefined,
        internal: isInternal,
      });
      if (out.length >= 500) break;
    }
    return { count: out.length, links: out };
  }

  function getImages() {
    const out = [];
    for (const img of document.images) {
      const r = img.getBoundingClientRect();
      out.push({
        src: img.currentSrc || img.src,
        alt: img.alt || '',
        width: img.naturalWidth || Math.round(r.width),
        height: img.naturalHeight || Math.round(r.height),
        loading: img.loading || undefined,
      });
      if (out.length >= 200) break;
    }
    return { count: out.length, images: out };
  }

  function getMeta() {
    const meta = {};
    for (const m of document.querySelectorAll('meta')) {
      const key = m.getAttribute('name') || m.getAttribute('property') || m.getAttribute('itemprop') || m.getAttribute('http-equiv');
      const val = m.getAttribute('content');
      if (key && val) meta[key] = val.slice(0, 600);
    }
    const link = {};
    for (const l of document.querySelectorAll('link[rel]')) {
      const rel = l.getAttribute('rel');
      if (!rel) continue;
      (link[rel] = link[rel] || []).push(l.href || l.getAttribute('href'));
    }
    return {
      title: document.title,
      url: location.href,
      canonical: link.canonical && link.canonical[0],
      meta,
      links: link,
    };
  }

  function getStructuredData() {
    const jsonld = [];
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
      try { jsonld.push(JSON.parse(s.textContent)); } catch {}
    }
    const og = {}, twitter = {};
    for (const m of document.querySelectorAll('meta')) {
      const prop = m.getAttribute('property') || '';
      const name = m.getAttribute('name') || '';
      const c = m.getAttribute('content');
      if (!c) continue;
      if (prop.startsWith('og:')) og[prop.slice(3)] = c;
      else if (name.startsWith('twitter:')) twitter[name.slice(8)] = c;
    }
    return { jsonld, openGraph: og, twitter };
  }

  // ── Readability (simple heuristic) ──
  function getReadability() {
    // Score candidates by text density and tag suitability.
    const candidates = document.querySelectorAll('article, [role="main"], main, .post, .article, .content, .entry, .markdown-body, #content, #main, [itemprop="articleBody"]');
    let best = null, bestScore = 0;
    const score = (el) => {
      const text = (el.innerText || '').trim();
      if (text.length < 200) return 0;
      const paras = el.querySelectorAll('p').length;
      const linkDensity = (el.querySelectorAll('a').reduce ? 0 : Array.from(el.querySelectorAll('a')).reduce((s,a)=>s+(a.innerText||'').length,0)) / text.length;
      return text.length * (1 + paras * 0.05) * (1 - Math.min(0.9, linkDensity));
    };
    for (const c of candidates) {
      const s = score(c);
      if (s > bestScore) { bestScore = s; best = c; }
    }
    if (!best) {
      // Fall back to densest <div> or <section>
      for (const c of document.querySelectorAll('section, div')) {
        const s = score(c);
        if (s > bestScore) { bestScore = s; best = c; }
      }
    }
    if (!best) best = document.body;
    const clone = redactedClone(best);
    // Strip nav/aside/footer noise
    clone.querySelectorAll('nav, aside, footer, header, script, style, noscript, .ad, .ads, .advert, [aria-hidden="true"]').forEach(n => n.remove());
    const text = (clone.innerText || '').trim();
    const meta = getMeta();
    const sd = getStructuredData();
    const article = (sd.jsonld || []).find(j => j['@type'] === 'Article' || j['@type'] === 'NewsArticle' || (Array.isArray(j['@type']) && j['@type'].includes('Article')));
    return {
      title: document.title,
      url: location.href,
      excerpt: meta.meta?.description || meta.meta?.['og:description'] || text.slice(0, 240),
      byline: article?.author?.name || article?.author || meta.meta?.author,
      published: article?.datePublished || meta.meta?.['article:published_time'],
      siteName: meta.meta?.['og:site_name'],
      length: text.length,
      textContent: text.slice(0, 50000),
      truncated: text.length > 50000,
      score: bestScore,
      container: best.tagName.toLowerCase() + (best.id ? '#' + best.id : '') + (best.className && typeof best.className === 'string' ? '.' + best.className.split(/\s+/).slice(0,2).join('.') : ''),
    };
  }

  // ── Markdown export (in-house, DOM walker) ──
  function getMarkdown(selector) {
    const root = selector ? resolveElement(selector) : document.body;
    if (!root) return notFound();
    // An element's own text nodes are left out when its text is sensitive.
    function walkChildren(el) {
      const sensitive = holdsSensitiveText(el);
      return Array.from(el.childNodes).map(child => sensitive && child.nodeType === 3 ? '' : walk(child)).join('');
    }
    function walk(node) {
      if (node.nodeType === 3) return node.textContent.replace(/\s+/g, ' ');
      if (node.nodeType !== 1 || isSensitiveTextField(node)) return '';
      const tag = node.tagName.toLowerCase();
      if (['script','style','noscript','svg','iframe','nav','aside','footer','header','form'].includes(tag)) return '';
      const children = () => walkChildren(node);
      switch (tag) {
        case 'h1': return '\n\n# ' + children().trim() + '\n\n';
        case 'h2': return '\n\n## ' + children().trim() + '\n\n';
        case 'h3': return '\n\n### ' + children().trim() + '\n\n';
        case 'h4': return '\n\n#### ' + children().trim() + '\n\n';
        case 'h5': return '\n\n##### ' + children().trim() + '\n\n';
        case 'h6': return '\n\n###### ' + children().trim() + '\n\n';
        case 'p':  return '\n\n' + children().trim() + '\n\n';
        case 'br': return '  \n';
        case 'hr': return '\n\n---\n\n';
        case 'strong': case 'b': return '**' + children() + '**';
        case 'em': case 'i': return '*' + children() + '*';
        case 'code': {
          if (node.parentElement && node.parentElement.tagName === 'PRE') return children();
          return '`' + children() + '`';
        }
        case 'pre': return '\n\n```\n' + renderedText(node) + '\n```\n\n';
        case 'a': {
          const href = node.getAttribute('href') || '';
          const t = children().trim();
          return href ? '[' + t + '](' + href + ')' : t;
        }
        case 'img': {
          const src = node.getAttribute('src') || '';
          const alt = node.getAttribute('alt') || '';
          return '![' + alt + '](' + src + ')';
        }
        case 'ul': {
          let out = '\n';
          for (const li of node.querySelectorAll(':scope > li')) {
            out += '- ' + walkChildren(li).trim() + '\n';
          }
          return out + '\n';
        }
        case 'ol': {
          let out = '\n', i = 1;
          for (const li of node.querySelectorAll(':scope > li')) {
            out += (i++) + '. ' + walkChildren(li).trim() + '\n';
          }
          return out + '\n';
        }
        case 'blockquote': return '\n> ' + children().trim().replace(/\n/g, '\n> ') + '\n\n';
        case 'table': {
          let out = '\n\n';
          const rows = node.querySelectorAll('tr');
          if (rows.length === 0) return '';
          rows.forEach((row, idx) => {
            const cells = Array.from(row.querySelectorAll('th, td')).map(c => renderedText(c).replace(/\|/g,'\\|').trim());
            out += '| ' + cells.join(' | ') + ' |\n';
            if (idx === 0) out += '| ' + cells.map(() => '---').join(' | ') + ' |\n';
          });
          return out + '\n';
        }
        default: return children();
      }
    }
    const md = walk(root).replace(/\n{3,}/g, '\n\n').trim();
    const truncated = md.length > 80000;
    return { markdown: md.slice(0, 80000), length: md.length, ...(truncated && { truncated: true }) };
  }

  // ── Interaction additions ──
  function findSelectOption(el, value, byText) {
    for (const opt of el.options) {
      if (byText ? (opt.text === value || opt.text.trim() === String(value).trim()) : opt.value === value) return opt;
    }
    return null;
  }

  function applySelectOption(el, opt) {
    el.value = opt.value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }

  // Several values (browser_select_option): each matches an option's value, else its text.
  function selectOptions(el, values) {
    if (values.length > 1 && !el.multiple) return { error: 'This <select> takes one value; got ' + values.length };
    const matched = [];
    for (const value of values) {
      const opt = findSelectOption(el, value, false) || findSelectOption(el, value, true);
      if (!opt) return { error: `No matching option for value or text: ${value}` };
      matched.push(opt);
    }
    lastActedElement = lastFilledField = el;
    if (el.multiple) {
      for (const opt of el.options) opt.selected = matched.includes(opt);
      el.dispatchEvent(new Event('change', { bubbles: true }));
      el.dispatchEvent(new Event('input', { bubbles: true }));
    } else if (matched.length) {
      applySelectOption(el, matched[0]);
    }
    return { ok: true, values: matched.map(opt => opt.value), texts: matched.map(opt => opt.text) };
  }

  function selectOption(selector, value, byText, first, values) {
    const el = resolveTarget(selector, null, { first });
    if (!el) return notFound();
    if (el.tagName !== 'SELECT') return { error: 'Element is not a <select>' };
    if (Array.isArray(values)) return selectOptions(el, values);
    const matched = findSelectOption(el, value, byText);
    if (!matched) return { error: `No matching option for ${byText ? 'text' : 'value'}: ${value}` };
    lastActedElement = lastFilledField = el;
    applySelectOption(el, matched);
    return { ok: true, value: el.value, text: matched.text };
  }

  function checkBox(selector, checked, first) {
    const el = resolveTarget(selector, null, { first });
    if (!el) return notFound();
    if (el.type !== 'checkbox' && el.type !== 'radio') return { error: 'Element is not a checkbox/radio' };
    lastActedElement = lastFilledField = el;
    el.checked = !!checked;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return { ok: true, checked: el.checked };
  }

  function focusEl(selector, first) {
    const el = resolveTarget(selector, null, { first });
    if (!el) return notFound();
    lastFilledField = el;
    el.focus();
    return { ok: true, active: activeElement() === el };
  }

  function blurEl(selector, first) {
    const el = selector ? resolveTarget(selector, null, { first }) : activeElement();
    if (!el) return notFound();
    if (typeof el.blur === 'function') el.blur();
    return { ok: true };
  }

  const KEY_ALIASES = { Space: ' ', Spacebar: ' ', Esc: 'Escape', Return: 'Enter',
    Up: 'ArrowUp', Down: 'ArrowDown', Left: 'ArrowLeft', Right: 'ArrowRight' };
  const KEY_CODES = { ' ': ['Space', 32], Backspace: ['Backspace', 8], Tab: ['Tab', 9],
    Enter: ['Enter', 13], Shift: ['ShiftLeft', 16], Control: ['ControlLeft', 17],
    Alt: ['AltLeft', 18], Escape: ['Escape', 27], PageUp: ['PageUp', 33],
    PageDown: ['PageDown', 34], End: ['End', 35], Home: ['Home', 36],
    ArrowLeft: ['ArrowLeft', 37], ArrowUp: ['ArrowUp', 38], ArrowRight: ['ArrowRight', 39],
    ArrowDown: ['ArrowDown', 40], Insert: ['Insert', 45], Delete: ['Delete', 46],
    Meta: ['MetaLeft', 91] };

  function keyboardKeyOptions(key) {
    if (Object.prototype.hasOwnProperty.call(KEY_ALIASES, key)) key = KEY_ALIASES[key];
    let code = '', keyCode = 0;
    if (Object.prototype.hasOwnProperty.call(KEY_CODES, key)) {
      [code, keyCode] = KEY_CODES[key];
    } else if (/^[a-zA-Z]$/.test(key)) {
      code = 'Key' + key.toUpperCase();
      keyCode = key.toUpperCase().charCodeAt(0);
    } else if (/^[0-9]$/.test(key)) {
      code = 'Digit' + key;
      keyCode = key.charCodeAt(0);
    }
    return { key, code, keyCode, which: keyCode };
  }

  // ── Key defaults (#267) ──
  // Synthetic key events have no browser default, so keypress applies the default edit of a
  // key in an input, textarea or editor itself, unless the page cancels keydown, keypress or
  // beforeinput. It never moves focus out of the field or brings the tab forward.
  const IS_MAC = /Mac/.test(globalThis.navigator?.platform || '');
  const CARET_KEYS = new Set(['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End']);
  const BACKWARD_KEYS = new Set(['ArrowLeft', 'ArrowUp', 'Home', 'Backspace']);
  const SCROLL_KEYS = new Set([' ', 'PageUp', 'PageDown', 'Home', 'End', 'ArrowUp', 'ArrowDown']);
  // Inputs that block implicit submission of a form without a submit button.
  const IMPLICIT_SUBMIT_TYPES = new Set(['text', 'search', 'email', 'url', 'tel', 'password', 'date', 'month',
    'week', 'time', 'datetime-local', 'number']);
  const WORD_CHARACTER = /[\p{L}\p{N}\p{M}_]/u;
  const KEY_NOTES = {
    focus: 'Kitsune does not move focus for Tab; act on the next field by its ref.',
    activation: 'Kitsune does not activate a control for Enter or Space; click it instead.',
    scroll: 'Kitsune does not scroll the page for this key.',
    shortcut: 'Kitsune does not emulate clipboard or undo shortcuts.',
  };

  // The key with its modifiers as Playwright names them: Control+Shift+ArrowLeft.
  function keyCombination(key, mods) {
    return [mods.ctrl && 'Control', mods.alt && 'Alt', mods.shift && 'Shift', mods.meta && 'Meta', key]
      .filter(Boolean).join('+');
  }

  // What a key types: one character (Shift capitalizes a letter) or a space. With Control, Alt
  // or Meta it is a shortcut and types nothing.
  function typedText(key, mods) {
    if (mods.ctrl || mods.alt || mods.meta || [...key].length !== 1) return null;
    return mods.shift ? key.toUpperCase() : key;
  }

  function selectAllKey(key, mods) {
    return !!(IS_MAC ? mods.meta : mods.ctrl) && /^a$/i.test(key);
  }

  // The field a key edits: a textarea, a text-like input or the editing host of an editor.
  function keyField(el) {
    if (el.tagName === 'TEXTAREA') return el;
    if (el.tagName === 'INPUT') return NON_TEXT_INPUT_TYPES.has(String(el.type || 'text').toLowerCase()) ? null : el;
    return isEditableRegion(el) ? editingHost(el) : null;
  }

  // How far a caret or delete key reaches with the platform's modifiers: a character, a word
  // (Alt on macOS, else Control), the line boundary (Home/End; Meta+arrow/Backspace/Delete on
  // macOS), a line (Up/Down) or the whole text (Control+Home/End; Meta+Up/Down on macOS).
  function keyUnit(key, mods) {
    if (key === 'Home' || key === 'End') return !IS_MAC && mods.ctrl ? 'document' : 'lineboundary';
    if (key === 'ArrowUp' || key === 'ArrowDown') return IS_MAC && mods.meta ? 'document' : 'line';
    if (IS_MAC && mods.meta) return 'lineboundary';
    return (IS_MAC ? mods.alt : mods.ctrl) ? 'word' : 'character';
  }

  function deleteInputType(backward, unit) {
    const kind = { word: 'Word', lineboundary: 'SoftLine' }[unit] || 'Content';
    return `delete${kind}${backward ? 'Backward' : 'Forward'}`;
  }

  function lineStart(text, pos) {
    return pos > 0 ? text.lastIndexOf('\n', pos - 1) + 1 : 0;
  }

  function lineEnd(text, pos) {
    const end = text.indexOf('\n', pos);
    return end < 0 ? text.length : end;
  }

  // The grapheme boundary before or after pos.
  function graphemeOffset(text, pos, backward) {
    if (!globalThis.Intl?.Segmenter) return Math.min(text.length, Math.max(0, pos + (backward ? -1 : 1)));
    const segments = new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(text);
    let before = 0;
    for (const { index } of segments) {
      if (!backward && index > pos) return index;
      if (backward && index >= pos) break;
      before = index;
    }
    return backward ? before : text.length;
  }

  // Where a caret at pos in text goes moving one unit backward or forward. Lines are the
  // text's own: a textarea's wrapped lines are not seen.
  function textOffset(text, pos, backward, unit, multiline) {
    if (unit === 'document' || (unit === 'lineboundary' && !multiline)) return backward ? 0 : text.length;
    if (unit === 'lineboundary') return backward ? lineStart(text, pos) : lineEnd(text, pos);
    if (unit === 'line') {
      if (!multiline) return pos;
      const start = lineStart(text, pos);
      if (backward) return start ? Math.min(lineStart(text, start - 1) + pos - start, start - 1) : 0;
      const end = lineEnd(text, pos);
      return end < text.length ? Math.min(end + 1 + pos - start, lineEnd(text, end + 1)) : text.length;
    }
    if (unit === 'word') {
      let i = pos;
      const at = j => WORD_CHARACTER.test(text[j] || '');
      if (backward) {
        while (i > 0 && !at(i - 1)) i--;
        while (i > 0 && at(i - 1)) i--;
      } else {
        while (i < text.length && !at(i)) i++;
        while (i < text.length && at(i)) i++;
      }
      return i;
    }
    return graphemeOffset(text, pos, backward);
  }

  // Replace [from, to) of an input or textarea with text as an edit does: beforeinput, which
  // the page can cancel, the change, then input. maxlength caps what is inserted.
  function replaceFieldText(el, from, to, text, inputType) {
    if (el.readOnly) return { effect: null };
    const value = String(el.value ?? '');
    if (text && el.maxLength >= 0) text = text.slice(0, Math.max(0, el.maxLength - value.length + to - from));
    if (!text && from === to) return { effect: null };
    const init = { bubbles: true, composed: true, inputType, data: inputType === 'insertText' ? text : null };
    if (!el.dispatchEvent(new InputEvent('beforeinput', { ...init, cancelable: true }))) {
      return { effect: null, prevented: true };
    }
    if (typeof el.selectionStart === 'number') {
      el.setRangeText(text, from, to, 'end');
    } else {
      // email and number inputs have no selection: edit at the end.
      const next = value.slice(0, from) + text + value.slice(to);
      const setter = nativeValueSetter(el);
      if (setter) setter.call(el, next);
      else el.value = next;
    }
    el.dispatchEvent(new InputEvent('input', init));
    return { effect: inputType };
  }

  // A key's default in an input or textarea: typing replaces the selection; Backspace/Delete
  // remove it or the unit before/after the caret; caret keys move the caret or, with Shift,
  // the selection's focus; Enter adds a line to a textarea and submits an input's form.
  function fieldKey(el, key, mods, text) {
    const value = String(el.value ?? '');
    const ranged = typeof el.selectionStart === 'number';
    const start = ranged ? el.selectionStart : value.length;
    const end = ranged ? el.selectionEnd : value.length;
    const multiline = el.tagName === 'TEXTAREA';
    if (text != null) return replaceFieldText(el, start, end, text, 'insertText');
    if (key === 'Enter') return multiline ? replaceFieldText(el, start, end, '\n', 'insertLineBreak') : implicitSubmit(el);
    if (selectAllKey(key, mods)) {
      if (!ranged || (start === 0 && end === value.length)) return { effect: null };
      el.setSelectionRange(0, value.length);
      return { effect: 'selectAll' };
    }
    const unit = keyUnit(key, mods);
    const backward = BACKWARD_KEYS.has(key);
    if (key === 'Backspace' || key === 'Delete') {
      if (start !== end) return replaceFieldText(el, start, end, '', deleteInputType(backward, 'character'));
      const reach = textOffset(value, start, backward, unit, multiline);
      return replaceFieldText(el, Math.min(start, reach), Math.max(start, reach), '', deleteInputType(backward, unit));
    }
    if (!CARET_KEYS.has(key) || !ranged) return { effect: null };
    if (mods.shift) {
      const [anchor, focus] = el.selectionDirection === 'backward' ? [end, start] : [start, end];
      const next = textOffset(value, focus, backward, unit, multiline);
      if (next === focus) return { effect: null };
      el.setSelectionRange(Math.min(anchor, next), Math.max(anchor, next), next < anchor ? 'backward' : 'forward');
      return { effect: 'extendSelection' };
    }
    // An arrow first collapses a selection to its start or end.
    const next = start !== end && unit === 'character' && (key === 'ArrowLeft' || key === 'ArrowRight')
      ? (backward ? start : end) : textOffset(value, backward ? start : end, backward, unit, multiline);
    if (next === start && next === end) return { effect: null };
    el.setSelectionRange(next, next);
    return { effect: 'moveCaret' };
  }

  // Enter in a text input submits its form as a browser does (HTML implicit submission): it
  // clicks the first submit control, or submits a form that has none and no other text input.
  // A disabled submit control submits nothing.
  function implicitSubmit(el) {
    const form = el.form;
    if (!form) return { effect: null };
    const button = formSubmitControl(form);
    if (button) {
      if (button.disabled) return { effect: null };
      button.click();
      return { effect: 'implicitSubmit' };
    }
    const blocking = Array.from(form.querySelectorAll('input')).filter(input =>
      IMPLICIT_SUBMIT_TYPES.has(String(input.type || 'text').toLowerCase()));
    if (blocking.length > 1) return { effect: null };
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.submit();
    return { effect: 'implicitSubmit' };
  }

  // An editing command at an editor's selection; null when the editor did not change.
  function editorCommand(host, command, inputType) {
    const observer = new MutationObserver(() => {});
    observer.observe(host, { childList: true, characterData: true, subtree: true });
    const done = docOf(host).execCommand(command, false);
    const changed = observer.takeRecords().length > 0;
    observer.disconnect();
    return { effect: done && changed ? inputType : null };
  }

  // The first or last text node in node, itself included.
  function edgeText(node, last) {
    if (!node || node.nodeType === 3) return node || null;
    const children = Array.from(node.childNodes || []);
    for (const child of last ? children.reverse() : children) {
      const text = edgeText(child, last);
      if (text) return text;
    }
    return null;
  }

  // A selection point in text where there is text next to it (the end of the text before it,
  // else the start of the text after it), so equivalent points such as (div, 1) and the end
  // of the div's text compare equal.
  function textPoint(node, offset) {
    if (!node || node.nodeType === 3) return [node, offset];
    const before = edgeText(node.childNodes?.[offset - 1], true);
    if (before) return [before, before.length];
    const after = edgeText(node.childNodes?.[offset], false);
    return after ? [after, 0] : [node, offset];
  }

  function selectionPoints(selection) {
    return [...textPoint(selection.anchorNode, selection.anchorOffset),
      ...textPoint(selection.focusNode, selection.focusOffset)];
  }

  // A key's default in an editor: the same edits through editing commands, with the caret
  // moved by Selection.modify within the editor.
  function editorKey(host, key, mods, text) {
    const doc = docOf(host);
    const selection = (doc.defaultView || window).getSelection?.();
    if (text != null) return insertEditableText(host, text) ? { effect: 'insertText' } : { effect: null, prevented: true };
    if (key === 'Enter') {
      const command = mods.shift ? 'insertLineBreak' : 'insertParagraph';
      return editorCommand(host, command, command);
    }
    if (!selection?.rangeCount) return { effect: null };
    if (selectAllKey(key, mods)) {
      const range = doc.createRange();
      range.selectNodeContents(host);
      selection.removeAllRanges();
      selection.addRange(range);
      return { effect: 'selectAll' };
    }
    const unit = keyUnit(key, mods);
    const backward = BACKWARD_KEYS.has(key);
    const direction = backward ? 'backward' : 'forward';
    if (key === 'Backspace' || key === 'Delete') {
      const inputType = deleteInputType(backward, selection.isCollapsed ? unit : 'character');
      if (selection.isCollapsed && unit !== 'character') selection.modify('extend', direction, unit);
      return editorCommand(host, backward ? 'delete' : 'forwardDelete', inputType);
    }
    if (!CARET_KEYS.has(key)) return { effect: null };
    const before = selectionPoints(selection);
    const raw = [selection.anchorNode, selection.anchorOffset, selection.focusNode, selection.focusOffset];
    if (unit === 'document') {
      const range = doc.createRange();
      range.selectNodeContents(host);
      range.collapse(backward);
      if (mods.shift) selection.extend(range.startContainer, range.startOffset);
      else {
        selection.removeAllRanges();
        selection.addRange(range);
      }
    } else if (!mods.shift && !selection.isCollapsed && unit === 'character' &&
        (key === 'ArrowLeft' || key === 'ArrowRight')) {
      if (backward) selection.collapseToStart();
      else selection.collapseToEnd();
    } else {
      selection.modify(mods.shift ? 'extend' : 'move', direction, unit);
    }
    // A caret key does not leave the editor.
    if (!host.contains(selection.focusNode)) selection.setBaseAndExtent(...raw);
    if (selectionPoints(selection).every((value, i) => value === before[i])) return { effect: null };
    return { effect: mods.shift ? 'extendSelection' : 'moveCaret' };
  }

  // Why a key whose browser default Kitsune does not apply had no effect.
  function keyNote(target, field, key, mods) {
    if ((IS_MAC ? mods.meta : mods.ctrl) && /^[cxvyz]$/i.test(key)) return KEY_NOTES.shortcut;
    if (key === 'Tab') return KEY_NOTES.focus;
    if (field) return null;
    const control = target.closest?.('a[href], button, input, select, summary, [role="button"], [role="link"], ' +
      '[role="checkbox"], [role="switch"], [role="tab"], [role="menuitem"], [role="option"]');
    if (control && (key === 'Enter' || key === ' ')) return KEY_NOTES.activation;
    if (!control && SCROLL_KEYS.has(key)) return KEY_NOTES.scroll;
    return null;
  }

  // Dispatches keydown, keypress (for a typed character or Enter) and keyup, and applies the
  // key's default in a field (#267). effect names what changed, null if nothing did;
  // prevented: the page cancelled the key; note: a default Kitsune does not apply.
  function keypress(selector, key, modifiers, text, first) {
    const target = selector ? resolveTarget(selector, null, { first }) : (activeElement() || document.body);
    if (!target) return notFound('No target');
    lastActedElement = selector || target !== docOf(target).body ? target : null;
    const mods = modifiers || {};
    const options = keyboardKeyOptions(key);
    const opts = {
      ...options,
      bubbles: true,
      cancelable: true,
      composed: true,
      ctrlKey: !!mods.ctrl,
      shiftKey: !!mods.shift,
      altKey: !!mods.alt,
      metaKey: !!mods.meta,
    };
    const field = keyField(target);
    // As for a user's key, the field has focus, and an editor a caret: where it was if inside
    // the target, else at the target's end.
    if (field && isEditableRegion(field)) {
      const at = isEditableRegion(target) ? target : field;
      if ((docOf(at).defaultView || window).getSelection && !selectionInside(at)) placeEditableCaret(at);
    } else if (field && activeElement() !== field) {
      field.focus({ preventScroll: true });
    }
    const typed = text ?? typedText(options.key, mods);
    const plainEnter = options.key === 'Enter' && !mods.ctrl && !mods.alt && !mods.meta;
    const result = { ok: true, key: keyCombination(key, mods), target: target.tagName?.toLowerCase(), effect: null };
    let applies = target.dispatchEvent(new KeyboardEvent('keydown', opts));
    if (applies && (typed != null || plainEnter)) applies = target.dispatchEvent(new KeyboardEvent('keypress', opts));
    if (!applies) result.prevented = true;
    else if (field) Object.assign(result, isEditableRegion(field) ? editorKey(field, options.key, mods, typed)
      : fieldKey(field, options.key, mods, typed));
    const note = !applies || result.effect ? null : keyNote(target, field, options.key, mods);
    if (note) result.note = note;
    target.dispatchEvent(new KeyboardEvent('keyup', opts));
    if (result.effect && result.effect !== 'implicitSubmit') lastFilledField = field;
    return result;
  }

  function doubleClick(selector, first, modifiers) {
    const el = resolveTarget(selector, null, { first, pointer: true });
    if (!el) return notFound();
    el.scrollIntoView({ behavior: 'instant', block: 'center' });
    const r = el.getBoundingClientRect();
    const init = { bubbles: true, cancelable: true, composed: true, view: el.ownerDocument?.defaultView || window,
      clientX: r.x + r.width/2, clientY: r.y + r.height/2, button: 0, ...modifierFlags(modifiers) };
    for (const type of ['mousedown', 'mouseup', 'click', 'mousedown', 'mouseup', 'click']) {
      el.dispatchEvent(new MouseEvent(type, init));
    }
    el.dispatchEvent(new MouseEvent('dblclick', { ...init, detail: 2 }));
    return { ok: true, ...notRenderedWarning(el) };
  }

  const NON_TEXT_INPUT_TYPES = new Set(['button', 'checkbox', 'radio', 'file', 'reset', 'hidden',
    'color', 'range', 'submit', 'image']);

  const BUTTON_INPUT_TYPES = new Set(['button', 'submit', 'image']);
  const ALTERNATIVE_NAMED_TAGS = new Set(['IMG', 'AREA', 'SVG']);

  // Ask-before-act: button labels that commit an order, payment, or form submission, for
  // JS-driven buttons that are not structural submit controls. The whole label (after
  // dropping an amount, a parenthetical, and edge punctuation) must be one of these
  // phrases, so "Submit search", "Order history", "Add to cart" or a bare "OK"/"확인"
  // do not match. Links and other non-button elements are never matched.
  const SUBMIT_LABEL_PATTERNS = [
    /^(?:place|submit|confirm|complete|finish)(?: (?:your|my|the))? (?:order|purchase|payment|booking|reservation)$/,
    /^(?:pay|buy|purchase|order|checkout)(?: it)? now$/,
    /^(?:pay|pay securely|pay with [\p{L} ]{1,20}|buy|purchase|submit|confirm(?: and pay| payment| purchase)?)$/u,
    /^(?:(?:proceed|continue|go) to )?(?:checkout|check out)$/,
    /^(?:proceed|continue|go) to payment$/,
    // Korean, Japanese, Chinese (simplified and traditional)
    /^(?:(?:지금|바로) ?)?(?:결제|주문|구매|제출)(?: ?(?:진행|완료|확정))?(?:하기)?$/,
    /^(?:今すぐ)?(?:注文|購入|決済|支払[いう]?|お支払い)(?:する|を確定する|手続きへ|に進む|へ進む)?$/,
    /^レジに進む$/,
    /^(?:立即|确认|確認)?(?:支付|付款|购买|購買|下单|下單)$/,
    /^(?:提交(?:订单|訂單)?|(?:确认|確認)(?:订单|訂單))$/,
    /^(?:去)?(?:结算|結帳|結算)$/,
    // German, French, Spanish
    /^(?:jetzt )?(?:kaufen|bezahlen|bestellen)$/,
    /^(?:zahlungspflichtig|kostenpflichtig) bestellen$/,
    /^zur kasse(?: gehen)?$/,
    /^(?:payer|acheter|commander)(?: maintenant)?$/,
    /^(?:passer|valider|confirmer) (?:la|ma) commande$/,
    /^(?:pagar|comprar)(?: ahora)?$/,
    /^(?:realizar|confirmar|tramitar) (?:el )?pedido$/,
    /^finalizar (?:la )?compra$/,
  ];

  // Ask-before-act: button and link labels that send, transfer, delete, or change an
  // account. Same whole-label rule as SUBMIT_LABEL_PATTERNS, so "Send feedback",
  // "Transfer history", "Remove filter", "Subscribe" or a bare "Cancel"/"취소" do not match.
  const CONSEQUENTIAL_LABEL_PATTERNS = [
    /^(?:send|transfer|wire)(?: (?:it|money|funds|payment))?(?: now)?$/,
    /^(?:confirm|complete|make)(?: the| a)? (?:transfer|wire transfer)$/,
    /^confirm and send$/,
    /^(?:delete|remove|erase)(?: (?:permanently|forever|all|everything))?$/,
    /^(?:permanently )?(?:delete|remove|erase)(?: (?:my|your|the|this|all))? (?:account|profile|data|repository|project|workspace)$/,
    /^(?:close|deactivate|terminate|cancel)(?: (?:my|your|the|this))? account$/,
    /^(?:cancel|end)(?: (?:my|your|the|this))? (?:order|booking|reservation|subscription|membership|plan)$/,
    /^unsubscribe(?: (?:from all|from everything|me|all))?$/,
    // Korean, Japanese, Chinese (simplified and traditional)
    /^(?:(?:지금|바로) ?)?(?:송금|이체|전송)(?: ?하기)?$/,
    /^보내기$/,
    /^(?:(?:계정|회원|영구) ?)?(?:삭제|제거)(?: ?하기)?$/,
    /^(?:회원 ?)?탈퇴(?: ?하기)?$/,
    /^(?:구독|멤버십|정기 ?결제|주문) ?(?:해지|취소)(?: ?하기)?$/,
    /^해지(?: ?하기)?$/,
    /^수신 ?거부(?: ?하기)?$/,
    /^(?:送金|振込|振り込み|送信)(?:する)?$/,
    /^振り込む$/,
    /^(?:アカウント(?:を)?)?削除(?:する)?$/,
    /^(?:退会|解約|配信停止|購読解除)(?:する)?$/,
    /^(?:サブスクリプション|定期購入|プラン|注文)を?(?:解約|キャンセル)(?:する)?$/,
    /^(?:立即|确认|確認)?(?:发送|發送|转账|轉帳|轉賬|汇款|匯款)$/,
    /^(?:永久)?(?:删除|刪除|移除)(?:账号|账户|帐号|帳號|帳戶)?$/,
    /^(?:注销|註銷)(?:账号|账户|帐号|帳號|帳戶)?$/,
    /^(?:退订|退訂|取消(?:订阅|訂閱|订单|訂單))$/,
    // German, French, Spanish
    /^(?:jetzt )?(?:senden|absenden|überweisen|löschen|entfernen|kündigen|abbestellen)$/,
    /^(?:konto|account|profil) (?:löschen|schließen|kündigen)$/,
    /^(?:abo|abonnement|vertrag|mitgliedschaft|newsletter) (?:kündigen|abbestellen)$/,
    /^überweisung (?:absenden|ausführen|bestätigen)$/,
    /^(?:envoyer|transférer|supprimer|résilier)(?: maintenant)?$/,
    /^(?:supprimer|fermer|clôturer|désactiver) (?:mon |le |votre )?compte$/,
    /^(?:effectuer|confirmer|valider) (?:le )?(?:virement|transfert)$/,
    /^(?:se )?(?:désabonner|désinscrire)$/,
    /^(?:résilier|annuler) (?:mon |votre |l['’] ?)?abonnement$/,
    /^(?:enviar|transferir|eliminar|borrar)(?: ahora)?$/,
    /^enviar dinero$/,
    /^(?:realizar|confirmar|hacer) (?:la )?transferencia$/,
    /^(?:eliminar|borrar|cerrar|desactivar) (?:mi |la |tu |su )?cuenta$/,
    /^dar(?:se|me)? de baja$/,
    /^(?:cancelar|anular) (?:mi |la |tu |su )?(?:suscripción|membresía)$/,
  ];

  // A label folded for whole-phrase matching: case, width and whitespace folded, a trailing
  // parenthetical, an amount and edge punctuation dropped. Over 60 characters never matches.
  function normalizedLabel(label) {
    let text = String(label).normalize('NFKC').toLowerCase().replace(/\s+/g, ' ').trim();
    if (!text || text.length > 60) return '';
    const edges = /^[\p{P}\p{S}\s]+|[\p{P}\p{S}\s]+$/gu;
    return text.replace(/\s*\([^)]*\)$/, '').replace(edges, '')
      .replace(/(?:^|\s)\p{Sc}?\s?\d[\d.,]*\s?(?:\p{Sc}|원|円|元|krw|usd|eur)?$/u, '')
      .replace(/^\p{Sc}?\s?\d[\d.,]*\s?(?:\p{Sc}|원|円|元)?\s/u, '')
      .replace(edges, '');
  }

  function isSubmitLabel(label) {
    const text = normalizedLabel(label);
    return !!text && SUBMIT_LABEL_PATTERNS.some(pattern => pattern.test(text));
  }

  function isConsequentialLabel(label) {
    const text = normalizedLabel(label);
    return !!text && CONSEQUENTIAL_LABEL_PATTERNS.some(pattern => pattern.test(text));
  }

  // The accessible name of a button or link, for icon-only controls too: aria-labelledby,
  // aria-label, text, a button input's value or alt, an icon's aria-label, alt or SVG title,
  // then title.
  function controlLabel(control, buttonInput) {
    const attr = name => String(control.getAttribute?.(name) || '').trim();
    const labelledBy = labelledByText(control);
    const text = redactedText(control).trim();
    const icon = !text && control.querySelector?.('[aria-label], img[alt], svg title');
    const iconLabel = icon ? String(icon.getAttribute('aria-label') || icon.getAttribute('alt') ||
      icon.textContent || '').trim() : '';
    return labelledBy || attr('aria-label') || text ||
      (buttonInput ? (isSensitiveField(control) ? '' : String(control.value || '').trim()) || attr('alt') : '') ||
      iconLabel || attr('title');
  }

  function labelledByText(el) {
    return String(el.getAttribute?.('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => { const label = elementByIdNear(el, id); return label ? redactedText(label) : ''; })
      .join(' ').trim();
  }

  // Images, areas and SVG graphics are named by their alternative text, not their content:
  // aria-labelledby, aria-label, alt (an <svg>'s <title>), then title unless alt="" marks
  // the image decorative. Snapshots name images the same way.
  function alternativeName(el) {
    const attr = name => String(el.getAttribute(name) || '').trim();
    const svgTitle = el.tagName.toLowerCase() === 'svg' &&
      Array.from(el.children).find(child => child.tagName.toLowerCase() === 'title');
    return labelledByText(el) || attr('aria-label') || attr('alt') ||
      (svgTitle ? redactedText(svgTitle).trim() : '') || (el.hasAttribute('alt') ? '' : attr('title'));
  }

  // Ask-before-act: whether clicking the target (or pressing Enter on it, with key) can submit
  // a form, is a button labeled like a submit or payment, or is a button or link labeled like
  // a send, transfer, delete or account change. Resolves the target the way click/keypress
  // do and reports no field values.
  function describeTarget(selector, coords, key, first, fieldKeys, draftFill) {
    if (fieldKeys) {
      const fields = fieldKeys.map(key => resolveFormField(key, first));
      if (fields.some(field => !field)) return notFound();
      return { ok: true, sensitive: fields.some(field => isSensitiveField(getFillTarget(field))) };
    }
    let el = selector || coords ? resolveTarget(selector, coords, { first, pointer: !key })
      : (activeElement() || document.body);
    if (!el) return notFound();
    if (coords) el = nearestInteractiveAncestor(el) || el;
    const control = el.closest ? el.closest('button, input, [role="button"]') ||
      el.closest('a[href], [role="link"]') || el : el;
    const tag = control.tagName?.toLowerCase();
    const type = String(control.type || '').toLowerCase();
    const role = String(control.getAttribute?.('role') || '').toLowerCase();
    const submitControl = !!control.form &&
      ((tag === 'button' && type === 'submit') || (tag === 'input' && (type === 'submit' || type === 'image')));
    const enterSubmits = keyboardKeyOptions(key).key === 'Enter' && !!control.form && tag === 'input' && !NON_TEXT_INPUT_TYPES.has(type);
    const buttonInput = tag === 'input' && BUTTON_INPUT_TYPES.has(type);
    const button = tag === 'button' || buttonInput || role === 'button';
    const link = !button && ((tag === 'a' && !!control.hasAttribute?.('href')) || role === 'link');
    const label = button || link ? controlLabel(control, buttonInput)
      : String(control.getAttribute?.('aria-label') || redactedText(control) || '').trim();
    return { ok: true, tag, type, ...(draftFill ? { sensitive: isSensitiveField(fillDestination(el)) } : {}),
      inForm: !!control.form, submits: submitControl || enterSubmits,
      submitLabel: button && isSubmitLabel(label), consequentialLabel: (button || link) && isConsequentialLabel(label),
      text: label.slice(0, 80) };
  }

  // The form's first submit control. A <button> without a type (or with an unknown one) is a
  // submit button, and its type property reads "submit".
  function formSubmitControl(form) {
    return Array.from(form.querySelectorAll('button, input')).find(el =>
      (el.tagName === 'BUTTON' && el.type === 'submit') ||
      (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'image'))) || null;
  }

  // A form as submit results and ambiguous_target candidates name it, with a new ref in the
  // current generation. Attributes, not properties: a field named "action" shadows form.action.
  function formSummary(form, button) {
    button ??= formSubmitControl(form);
    return { ref: issueRef(form), action: form.getAttribute('action') || '',
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      button: button ? controlLabel(button, button.tagName === 'INPUT').slice(0, 80) : '' };
  }

  // Without a target: the form of the field last filled or focused, else the only form.
  function submitForm(selector, first) {
    let form;
    if (selector) {
      const el = resolveTarget(selector, null, { first });
      if (!el) return notFound();
      lastActedElement = el;
      form = el.tagName === 'FORM' ? el : el.closest('form');
    } else {
      form = lastFilledField?.isConnected ? lastFilledField.form || lastFilledField.closest('form') : null;
      if (!form) {
        const forms = deepQuerySelectorAll('form');
        if (forms.length > 1) {
          return ambiguousTarget(`${forms.length} forms on the page and no filled field names one; nothing was submitted`,
            'Pass the ref of the form to submit from state.candidates, or fill one of its fields first.',
            { forms: forms.length, candidates: forms.slice(0, AMBIGUOUS_CANDIDATE_LIMIT).map(f => formSummary(f)) });
        }
        form = forms[0];
      }
    }
    if (!form) return { error: 'No <form> found' };
    // Click the submit control, else requestSubmit, so submit handlers and validation run as
    // for a user's submit. form.submit() skips both and is the last resort.
    const btn = formSubmitControl(form);
    // Named before submitting, which can remove the form.
    const submitted = formSummary(form, btn);
    if (btn) { btn.click(); return { ok: true, submitted: 'button', form: submitted }; }
    if (typeof form.requestSubmit === 'function') {
      form.requestSubmit();
      return { ok: true, submitted: 'requestSubmit', form: submitted };
    }
    form.submit();
    return { ok: true, submitted: 'method', form: submitted };
  }

  // Strict boolean parsing for checkbox/radio values: JS truthiness would turn "false" into checked.
  const FORM_TRUE_VALUES = new Set(['true', 'on', 'yes', '1']);
  const FORM_FALSE_VALUES = new Set(['false', 'off', 'no', '0']);
  function parseFormBoolean(value) {
    if (typeof value === 'boolean') return value;
    if (value === 1 || value === 0) return value === 1;
    if (typeof value !== 'string') return undefined;
    const text = value.trim().toLowerCase();
    if (FORM_TRUE_VALUES.has(text)) return true;
    if (FORM_FALSE_VALUES.has(text)) return false;
    return undefined;
  }

  // Collect every label match, else every placeholder match, per searched tree. Multiple
  // labels for the same control name one target, not several.
  function fieldsByLabelText(key) {
    const text = key.toLowerCase();
    const fields = new Set();
    for (const root of searchedDocuments().flatMap(doc => [doc, ...shadowRootsIn(doc)])) {
      const labeled = [];
      for (const lbl of root.querySelectorAll('label')) {
        if (!(lbl.innerText || '').trim().toLowerCase().includes(text)) continue;
        const el = lbl.htmlFor ? root.getElementById(lbl.htmlFor) : lbl.querySelector('input, textarea, select');
        if (el) labeled.push(el);
      }
      const matches = labeled.length ? labeled
        : Array.from(root.querySelectorAll('input[placeholder], textarea[placeholder]'))
          .filter(inp => inp.placeholder.toLowerCase().includes(text));
      for (const el of matches) fields.add(el);
    }
    return [...fields];
  }

  function resolveFormField(key, first) {
    let el = null;
    for (const selector of [key, `[name="${CSS.escape(key)}"]`]) {
      try { el = deepQuerySelector(selector); } catch {}
      if (el && !first) assertOneTarget(selector, false);
      if (el) return el;
    }
    const matches = fieldsByLabelText(key);
    const actionable = matches.filter(field => isActionable(field, false));
    if (actionable.length > 1 && !first) {
      const result = ambiguousTarget(
        `${actionable.length} fields match the label or placeholder; nothing was written`,
        'Fill one of them with browser_type and its ref from state.candidates, or key it with a CSS ' +
          'selector or name that matches one field. first: true fills the first match.',
        { field: key, matches: actionable.length,
          candidates: actionable.slice(0, AMBIGUOUS_CANDIDATE_LIMIT).map(targetSummary) });
      throw new ActionError(result.error, result);
    }
    return matches[0] || null;
  }

  function formFill(fieldMap, first) {
    if (!fieldMap || typeof fieldMap !== 'object') return { error: 'fields must be an object' };
    const results = {};
    for (const key of Object.keys(fieldMap)) {
      const value = fieldMap[key];
      let el;
      try { el = resolveFormField(key, first); }
      catch (e) { results[key] = errorResult(e); continue; }
      if (!el) { results[key] = { error: 'Not found' }; continue; }
      // The same lookup as browser_type's fill mode, through nested open and closed shadow roots.
      const field = getFillTarget(el);
      if (!FILL_TAGS.includes(field.tagName)) { results[key] = notFillable(el, 'input, textarea or select'); continue; }
      el = field;
      try {
        if (el.tagName === 'SELECT') {
          // Assigning an unknown value would silently clear the selection.
          const matched = findSelectOption(el, String(value), false);
          if (!matched) { results[key] = { error: `No matching option for value: ${value}` }; continue; }
          applySelectOption(el, matched);
          results[key] = { ok: true, tag: 'select', value: el.value };
        } else if (el.type === 'checkbox' || el.type === 'radio') {
          const checked = parseFormBoolean(value);
          if (checked === undefined) {
            results[key] = { error: `Invalid ${el.type} value: ${JSON.stringify(value)} (expected true/false, "true"/"false", "on"/"off", "yes"/"no" or "1"/"0")` };
            continue;
          }
          if (el.type === 'radio' && !checked) {
            // Unchecking would leave the group with no selection; select another radio instead.
            if (el.checked) { results[key] = { error: 'Cannot uncheck a selected radio; set another option in its group to true' }; continue; }
            results[key] = { ok: true, tag: el.tagName.toLowerCase(), checked: false };
            continue;
          }
          el.checked = checked;
          el.dispatchEvent(new Event('change', { bubbles: true }));
          results[key] = { ok: true, tag: el.tagName.toLowerCase(), checked: el.checked };
        } else {
          el.focus();
          const setter = nativeValueSetter(el);
          if (setter) setter.call(el, String(value));
          else el.value = String(value);
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          results[key] = { ok: true, tag: el.tagName.toLowerCase() };
        }
      } catch (e) {
        results[key] = { error: e.message };
      }
      if (results[key].ok) lastFilledField = el;
    }
    // Any filled field makes this a partial success; with none, nothing was written.
    const values = Object.values(results);
    if (values.length && values.every(result => result.error)) return { error: 'No field was filled', results };
    return { results };
  }

  // Pointer drags move in steps so libraries with a drag threshold (a few px) start.
  const DRAG_POINTER_STEPS = 10;

  // A task boundary background tabs do not throttle, so page frameworks render between moves.
  function nextTask() {
    return new Promise(resolve => {
      const channel = new MessageChannel();
      channel.port1.onmessage = () => { channel.port1.close(); resolve(); };
      channel.port2.postMessage(null);
    });
  }

  async function dragElement(fromSel, toSel, first) {
    const from = resolveTarget(fromSel, null, { first, pointer: true });
    const to = resolveTarget(toSel, null, { first, pointer: true });
    if (!from) return notFound('from element not found');
    if (!to) return notFound('to element not found');
    const fr = from.getBoundingClientRect();
    const tr = to.getBoundingClientRect();
    const fx = fr.x + fr.width/2, fy = fr.y + fr.height/2;
    const tx = tr.x + tr.width/2, ty = tr.y + tr.height/2;
    const dt = new DataTransfer();
    const dispatch = (target, type, x, y, related) => {
      const ev = new DragEvent(type, { bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, dataTransfer: dt, relatedTarget: related || null });
      return target.dispatchEvent(ev);
    };
    const press = (target, type, x, y) => {
      dispatchPointerOrMouse(target, type, {
        bubbles: true, cancelable: true, composed: true, view: target.ownerDocument?.defaultView || window,
        clientX: x, clientY: y, button: 0, buttons: type.endsWith('up') ? 0 : 1,
      });
    };
    press(from, 'pointerdown', fx, fy);
    press(from, 'mousedown', fx, fy);
    // As natively, an HTML5 drag needs a draggable source whose dragstart the page let
    // through and a drop target that accepted it by cancelling dragenter, dragover or drop.
    const draggable = from.draggable || !!from.closest('[draggable="true"]');
    const started = dispatch(from, 'dragstart', fx, fy);
    dispatch(from, 'drag', fx, fy);
    const accepted = [dispatch(to, 'dragenter', tx, ty), dispatch(to, 'dragover', tx, ty), dispatch(to, 'drop', tx, ty)]
      .includes(false);
    dispatch(from, 'dragend', tx, ty);
    if (draggable && started && accepted) {
      press(to, 'pointerup', tx, ty);
      press(to, 'mouseup', tx, ty);
      return { ok: true, from: fromSel, to: toSel, via: 'html5' };
    }
    // Pointer events stay on the source, where a pointerdown handler's setPointerCapture
    // routes them (the page's capture is invisible here); they bubble to document listeners.
    // The compat mouseup still lands on the destination.
    for (let step = 1; step <= DRAG_POINTER_STEPS; step++) {
      await nextTask();
      const x = fx + (tx - fx) * step / DRAG_POINTER_STEPS, y = fy + (ty - fy) * step / DRAG_POINTER_STEPS;
      press(from, 'pointermove', x, y);
      press(from, 'mousemove', x, y);
    }
    await nextTask();
    press(from, 'pointerup', tx, ty);
    press(to, 'mouseup', tx, ty);
    return { ok: true, from: fromSel, to: toSel, via: 'pointer' };
  }

  // ── waitForUrl / waitForTitle ──
  function waitForUrl(pattern, timeout) {
    const max = timeout || 10000;
    const start = Date.now();
    const re = new RegExp(pattern);
    return new Promise((resolve) => {
      const tick = () => {
        if (re.test(location.href)) return resolve({ ok: true, url: location.href, elapsed: Date.now() - start });
        if (Date.now() - start >= max) return resolve({ ok: false, url: location.href, error: 'Timed out' });
        setTimeout(tick, 200);
      };
      tick();
    });
  }

  function waitForTitle(pattern, timeout) {
    const max = timeout || 10000;
    const start = Date.now();
    const re = new RegExp(pattern);
    return new Promise((resolve) => {
      const tick = () => {
        if (re.test(document.title)) return resolve({ ok: true, title: document.title, elapsed: Date.now() - start });
        if (Date.now() - start >= max) return resolve({ ok: false, title: document.title, error: 'Timed out' });
        setTimeout(tick, 200);
      };
      tick();
    });
  }

  // ── Network: PerformanceObserver-based capture + idle wait ──
  const __netState = window.__zenlinkNet = window.__zenlinkNet || { entries: [], observer: null, capture: false };

  function ensureNetObserver() {
    if (__netState.observer) return;
    try {
      __netState.observer = new PerformanceObserver((list) => {
        if (!__netState.capture) return;
        for (const e of list.getEntries()) {
          __netState.entries.push({
            url: e.name,
            initiator: e.initiatorType,
            startTime: Math.round(e.startTime),
            duration: Math.round(e.duration),
            transferSize: e.transferSize,
            responseStatus: e.responseStatus,
          });
          if (__netState.entries.length > 1000) __netState.entries.shift();
        }
      });
      __netState.observer.observe({ type: 'resource', buffered: true });
    } catch (e) {
      // PerformanceObserver may not be available everywhere
    }
  }

  function captureNetwork(op, since) {
    ensureNetObserver();
    if (op === 'start') {
      __netState.capture = true;
      __netState.entries.length = 0;
      return { ok: true, capturing: true };
    }
    if (op === 'stop') {
      __netState.capture = false;
      return { ok: true, capturing: false, count: __netState.entries.length };
    }
    if (op === 'clear') {
      __netState.entries.length = 0;
      return { ok: true, cleared: true };
    }
    if (op === 'all') {
      // Every request since the page loaded, whether or not capture was started.
      const entries = performance.getEntriesByType('navigation').concat(performance.getEntriesByType('resource'));
      return { count: entries.length, entries: entries.slice(-1000).map(e => ({
        url: e.name, initiator: e.initiatorType, startTime: Math.round(e.startTime),
        duration: Math.round(e.duration), transferSize: e.transferSize, responseStatus: e.responseStatus,
      })) };
    }
    // Default: read
    const sinceMs = since || 0;
    const entries = sinceMs ? __netState.entries.filter(e => e.startTime >= sinceMs) : __netState.entries.slice();
    return { capturing: __netState.capture, count: entries.length, entries: entries.slice(-500) };
  }

  function waitForNetworkIdle(idleMs, timeout) {
    ensureNetObserver();
    const idleThreshold = idleMs || 500;
    const max = timeout || 10000;
    return new Promise((resolve) => {
      const start = Date.now();
      let lastSize = 0, idleSince = Date.now();
      const tick = () => {
        const cur = performance.getEntriesByType('resource').length;
        if (cur !== lastSize) {
          lastSize = cur;
          idleSince = Date.now();
        }
        if (Date.now() - idleSince >= idleThreshold) {
          return resolve({ ok: true, elapsed: Date.now() - start, resourceCount: cur });
        }
        if (Date.now() - start >= max) {
          return resolve({ ok: false, elapsed: Date.now() - start, resourceCount: cur, error: 'Timed out' });
        }
        setTimeout(tick, 100);
      };
      tick();
    });
  }

  // ── Console intercept ──
  const __conState = window.__zenlinkConsole = window.__zenlinkConsole || { enabled: false, logs: [], patched: false };

  function recordConsole(lvl, args) {
    try {
      __conState.logs.push({
        level: lvl,
        time: Date.now(),
        message: Array.from(args, a => {
          try { return typeof a === 'string' ? a : JSON.stringify(a); } catch { return String(a); }
        }).join(' ').slice(0, 2000),
      });
      if (__conState.logs.length > 500) __conState.logs.shift();
    } catch {}
  }

  function watchConsole(enabled) {
    __conState.enabled = !!enabled;
    if (enabled && !__conState.patched) {
      __conState.patched = true;
      // The page's own console: a content script's console is a separate object, so
      // patching only that one never saw the page's messages.
      const pageConsole = typeof exportFunction === 'function' ? window.wrappedJSObject?.console : null;
      for (const lvl of ['log','info','warn','error','debug']) {
        if (pageConsole) {
          const original = pageConsole[lvl];
          exportFunction(function (...args) {
            recordConsole(lvl, args);
            return original.apply(pageConsole, args);
          }, pageConsole, { defineAs: lvl });
          continue;
        }
        const orig = console[lvl].bind(console);
        console[lvl] = function(...args) {
          recordConsole(lvl, args);
          orig(...args);
        };
      }
      window.addEventListener('error', (e) => {
        __conState.logs.push({ level: 'error', time: Date.now(), message: (e.message || 'error') + ' at ' + (e.filename || '') + ':' + (e.lineno || 0) });
        if (__conState.logs.length > 500) __conState.logs.shift();
      });
    }
    return { ok: true, enabled: __conState.enabled, buffered: __conState.logs.length };
  }

  function consoleLogs(since, level) {
    let logs = __conState.logs.slice();
    if (since) logs = logs.filter(l => l.time >= since);
    if (level) logs = logs.filter(l => l.level === level);
    return { count: logs.length, logs: logs.slice(-200) };
  }

  // ── localStorage / sessionStorage ──
  function storageOp(kind, op, key, value) {
    const store = kind === 'session' ? window.sessionStorage : window.localStorage;
    try {
      if (op === 'get') return { ok: true, key, value: store.getItem(key) };
      if (op === 'set') { store.setItem(key, String(value)); return { ok: true }; }
      if (op === 'remove') { store.removeItem(key); return { ok: true }; }
      if (op === 'clear') { store.clear(); return { ok: true }; }
      if (op === 'list') {
        const out = {};
        for (let i = 0; i < store.length; i++) {
          const k = store.key(i);
          out[k] = (store.getItem(k) || '').slice(0, 500);
        }
        return { ok: true, count: Object.keys(out).length, items: out };
      }
      if (op === 'snapshot') {
        const out = {};
        for (let i = 0; i < store.length; i++) {
          const k = store.key(i);
          out[k] = store.getItem(k);
        }
        return { ok: true, items: out };
      }
      if (op === 'restore') {
        // value is expected to be an object
        store.clear();
        for (const k of Object.keys(value || {})) store.setItem(k, String(value[k]));
        return { ok: true, restored: Object.keys(value || {}).length };
      }
      return { error: 'Unknown storage op: ' + op };
    } catch (e) {
      return { error: e.message };
    }
  }

  // ── iframes ──
  function getIframes() {
    const out = [];
    for (const f of document.querySelectorAll('iframe, frame')) {
      const r = f.getBoundingClientRect();
      let accessible = false, sameOrigin = false;
      try { sameOrigin = !!f.contentDocument; accessible = sameOrigin; } catch {}
      out.push({
        src: f.src || f.getAttribute('src') || '',
        name: f.name || f.getAttribute('name') || '',
        id: f.id || '',
        sameOrigin,
        accessible,
        bounds: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      });
    }
    return { count: out.length, iframes: out };
  }

  // ── explain selector ──
  function explainSelector(selector) {
    if (!selector) return { error: 'selector required' };
    if (valueSelectorBlocked(selector)) return { error: VALUE_SELECTOR_ERROR };
    let nodes;
    try { nodes = document.querySelectorAll(selector); } catch (e) { return { error: 'Invalid: ' + e.message }; }
    const samples = [];
    for (let i = 0; i < Math.min(nodes.length, 5); i++) {
      const el = nodes[i];
      const r = el.getBoundingClientRect();
      samples.push({
        tag: el.tagName.toLowerCase(),
        text: renderedText(el).trim().slice(0, 120),
        id: el.id || undefined,
        cls: typeof el.className === 'string' ? el.className.slice(0, 100) : undefined,
        visible: !isHidden(el),
        bounds: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      });
    }
    // Suggest a more-specific selector if there are too many matches
    const suggestions = [];
    if (nodes.length > 5) {
      const first = nodes[0];
      if (first.id) suggestions.push('#' + first.id);
      if (first.dataset && Object.keys(first.dataset).length) {
        const k = Object.keys(first.dataset)[0];
        suggestions.push(`[data-${k}="${first.dataset[k]}"]`);
      }
      if (first.getAttribute('role')) suggestions.push(`[role="${first.getAttribute('role')}"]`);
    }
    return { selector, matches: nodes.length, samples, suggestions };
  }

  function fullPageMetrics() {
    return {
      docWidth: document.documentElement.scrollWidth,
      docHeight: document.documentElement.scrollHeight,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      devicePixelRatio: window.devicePixelRatio || 1,
    };
  }

})();
