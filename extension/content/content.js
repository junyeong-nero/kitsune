// Kitsune - Content Script
// Handles all DOM interaction on web pages

(function() {
  'use strict';
  const CONTENT_VERSION = 37;
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

  // Element reference map (capped to prevent memory leaks in long sessions)
  const REF_MAP_LIMIT = 10000;
  const refMap = {};
  let refCounter = 0;
  let lastActedElement = null;
  // The field type, fill, form_fill, select_option or check last wrote, or focus last
  // focused; a submit without a target submits its form.
  let lastFilledField = null;
  // Persist across reinjection; seed per document so navigation cannot reuse refs.
  let refGeneration = window.__kitsuneRefGeneration ||
    `${Date.now()}${crypto.getRandomValues(new Uint32Array(1))[0]}`;
  // Generations in [refSeed, refGeneration] belong to this document; others name another one.
  const refSeed = window.__kitsuneRefSeed || (window.__kitsuneRefSeed = refGeneration);
  // Snapshot refs of the current generation: { label: 'role "name"', parent: ancestor's ref }.
  const refLabels = {};
  // The call whose observation started the current generation (state.latestObservation).
  let refObservation = null;
  // The last few generations, so a stale_ref can name the same element's current ref.
  const RETIRED_GENERATION_LIMIT = 3;
  const retiredGenerations = new Map();

  function newRefGeneration(observation) {
    retiredGenerations.set(refGeneration, { refs: { ...refMap }, labels: { ...refLabels } });
    if (retiredGenerations.size > RETIRED_GENERATION_LIMIT) {
      retiredGenerations.delete(retiredGenerations.keys().next().value);
    }
    refGeneration = (BigInt(refGeneration) + 1n).toString();
    window.__kitsuneRefGeneration = refGeneration;
    refObservation = observation;
    refCounter = 0;
    for (const key of Object.keys(refMap)) delete refMap[key];
    for (const key of Object.keys(refLabels)) delete refLabels[key];
  }

  function labelPath(labels, ref) {
    const path = [];
    for (let entry = labels[ref]; entry; entry = labels[entry.parent]) path.push(entry.label);
    return path.join('\n');
  }

  // A ref from a replaced generation names an element of the current one only when it is
  // the same connected node with the same role, name and snapshot ancestors.
  function currentRefFor(ref, generation) {
    const retired = retiredGenerations.get(generation);
    const el = retired?.refs[ref];
    if (!el?.isConnected || !retired.labels[ref]) return null;
    const path = labelPath(retired.labels, ref);
    const matches = Object.keys(refMap).filter(current => refMap[current] === el &&
      refLabels[current] && labelPath(refLabels, current) === path);
    return matches.length === 1 ? matches[0] : null;
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

  const STALE_REF_HINTS = {
    element_removed: 'The element was removed from the page; take a new browser_snapshot and use its refs.',
    not_in_snapshot: 'The latest snapshot did not issue this ref; take a new browser_snapshot and use its refs.',
    snapshot_replaced: 'A newer snapshot replaced this ref; use refs from the latest snapshot or a new browser_snapshot.',
    other_document: 'The ref belongs to another document: the tab navigated or a different tab is targeted. ' +
      'Pass tab_id and take a new browser_snapshot.',
  };

  const CURRENT_REF_HINT = 'A newer snapshot replaced this ref; state.currentRef is the same element (same node, ' +
    'role, name and ancestors) in the latest snapshot. Use it only if that snapshot shows the action is still needed.';

  function staleRef(ref, el) {
    const generation = /^s(\d+)e\d+$/.exec(ref)?.[1];
    let reason = 'snapshot_replaced';  // legacy r<n> refs predate generations
    if (generation === refGeneration) reason = el ? 'element_removed' : 'not_in_snapshot';
    else if (generation && (BigInt(generation) < BigInt(refSeed) ||
        BigInt(generation) > BigInt(refGeneration))) reason = 'other_document';
    const state = { reason, ref };
    let hint = STALE_REF_HINTS[reason];
    if (reason === 'snapshot_replaced') {
      if (refObservation) {
        state.latestObservation = refObservation;
        hint = `A newer snapshot from ${refObservation} replaced this ref; use its refs or a new browser_snapshot.`;
      }
      const currentRef = generation && currentRefFor(ref, generation);
      if (currentRef) {
        state.currentRef = currentRef;
        hint = CURRENT_REF_HINT;
      }
    }
    const { error, ...details } = actionError('stale ref, call browser_snapshot again', 'stale_ref', hint, state);
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

  function pruneRefMap() {
    const keys = Object.keys(refMap);
    if (keys.length > REF_MAP_LIMIT) {
      // Drop oldest half
      keys.slice(0, Math.floor(keys.length / 2)).forEach(k => delete refMap[k]);
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
      'waitForText':           () => waitForText(msg.text, msg.text_gone, msg.timeout),
      'getPageText':           () => getPageText(msg.selector),
      'getSnapshot':           () => getSnapshot(msg.maxChars, msg.focusLastAction, msg.ref || msg.selector, msg.maxDepth, !!msg.text,
                                     msg.observation),
      'getAccessibilityTree':  () => getAccessibilityTree(msg.depth || 6),
      'getFormFields':         () => getFormFields(),
      'click':                 () => clickElement(msg.selector, msg.coords, msg.first),
      'type':                  () => typeText(msg.selector, msg.text, msg.clear, msg.first),
      'setEditableContent':    () => setEditableContent(msg.selector, msg.value, msg.format || 'text', msg.clear !== false,
                                     msg.first),
      'scroll':                () => scrollPage(msg.direction, msg.amount, msg.selector),
      'hover':                 () => hoverElement(msg.selector, msg.coords, msg.first),
      'fill':                  () => fillFieldSafe(msg.selector, msg.value, msg.first),
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
      'screenPoint':           () => screenPoint(msg.selector, msg.x, msg.y, msg.click),
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
      'selectOption':          () => selectOption(msg.selector, msg.value, msg.byText, msg.first),
      'checkBox':              () => checkBox(msg.selector, msg.checked, msg.first),
      'focusElement':          () => focusEl(msg.selector),
      'blurElement':           () => blurEl(msg.selector),
      'keypress':              () => keypress(msg.selector, msg.key, msg.modifiers, msg.text, msg.first),
      'doubleClick':           () => doubleClick(msg.selector, msg.first),
      'submitForm':            () => submitForm(msg.selector, msg.first),
      'describeTarget':        () => describeTarget(msg.selector, msg.coords, msg.key),
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
      'uploadCommit':          () => uploadCommit(msg.uploadId, msg.selector, msg.files, msg.mode, msg.first),
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
      const result = REDACTION_CACHED_READS.has(msg.action) ? withRedactionCache(handler) : handler();
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
  function uploadCommit(id, selector, files, mode, first) {
    const parts = __zlUploads[id];
    delete __zlUploads[id];
    if (!parts) return { error: 'upload ' + id + ' not found (chunks never arrived)' };
    if (selector && !first) assertOneTarget(selector, false);
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
    newRefGeneration('browser_dom');
    
    function buildNode(el, depth) {
      if (depth > maxDepth || !el || el.nodeType !== 1) return null;
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
      if (frame) {
        if (el.title) node.title = el.title;
        const { reason } = frameAccess(el);
        if (reason) Object.assign(node, { content: reason, src: frameSource(el) });
      }

      // Bounds only for interactive or visible elements
      if (isInteractive || text || frame) {
        node.bounds = [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)];
      }

      // Recurse children, and the body of a same-origin frame
      const children = [];
      for (const child of childrenAcrossFrames(el)) {
        const childNode = buildNode(child, depth + 1);
        if (childNode) children.push(childNode);
      }
      if (children.length) node.children = children;

      return node;
    }

    const tree = buildNode(document.body, 0);
    return { tree, refs: refCounter };
  }

  // Text mode adds readable text lines (without refs) and is always capped.
  const TEXT_SNAPSHOT_LIMIT = 12000;
  const TEXT_LINE_LIMIT = 1000;
  const TEXT_TRUNCATED_LINE = '- … truncated; scope with selector, ref or max_depth';
  // Inline content joins its block's text line; other elements start their own.
  const INLINE_TAGS = new Set(['A', 'ABBR', 'B', 'BDI', 'BDO', 'CITE', 'CODE', 'DATA', 'DFN', 'EM',
    'I', 'KBD', 'LABEL', 'MARK', 'Q', 'S', 'SAMP', 'SMALL', 'SPAN', 'STRONG', 'SUB', 'SUP',
    'TIME', 'U', 'VAR']);
  const FIELD_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT', 'OPTION']);

  // Compact accessibility-style view (DOM semantics, not a platform AX tree).
  // observation: the tool whose call took this snapshot (an action's post-action snapshot).
  function getSnapshot(maxChars, focusLastAction, selector, maxDepth, withText = false,
      observation = 'browser_snapshot') {
    if (maxDepth != null && (!Number.isInteger(maxDepth) || maxDepth < 0)) {
      return { error: 'max_depth must be a non-negative integer' };
    }
    // Resolve subtree refs before invalidating the previous snapshot's generation.
    const root = selector ? resolveElement(selector) : document.body;
    if (!root) return { error: 'Selector not found: ' + selector };
    // Keep the DOM element, not its old ref: starting a snapshot invalidates refs.
    const target = focusLastAction && lastActedElement?.isConnected ? lastActedElement : null;
    lastActedElement = null;
    newRefGeneration(observation);
    const entries = [];
    let limit = Number.isFinite(maxChars) ? Math.max(0, maxChars) : Infinity;
    // Reserve room for the truncation marker so text snapshots stay within the cap.
    if (withText) limit = Math.max(0, Math.min(limit, TEXT_SNAPSHOT_LIMIT) - TEXT_TRUNCATED_LINE.length - 1);
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
      const interactive = isInteractiveElement(el);
      const role = el.getAttribute('role') || landmarks[el.tagName] ||
        (/^H[1-6]$/.test(el.tagName) ? 'heading' : inferRole(el));
      // Rendered frames are listed even when their content is unavailable (cross-origin).
      data.useful = (maxDepth == null || domDepth <= maxDepth) &&
        (children.some(child => child.useful) || (interactive && !data.wrapper) || media ||
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
      const explicitRole = el.getAttribute('role');
      const isLandmark = landmarks[el.tagName] ||
        (semanticRoles.has(explicitRole) && explicitRole !== 'heading');
      const isField = ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
      if (interactive && !nativeTags.has(el.tagName)) {
        const name = clean(el.getAttribute('aria-label') || el.getAttribute('title') ||
          el.getAttribute('alt') || directText(el));
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
      const cost = entry.line.length + (entries.length ? 1 : 0);
      if (!target && size + cost > limit) { truncated = true; return false; }
      entries.push(entry);
      size += cost;
      return true;
    }
    // named: an ancestor control or heading already shows this text as its name.
    function visit(el, depth, parent = -1, domDepth = 0, named = false) {
      if (!metadata.has(el) || (maxDepth != null && domDepth > maxDepth)) return;
      const frame = FRAME_TAGS.has(el.tagName);
      let role = el.getAttribute('role') || landmarks[el.tagName] ||
        (/^H[1-6]$/.test(el.tagName) ? 'heading' : inferRole(el)) || (frame ? 'iframe' : '');
      const media = MEDIA_TAGS.has(el.tagName);
      const interactive = (isInteractiveElement(el) && !metadata.get(el).wrapper) || media;
      const include = interactive || semanticRoles.has(role) || el === target || frame;
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
        const line = `${'  '.repeat(Math.min(depth, 12))}- ${label} [ref=${ref}]${states.length ? ' [' + states.join(' ') + ']' : ''}`;
        if (!append({ el, ref, line, parent, interactive })) return;
        refLabels[ref] = { label, parent: entries[parent]?.ref ?? null };
        parent = entries.length - 1;
      }
      const childDepth = depth + (include ? 1 : 0);
      named ||= interactive || role === 'heading';
      if (withText && !named && (el === root || !INLINE_TAGS.has(el.tagName)) &&
          !FIELD_TAGS.has(el.tagName) && !isSensitiveField(el)) {
        const [all, plain] = inlineText(el);
        const text = normalize(all);
        if (normalize(plain)) {
          const clipped = text.length > TEXT_LINE_LIMIT ? text.slice(0, TEXT_LINE_LIMIT) + '…' : text;
          if (!append({ line: `${'  '.repeat(Math.min(childDepth, 12))}- text ${JSON.stringify(clipped)}`,
            parent, interactive: false })) return;
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
            if (!truncated) visit(children[i], childDepth, parent, domDepth + 1, named);
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
    let snapshot = selected.map(entry => entry.line).join('\n');
    if (withText && truncated) snapshot += (snapshot ? '\n' : '') + TEXT_TRUNCATED_LINE;
    return {
      snapshot,
      ...(truncated ? { truncated: true } : {}),
      ...(targetIndex >= 0 ? { target_ref: entries[targetIndex].ref } : {}),
      ...(withText ? { url: location.href, title: document.title } : {}),
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
    const ref = `s${refGeneration}e${refCounter++}`;
    refMap[ref] = el;
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
  function clickElement(selector, coords, first) {
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
      button: 0,
      buttons: 1,
    };

    // el.click() below emits the click and performs native activation exactly once.
    for (const type of ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
      const EventCtor = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
      const proceed = el.dispatchEvent(new EventCtor(type, {
        ...eventInit,
        pointerId: 1,
        pointerType: 'mouse',
        isPrimary: true,
      }));
      // As a real mousedown does, unless the page cancelled it. The background has already
      // refused a tab the user is using (user_active), so this never takes focus from them.
      if (type === 'mousedown' && proceed) focusForClick(el, coords && { x, y });
    }

    const link = blankTargetLink(el);
    const media = MEDIA_TAGS.has(el.tagName) ? el : null;
    const wasPaused = media?.paused;
    let clickEvent = null;
    const onClick = event => { clickEvent = event; };
    if (link || media) el.addEventListener('click', onClick);
    if (typeof el.click === 'function') {
      el.click();
    }
    if (link || media) el.removeEventListener('click', onClick);
    // A page handler that cancelled the click took the link over, including any tab it opens.
    let blankLink = {};
    if (link && clickEvent && !clickEvent.defaultPrevented) {
      blankLink = { targetBlankLink: { url: link.href, opener: link.relList.contains('opener') } };
    }

    // A media element's text is its fallback content, which the browser does not show.
    const result = { ok: true, tag: el.tagName.toLowerCase(),
      text: media ? '' : redactedText(el).trim().slice(0, 60), ...warning, ...blankLink };
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
    const started = Promise.resolve(el.play()).then(() => null, e => ({ reason: 'refused',
      error: `play() was refused (${e.name}: ${e.message}); muted media can play, or use browser_trusted_click` }));
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

  // Replace the selection with text as typing does, firing beforeinput and input.
  function insertEditableText(el, text) {
    if (docOf(el).execCommand('insertText', false, text)) return;
    const init = { bubbles: true, composed: true, inputType: 'insertText', data: text };
    if (!el.dispatchEvent(new InputEvent('beforeinput', { ...init, cancelable: true }))) return;
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
  }

  // The http(s) link a click on el follows into a new tab (target=_blank, or <base target>).
  // The popup blocker can drop that tab for a synthetic click, so the background checks it.
  function blankTargetLink(el) {
    const link = el.closest('a[href], area[href]');
    if (!link || link.hasAttribute('download') || !/^https?:$/.test(link.protocol)) return null;
    const target = link.getAttribute('target') ?? docOf(link).querySelector('base[target]')?.getAttribute('target');
    return target?.toLowerCase() === '_blank' ? link : null;
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
  function hoverElement(selector, coords, first) {
    const el = resolveTarget(selector, coords, { first, pointer: true });
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
        (tooDeep ? 'A field nested deeper is not reached; use browser_js for it. ' : '') +
        'For a contenteditable editor use mode="editable".',
      { tag: el.tagName.toLowerCase(), reason: tooDeep ? 'shadow_depth' : 'no_field' });
  }

  function fillField(selector, value, first) {
    const el = resolveTarget(selector, null, { first });
    if (!el) return notFound();

    // Auto-pierce shadow DOM: if element isn't a standard input, look inside shadow root
    const target = getFillTarget(el);
    if (!FILL_TAGS.includes(target.tagName)) return notFillable(el, 'input, textarea or select');
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

      const ref = `s${refGeneration}e${refCounter++}`;
      refMap[ref] = el;
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
            ? resolveElement(selector) : deepQuerySelector(selector);
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

  function screenPoint(selector, x, y, click) {
    // Viewport point (element centre or x/y) plus its desktop position for OS-level clicks.
    if (click) agentTrustedClickUntil = Date.now() + AGENT_TRUSTED_CLICK_MS;
    if (selector) {
      const el = resolveElement(selector);
      if (!el) return notFound();
      el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
      const rect = viewportRect(el);
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

  function resolveElement(selector, coords) {
    if (!selector && coords) return elementAtPoint(coords.x, coords.y).el;
    if (!selector) return null;
    
    // Try ref map first
    if (REF_SELECTOR.test(selector)) {
      const el = refMap[selector];
      if (!selector.startsWith(`s${refGeneration}e`) || !el || !el.isConnected) throw staleRef(selector, el);
      return el;
    }
    
    // Try CSS selector
    try { return deepQuerySelector(selector); } catch {}
    return null;
  }

  // Write actions: like resolveElement, but a CSS selector matching several actionable
  // elements is ambiguous unless first is set. Refs and coordinates name one element.
  function resolveTarget(selector, coords, { first = false, pointer = false } = {}) {
    if (selector && !first && !REF_SELECTOR.test(selector)) assertOneTarget(selector, pointer);
    return resolveElement(selector, coords);
  }

  // What a write action could act on: enabled and not <input type=hidden>; for pointer
  // actions also rendered, as their actionability wait requires.
  function isActionable(el, pointer) {
    if (el.matches?.(':disabled') || (el.tagName === 'INPUT' && el.type === 'hidden')) return false;
    return !pointer || isRendered(el);
  }

  function assertOneTarget(selector, pointer) {
    let matches = [];
    try { matches = deepQuerySelectorAll(selector); } catch {}
    if (matches.length < 2) return;
    const actionable = matches.filter(el => isActionable(el, pointer));
    if (actionable.length < 2) return;
    const { error, ...details } = ambiguousTarget(
      `${actionable.length} elements match the selector; nothing was dispatched`,
      'Act on one element: pass its ref from state.candidates or a new browser_snapshot, or a more ' +
        'specific selector. first: true acts on the first match.',
      { selector, matches: actionable.length,
        candidates: actionable.slice(0, AMBIGUOUS_CANDIDATE_LIMIT).map(targetSummary) });
    throw new ActionError(error, details);
  }

  // A new ref for el in the current generation.
  function issueRef(el) {
    const ref = `s${refGeneration}e${refCounter++}`;
    refMap[ref] = el;
    return ref;
  }

  // An ambiguous_target candidate: its ref, role and name (never a field value).
  function targetSummary(el) {
    const buttonInput = el.tagName === 'INPUT' && BUTTON_INPUT_TYPES.has(String(el.type).toLowerCase());
    const name = controlLabel(el, buttonInput) ||
      (FILL_TAGS.includes(el.tagName) ? findLabel(el) || el.placeholder || '' : '');
    return { ref: issueRef(el), role: el.getAttribute('role') || inferRole(el) || el.tagName.toLowerCase(),
      name: String(name).trim().slice(0, 80) };
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
    redactionCache = { masked: new WeakMap(), region: new WeakMap(), text: new WeakMap() };
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
  function redactedText(el) {
    return withRedactionCache(() => cachedFor('text', el, () => {
      if (isSensitiveTextField(el)) return '';
      const sensitive = holdsSensitiveText(el);
      if (!el.firstElementChild) return sensitive ? '' : el.textContent || '';
      let text = '';
      for (const node of el.childNodes) {
        if (node.nodeType === 3) text += sensitive ? '' : node.textContent;
        else if (node.nodeType === 1) text += redactedText(node);
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

  function selectOption(selector, value, byText, first) {
    const el = resolveTarget(selector, null, { first });
    if (!el) return notFound();
    if (el.tagName !== 'SELECT') return { error: 'Element is not a <select>' };
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

  function focusEl(selector) {
    const el = resolveElement(selector);
    if (!el) return notFound();
    lastFilledField = el;
    el.focus();
    return { ok: true, active: activeElement() === el };
  }

  function blurEl(selector) {
    const el = selector ? resolveElement(selector) : activeElement();
    if (!el) return notFound();
    if (typeof el.blur === 'function') el.blur();
    return { ok: true };
  }

  function keypress(selector, key, modifiers, text, first) {
    const target = selector ? resolveTarget(selector, null, { first }) : (activeElement() || document.body);
    if (!target) return notFound('No target');
    lastActedElement = selector || target !== docOf(target).body ? target : null;
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
        docOf(target).execCommand('insertText', false, text);
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

  function doubleClick(selector, first) {
    const el = resolveTarget(selector, null, { first, pointer: true });
    if (!el) return notFound();
    el.scrollIntoView({ behavior: 'instant', block: 'center' });
    const r = el.getBoundingClientRect();
    const init = { bubbles: true, cancelable: true, composed: true, view: el.ownerDocument?.defaultView || window,
      clientX: r.x + r.width/2, clientY: r.y + r.height/2, button: 0 };
    el.dispatchEvent(new MouseEvent('mousedown', init));
    el.dispatchEvent(new MouseEvent('mouseup', init));
    el.dispatchEvent(new MouseEvent('click', init));
    el.dispatchEvent(new MouseEvent('mousedown', init));
    el.dispatchEvent(new MouseEvent('mouseup', init));
    el.dispatchEvent(new MouseEvent('click', init));
    el.dispatchEvent(new MouseEvent('dblclick', { ...init, detail: 2 }));
    return { ok: true, ...notRenderedWarning(el) };
  }

  const NON_TEXT_INPUT_TYPES = new Set(['button', 'checkbox', 'radio', 'file', 'reset', 'hidden',
    'color', 'range', 'submit', 'image']);

  const BUTTON_INPUT_TYPES = new Set(['button', 'submit', 'image']);

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
    const labelledBy = attr('aria-labelledby').split(/\s+/).filter(Boolean)
      .map(id => { const el = elementByIdNear(control, id); return el ? redactedText(el) : ''; })
      .join(' ').trim();
    const text = redactedText(control).trim();
    const icon = !text && control.querySelector?.('[aria-label], img[alt], svg title');
    const iconLabel = icon ? String(icon.getAttribute('aria-label') || icon.getAttribute('alt') ||
      icon.textContent || '').trim() : '';
    return labelledBy || attr('aria-label') || text ||
      (buttonInput ? (isSensitiveField(control) ? '' : String(control.value || '').trim()) || attr('alt') : '') ||
      iconLabel || attr('title');
  }

  // Ask-before-act: whether clicking the target (or pressing Enter on it, with key) can submit
  // a form, is a button labeled like a submit or payment, or is a button or link labeled like
  // a send, transfer, delete or account change. Resolves the target the way click/keypress
  // do and reports no field values.
  function describeTarget(selector, coords, key) {
    let el = selector || coords ? resolveElement(selector, coords)
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
    const enterSubmits = key === 'Enter' && !!control.form && tag === 'input' && !NON_TEXT_INPUT_TYPES.has(type);
    const buttonInput = tag === 'input' && BUTTON_INPUT_TYPES.has(type);
    const button = tag === 'button' || buttonInput || role === 'button';
    const link = !button && ((tag === 'a' && !!control.hasAttribute?.('href')) || role === 'link');
    const label = button || link ? controlLabel(control, buttonInput)
      : String(control.getAttribute?.('aria-label') || redactedText(control) || '').trim();
    return { ok: true, tag, type, inForm: !!control.form, submits: submitControl || enterSubmits,
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

  // form_fill's label match, else placeholder match, for key: at most one field per tree
  // (the document, then each shadow root), as the light-DOM lookup always picked the first.
  function fieldsByLabelText(key) {
    const text = key.toLowerCase();
    const fields = [];
    for (const root of searchedDocuments().flatMap(doc => [doc, ...shadowRootsIn(doc)])) {
      let el = null;
      for (const lbl of root.querySelectorAll('label')) {
        if (!(lbl.innerText || '').trim().toLowerCase().includes(text)) continue;
        el = lbl.htmlFor ? root.getElementById(lbl.htmlFor) : lbl.querySelector('input, textarea, select');
        if (el) break;
      }
      el ||= Array.from(root.querySelectorAll('input[placeholder], textarea[placeholder]'))
        .find(inp => inp.placeholder.toLowerCase().includes(text));
      if (el) fields.push(el);
    }
    return fields;
  }

  function formFill(fieldMap, first) {
    if (!fieldMap || typeof fieldMap !== 'object') return { error: 'fields must be an object' };
    const results = {};
    for (const key of Object.keys(fieldMap)) {
      const value = fieldMap[key];
      // Try as CSS selector first, then by name attr; both must name one field.
      let el = null;
      try {
        for (const selector of [key, `[name="${CSS.escape(key)}"]`]) {
          try { el = deepQuerySelector(selector); } catch {}
          if (el && !first) assertOneTarget(selector, false);
          if (el) break;
        }
      } catch (e) {
        results[key] = errorResult(e);
        continue;
      }
      // Then by label text, then placeholder. A key matching fields in several trees (the
      // page and web components' shadow roots) never silently writes into one of them.
      if (!el) {
        const matches = fieldsByLabelText(key);
        const actionable = matches.filter(field => isActionable(field, false));
        if (actionable.length > 1 && !first) {
          results[key] = ambiguousTarget(
            `${actionable.length} fields in separate components match the label or placeholder; nothing was written`,
            'Fill one of them with browser_type and its ref from state.candidates, or key it with a CSS ' +
              'selector or name that matches one field. first: true fills the first match.',
            { field: key, matches: actionable.length,
              candidates: actionable.slice(0, AMBIGUOUS_CANDIDATE_LIMIT).map(targetSummary) });
          continue;
        }
        el = matches[0] || null;
      }
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
      const EventCtor = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
      target.dispatchEvent(new EventCtor(type, {
        bubbles: true, cancelable: true, composed: true, view: target.ownerDocument?.defaultView || window,
        clientX: x, clientY: y, button: 0, buttons: type.endsWith('up') ? 0 : 1,
        pointerId: 1, pointerType: 'mouse', isPrimary: true,
      }));
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
