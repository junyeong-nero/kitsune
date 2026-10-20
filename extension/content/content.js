// Kitsune - Content Script
// Handles all DOM interaction on web pages

(function() {
  'use strict';
  const CONTENT_VERSION = 24;
  if (window.__claudeBridgeVersion >= CONTENT_VERSION) return;
  window.__claudeBridgeVersion = CONTENT_VERSION;

  // scripts/build_extension.py sets 'signed' and strips @kitsune-dev-only regions:
  // Mozilla policy forbids running code that is not part of the signed package.
  const KITSUNE_BUILD = 'dev';
  const DEV_BUILD_ACTIONS = new Set(['executeJS', 'waitForResult']);
  const DEV_BUILD_REQUIRED = 'Agent-supplied JavaScript needs the Kitsune developer extension build; ' +
    'the signed build cannot run it (Mozilla add-on policy). See docs/installation.md#developer-build';

  // Install page-world functions before page scripts run. The script-element path is
  // shared with fillViaPageContext; Firefox exports also work under strict page CSP.
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

  // Element reference map (capped to prevent memory leaks in long sessions)
  const REF_MAP_LIMIT = 10000;
  const refMap = {};
  let refCounter = 0;
  let lastActedElement = null;
  // Persist across reinjection; seed per document so navigation cannot reuse refs.
  let refGeneration = window.__kitsuneRefGeneration ||
    `${Date.now()}${crypto.getRandomValues(new Uint32Array(1))[0]}`;
  // Generations in [refSeed, refGeneration] belong to this document; others name another one.
  const refSeed = window.__kitsuneRefSeed || (window.__kitsuneRefSeed = refGeneration);

  function newRefGeneration() {
    refGeneration = (BigInt(refGeneration) + 1n).toString();
    window.__kitsuneRefGeneration = refGeneration;
    refCounter = 0;
    for (const key of Object.keys(refMap)) delete refMap[key];
  }

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

  function errorResult(e) {
    return e instanceof ActionError ? { error: e.message, ...e.details } : { error: e.message };
  }

  function notFound(message = 'Element not found') {
    return actionError(message, 'not_found',
      'No element matches; take a new browser_snapshot, or browser_wait_for the element if the page is still updating.');
  }

  const STALE_REF_HINTS = {
    element_removed: 'The element was removed from the page; take a new browser_snapshot and use its refs.',
    not_in_snapshot: 'The latest snapshot did not issue this ref; take a new browser_snapshot and use its refs.',
    snapshot_replaced: 'A newer snapshot replaced this ref; use refs from the latest browser_snapshot.',
    other_document: 'The ref belongs to another document: the tab navigated or a different tab is targeted. ' +
      'Pass tab_id and take a new browser_snapshot.',
  };

  function staleRef(ref, el) {
    const generation = /^s(\d+)e\d+$/.exec(ref)?.[1];
    let reason = 'snapshot_replaced';  // legacy r<n> refs predate generations
    if (generation === refGeneration) reason = el ? 'element_removed' : 'not_in_snapshot';
    else if (generation && (BigInt(generation) < BigInt(refSeed) ||
        BigInt(generation) > BigInt(refGeneration))) reason = 'other_document';
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
            el = resolveElement(selector);
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

  function pruneRefMap() {
    const keys = Object.keys(refMap);
    if (keys.length > REF_MAP_LIMIT) {
      // Drop oldest half
      keys.slice(0, Math.floor(keys.length / 2)).forEach(k => delete refMap[k]);
    }
  }

  // ── Message Handler ──
  browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    const handlers = {
      'ping':                  () => ({ ok: true, version: CONTENT_VERSION }),
      'getPageInfo':           () => getPageInfo(),
      'handleDialog':          () => handleDialog(msg.accept, msg.prompt_text),
      'waitForText':           () => waitForText(msg.text, msg.text_gone, msg.timeout),
      'getPageText':           () => getPageText(msg.selector),
      'getSnapshot':           () => getSnapshot(msg.maxChars, msg.focusLastAction, msg.ref || msg.selector, msg.maxDepth),
      'getAccessibilityTree':  () => getAccessibilityTree(msg.depth || 6),
      'getFormFields':         () => getFormFields(),
      'click':                 () => clickElement(msg.selector, msg.coords),
      'type':                  () => typeText(msg.selector, msg.text, msg.clear),
      'setEditableContent':    () => setEditableContent(msg.selector, msg.value, msg.format || 'text', msg.clear !== false),
      'scroll':                () => scrollPage(msg.direction, msg.amount, msg.selector),
      'hover':                 () => hoverElement(msg.selector, msg.coords),
      'fill':                  () => fillFieldSafe(msg.selector, msg.value),
      'find':                  () => findElements(msg.query),
      // @kitsune-dev-only:start
      'executeJS':             () => executeJS(msg.code),
      'waitForResult':         () => waitForResult(msg.code, msg.timeout, msg.pollInterval),
      // @kitsune-dev-only:end
      'highlight':             () => highlightElement(msg.selector),
      'clearHighlight':        () => clearHighlight(),
      'waitForElement':        () => waitForElement(msg.selector, msg.timeout, msg.pollInterval),
      // Fixed operations the bridge and background used to send as code strings.
      'keepAlive':             () => ({ ok: true }),
      'neuterUnload':          () => neuterUnload(),
      'screenPoint':           () => screenPoint(msg.selector, msg.x, msg.y),
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
      'selectOption':          () => selectOption(msg.selector, msg.value, msg.byText),
      'checkBox':              () => checkBox(msg.selector, msg.checked),
      'focusElement':          () => focusEl(msg.selector),
      'blurElement':           () => blurEl(msg.selector),
      'keypress':              () => keypress(msg.selector, msg.key, msg.modifiers, msg.text),
      'doubleClick':           () => doubleClick(msg.selector),
      'submitForm':            () => submitForm(msg.selector),
      'formFill':              () => formFill(msg.fields),
      'drag':                  () => dragElement(msg.from, msg.to),
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
      'uploadCommit':          () => uploadCommit(msg.uploadId, msg.selector, msg.files, msg.mode),
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
      if (msg.wait && (POINTER_WAIT_ACTIONS.has(msg.action) || VALUE_WAIT_ACTIONS.has(msg.action))) {
        waitForActionable(msg).then(failure => failure || handler())
          .then(r => sendResponse(r)).catch(e => sendResponse(errorResult(e)));
        return true;
      }
      const result = handler();
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
  function uploadCommit(id, selector, files, mode) {
    const parts = __zlUploads[id];
    delete __zlUploads[id];
    if (!parts) return { error: 'upload ' + id + ' not found (chunks never arrived)' };
    return __zlAttachFiles(parts, selector, files, mode);
  }
  const __zlAttachFiles = function(parts, selector, files, mode) {
    var toBytes = function (b64) { var bin = atob(b64), a = new Uint8Array(bin.length); for (var i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i); return a; };
    // 2.1.0: build the File + DataTransfer with the PAGE's own constructors (wrappedJSObject + cloneInto).
    // Content-script Files were silently dropped by some uploaders (YouTube thumbnails, TikTok/Instagram covers).
    var W = (typeof cloneInto === 'function' && window.wrappedJSObject) ? window.wrappedJSObject : null;
    var objs = files.map(function (f) {
      var bytes = parts.slice(f.start, f.end).map(toBytes), opts = { type: f.type || 'application/octet-stream', lastModified: Date.now() };
      return W ? new W.File(cloneInto(bytes, W), f.name, cloneInto(opts, W)) : new File(bytes, f.name, opts);
    });
    var target = null;
    if (selector) { target = document.querySelector(selector); if (!target) return { error: 'No element matches: ' + selector }; }
    var input = null;
    if (mode !== 'drop') {
      if (target) input = target.matches('input[type=file]') ? target : target.querySelector('input[type=file]');
      if (!input && (!target || mode === 'input')) input = document.querySelector('input[type=file]');
      if (!input && mode === 'input') return { error: 'No input[type=file] found' };
    }
    var dt = W ? new W.DataTransfer() : new DataTransfer();
    objs.forEach(function (f) { dt.items.add(f); });
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
  function getAccessibilityTree(maxDepth) {
    newRefGeneration();
    
    function buildNode(el, depth) {
      if (depth > maxDepth || !el || el.nodeType !== 1) return null;
      if (isHidden(el)) return null;

      const tag = el.tagName.toLowerCase();
      // Skip noise
      if (['script', 'style', 'noscript', 'svg', 'path', 'br', 'hr'].includes(tag)) return null;

      const rect = el.getBoundingClientRect();
      const isInteractive = isInteractiveElement(el);
      const text = directText(el);

      // Skip non-interactive elements with no text and small size
      if (!isInteractive && !text && rect.width < 5 && rect.height < 5) return null;

      const ref = `s${refGeneration}e${refCounter++}`;
      refMap[ref] = el;

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

      // Bounds only for interactive or visible elements
      if (isInteractive || text) {
        node.bounds = [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)];
      }

      // Recurse children
      const children = [];
      for (const child of el.children) {
        const childNode = buildNode(child, depth + 1);
        if (childNode) children.push(childNode);
      }
      if (children.length) node.children = children;

      return node;
    }

    const tree = buildNode(document.body, 0);
    return { tree, refs: refCounter };
  }

  // Compact accessibility-style view (DOM semantics, not a platform AX tree).
  function getSnapshot(maxChars, focusLastAction, selector, maxDepth) {
    if (maxDepth != null && (!Number.isInteger(maxDepth) || maxDepth < 0)) {
      return { error: 'max_depth must be a non-negative integer' };
    }
    // Resolve subtree refs before invalidating the previous snapshot's generation.
    const root = selector ? resolveElement(selector) : document.body;
    if (!root) return { error: 'Selector not found: ' + selector };
    // Keep the DOM element, not its old ref: starting a snapshot invalidates refs.
    const target = focusLastAction && lastActedElement?.isConnected ? lastActedElement : null;
    lastActedElement = null;
    newRefGeneration();
    const entries = [];
    const limit = Number.isFinite(maxChars) ? Math.max(0, maxChars) : Infinity;
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
    for (let el = target; el; el = el.parentElement) targetAncestors.add(el);
    function ignored(el) {
      return !el || el.nodeType !== 1 || isHidden(el) ||
        el.getAttribute('aria-hidden') === 'true' ||
        ['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG'].includes(el.tagName);
    }
    // Bottom-up metadata avoids repeatedly scanning large clickable ancestor trees.
    function measure(el, domDepth = 0) {
      if (ignored(el)) return null;
      const children = Array.from(el.children, child => measure(child, domDepth + 1)).filter(Boolean);
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
      const interactive = isInteractiveElement(el);
      const role = el.getAttribute('role') || landmarks[el.tagName] ||
        (/^H[1-6]$/.test(el.tagName) ? 'heading' : inferRole(el));
      data.useful = (maxDepth == null || domDepth <= maxDepth) &&
        (children.some(child => child.useful) || (interactive && !data.wrapper) ||
          semanticRoles.has(role) || el === target);
      data.interactive += interactive ? 1 : 0;
      data.native += nativeTags.has(el.tagName) ? 1 : 0;
      metadata.set(el, data);
      return data;
    }
    measure(root);
    function nameOf(el, interactive) {
      const labelledBy = el.getAttribute('aria-labelledby');
      if (labelledBy) {
        const label = labelledBy.split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ');
        if (clean(label)) return clean(label);
      }
      const explicitRole = el.getAttribute('role');
      const isLandmark = landmarks[el.tagName] ||
        (semanticRoles.has(explicitRole) && explicitRole !== 'heading');
      const isField = ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
      if (interactive && !nativeTags.has(el.tagName)) {
        const name = clean(el.getAttribute('aria-label') || el.getAttribute('title') ||
          el.getAttribute('alt') || directText(el));
        if (name) return name;
        const text = metadata.get(el).nodes <= 5 ? normalize(el.textContent) : '';
        return text.length <= 80 ? text : '';
      }
      let fallback;
      if (isField) fallback = el.placeholder || el.title;
      else if (isLandmark) fallback = el.title;
      else fallback = el.textContent || el.title;
      return clean(el.getAttribute('aria-label') || findLabel(el) ||
        (el.type === 'image' ? el.alt : '') ||
        (['button', 'submit', 'reset'].includes(el.type) ? el.value : '') || fallback);
    }
    function append(entry) {
      const cost = entry.line.length + (entries.length ? 1 : 0);
      if (!target && size + cost > limit) { truncated = true; return false; }
      entries.push(entry);
      size += cost;
      return true;
    }
    function visit(el, depth, parent = -1, domDepth = 0) {
      if (!metadata.has(el) || (maxDepth != null && domDepth > maxDepth)) return;
      let role = el.getAttribute('role') || landmarks[el.tagName] ||
        (/^H[1-6]$/.test(el.tagName) ? 'heading' : inferRole(el));
      const interactive = isInteractiveElement(el) && !metadata.get(el).wrapper;
      const include = interactive || semanticRoles.has(role) || el === target;
      if (include) {
        if (!role) {
          if (el.isContentEditable) role = 'textbox';
          else if (el === target) role = el.tagName.toLowerCase();
          else role = 'button';
        }
        const ref = `s${refGeneration}e${refCounter++}`;
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
        const line = `${'  '.repeat(Math.min(depth, 12))}- ${role} ${JSON.stringify(nameOf(el, interactive || el === target))} [ref=${ref}]${states.length ? ' [' + states.join(' ') + ']' : ''}`;
        if (!append({ el, ref, line, parent, interactive })) return;
        parent = entries.length - 1;
      }
      if (maxDepth != null && domDepth >= maxDepth) return;
      const childDepth = depth + (include ? 1 : 0);
      const children = Array.from(el.children).filter(child => metadata.get(child)?.useful);
      // Only composite children / list items collapse: a toolbar of leaf controls
      // should remain fully discoverable. Text, IDs and classes are not structure.
      const repeatable = child => child.children.length > 0 || child.tagName === 'LI' ||
        child.getAttribute('role') === 'listitem';
      for (let start = 0; start < children.length && !truncated;) {
        let end = start + 1;
        if (repeatable(children[start])) {
          while (end < children.length &&
              metadata.get(children[end]).signature === metadata.get(children[start]).signature) end++;
        }
        let omitted = 0;
        function flushOmitted() {
          if (!omitted || truncated) return;
          append({ line: `${'  '.repeat(Math.min(childDepth, 12))}- … ${omitted} more similar items`,
            parent, interactive: false });
          omitted = 0;
        }
        for (let i = start; i < end && !truncated; i++) {
          if (i - start < 5 || targetAncestors.has(children[i])) {
            flushOmitted();
            if (!truncated) visit(children[i], childDepth, parent, domDepth + 1);
          } else omitted++;
        }
        flushOmitted();
        start = end;
      }
    }
    visit(root, 0);

    const targetIndex = entries.findIndex(entry => entry.el === target);
    let selected = entries;
    if (size > limit) {
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
      if (targetIndex >= 0) {
        // The target's complete line wins even if an unusually small cap cannot fit it.
        kept.add(targetIndex);
        used = entries[targetIndex].line.length + 1;
        const ancestors = [];
        for (let i = entries[targetIndex].parent; i >= 0; i = entries[i].parent) ancestors.push(i);
        for (const i of ancestors.reverse()) add(i, false);

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
      }
      // With no surviving target this is the usual document-order prefix.
      for (let i = 0; i < entries.length; i++) {
        if (targetIndex < 0 && used + entries[i].line.length + 1 > limit + 1) break;
        add(i);
      }
      selected = entries.filter((_, i) => kept.has(i));
      truncated = selected.length < entries.length;
    }
    for (const entry of selected) {
      if (entry.ref) refMap[entry.ref] = entry.el;
    }
    return {
      snapshot: selected.map(entry => entry.line).join('\n'),
      ...(truncated ? { truncated: true } : {}),
      ...(targetIndex >= 0 ? { target_ref: entries[targetIndex].ref } : {}),
    };
  }

  // ── Find Elements ──
  function findElements(query) {
    pruneRefMap();
    const q = query.toLowerCase();
    const results = [];

    // Try as CSS selector
    try {
      if (valueSelectorBlocked(query)) throw new Error(VALUE_SELECTOR_ERROR);
      const matches = document.querySelectorAll(query);
      if (matches.length > 0 && matches.length < 50) {
        matches.forEach(el => {
          if (!isHidden(el)) results.push(makeResult(el));
        });
        if (results.length) return { results: results.slice(0, 20) };
      }
    } catch {}

    // Text/attribute search
    const candidates = document.querySelectorAll('a, button, input, select, textarea, [role], [onclick], label, h1, h2, h3, h4, h5, h6, p, span, div, li, td, th, img');
    for (const el of candidates) {
      if (isHidden(el)) continue;
      const score = scoreMatch(el, q);
      if (score > 0) results.push({ ...makeResult(el), score });
    }

    results.sort((a, b) => b.score - a.score);
    return { results: results.slice(0, 20) };
  }

  function makeResult(el) {
    const ref = `s${refGeneration}e${refCounter++}`;
    refMap[ref] = el;
    const rect = el.getBoundingClientRect();
    return {
      ref,
      tag: el.tagName.toLowerCase(),
      text: (el.textContent || '').trim().slice(0, 120),
      selector: buildSelector(el),
      bounds: [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)],
      interactive: isInteractiveElement(el)
    };
  }

  function scoreMatch(el, query) {
    let score = 0;
    const fields = [
      [el.textContent, 2],
      [el.ariaLabel, 5],
      [el.placeholder, 4],
      [el.title, 3],
      [el.alt, 3],
      [el.id, 3],
      [el.name, 2],
      // Matching on a sensitive value would let queries probe it.
      [isSensitiveField(el) ? '' : el.value, 2],
    ];
    for (const [val, weight] of fields) {
      if (val && String(val).toLowerCase().includes(query)) score += weight;
    }
    if (isInteractiveElement(el)) score += 1;
    return score;
  }

  // ── Click ──
  function clickElement(selector, coords) {
    let el = resolveElement(selector, coords);
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
    const x = coords ? coords.x : rect.x + rect.width / 2;
    const y = coords ? coords.y : rect.y + rect.height / 2;
    const eventInit = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX: x,
      clientY: y,
      button: 0,
      buttons: 1,
    };

    // el.click() below emits the click and performs native activation exactly once.
    for (const type of ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
      const EventCtor = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
      el.dispatchEvent(new EventCtor(type, {
        ...eventInit,
        pointerId: 1,
        pointerType: 'mouse',
        isPrimary: true,
      }));
    }

    if (typeof el.click === 'function') {
      el.click();
    }

    return { ok: true, tag: el.tagName.toLowerCase(), text: (el.textContent || '').trim().slice(0, 60), ...warning };
  }

  // ── Type ──
  function typeText(selector, text, clear) {
    const el = resolveElement(selector);
    if (!el) return notFound();
    lastActedElement = el;

    el.focus();
    
    if (clear) {
      // Use native value setter to properly trigger React/Vue state
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) {
        setter.call(el, text);
      } else {
        el.value = text;
      }
    } else {
      if (el.contentEditable === 'true') {
        document.execCommand('insertText', false, text);
        return { ok: true };
      }
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) {
        setter.call(el, el.value + text);
      } else {
        el.value += text;
      }
    }

    // Fire all the events React/Vue/Angular listen for
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));

    return { ok: true };
  }

  // ── Set Editable Content ──
  function setEditableContent(selector, value, format, clear) {
    if (format === 'html' && KITSUNE_BUILD !== 'dev') {
      return { error: DEV_BUILD_REQUIRED, devBuildRequired: true };
    }
    const el = resolveElement(selector);
    if (!el) return notFound();

    const target = getEditableTarget(el);
    if (!target) return { error: 'Element is not editable' };

    target.focus();
    const text = String(value ?? '');

    if (target.isContentEditable || target.contentEditable === 'true') {
      if (clear) {
        // @kitsune-dev-only:start
        if (format === 'html') target.innerHTML = text; else
        // @kitsune-dev-only:end
        target.innerText = text;
      } else {
        document.execCommand(format === 'html' ? 'insertHTML' : 'insertText', false, text);
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
      const proto = target.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
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
  function scrollPage(direction, amount, selector) {
    let target = (selector ? resolveElement(selector) : null) || window;
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
  function hoverElement(selector, coords) {
    const el = resolveElement(selector, coords);
    if (!el) return notFound();

    const rect = el.getBoundingClientRect();
    const x = rect.x + rect.width / 2;
    const y = rect.y + rect.height / 2;

    for (const type of ['mouseenter', 'mouseover', 'mousemove']) {
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, clientX: x, clientY: y }));
    }

    return { ok: true, tag: el.tagName.toLowerCase(), ...(selector ? notRenderedWarning(el) : {}) };
  }

  // ── Fill Form ──
  async function fillFieldSafe(selector, value) {
    let result;
    try { result = fillField(selector, value); } catch(e) { result = errorResult(e); }
    if (result.error && (result.error.includes('HTMLInputElement') || result.error.includes('HTMLTextAreaElement'))) {
      // Shadow DOM not accessible from content script — use page context
      const pageResult = await fillViaPageContext(selector, value);
      if (pageResult.startsWith('ok:')) {
        return { ok: true, pierced: true, method: 'pageContext', tag: pageResult.split(':')[1].toLowerCase() };
      }
      return { error: pageResult };
    }
    return result;
  }

  function fillField(selector, value) {
    const el = resolveElement(selector);
    if (!el) return notFound();

    // Auto-pierce shadow DOM: if element isn't a standard input, look inside shadow root
    const target = getFillTarget(el);
    lastActedElement = target;

    if (target.type === 'checkbox' || target.type === 'radio') {
      target.checked = !!value;
      target.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (target.tagName === 'SELECT') {
      target.value = value;
      target.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      // Use native setter to trigger React/Vue/custom element state
      target.focus();
      const proto = target.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
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

  // Runs in the page world (closed shadow DOM). Only this packaged function is injected;
  // the selector and value travel as JSON arguments, never as generated code.
  function pageFill(selector, value, callbackName) {
    const el = document.querySelector(selector);
    if (!el) { window[callbackName] = 'not found'; return; }
    const target = el.shadowRoot
      ? (el.shadowRoot.querySelector('input,textarea,select') || el)
      : (el.querySelector('input,textarea,select') || el);
    target.focus();
    const proto = target.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value');
    if (setter && setter.set) setter.set.call(target, value);
    else target.value = value;
    target.dispatchEvent(new Event('input', { bubbles: true }));
    target.dispatchEvent(new Event('change', { bubbles: true }));
    window[callbackName] = 'ok:' + target.tagName;
  }

  function fillViaPageContext(selector, value) {
    // Last resort: pierce closed shadow DOM from the page context.
    return new Promise((resolve) => {
      const callbackName = '__claudeFill_' + Date.now();
      const args = [selector, String(value), callbackName].map(arg => JSON.stringify(arg)).join(',');
      injectPageScript('(' + pageFill.toString() + ')(' + args + ');');
      const result = window.wrappedJSObject?.[callbackName] || window[callbackName] || 'unknown';
      resolve(result);
    });
  }

  function getFillTarget(el) {
    const tag = el.tagName.toLowerCase();
    if (['input', 'textarea', 'select'].includes(tag)) return el;

    // Open shadow root
    if (el.shadowRoot) {
      const inner = el.shadowRoot.querySelector('input, textarea, select');
      if (inner) return inner;
    }

    // Regular children
    const child = el.querySelector('input, textarea, select');
    if (child) return child;

    // Closed shadow root — try page context access via wrappedJSObject (Firefox)
    try {
      const unwrapped = el.wrappedJSObject || el;
      if (unwrapped.shadowRoot) {
        const inner = unwrapped.shadowRoot.querySelector('input, textarea, select');
        if (inner) return XPCNativeWrapper(inner);
      }
    } catch {}

    return el;
  }

  function getEditableTarget(el) {
    if (el.isContentEditable || el.contentEditable === 'true') return el;
    if (['INPUT', 'TEXTAREA'].includes(el.tagName)) return el;

    if (el.shadowRoot) {
      const inner = el.shadowRoot.querySelector('[contenteditable="true"], textarea, input');
      if (inner) return inner;
    }

    const child = el.querySelector?.('[contenteditable="true"], textarea, input');
    if (child) return child;

    try {
      const unwrapped = el.wrappedJSObject || el;
      if (unwrapped.shadowRoot) {
        const inner = unwrapped.shadowRoot.querySelector('[contenteditable="true"], textarea, input');
        if (inner) return XPCNativeWrapper(inner);
      }
    } catch {}

    return null;
  }

  // ── Get Form Fields ──
  function getFormFields() {
    const fields = [];
    document.querySelectorAll('input, select, textarea').forEach(el => {
      if (isHidden(el)) return;
      const rect = el.getBoundingClientRect();
      if (rect.width < 2 || rect.height < 2) return;

      const ref = `s${refGeneration}e${refCounter++}`;
      refMap[ref] = el;

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
        bounds: [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)]
      });
    });
    return { fields };
  }

  // ── Execute JS ──
  const JS_RESULT_LIMIT = 8000000; // 2.1.0: bridge WebSocket now takes up to 256 MB (was 50 KB under the old 1 MB socket limit)

  // @kitsune-dev-only:start
  function executeJS(code) {
    try {
      const result = eval(code);
      const str = String(result ?? '');
      const truncated = str.length > JS_RESULT_LIMIT;
      return {
        result: truncated ? str.slice(0, JS_RESULT_LIMIT) : str,
        ...(truncated && { truncated: true, fullLength: str.length }),
      };
    } catch (e) {
      return { error: e.message };
    }
  }
  // @kitsune-dev-only:end

  function waitForText(text, textGone, timeout = 10000) {
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
          error: `Timed out after ${timeout}ms waiting for visible text` });
        else setTimeout(poll, Math.min(100, timeout - elapsed));
      }
      poll();
    });
  }

  // ── Wait For Element ──
  function waitForElement(selector, timeout, pollInterval) {
    const maxWait = timeout || 10000;
    const interval = pollInterval || 200;
    const start = Date.now();

    return new Promise((resolve) => {
      function poll() {
        let el;
        try {
          // Preserve native CSS selector errors while resolving snapshot refs.
          el = /^(?:s\d+e\d+|r\d+)$/.test(selector)
            ? resolveElement(selector) : document.querySelector(selector);
        } catch (e) {
          resolve(errorResult(e));
          return;
        }
        if (el && !isHidden(el)) {
          const elapsed = Date.now() - start;
          const ref = `s${refGeneration}e${refCounter++}`;
          refMap[ref] = el;
          resolve({
            ok: true,
            found: true,
            selector,
            elapsed,
            ref,
            tag: el.tagName.toLowerCase(),
            text: (el.textContent || '').trim().slice(0, 120),
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

  function screenPoint(selector, x, y) {
    // Viewport point (element centre or x/y) plus its desktop position for OS-level clicks.
    if (selector) {
      const el = resolveElement(selector);
      if (!el) return notFound();
      el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
      const rect = el.getBoundingClientRect();
      x = rect.left + rect.width / 2;
      y = rect.top + rect.height / 2;
    }
    if (x == null || y == null) return { error: 'No target point' };
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

  function captureMeasure(selector) {
    const el = document.querySelector(selector);
    if (!el) return { missing: true };
    const b = el.getBoundingClientRect();
    return { x: b.left, y: b.top, w: b.width, h: b.height, vw: innerWidth, vh: innerHeight,
      sy: scrollY, dpr: devicePixelRatio || 1 };
  }

  function captureScrollIntoView(selector, block) {
    const el = document.querySelector(selector);
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
    const target = selector ? document.querySelector(selector) : null;
    let hidden = 0;
    document.querySelectorAll('body *').forEach(el => {
      const style = getComputedStyle(el);
      if ((style.position === 'fixed' || style.position === 'sticky') && !(target && el.contains(target)) &&
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

  // ── Helpers ──

  function resolveElement(selector, coords) {
    if (!selector && coords) return document.elementFromPoint(coords.x, coords.y);
    if (!selector) return null;
    
    // Try ref map first
    if (/^(?:s\d+e\d+|r\d+)$/.test(selector)) {
      const el = refMap[selector];
      if (!selector.startsWith(`s${refGeneration}e`) || !el || !el.isConnected) throw staleRef(selector, el);
      return el;
    }
    
    // Try CSS selector
    try { return document.querySelector(selector); } catch {}
    return null;
  }

  function nearestInteractiveAncestor(el) {
    let cur = el;
    while (cur && cur !== document.body) {
      if (isInteractiveElement(cur)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }

  function isHidden(el) {
    if (!el || el.hidden) return true;
    const s = window.getComputedStyle(el);
    return s.display === 'none' || s.visibility === 'hidden';
  }

  function isInteractiveElement(el) {
    const interactiveTags = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'DETAILS', 'SUMMARY']);
    if (interactiveTags.has(el.tagName)) return true;
    if (el.onclick || el.hasAttribute('tabindex') || el.isContentEditable || el.contentEditable === 'true') return true;
    const role = el.getAttribute('role');
    if (role && ['button', 'link', 'textbox', 'checkbox', 'radio', 'combobox', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'listbox', 'option', 'treeitem', 'spinbutton', 'searchbox', 'switch', 'slider'].includes(role)) return true;
    return false;
  }

  function inferRole(el) {
    const inputRole = { button: 'button', submit: 'button', reset: 'button',
      checkbox: 'checkbox', radio: 'radio', range: 'slider', number: 'spinbutton',
      search: 'searchbox' };
    const map = { A: 'link', BUTTON: 'button', INPUT: inputRole[el.type] || 'textbox',
      SELECT: 'combobox', TEXTAREA: 'textbox', SUMMARY: 'button', DETAILS: 'group' };
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

  function isMasked(el) {
    const style = window.getComputedStyle(el);
    const security = style.webkitTextSecurity || style.getPropertyValue?.('-webkit-text-security');
    return TEXT_SECURITY_MASKS.has(security);
  }

  function isSensitiveField(el) {
    if (el.tagName === 'INPUT') {
      return el.type === 'password' || hasSensitiveAutocomplete(el) ||
        (TEXT_ENTRY_TYPES.has(el.type) && hasSecretName(el)) || isAutofilled(el) || isMasked(el);
    }
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
    return { redacted: true, hasValue: !!el.value };
  }

  // Frameworks such as React mirror the live value into the `value` attribute,
  // so markup is serialized from a clone with sensitive value attributes removed.
  // Sensitivity is decided on the live elements (autofill, styles, shadow roots);
  // a clone that does not mirror the original tree loses every value attribute.
  function redactMarkup(el, outer) {
    const clone = el.cloneNode(true);
    const originals = [el, ...el.querySelectorAll('*')];
    const copies = [clone, ...clone.querySelectorAll('*')];
    const aligned = copies.length === originals.length &&
      copies.every((copy, i) => copy.tagName === originals[i].tagName);
    copies.forEach((copy, i) => {
      if (!aligned || isSensitiveField(originals[i])) copy.removeAttribute('value');
    });
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
    for (const el of document.querySelectorAll('[value]')) {
      if (isSensitiveField(el)) return true;
    }
    return false;
  }

  const VALUE_SELECTOR_ERROR = 'value-attribute selectors are disabled while a sensitive field has a value attribute';

  function directText(el) {
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
    for (let i = 0; i < 4 && cur && cur !== document.body; i++) {
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
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return lbl.textContent.trim().slice(0, 80);
    }
    const parent = el.closest('label');
    return parent ? parent.textContent.trim().slice(0, 80) : (el.ariaLabel || '');
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
          if (f === 'text') row.text = (el.innerText || el.textContent || '').trim().slice(0, 400);
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

  // A temporary exact selector lets the existing screenshot capture path use refs.
  function markScreenshotTarget(ref) {
    const el = resolveElement(ref);
    if (!el) return notFound();
    const attribute = `data-kitsune-capture-${crypto.getRandomValues(new Uint32Array(1))[0]}`;
    el.setAttribute(attribute, '');
    return { selector: `[${attribute}]`, attribute };
  }

  function unmarkScreenshotTarget(attribute) {
    if (/^data-kitsune-capture-\d+$/.test(attribute)) {
      document.querySelectorAll(`[${attribute}]`).forEach(el => el.removeAttribute(attribute));
    }
    return { ok: true };
  }

  function getBounds(selector) {
    const el = resolveElement(selector);
    if (!el) return notFound();
    const r = el.getBoundingClientRect();
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
    const clone = best.cloneNode(true);
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
    function walk(node) {
      if (node.nodeType === 3) return node.textContent.replace(/\s+/g, ' ');
      if (node.nodeType !== 1) return '';
      const tag = node.tagName.toLowerCase();
      if (['script','style','noscript','svg','iframe','nav','aside','footer','header','form'].includes(tag)) return '';
      const children = () => Array.from(node.childNodes).map(walk).join('');
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
        case 'pre': return '\n\n```\n' + (node.innerText || '') + '\n```\n\n';
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
            out += '- ' + Array.from(li.childNodes).map(walk).join('').trim() + '\n';
          }
          return out + '\n';
        }
        case 'ol': {
          let out = '\n', i = 1;
          for (const li of node.querySelectorAll(':scope > li')) {
            out += (i++) + '. ' + Array.from(li.childNodes).map(walk).join('').trim() + '\n';
          }
          return out + '\n';
        }
        case 'blockquote': return '\n> ' + children().trim().replace(/\n/g, '\n> ') + '\n\n';
        case 'table': {
          let out = '\n\n';
          const rows = node.querySelectorAll('tr');
          if (rows.length === 0) return '';
          rows.forEach((row, idx) => {
            const cells = Array.from(row.querySelectorAll('th, td')).map(c => (c.innerText||'').replace(/\|/g,'\\|').trim());
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

  function selectOption(selector, value, byText) {
    const el = resolveElement(selector);
    if (!el) return notFound();
    if (el.tagName !== 'SELECT') return { error: 'Element is not a <select>' };
    const matched = findSelectOption(el, value, byText);
    if (!matched) return { error: `No matching option for ${byText ? 'text' : 'value'}: ${value}` };
    lastActedElement = el;
    applySelectOption(el, matched);
    return { ok: true, value: el.value, text: matched.text };
  }

  function checkBox(selector, checked) {
    const el = resolveElement(selector);
    if (!el) return notFound();
    if (el.type !== 'checkbox' && el.type !== 'radio') return { error: 'Element is not a checkbox/radio' };
    lastActedElement = el;
    el.checked = !!checked;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return { ok: true, checked: el.checked };
  }

  function focusEl(selector) {
    const el = resolveElement(selector);
    if (!el) return notFound();
    el.focus();
    return { ok: true, active: document.activeElement === el };
  }

  function blurEl(selector) {
    const el = selector ? resolveElement(selector) : document.activeElement;
    if (!el) return notFound();
    if (typeof el.blur === 'function') el.blur();
    return { ok: true };
  }

  function keypress(selector, key, modifiers, text) {
    const target = selector ? resolveElement(selector) : (document.activeElement || document.body);
    if (!target) return notFound('No target');
    lastActedElement = selector || target !== document.body ? target : null;
    const mods = modifiers || {};
    const opts = {
      key,
      bubbles: true,
      cancelable: true,
      composed: true,
      ctrlKey: !!mods.ctrl,
      shiftKey: !!mods.shift,
      altKey: !!mods.alt,
      metaKey: !!mods.meta,
    };
    target.dispatchEvent(new KeyboardEvent('keydown', opts));
    if (text) {
      target.dispatchEvent(new KeyboardEvent('keypress', opts));
      // Insert text into editable targets
      if (target.isContentEditable) {
        document.execCommand('insertText', false, text);
      } else if (['INPUT','TEXTAREA'].includes(target.tagName)) {
        const proto = target.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
        if (setter) setter.call(target, (target.value || '') + text);
        else target.value = (target.value || '') + text;
        target.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }
    target.dispatchEvent(new KeyboardEvent('keyup', opts));
    return { ok: true, key, target: target.tagName?.toLowerCase() };
  }

  function doubleClick(selector) {
    const el = resolveElement(selector);
    if (!el) return notFound();
    el.scrollIntoView({ behavior: 'instant', block: 'center' });
    const r = el.getBoundingClientRect();
    const init = { bubbles: true, cancelable: true, composed: true, view: window, clientX: r.x + r.width/2, clientY: r.y + r.height/2, button: 0 };
    el.dispatchEvent(new MouseEvent('mousedown', init));
    el.dispatchEvent(new MouseEvent('mouseup', init));
    el.dispatchEvent(new MouseEvent('click', init));
    el.dispatchEvent(new MouseEvent('mousedown', init));
    el.dispatchEvent(new MouseEvent('mouseup', init));
    el.dispatchEvent(new MouseEvent('click', init));
    el.dispatchEvent(new MouseEvent('dblclick', { ...init, detail: 2 }));
    return { ok: true, ...notRenderedWarning(el) };
  }

  function submitForm(selector) {
    let form;
    if (selector) {
      const el = resolveElement(selector);
      if (!el) return notFound();
      lastActedElement = el;
      form = el.tagName === 'FORM' ? el : el.closest('form');
    } else {
      form = document.querySelector('form');
    }
    if (!form) return { error: 'No <form> found' };
    // Prefer the submit-button click path so form-validation listeners fire.
    const btn = form.querySelector('button[type="submit"], input[type="submit"]');
    if (btn) { btn.click(); return { ok: true, submitted: 'button' }; }
    form.submit();
    return { ok: true, submitted: 'method' };
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

  function formFill(fieldMap) {
    if (!fieldMap || typeof fieldMap !== 'object') return { error: 'fields must be an object' };
    const results = {};
    for (const key of Object.keys(fieldMap)) {
      const value = fieldMap[key];
      // Try as CSS selector first
      let el = null;
      try { el = document.querySelector(key); } catch {}
      // Try by name attr
      if (!el) el = document.querySelector(`[name="${CSS.escape(key)}"]`);
      // Try by label text
      if (!el) {
        for (const lbl of document.querySelectorAll('label')) {
          const t = (lbl.innerText || '').trim();
          if (t.toLowerCase().includes(key.toLowerCase())) {
            if (lbl.htmlFor) el = document.getElementById(lbl.htmlFor);
            else el = lbl.querySelector('input, textarea, select');
            if (el) break;
          }
        }
      }
      // Try by placeholder
      if (!el) {
        for (const inp of document.querySelectorAll('input[placeholder], textarea[placeholder]')) {
          if (inp.placeholder.toLowerCase().includes(key.toLowerCase())) { el = inp; break; }
        }
      }
      if (!el) { results[key] = { error: 'Not found' }; continue; }
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
          const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
          if (setter) setter.call(el, String(value));
          else el.value = String(value);
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          results[key] = { ok: true, tag: el.tagName.toLowerCase() };
        }
      } catch (e) {
        results[key] = { error: e.message };
      }
    }
    return { results };
  }

  function dragElement(fromSel, toSel) {
    const from = resolveElement(fromSel);
    const to = resolveElement(toSel);
    if (!from) return notFound('from element not found');
    if (!to) return notFound('to element not found');
    const fr = from.getBoundingClientRect();
    const tr = to.getBoundingClientRect();
    const fx = fr.x + fr.width/2, fy = fr.y + fr.height/2;
    const tx = tr.x + tr.width/2, ty = tr.y + tr.height/2;
    const dt = new DataTransfer();
    const dispatch = (target, type, x, y, related) => {
      const ev = new DragEvent(type, { bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, dataTransfer: dt, relatedTarget: related || null });
      target.dispatchEvent(ev);
    };
    from.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: fx, clientY: fy, button: 0 }));
    dispatch(from, 'dragstart', fx, fy);
    dispatch(from, 'drag', fx, fy);
    dispatch(to, 'dragenter', tx, ty);
    dispatch(to, 'dragover', tx, ty);
    dispatch(to, 'drop', tx, ty);
    dispatch(from, 'dragend', tx, ty);
    to.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: tx, clientY: ty, button: 0 }));
    return { ok: true, from: fromSel, to: toSel };
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

  function watchConsole(enabled) {
    __conState.enabled = !!enabled;
    if (enabled && !__conState.patched) {
      __conState.patched = true;
      for (const lvl of ['log','info','warn','error','debug']) {
        const orig = console[lvl].bind(console);
        console[lvl] = function(...args) {
          try {
            __conState.logs.push({
              level: lvl,
              time: Date.now(),
              message: args.map(a => {
                try { return typeof a === 'string' ? a : JSON.stringify(a); } catch { return String(a); }
              }).join(' ').slice(0, 2000),
            });
            if (__conState.logs.length > 500) __conState.logs.shift();
          } catch {}
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
        text: (el.innerText || el.textContent || '').trim().slice(0, 120),
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
