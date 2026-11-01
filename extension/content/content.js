// Kitsune - Content Script
// Handles all DOM interaction on web pages

(function() {
  'use strict';
  const CONTENT_VERSION = 50;
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
  const WINDOW_OPEN_CALL_LIMIT = 20;
  const WINDOW_OPEN_URL_LIMIT = 2048;
  // Both page-world installs share one <script>, so a strict CSP blocks (and reports) one script,
  // not two; a failed window.open watch still leaves the dialogs installed.
  injectPageScript('try { (' + installWindowOpenWatch.toString() + ')(window, ' + WINDOW_OPEN_CALL_LIMIT + ', ' +
    WINDOW_OPEN_URL_LIMIT + '); } catch {}\n(' + installDialogs.toString() + ')(window, f => f);');
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
  // Records the page's window.open calls so a synthetic click can report a popup that the popup
  // blocker refused (#304). The wrapper calls the original with the same this and arguments and
  // returns its result or exception unchanged. It is page code, not an exportFunction export,
  // which would throw "dead object" once the extension reloads; so under a CSP that blocks the
  // injected script there is no wrapper, and no warning. It runs in the page, so the limits
  // arrive as arguments.
  function installWindowOpenWatch(W, callLimit, urlLimit) {
    if (W.__kitsuneWindowOpenCalls || typeof W.open !== 'function') return;
    const nativeOpen = W.open;
    const calls = [];
    let seq = 0;
    W.open = function open(...args) {
      const opened = nativeOpen.apply(this, args);
      try {
        // With noopener (or noreferrer), window.open returns null for a popup that opened.
        // Only a refused call keeps its URL, so the page holds no other window.open URLs.
        const noopener = typeof args[2] === 'string' && /\bno(opener|referrer)\b/i.test(args[2]);
        const blocked = opened === null && !noopener;
        calls.push({ seq: ++seq, blocked,
          url: blocked && typeof args[0] === 'string' ? args[0].slice(0, urlLimit) : null });
        if (calls.length > callLimit) calls.shift();
      } catch {}
      return opened;
    };
    W.__kitsuneWindowOpenCalls = () => JSON.stringify(calls);
  }

  // The record is page data a page can replace, so this reads a bounded, type-checked copy.
  function readWindowOpenCalls() {
    try {
      const read = pageWindow.__kitsuneWindowOpenCalls;
      const json = typeof read === 'function' ? read() : null;
      if (typeof json !== 'string' || json.length > 100000) return null;
      const calls = JSON.parse(json);
      if (!Array.isArray(calls)) return null;
      return calls.slice(-WINDOW_OPEN_CALL_LIMIT).filter(call => Number.isSafeInteger(call?.seq))
        .map(call => ({ seq: call.seq, blocked: call.blocked === true,
          url: typeof call.url === 'string' ? call.url.slice(0, WINDOW_OPEN_URL_LIMIT) : null }));
    } catch { return null; }
  }

  // The URL as window.open resolves it, like a link's absolute href; a URL that does not parse
  // stays as given.
  function absoluteWindowOpenUrl(url) {
    try { url = new URL(url, document.baseURI).href; } catch {}
    return url.slice(0, WINDOW_OPEN_URL_LIMIT);
  }

  // A click during which the page called window.open and got null adds scriptPopup, which the
  // background turns into a popup_blocked warning when no tab opened (#304).
  // A held click (#351) clicks after a pause, so the calls are read once its result is in.
  function withScriptPopupCheck(act) {
    const before = readWindowOpenCalls();
    const result = act();
    if (!before) return result;
    const since = before.length ? before[before.length - 1].seq : 0;
    const attach = r => {
      const blocked = (readWindowOpenCalls() || []).filter(call => call.seq > since && call.blocked);
      if (!blocked.length || !r || typeof r !== 'object' || r.error) return r;
      const urls = blocked.filter(call => call.url).map(call => absoluteWindowOpenUrl(call.url));
      return { ...r, scriptPopup: { ...(urls.length ? { url: urls[0] } : {}), urls } };
    };
    return typeof result?.then === 'function' ? result.then(attach) : attach(result);
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
  // The element the last hover pointed at. A real pointer would still be inside its ancestors,
  // so the next hover sends them no enter events (#327). Click sends no mouseenter, so it does
  // not count.
  let lastPointedElement = null;
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

  // A background tab or a minimized window. Pages may then skip painting and animation frames, so
  // a menu a click just opened can stay unpainted until the tab is shown (#328).
  const tabHidden = () => document.visibilityState === 'hidden';

  // Share the snapshot's open-state rules, including popups awaiting a paint.
  const POPUP_SELECTOR = '[role=menu], [role=listbox], [role=dialog], dialog[open], [popover]';

  function inOpenPopup(el) {
    for (let node = flatParent(el); node; node = flatParent(node)) {
      if (!node.matches?.(POPUP_SELECTOR)) continue;
      if (popupOpen(node)) return true;
    }
    return false;
  }

  // Synthetic events still reach an unrendered target, so pointer actions report it instead of refusing.
  // A hidden form control has a label to act on instead, in a popup or a hidden tab too: CSS hides it
  // whether or not the page paints. Another item of an open popup, or any other target in a hidden
  // tab, is usually the intended one that the page has not painted (#328).
  function notRenderedWarning(el) {
    if (isRendered(el)) return {};
    const hiddenTab = tabHidden();
    const formControl = ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName);
    const inPopup = !formControl && inOpenPopup(el);
    const state = { reason: 'not_rendered', ...(hiddenTab ? { hiddenTab: true } : {}),
      ...(inPopup ? { inPopup: true } : {}) };
    if (formControl || (!hiddenTab && !inPopup)) {
      return { warning: { code: 'hidden_or_covered', state,
        hint: 'Events were dispatched to a hidden element a user could not click; ' +
          'prefer its label or visible control from a new browser_snapshot.' } };
    }
    const cause = hiddenTab
      ? 'The tab is in the background, and the page may not paint this element until the tab is shown. '
      : 'The element is in an open menu, listbox, dialog or popover that the page has not painted. ';
    return { warning: { code: 'hidden_or_covered', state,
      hint: cause + 'Events dispatched to it work normally: if it belongs to the control you meant, ' +
        'act on it and check the effect instead of looking for another element.' } };
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
  // window for its pointer events and for key presses of the modifiers it holds. Typing counts
  // while the field the user typed into keeps focus in a focused document and is not empty.
  // Values are never read out.
  const USER_INPUT_EVENTS = ['keydown', 'mousedown', 'pointerdown', 'touchstart', 'wheel', 'paste'];
  const USER_INPUT_WINDOW_MS = 3000;
  const AGENT_TRUSTED_CLICK_MS = 2000;
  let lastUserInput = null;   // { at, type }
  let userTypedField = null;  // the editable element (as the document sees it) the user last typed into
  let agentTrustedClickUntil = 0;
  let agentTrustedClickKeys = new Set();  // modifier key names ("Shift", "Meta") the trusted click holds
  // Until then, trusted keyboard and paste input is the agent's OS keyboard (#346, #347).
  let agentTrustedKeysUntil = 0;

  function noteUserInput(event) {
    if (!event.isTrusted) return;
    const now = Date.now();
    if (now < agentTrustedClickUntil && (/^(mouse|pointer|touch)/.test(event.type) ||
        (event.type === 'keydown' && agentTrustedClickKeys.has(event.key)))) return;
    if (now < agentTrustedKeysUntil && /^(key|paste|input|beforeinput|composition)/.test(event.type)) return;
    lastUserInput = { at: now, type: event.type };
    // Timestamp the event, not delivery: the background also reconciles a tab reported first.
    // The lightweight child-frame observer reports independently of command injection.
    if (!window.__kitsuneFrameInputInstalled) {
      browser.runtime.sendMessage({ action: 'trustedTabInput', at: now, type: event.type,
        ...(event.type === 'keydown' && ['Alt', 'Control', 'Meta', 'Shift'].includes(event.key)
          ? { modifier: event.key } : {}),
      }).catch(() => {});
    }
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
  // Snapshots and page actions in a hidden tab say so: an empty snapshot or an unrendered target
  // can then mean the page has not painted yet (#328). Visible tabs add nothing.
  const VISIBILITY_ACTIONS = new Set(['getSnapshot', 'click', 'doubleClick', 'type', 'fill',
    'setEditableContent', 'selectOption', 'checkBox', 'keypress', 'submitForm', 'formFill', 'hover',
    'scroll', 'drag', 'focusElement', 'blurElement']);

  function withVisibility(action, result) {
    if (!VISIBILITY_ACTIONS.has(action) || !tabHidden() || !result || typeof result !== 'object' ||
        Array.isArray(result)) return result;
    return { ...result, visibilityState: 'hidden' };
  }

  browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    const handlers = {
      // A frame's ping names its document (origin and path) for ask-before-act (#343).
      'ping':                  () => ({ ok: true, version: CONTENT_VERSION,
                                     ...(window !== window.top ? { frameUrl: location.origin + location.pathname } : {}) }),
      'settleState':           () => settleState(),
      'getPageInfo':           () => getPageInfo(),
      'handleDialog':          () => handleDialog(msg.accept, msg.prompt_text),
      'waitForText':           () => waitForText(msg.text, msg.text_gone, msg.timeout, msg.diagnose === true,
                                     msg.text_seen),
      'getPageText':           () => getPageText(msg.selector),
      'getSnapshot':           () => {
                                     // sinceObservation (#350): the delta against the page the agent last saw.
                                     if (msg.sinceObservation) actionBaseline = lastObservation;
                                     // textWhenFull (#354): a frame's delta with nothing to diff against (a
                                     // document an action loaded) lists text, as a delta lists text an action shows.
                                     const full = (msg.delta || msg.sinceObservation) && !actionBaseline && msg.textWhenFull;
                                     return getSnapshot(msg.maxChars, msg.focusLastAction, msg.ref || msg.selector, msg.maxDepth,
                                       full ? true : msg.text ?? (msg.observation ? false : undefined), msg.observation,
                                       msg.delta || msg.sinceObservation ? 'delta' : 'full', !!msg.boxes, msg.collapse !== false,
                                       !!msg.absoluteUrls, Array.isArray(msg.keepRefs) ? msg.keepRefs : []);
                                   },
      'getAccessibilityTree':  () => getAccessibilityTree(msg.depth ?? 6, msg.ref || msg.selector,
                                     Number.isFinite(msg.limit) ? msg.limit : DOM_TREE_LIMIT),
      'getFormFields':         () => getFormFields(),
      'click':                 () => withPopupWatch(withScriptPopupCheck(() =>
                                     clickElement(msg.selector, msg.coords, msg.first, msg.button, msg.modifiers,
                                       msg.holdMs)),
                                     msg.reportPopup !== false),
      'type':                  () => typeText(msg.selector, msg.text, msg.clear, msg.first),
      'setEditableContent':    () => setEditableContent(msg.selector, msg.value, msg.format || 'text', msg.clear !== false,
                                     msg.first),
      'scroll':                () => scrollPage(msg.direction, msg.amount, msg.selector, msg.first),
      'hover':                 () => withPopupWatch(hoverElement(msg.selector, msg.coords, msg.first, msg.durationMs,
                                     msg.steps)),
      'openedPopup':           () => takeOpenedPopup(),
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
      'screenPoint':           () => screenPoint(msg.selector, msg.x, msg.y, msg.click, msg.first, msg.zoom, msg.modifiers,
                                     msg.frame, msg.scroll !== false),
      'focusState':            () => focusState(msg.selector, msg.first),
      'agentInput':            () => agentInput(msg.ms),
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
      'keypress':              () => pressKey(msg.selector, msg.key, msg.modifiers, msg.text, msg.first, msg.repeat),
      'doubleClick':           () => withPopupWatch(doubleClick(msg.selector, msg.first, msg.modifiers)),
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

    const respond = result => sendResponse(withVisibility(msg.action, result));
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
      // The baseline always records text lines: a delta lists text the action made appear (#348).
      const run = msg.deltaBaseline
        ? () => { takeActionBaseline(true); startSettleWatch(); return handler(); }
        : msg.settleWatch ? () => { startSettleWatch(); return handler(); } : handler;
      if (msg.wait && (POINTER_WAIT_ACTIONS.has(msg.action) || VALUE_WAIT_ACTIONS.has(msg.action))) {
        waitForActionable(msg).then(failure => failure || run())
          .then(respond).catch(e => respond(errorResult(e)));
        return true;
      }
      const result = REDACTION_CACHED_READS.has(msg.action) ? withRedactionCache(handler) : run();
      if (result && typeof result.then === 'function') {
        // Async handler — keep message channel open
        result.then(respond).catch(e => respond(errorResult(e)));
        return true;
      }
      respond(result);
    } catch (e) {
      respond(errorResult(e));
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
      ...withNeedsUser(),
    };
  }

  // ── Needs the user (#376) ──
  // What only the user can do on this page, a hint (needs_user) on page info, snapshots and the
  // actions that snapshot; nothing waits for it or is refused. Only a shown frame of a known CAPTCHA
  // or verification widget, by origin and path, until it is solved (human_check), or a shown sign-in
  // field (sign_in) counts. Page text never does, so a page cannot word itself into one.
  const HUMAN_CHECK_FRAMES = [
    // A reCAPTCHA checkbox or its challenge; an invisible one's anchor shows no widget.
    { widget: 'recaptcha', host: /^(?:www\.)?(?:google\.com|recaptcha\.net)$/,
      path: /^\/recaptcha\/(?:api2|enterprise)\/(?:anchor|bframe)$/ },
    { widget: 'hcaptcha', host: /(?:^|\.)hcaptcha\.com$/, path: /^\/captcha\// },
    // Turnstile, also on Cloudflare's challenge pages.
    { widget: 'turnstile', host: /^challenges\.cloudflare\.com$/, path: /^\/cdn-cgi\/challenge-platform\// },
  ];
  // An input with one of these autocomplete tokens signs in.
  const SIGN_IN_AUTOCOMPLETE = new Set(['current-password', 'username', 'webauthn']);

  // Rendered with a size, on the page: a challenge waiting off-page or hidden is not shown.
  function shownOnPage(el) {
    if (!isRendered(el)) return false;
    const rect = el.getBoundingClientRect();
    const view = el.ownerDocument?.defaultView || window;
    return rect.width >= 2 && rect.height >= 2 &&
      rect.right + (view.scrollX || 0) > 0 && rect.bottom + (view.scrollY || 0) > 0;
  }

  // { widget, frame } of a known CAPTCHA or verification frame; frame is its origin and path.
  function humanCheckFrame(frame) {
    let url;
    try { url = new URL(frame.getAttribute('src') || '', frame.baseURI || document.baseURI); } catch { return null; }
    if (url.protocol !== 'https:' || /[?&#]size=invisible(?:[&#]|$)/.test(url.search + url.hash)) return null;
    const known = HUMAN_CHECK_FRAMES.find(({ host, path }) => host.test(url.hostname) && path.test(url.pathname));
    return known ? { widget: known.widget, frame: url.origin + url.pathname } : null;
  }

  // 'password', 'username' or 'webauthn' for a field to sign in with, 'new-password' for one that sets
  // a password (a sign-up or a password change), else null.
  function signInField(input) {
    const tokens = String(input.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/);
    if (tokens.includes('new-password')) return 'new-password';
    if (String(input.type).toLowerCase() === 'password' || tokens.includes('current-password')) return 'password';
    return tokens.find(token => SIGN_IN_AUTOCOMPLETE.has(token)) || null;
  }

  // A solved widget leaves its token in its response field.
  const HUMAN_CHECK_RESPONSES = 'textarea[name="g-recaptcha-response"], textarea[name="h-captcha-response"], ' +
    'input[name="cf-turnstile-response"]';

  // { reason, evidence } of the top document and its shadow roots, else null: an unsolved CAPTCHA
  // (a response field still empty) before a sign-in field, which a shown new-password field rules out.
  function pageNeedsUser() {
    let check = null, signIn = null, newPassword = false, responses = 0, solved = 0;
    for (const el of queryTreeSelectorAll('iframe, input, textarea', document)) {
      if (el.tagName === 'IFRAME') {
        const widget = check ? null : humanCheckFrame(el);
        if (widget && shownOnPage(el)) check = widget;
      } else if (el.matches(HUMAN_CHECK_RESPONSES)) {
        responses++;
        if (el.value) solved++;
      } else if (!newPassword && el.tagName === 'INPUT') {
        const field = signInField(el);
        if (!field || !shownOnPage(el)) continue;
        if (field === 'new-password') newPassword = true;
        else signIn ??= { reason: 'sign_in', evidence: { field } };
      }
    }
    if (check && !(responses && solved === responses)) return { reason: 'human_check', evidence: check };
    return newPassword ? null : signIn;
  }

  function withNeedsUser() {
    let needsUser = null;
    try { needsUser = pageNeedsUser(); } catch {}  // a hint never fails the read it comes with
    return needsUser ? { needs_user: needsUser } : {};
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

  // limit: the cap; a cross-origin frame's tree is read with what its page's tree left (#354).
  function getAccessibilityTree(maxDepth, selector, limit = DOM_TREE_LIMIT) {
    // Resolve the scope before pruning detached refs.
    const root = selector ? resolveElement(selector) : document.body;
    if (!root) return selectorNotFound(selector);
    pruneRefMap();
    let refs = 0;
    let size = 0;
    let truncated = false;
    // Cross-origin frame nodes the background reads through each frame's own content script.
    const frames = [];

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
      if (size + cost > limit) { truncated = true; return null; }
      size += cost;
      issueRef(el);
      refs++;
      const frameId = frame ? readableFrameId(el) : -1;
      if (frameId > 0) frames.push({ ref, frameId, depth, offset: frameTopOffset(el) });

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
    return { tree, refs, ...(truncated && { truncated: true, hint: DOM_TRUNCATED_HINT }),
      ...(frames.length ? { frames } : {}) };
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
  const NEW_ITEM_LIMIT = 20;
  const SCOPED_ITEM_LIMIT = 100;

  // What a delta snapshot is diffed against: the page's snapshot lines (without refs) per
  // element, url and title, recorded right before an action (takeActionBaseline).
  let actionBaseline = null;

  // The page as of the last whole-page snapshot or delta this document returned, recorded like
  // an action baseline: what browser_wait_for(time) diffs against (#350).
  let lastObservation = null;

  // ── Settling after an action (#348) ──
  // A post-action delta waits for the page's reaction to the action. The background polls
  // settleState (page timers are throttled in background tabs, messages are not): how long ago
  // the action started and the page last changed, counting mutations in every document Kitsune
  // reads and in their shadow roots since right before the action dispatched.
  // The watch also keeps what a delta may not list but is still a change (#385): elements added
  // anywhere (up to SETTLE_ADDED_LIMIT; more set changed) and edits inside an editor (changed).
  let settleWatch = null;
  const SETTLE_ADDED_LIMIT = 200;

  function noteSettleRecords(watch, records) {
    for (const record of records) {
      const target = record.target?.nodeType === 1 ? record.target : record.target?.parentElement;
      if (target && record.type !== 'attributes' && isEditableRegion(target)) watch.changed = true;
      for (const node of record.addedNodes || []) {
        if (node.nodeType !== 1 || TEXTLESS_TAGS.has(node.tagName)) continue;
        if (watch.added.length < SETTLE_ADDED_LIMIT) watch.added.push(node);
        else watch.changed = true;
      }
    }
  }

  // Whether the page changed during the watch in a way a delta may not list.
  function settleWatchChanged(watch) {
    if (!watch) return false;
    if (watch.observer) noteSettleRecords(watch, watch.observer.takeRecords());
    return watch.changed || watch.added.some(node => node.isConnected);
  }

  function startSettleWatch() {
    stopSettleWatch();
    const watch = { start: Date.now(), last: Date.now(), mutations: 0, added: [], changed: false };
    // Absent in vm test harnesses.
    if (typeof MutationObserver === 'function') {
      watch.observer = new MutationObserver(records => {
        watch.mutations += records.length;
        watch.last = Date.now();
        noteSettleRecords(watch, records);
      });
      for (const doc of searchedDocuments()) {
        for (const root of [doc, ...shadowRootsIn(doc)]) {
          try {
            watch.observer.observe(root, { childList: true, subtree: true, characterData: true, attributes: true });
          } catch {}
        }
      }
    }
    settleWatch = watch;
  }

  function stopSettleWatch() {
    settleWatch?.observer?.disconnect();
    settleWatch = null;
  }

  function settleState() {
    if (!settleWatch) return { watching: false };
    const now = Date.now();
    return { watching: true, elapsedMs: now - settleWatch.start, quietMs: now - settleWatch.last,
      mutations: settleWatch.mutations, hidden: !!document.hidden };
  }

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

  // keepRefs: in a delta, cross-origin frames whose own delta lists changes (#354); their lines
  // are kept like changed lines, so the background can list that delta under them.
  function getSnapshot(maxChars, focusLastAction, selector, maxDepth, textMode,
      observation = 'browser_snapshot', mode = 'full', boxes = false, collapse = true, absoluteUrls = false,
      keepRefs = []) {
    // A delta diffed against a baseline with text lines lists text too (#348).
    const withText = textMode !== false || (mode === 'delta' && actionBaseline?.withText === true);
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
    let changedDuringWatch = false;
    if (mode !== 'baseline') {
      actionBaseline = null;
      changedDuringWatch = mode === 'delta' && settleWatchChanged(settleWatch);
      stopSettleWatch();
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
    const targetRoles = new Set(['group', 'article', 'figure', 'listitem', 'row', 'cell',
      'tabpanel', 'dialog', 'application']);
    const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
    const clean = value => normalize(value).slice(0, 160);
    const nativeTags = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'DETAILS', 'SUMMARY']);
    const metadata = new WeakMap();
    const signatures = new Map();
    const targetAncestors = new Set();
    for (let el = target; el; el = parentAcrossFrames(el)) targetAncestors.add(el);
    // A popup an action reported that is open but not rendered is listed anyway, marked
    // [not_rendered] (#329). Below a hidden ancestor only the path to it is walked; inside it,
    // elements hidden themselves (hiddenInPopup) stay out.
    const shownPopups = new Set();
    const popupPath = new Set();
    const unhidden = new Set();
    let rootInPopup = false;
    for (const popup of unrenderedPopups()) {
      const path = [];
      let el = popup;
      for (; el && el !== root; el = parentAcrossFrames(el)) path.push(el);
      if (el === root) {
        shownPopups.add(popup);
        [...path, root].forEach(node => popupPath.add(node));
        continue;
      }
      for (el = root; el && el !== popup; el = parentAcrossFrames(el));
      if (el) {
        shownPopups.add(popup);
        rootInPopup = true;
      }
    }
    const hiddenElement = el => isHidden(el) || el.getAttribute('aria-hidden') === 'true';
    function ignored(el) {
      return !el || el.nodeType !== 1 || (!unhidden.has(el) && hiddenElement(el)) ||
        ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(el.tagName) ||
        (el.tagName.toUpperCase() === 'SVG' && !isImage(el));
    }
    const hasOwnText = el => flatChildNodes(el).some(node => node.nodeType === 3 && node.textContent.trim());
    // An <img> is content unless role=presentation/none marks it decorative; one without a
    // name (alt="", no alt) is left out too. Its src is never listed.
    const isImage = el => !['presentation', 'none'].includes(el.getAttribute('role')) &&
      (el.tagName === 'IMG' || ['img', 'graphics-document', 'graphics-symbol'].includes(el.getAttribute('role')));
    const isCanvas = el => el.tagName === 'CANVAS' && !el.getAttribute('role') &&
      el.getBoundingClientRect().width >= 24 && el.getBoundingClientRect().height >= 24;
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
    function measure(el, domDepth = 0, pathOnly = false, inPopup = false) {
      inPopup ||= shownPopups.has(el);
      if (shownPopups.has(el) || popupPath.has(el)) unhidden.add(el);
      else if (inPopup && el?.nodeType === 1) {
        if (hiddenInPopup(el)) return null;
        unhidden.add(el);
      }
      if (ignored(el)) return null;
      pathOnly = !inPopup && (pathOnly || (popupPath.has(el) && hiddenElement(el)));
      // A media element's children are sources, tracks and fallback content, not page structure.
      const media = MEDIA_TAGS.has(el.tagName);
      const children = media ? [] : childrenAcrossFrames(el).filter(child => !pathOnly || popupPath.has(child))
        .map(child => measure(child, domDepth + 1, pathOnly, inPopup)).filter(Boolean);
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
      data.canvas = !interactive && isCanvas(el);
      data.namedTarget = targetRoles.has(el.getAttribute('role')) && !!nameOf(el, false);
      // Rendered frames are listed even when their content is unavailable (cross-origin).
      data.pathOnly = pathOnly;
      data.useful = (maxDepth == null || domDepth <= maxDepth) &&
        (children.some(child => child.useful) || (interactive && !data.wrapper) || media || data.image ||
          data.canvas || data.namedTarget ||
          semanticRoles.has(role) || el === target || (withText && hasOwnText(el)) ||
          (FRAME_TAGS.has(el.tagName) && hasArea(el)) || shownPopups.has(el));
      data.interactive += interactive ? 1 : 0;
      data.native += nativeTags.has(el.tagName) ? 1 : 0;
      metadata.set(el, data);
      return data;
    }
    measure(root, 0, false, rootInPopup);
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
    // A same-origin link URL is printed as its path (#310).
    const page = (() => {
      try { return new URL(window.location?.href ?? globalThis.location?.href); } catch { return null; }
    })();
    // The last entry under each parent entry, for a link's previous sibling line.
    const lastChild = new Map();
    function append(entry) {
      size += entry.line.length + (entries.length ? 1 : 0);
      lastChild.set(entry.parent, entries.length);
      entries.push(entry);
    }
    // Every child the walk considered for a run of similar items, listed or collapsed.
    const runItems = new Set();
    // named: an ancestor control or heading already shows this text as its name.
    function visit(el, depth, parent = -1, domDepth = 0, named = false) {
      if (!metadata.has(el) || (maxDepth != null && domDepth > maxDepth)) return;
      const frame = FRAME_TAGS.has(el.tagName);
      const image = metadata.get(el).image;
      const canvas = metadata.get(el).canvas;
      const namedTarget = metadata.get(el).namedTarget;
      const shownPopup = shownPopups.has(el);
      let role = el.getAttribute('role') || landmarks[el.tagName] ||
        (/^H[1-6]$/.test(el.tagName) ? 'heading' : inferRole(el)) || (frame ? 'iframe' : '') || (image ? 'img' : '') ||
        (canvas ? 'canvas' : '') || (shownPopup ? popupRole(el) : '');
      const media = MEDIA_TAGS.has(el.tagName);
      const interactive = (isSnapshotControl(el) && !metadata.get(el).wrapper) || media;
      const include = interactive || semanticRoles.has(role) || el === target || frame || image ||
        canvas || namedTarget || shownPopup;
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
        // An editor's content is its value (#385), cut like a field's and marked when cut.
        else if (isEditingHost(el) && !isSensitiveField(el)) {
          const text = normalize(renderedText(el));
          states.push(`value=${JSON.stringify(text.length > 160 ? clean(text) + '…' : text)}`);
        }
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
        const label = `${role} ${JSON.stringify(shownPopup ? popupName(el) : nameOf(el, interactive || el === target))}`;
        const head = `${'  '.repeat(Math.min(depth, 12))}- ${label} [ref=`;
        const stateTail = `]${states.length ? ' [' + states.join(' ') + ']' : ''}${shownPopup ? ' [not_rendered]' : ''}`;
        // Boxes stay out of the signature, so repeated siblings still group.
        const tail = stateTail + (boxes ? ` [box=${boxOf(el)}]` : '');
        const previous = entries[lastChild.get(parent)];
        const entry = { el, ref, line: head + (ref ?? pendingRef) + tail, parent, interactive,
          head, tail, label, role, signature: label + stateTail };
        // A listed cross-origin frame: the background reads it through its own content script
        // (#343) and lists that frame's snapshot under the frame's line.
        const frameId = frame ? readableFrameId(el) : -1;
        if (frameId > 0) entry.frameId = frameId;
        append(entry);
        parent = entries.length - 1;
        // Only web links are exposed: other schemes can hold executable code or local data.
        if (role === 'link' && el.href) {
          let url;
          try { url = new URL(el.href); } catch {}
          if (url && ['http:', 'https:'].includes(url.protocol)) {
            entry.href = url.href;
            // A link repeating the URL of the link line right before it at its level, or (as a
            // first child) the page's own URL, gets no /url line; the acted-on target keeps it.
            // Selection keeps a link with its own /url entry (urlIndex) or the one it repeats.
            if (el !== target && (previous ? previous.href === url.href : url.href === page?.href)) {
              // The /url entry a bounded snapshot keeps with this link (none for the page's URL).
              entry.urlFrom = previous && (previous.urlIndex ?? previous.urlFrom);
            } else {
              entry.urlIndex = entries.length;
              const shown = !absoluteUrls && url.origin === page?.origin && !url.pathname.startsWith('//')
                ? url.pathname + url.search + url.hash : url.href;
              const signature = `/url: ${JSON.stringify(shown)}`;
              const indent = `${'  '.repeat(Math.min(depth + 1, 12))}- `;
              append({ line: indent + signature, shortLine: `${indent}/url: "…"`,
                parent, interactive: false, source: el, signature, url: true });
            }
          }
        }
      }
      const childDepth = depth + (include ? 1 : 0);
      named ||= interactive || role === 'heading' || image ||
        (namedTarget && nameOf(el, false) === normalize(redactedText(el)));
      // A hidden container on the way to a popup has other hidden content.
      if (withText && !named && !metadata.get(el).pathOnly && (el === root || !INLINE_TAGS.has(el.tagName)) &&
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
        // A delta lists up to NEW_ITEM_LIMIT items that are new since its baseline past the
        // collapse (#350): an infinite list's next batch is what the action loaded.
        let newItems = 0;
        for (let i = start; i < end; i++) {
          runItems.add(children[i]);
          const listNew = i - start >= shown && !!baseline?.items && !baseline.items.has(children[i]) &&
            newItems < NEW_ITEM_LIMIT;
          if (listNew) newItems++;
          if (i - start < shown || targetAncestors.has(children[i]) || listNew) {
            flushOmitted();
            visit(children[i], childDepth, parent, domDepth + 1, named);
          } else omitted++;
        }
        flushOmitted();
        start = end;
      }
    }
    visit(root, 0);

    const record = () => {
      const signatures = new Map();
      for (const entry of entries) {
        const el = entry.el || entry.source;
        if (!el) continue;
        if (!signatures.has(el)) signatures.set(el, new Set());
        signatures.get(el).add(entry.signature);
      }
      return { signatures, items: runItems, url: window.location?.href, title: document.title, withText };
    };
    if (mode === 'baseline') {
      actionBaseline = record();
      return { ok: true };
    }
    // Only a whole-page read is what the agent saw of the page.
    if (!selector && maxDepth == null) lastObservation = record();

    const targetIndex = entries.findIndex(entry => entry.el === target);
    // Where a bounded snapshot without a target may end: before entry i unless a small block
    // (SMALL_BLOCK_LIMIT) has lines on both sides of it.
    const blockLimit = Math.min(SMALL_BLOCK_LIMIT, limit / 4);
    let cuts = null;
    if (!baseline && targetIndex < 0 && Number.isFinite(limit)) {
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
    // A delta that found no new or changed line (#348).
    let unchanged = false;
    const kept = new Set();
    let used = 0;
    // Keeps the entries, each with its ancestors and the /url entry of a link (its own, or the
    // one whose URL it repeats), plus extra characters (a marker): all of them or none. With
    // shortUrls, /url entries not kept yet are shortened to `- /url: "…"` (#310), so a link
    // keeps its name and ref and is never read as having another link's URL.
    function add(indices, withAncestors = true, extra = 0, shortUrls = false) {
      const pending = new Set();
      const gather = (index, ancestors) => {
        for (let i = index; i >= 0 && !kept.has(i) && !pending.has(i); i = ancestors ? entries[i].parent : -1) {
          pending.add(i);
          if (entries[i].urlFrom != null) gather(entries[i].urlFrom, true);
          if (entries[i].urlIndex != null) gather(entries[i].urlIndex, false);
        }
      };
      for (const index of [].concat(indices)) gather(index, withAncestors);
      const lineOf = i => shortUrls && entries[i].url ? entries[i].shortLine : entries[i].line;
      let cost = extra;
      for (const i of pending) cost += lineOf(i).length + 1;
      if (used + cost > limit + 1) return false;
      for (const i of pending) {
        entries[i].line = lineOf(i);
        kept.add(i);
      }
      used += cost;
      return true;
    }
    const keep = index => add(index) || add(index, true, 0, true);
    // The spans between cuts, in document order, of the entries pick accepts until one does
    // not fit. That span is still kept with shortened /url lines if it fits then, and ends the
    // fill. Returns whether every span fit.
    function fill(pick) {
      for (let c = 1; c < cuts.length; c++) {
        const span = [];
        for (let i = cuts[c - 1]; i < cuts[c]; i++) if (pick(i)) span.push(i);
        if (!span.length || add(span)) continue;
        add(span, true, 0, true);
        return false;
      }
      return true;
    }
    function keepTarget() {
      // The target's complete line wins even if an unusually small cap cannot fit it.
      kept.add(targetIndex);
      used = entries[targetIndex].line.length + 1;
      const ancestors = [];
      for (let i = entries[targetIndex].parent; i >= 0; i = entries[i].parent) ancestors.push(i);
      for (const i of ancestors.reverse()) add(i, false);
      const { urlIndex } = entries[targetIndex];
      if (urlIndex != null) add(urlIndex, false) || add(urlIndex, false, 0, true);
    }
    if (baseline) {
      // The target, then lines the baseline lacks (new or newly shown elements, changed names,
      // states or text) in document order, each under its ancestors. Removals are not listed.
      if (targetIndex >= 0) keepTarget();
      const changedFrames = new Set(keepRefs.map(ref => Object.hasOwn(refMap, ref) ? refMap[ref] : null));
      const changed = [];
      // Lines of frames whose own delta lists changes (keepRefs); the background lists them
      // below and says whether those changed.
      const frameLines = [];
      entries.forEach((entry, i) => {
        const el = entry.el || entry.source;
        if (el && !baseline.signatures.get(el)?.has(entry.signature)) changed.push(i);
        else if (entry.frameId && changedFrames.has(el)) frameLines.push(i);
      });
      const listed = [...changed, ...frameLines].sort((a, b) => a - b);
      for (const i of listed) keep(i);
      unchanged = !changed.length;
      // A collapse marker follows the listed items of its run.
      entries.forEach((entry, i) => {
        if (entry.run === undefined || kept.has(i)) return;
        for (let j = entry.run; j < i; j++) if (kept.has(j)) return add(i);
      });
      selected = entries.filter((_, i) => kept.has(i));
      truncated = listed.some(i => !kept.has(i));
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
                keep(i);
                if (++count === 2) break;
              }
            }
          }
        }
        for (let i = 0; i < entries.length; i++) keep(i);
        selected = entries.filter((_, i) => kept.has(i));
      } else {
        // With no surviving target this is the longest document-order prefix ending at a cut.
        // On a page with a main landmark, a navigation, banner or contentinfo landmark outside
        // main that is larger than a small block waits (#310): it is reserved as its line and a
        // marker, and listed after the rest, whole or as far as it fits.
        const after = entries.map((_, i) => i + 1);
        for (let i = entries.length - 1; i >= 0; i--) {
          const parent = entries[i].parent;
          if (parent >= 0) after[parent] = Math.max(after[parent], after[i]);
        }
        const within = (i, ancestor) => ancestor <= i && i < after[ancestor];
        const inside = l => Array.from({ length: after[l] - l - 1 }, (_, k) => l + 1 + k);
        const main = entries.findIndex(entry => entry.role === 'main');
        const deferred = [];
        if (main >= 0) {
          entries.forEach((entry, i) => {
            if (!['navigation', 'banner', 'contentinfo'].includes(entry.role) || within(main, i) ||
                within(i, main) || deferred.some(l => within(i, l))) return;
            const cost = [i, ...inside(i)].reduce((sum, j) => sum + entries[j].line.length + 1, 0);
            if (cost > blockLimit) deferred.push(i);
          });
        }
        const markerLine = (l, hidden) => {
          const indent = '  '.repeat(Math.min(entries[l].line.search(/\S/) / 2 + 1, 12));
          const links = hidden.filter(i => entries[i].role === 'link').length;
          return `${indent}- … ${hidden.length} lines (${links} links) not shown; snapshot ${entries[l].ref} to list all`;
        };
        const reserved = deferred.filter(l => add(l, true, markerLine(l, inside(l)).length + 1));
        if (fill(i => !reserved.some(l => i > l && i < after[l]))) {
          const partial = reserved.filter(l => !add(inside(l)));
          for (const l of partial) if (!fill(i => i > l && i < after[l])) break;
        }
        // Kept lines in document order, a marker after a landmark's last kept line, and the
        // entry index each one stands at, so the cuts carry over to them.
        const markers = new Map();
        for (const l of reserved) {
          const hidden = [];
          let last = l;
          for (let i = l + 1; i < after[l]; i++) {
            if (kept.has(i)) last = i;
            else hidden.push(i);
          }
          if (hidden.length) markers.set(last, { line: markerLine(l, hidden) });
        }
        const at = [];
        selected = [];
        entries.forEach((entry, i) => {
          if (kept.has(i)) {
            selected.push(entry);
            at.push(i);
          }
          if (markers.has(i)) {
            selected.push(markers.get(i));
            at.push(i);
          }
        });
        // A cut lies between two kept lines when one lies between their entries.
        const isCut = new Set(cuts), cutsUpTo = [];
        for (let i = 0, count = 0; i <= entries.length; i++) cutsUpTo[i] = count += isCut.has(i) ? 1 : 0;
        cuts = [0, ...at.flatMap((i, p) => p && cutsUpTo[i] > cutsUpTo[at[p - 1]] ? [p] : []), selected.length];
      }
      truncated = kept.size < entries.length;
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
    const frames = selected.filter(entry => entry.frameId).map(entry => ({ ref: entry.ref, frameId: entry.frameId }));
    return {
      snapshot,
      ...(truncated ? { truncated: true } : {}),
      ...(targetIndex >= 0 ? { target_ref: entries[targetIndex].ref } : {}),
      // For boundPublicSnapshot, which can cut again after aliases and URL redaction.
      ...(cuts ? { cuts: cuts.filter(cut => cut <= selected.length) } : {}),
      ...(frames.length ? { frames } : {}),
      ...(withText && !baseline ? { url: window.location?.href ?? globalThis.location?.href, title: document.title } : {}),
      ...(explicit ? { readyState: document.readyState } : {}),
      ...(explicit && !snapshot && !truncated ? { empty: true, hint: EMPTY_SNAPSHOT_HINT } : {}),
      ...(baseline ? {
        delta: true,
        ...(window.location?.href !== baseline.url ? { url: window.location?.href } : {}),
        ...(document.title !== baseline.title ? { title: document.title } : {}),
        ...(unchanged && !changedDuringWatch && window.location?.href === baseline.url &&
          document.title === baseline.title ? { unchanged: true } : {}),
      } : {}),
      ...withNeedsUser(),
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
    const found = findInDocuments(query);
    // Cross-origin frames are searched by their own content scripts (#354).
    const frames = readableFrames();
    return frames.length ? { ...found, frames } : found;
  }

  // The top document and its same-origin frames.
  function findInDocuments(query) {
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

  // ── Opened popups (#329) ──
  // A click or hover can open a menu, listbox or dialog in a shared container at the end of
  // <body>, which a background tab may never paint (#320). The action reports it as opened_popup,
  // with the acted-on control and its items' refs, and snapshots keep it while it stays open.
  const POPUP_ROLE_SELECTOR = '[role=menu], [role=listbox], [role=dialog]';
  const POPUP_ROLES = new Set(['menu', 'listbox', 'dialog']);
  const POPUP_ITEM_ROLES = new Set(['menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'button', 'link']);
  const POPUP_ITEM_LIMIT = 20;
  // The last click or hover's watch, until the background takes its popup (openedPopup).
  let popupWatch = null;
  // Reported popups: { opener } keeps one while the opener's aria-expanded stays "true" and it
  // is not hidden by an attribute;
  // { signature } keeps one until it closes or its popupSignature changes.
  const keptPopups = new Map();

  // It or an ancestor below <body> has hidden or aria-hidden="true", which pages set at once
  // when they close a popup or mount a closed one.
  function hiddenByAttribute(el) {
    for (let node = el; node && node !== docOf(el).body; node = flatParent(node)) {
      if (node.hasAttribute?.('hidden') || node.getAttribute?.('aria-hidden') === 'true') return true;
    }
    return false;
  }

  // A role popup is open unless it is hidden itself or hidden by an attribute; a display:none
  // ancestor alone may only be waiting for a paint.
  function popupOpen(el) {
    if (el.tagName === 'DIALOG') return el.hasAttribute('open');
    if (el.hasAttribute('popover')) return !!el.matches?.(':popover-open');
    return POPUP_ROLES.has(el.getAttribute('role')) && !isHidden(el) && !hiddenByAttribute(el);
  }

  // Inside an unpainted popup, an element hidden by itself rather than by an ancestor:
  // display is not inherited; visibility is, so visibility:hidden counts only when its parent's
  // visibility is not hidden.
  function hiddenInPopup(el) {
    if (el.hidden || el.getAttribute('aria-hidden') === 'true' || (el.tagName === 'INPUT' && el.type === 'hidden')) return true;
    const style = styleOf(el);
    if (style.display === 'none') return true;
    const parent = flatParent(el);
    return style.visibility === 'hidden' && !(parent?.nodeType === 1 && styleOf(parent).visibility === 'hidden');
  }

  function popupRole(el) {
    return el.getAttribute('role') || (el.tagName === 'DIALOG' ? 'dialog' : inferRole(el) || 'generic');
  }

  // Its label only: a popup's text is its items'.
  function popupName(el) {
    return String(labelledByText(el) || el.getAttribute('aria-label') || el.getAttribute('title') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 80);
  }

  // What a page closes a popup with, on it and its ancestors below <body>: the hidden, class,
  // aria-hidden and open attributes and the inline display and visibility. Other inline style
  // (position, size) can change while it stays open.
  function popupSignature(popup) {
    const parts = [];
    for (let el = popup; el && el !== docOf(popup).body; el = flatParent(el)) {
      parts.push([...['hidden', 'class', 'aria-hidden', 'open'].map(name => el.getAttribute?.(name)),
        el.style?.display, el.style?.visibility]);
    }
    return JSON.stringify(parts);
  }

  // Up to POPUP_ITEM_LIMIT items, not those inside another item. Items of an unpainted popup
  // are skipped only when they are hidden themselves.
  function popupItems(popup) {
    const hidden = isRendered(popup) ? isHidden : hiddenInPopup;
    const items = [];
    (function walk(el) {
      for (const child of flatChildren(el)) {
        if (items.length >= POPUP_ITEM_LIMIT) return;
        if (hidden(child)) continue;
        if (POPUP_ITEM_ROLES.has(child.getAttribute('role') || inferRole(child))) items.push(child);
        else walk(child);
      }
    })(popup);
    return items;
  }

  // The trees a popup is looked for in: the target's document and its shadow root.
  function popupRoots(el) {
    const doc = docOf(el);
    const root = el.getRootNode?.();
    return root && root !== doc && root.querySelectorAll ? [doc, root] : [doc];
  }

  function openPopups(roots) {
    return new Set(roots.flatMap(root => Array.from(root.querySelectorAll(POPUP_SELECTOR))).filter(popupOpen));
  }

  // Right before a click or hover dispatches: the aria-expanded of the target or its nearest
  // ancestor that has one, and the popups already open. Openness is compared directly, since
  // showPopover() changes no attribute; the observer finds a menu that is only filled in.
  function watchPopups(el) {
    stopPopupWatch();
    // The action dispatches even when the watch cannot start.
    try {
      popupWatch = startPopupWatch(el);
    } catch {}
  }

  function stopPopupWatch() {
    popupWatch?.observer?.disconnect();
    popupWatch = null;
  }

  // el or its nearest ancestor below <body> that has the attribute.
  function nearestWithAttribute(el, name) {
    for (let node = el; node && node !== docOf(el).body; node = flatParent(node)) {
      if (node.hasAttribute?.(name)) return node;
    }
    return null;
  }

  function startPopupWatch(el) {
    const opener = nearestWithAttribute(el, 'aria-expanded');
    const hasPopup = nearestWithAttribute(el, 'aria-haspopup')?.getAttribute('aria-haspopup');
    const roots = popupRoots(el);
    const watch = { target: el, opener, expanded: opener?.getAttribute('aria-expanded'),
      hasPopup: hasPopup != null && hasPopup !== 'false', roots, before: openPopups(roots), filled: new Set(),
      added: new Set() };
    watch.note = records => {
      for (const record of records) {
        for (const node of record.addedNodes) watch.added.add(node);
        const popup = record.addedNodes.length ? record.target.closest?.(POPUP_ROLE_SELECTOR) : null;
        if (!popup) continue;
        watch.filled.delete(popup);
        watch.filled.add(popup);
      }
    };
    // Absent in vm test harnesses.
    if (typeof MutationObserver === 'function') {
      watch.observer = new MutationObserver(watch.note);
      for (const root of roots) watch.observer.observe(root, { childList: true, subtree: true });
    }
    return watch;
  }

  // A new unrendered role popup without an opener signal counts only when its nearest
  // display:none ancestor was there before the action (a shared layer). A closed dropdown the
  // action mounted in its own display:none host does not.
  function inEarlierHiddenLayer(popup, added) {
    let layer = flatParent(popup);
    while (layer?.nodeType === 1 && styleOf(layer).display !== 'none') layer = flatParent(layer);
    if (layer?.nodeType !== 1) return false;
    for (let node = layer; node; node = flatParent(node)) if (added.has(node)) return false;
    return true;
  }

  // The popup the watched action opened: the element the opener's aria-controls/aria-owns names
  // once its aria-expanded turned "true" (unless hidden by an attribute), else the popup that
  // opened last, else (aria-expanded turned "true", or the target has aria-haspopup) the open
  // menu, listbox or dialog that items were added to.
  function takeOpenedPopup() {
    const watch = popupWatch;
    if (!watch) return {};
    if (watch.observer) watch.note(watch.observer.takeRecords());
    stopPopupWatch();
    const { target, opener } = watch;
    if (!target.isConnected) return {};
    const expanded = !!opener?.isConnected && watch.expanded !== 'true' &&
      opener.getAttribute('aria-expanded') === 'true';
    const outside = popup => popup?.isConnected && !popup.contains(target);
    const signalled = expanded || watch.hasPopup;
    const opened = p => signalled || p.tagName === 'DIALOG' || p.hasAttribute('popover') || isRendered(p) ||
      inEarlierHiddenLayer(p, watch.added);
    let popup = null;
    if (expanded) {
      const ids = `${opener.getAttribute('aria-controls') || ''} ${opener.getAttribute('aria-owns') || ''}`;
      popup = ids.split(/\s+/).filter(Boolean).map(id => elementByIdNear(opener, id))
        .find(p => outside(p) && !hiddenByAttribute(p)) || null;
    }
    popup ||= [...openPopups(watch.roots)].filter(p => !watch.before.has(p) && outside(p) && opened(p)).at(-1) ||
      (signalled ? [...watch.filled].filter(p => outside(p) && popupOpen(p)).at(-1) : null);
    if (!popup) return {};
    keptPopups.set(popup, expanded ? { opener } : { signature: popupSignature(popup) });
    return { opened_popup: { ref: issueRef(popup), role: popupRole(popup), name: popupName(popup),
      opened_by: issueRef(target), rendered: isRendered(popup), items: popupItems(popup).map(targetSummary) } };
  }

  // Marks a successful click or hover that watched for a popup, so the background takes it.
  // Otherwise (an error, or a click whose popup nobody takes) the watch stops here.
  function withPopupWatch(result, report = true) {
    const mark = r => {
      if (popupWatch && r?.ok && report) return { ...r, popupWatch: true };
      stopPopupWatch();
      return r;
    };
    return typeof result?.then === 'function' ? result.then(mark) : mark(result);
  }

  // Reported popups that are still open but not rendered; closed ones are forgotten.
  function unrenderedPopups() {
    const popups = [];
    for (const [popup, { opener, signature }] of keptPopups) {
      let open = false;
      // Firefox revokes wrappers from a destroyed frame ("can't access dead object").
      try {
        open = refAttached(popup) && (opener
          ? refAttached(opener) && opener.getAttribute('aria-expanded') === 'true' && !hiddenByAttribute(popup)
          : popupOpen(popup) && popupSignature(popup) === signature);
      } catch {}
      if (!open) keptPopups.delete(popup);
      else if (!isRendered(popup)) popups.push(popup);
    }
    return popups;
  }

  // ── Click ──
  const DISABLED_CONTROLS = 'button:disabled, input:disabled, select:disabled, textarea:disabled';

  // A held press or a sustained hover lasts at most this long (#351).
  const POINTER_MAX_MS = 10000;

  // { ms } of a hold or hover duration (0 when absent), else { error }.
  function pointerMs(name, value) {
    if (value == null) return { ms: 0 };
    if (!Number.isInteger(value) || value < 0 || value > POINTER_MAX_MS) {
      return { error: `${name} must be an integer from 0 to ${POINTER_MAX_MS}` };
    }
    return { ms: value };
  }

  // Page timers in a hidden tab fire after 1 s at the earliest, so the background, whose timers
  // are not throttled, times the pause (#351). Without its answer a page timer does.
  function pointerPause(ms) {
    return Promise.resolve(browser.runtime.sendMessage({ action: 'pointerPause', ms })).catch(() => null)
      .then(reply => reply?.ok ? null : new Promise(resolve => setTimeout(resolve, ms)));
  }

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

  // holdMs (#351) keeps the button pressed that long between pointerdown/mousedown and
  // pointerup/mouseup, for long-press controls; the result reports held_ms.
  async function clickElement(selector, coords, first, button, modifiers, holdMs) {
    button ??= 'left';
    if (!Object.prototype.hasOwnProperty.call(MOUSE_BUTTONS, button)) {
      return { error: 'button must be left, middle or right' };
    }
    const hold = pointerMs('holdMs', holdMs);
    if (hold.error) return hold;
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
    watchPopups(el);
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

    movePointerTo(el, { ...released, button: 0 });
    let held = {};
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
      if (type === 'pointerup' && hold.ms) {
        const pressed = performance.now();
        await pointerPause(hold.ms);
        held = { held_ms: Math.round(performance.now() - pressed) };
      }
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
        ...warning, ...(link ? { newTabLink: { url: link.href, prevented: !proceed } } : {}), ...held };
    }

    const link = newTab ? followedLink(el) : blankTargetLink(el);
    const media = MEDIA_TAGS.has(el.tagName) ? el : null;
    const wasPaused = media?.paused;
    let clickEvent = null;
    const onClick = event => { clickEvent = event; };
    if (link || media) el.addEventListener('click', onClick);
    // A dispatched click runs native activation exactly once, as el.click() does, and also
    // carries the point. Like el.click(), it skips disabled form controls.
    // SVG has no HTMLElement.click(), but still accepts dispatched click events.
    if (!el.matches?.(DISABLED_CONTROLS)) {
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
      text: media ? '' : redactedText(el).trim().slice(0, 60), ...warning, ...linkTab, ...held };
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

  // Runs an editing command at the selection in el's editor and reports what it did: done,
  // execCommand's result; fired, whether the editor saw beforeinput; prevented, whether the
  // page cancelled it; changed, whether the editor's content changed. execCommand's result
  // alone proves no edit: Firefox returns true for one the page cancelled (#289).
  function runEditCommand(el, command, value) {
    const host = editingHost(el);
    let beforeInput = null;
    const onBeforeInput = event => { beforeInput ??= event; };
    host.addEventListener('beforeinput', onBeforeInput);
    const observer = new MutationObserver(() => {});
    observer.observe(host, { childList: true, characterData: true, subtree: true });
    const done = docOf(host).execCommand(command, false, value);
    const changed = observer.takeRecords().length > 0;
    observer.disconnect();
    host.removeEventListener('beforeinput', onBeforeInput);
    return { done, fired: !!beforeInput, prevented: !!beforeInput?.defaultPrevented, changed };
  }

  // An editing command's outcome as keypress reports it: effect is inputType when the editor
  // changed, else null; prevented: the page cancelled beforeinput, and its edit was not
  // applied or repeated.
  function editOutcome(edit, inputType) {
    if (edit.prevented) return { effect: null, prevented: true };
    return { effect: edit.done && edit.changed ? inputType : null };
  }

  // Replace the selection with text as typing does, firing beforeinput and input; the outcome
  // as editOutcome reports it. Only where the editing command did not run at all is the text
  // inserted directly, after a beforeinput of Kitsune's own.
  function insertEditableText(el, text) {
    const edit = runEditCommand(el, 'insertText', text);
    if (edit.done || edit.fired) return editOutcome(edit, 'insertText');
    const init = { bubbles: true, composed: true, inputType: 'insertText', data: text };
    if (!el.dispatchEvent(new InputEvent('beforeinput', { ...init, cancelable: true }))) {
      return { effect: null, prevented: true };
    }
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
    return { effect: 'insertText' };
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
      const edit = insertEditableText(el, text);
      return { ok: true, ...(edit.prevented ? { prevented: true } : {}) };
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
  // The ancestors a pointer enters on its way from previous to el, outermost first, el last. The
  // chain crosses shadow roots to their hosts and ends at el's documentElement. Ancestors that
  // also contain previous were entered already; with no connected previous, all of them are new.
  function enteredElements(el, previous) {
    // Only a shadow root (a document fragment, nodeType 11) has a host to cross to: a page's
    // <form name="host"> makes document.host an element, which would loop back to the root.
    const parent = node => node.parentElement
      || (node.parentNode?.nodeType === 11 ? node.parentNode.host : null);
    const inside = new Set();
    for (let node = previous?.isConnected ? previous : null; node; node = parent(node)) inside.add(node);
    const entered = [];
    for (let node = el; node && !inside.has(node); node = parent(node)) entered.unshift(node);
    return entered;
  }

  const HOVER_DEFAULT_STEPS = 5;
  const HOVER_MAX_STEPS = 50;

  // durationMs and steps (#351) keep the pointer moving over the target, for hover-intent
  // controls that wait for several moves over some time: after arriving, `steps` more
  // pointermove/mousemove pairs cross the target, spread over durationMs (default 5 steps), or a
  // task apart without it. The result then reports moves and hovered_ms.
  function hoverElement(selector, coords, first, durationMs, steps) {
    const duration = pointerMs('durationMs', durationMs);
    if (duration.error) return duration;
    if (steps != null && (!Number.isInteger(steps) || steps < 1 || steps > HOVER_MAX_STEPS)) {
      return { error: `steps must be an integer from 1 to ${HOVER_MAX_STEPS}` };
    }
    steps ??= duration.ms ? HOVER_DEFAULT_STEPS : 0;
    const el = resolveTarget(selector, coords, { first, pointer: true });
    if (!el) return notFound();

    // As a pointer reaching it would, bring the target into view first (#220). scrollIntoView
    // scrolls every scrolling ancestor, so an inner list that loads more at its end sees it;
    // the point is read after the scroll.
    if (!coords) el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
    const rect = el.getBoundingClientRect();
    const y = rect.y + rect.height / 2;
    // A sustained hover crosses the middle half of the target, left to right.
    const xAt = step => rect.x + rect.width * (steps ? 0.25 + 0.5 * step / steps : 0.5);
    const init = { view: el.ownerDocument?.defaultView || window, clientY: y };

    watchPopups(el);
    movePointerTo(el, { ...init, clientX: xAt(0) });
    const result = { ok: true, tag: el.tagName.toLowerCase(), ...(selector ? notRenderedWarning(el) : {}) };
    if (!steps) return result;
    const start = performance.now();
    return (async () => {
      for (let step = 1; step <= steps; step++) {
        await (duration.ms ? pointerPause(duration.ms / steps) : nextTask());
        movePointerWithin(el, { ...init, clientX: xAt(step) });
      }
      return { ...result, moves: steps + 1, hovered_ms: Math.round(performance.now() - start) };
    })();
  }

  function movePointerTo(el, init) {
    // Like a real pointer's events, over and move bubble out of shadow roots, while enter events
    // do not bubble and go to each newly entered ancestor, outermost first (#327).
    // Without leave events Kitsune cannot tell whether the pointer left meanwhile, so hovering
    // the same element again enters its whole chain, letting a retry reveal what was hidden.
    const entered = enteredElements(el, lastPointedElement === el ? null : lastPointedElement);
    lastPointedElement = el;
    const point = { ...init, bubbles: false, cancelable: false, composed: false, buttons: 0 };
    const passing = { ...point, bubbles: true, cancelable: true, composed: true };
    for (const [over, enter] of [['pointerover', 'pointerenter'], ['mouseover', 'mouseenter']]) {
      dispatchPointerOrMouse(el, over, passing);
      for (const target of entered) dispatchPointerOrMouse(target, enter, point);
    }
    movePointerWithin(el, init);
  }

  // A move of a pointer already over el; it bubbles out of shadow roots.
  function movePointerWithin(el, init) {
    const passing = { ...init, bubbles: true, cancelable: true, composed: true, buttons: 0 };
    for (const type of ['pointermove', 'mousemove']) dispatchPointerOrMouse(el, type, passing);
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
    const role = ariaRangeRole(el);
    return actionError(`Element is not a fillable field: ${reason}`,
      'not_fillable', role ? ariaRangeHint(el, role) : 'Target the field or its web-component host from a new browser_snapshot. ' +
        (tooDeep ? 'A field nested deeper is not reached; use browser_evaluate for it. ' : '') +
        'A contenteditable editor is filled through its textbox ref.',
      { tag: el.tagName.toLowerCase(), reason: tooDeep ? 'shadow_depth' : 'no_field', ...(role ? { role } : {}) });
  }

  // The keyboard alternative for an ARIA slider or spinbutton that a fill cannot set (#311).
  function ariaRangeHint(el, role) {
    return (ariaNumber(el, 'aria-valuenow') == null
      ? `It is an ARIA ${role} without a numeric aria-valuenow for fill to step toward: click`
      : `It is an ARIA ${role}: fill (browser_fill_form type slider, browser_type without slowly or in fill mode) ` +
        'steps it with arrow keys. Or click') +
      ' it to focus it, then press arrow keys with browser_press_key; repeat presses a key up to 100 times ' +
      'in one call.';
  }

  function fillField(selector, value, first) {
    const el = resolveTarget(selector, null, { first });
    if (!el) return notFound();

    const target = fillDestination(el);
    if (!FILL_TAGS.includes(target.tagName)) {
      if (isEditableRegion(target)) return fillEditable(target, value, target !== el);
      const role = ariaRangeRole(el);
      if (role && ariaNumber(el, 'aria-valuenow') != null) return fillAriaRange(el, role, value);
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
  // sees beforeinput and input (#255). prevented: the page cancelled the replacement or the
  // clearing, so the content is as the page left it (#289).
  function fillEditable(host, value, pierced) {
    lastActedElement = lastFilledField = host;
    const range = docOf(host).createRange();
    range.selectNodeContents(host);
    placeEditableCaret(host, range);
    const text = String(value ?? '');
    const edit = text ? insertEditableText(host, text)
      : editOutcome(runEditCommand(host, 'delete'), 'deleteContentBackward');
    return { ok: true, pierced, tag: host.tagName.toLowerCase(), contentEditable: true,
      ...(edit.prevented ? { prevented: true } : {}) };
  }

  // ── ARIA sliders (#311) ──
  // A role=slider or spinbutton without a native input has no value to assign, so fill sets it as
  // a user does: it focuses it and presses arrow keys (Home/End for an end), dispatched as
  // keypress dispatches them, until aria-valuenow is the value. A task passes between keys so the
  // page's framework renders each step before the next is read. A widget that renders only on an
  // animation frame (throttled in background tabs) is not waited for: it looks stuck, and may
  // still move after the result.
  const ARIA_RANGE_ROLES = new Set(['slider', 'spinbutton']);
  const ARIA_RANGE_MAX_KEYS = 1000;
  // Well inside the bridge's 35 s for /api/fill, so the page stops pressing before it times out.
  const ARIA_RANGE_MAX_MS = 20000;
  const ARIA_RANGE_HINTS = {
    stepped_past: 'Its steps skip the value; fill a value its steps reach (state.value is one).',
    stuck: 'It stopped moving: it is at an end (state.min, state.max), disabled, or ignores synthetic ' +
      'keys, or renders only on an animation frame.',
    moved_away: 'Its arrow keys moved it away from the value and the other arrow keys did not move it ' +
      'back; state.value is where it was left.',
    key_limit: `It took more than ${ARIA_RANGE_MAX_KEYS} keys; fill again to continue from state.value, ` +
      'or press PageUp/PageDown with browser_press_key for larger steps.',
    time_limit: `It took more than ${ARIA_RANGE_MAX_MS / 1000} s; fill again to continue from state.value, ` +
      'or press PageUp/PageDown with browser_press_key for larger steps.',
  };

  function ariaRangeRole(el) {
    const role = String(el.getAttribute?.('role') || '').trim().split(/\s+/)[0];
    return ARIA_RANGE_ROLES.has(role) ? role : null;
  }

  function ariaNumber(el, name) {
    const raw = String(el.getAttribute?.(name) ?? '').trim();
    return raw && Number.isFinite(Number(raw)) ? Number(raw) : null;
  }

  // Equal up to float rounding: a widget may publish 0.30000000000000004 for a step to 0.3.
  function sameRangeValue(a, b) {
    return a != null && b != null && Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(b));
  }

  // Presses key on el and returns aria-valuenow once the page rendered it: after one task, or
  // two when the first left it unchanged.
  async function pressAriaRangeKey(el, key) {
    const before = ariaNumber(el, 'aria-valuenow');
    const init = keyEventInit(key, {});
    el.dispatchEvent(new KeyboardEvent('keydown', init));
    el.dispatchEvent(new KeyboardEvent('keyup', init));
    await nextTask();
    if (ariaNumber(el, 'aria-valuenow') === before) await nextTask();
    return ariaNumber(el, 'aria-valuenow');
  }

  // value: the number to reach. A value outside aria-valuemin/max fails before any key; otherwise
  // value_not_reached means keys were pressed and it did not get there: a key moved past it,
  // stopped moving the value, or moved it away, or the key or time limit ran out. state.value is
  // where it stopped.
  async function fillAriaRange(el, role, value) {
    const text = String(value ?? '').trim();
    const wanted = text && Number.isFinite(Number(text)) ? Number(text) : null;
    if (wanted == null) return { error: `An ARIA ${role} takes a number, not ${JSON.stringify(value)}` };
    const min = ariaNumber(el, 'aria-valuemin');
    const max = ariaNumber(el, 'aria-valuemax');
    let now = ariaNumber(el, 'aria-valuenow');
    if ((min != null && wanted < min && !sameRangeValue(wanted, min)) ||
        (max != null && wanted > max && !sameRangeValue(wanted, max))) {
      return actionError(`${wanted} is outside the ${role}'s range, ${min ?? '-∞'} to ${max ?? '∞'}`,
        'value_not_reached', 'Nothing was pressed. Fill a value from state.min to state.max.',
        { role, value: now, target: wanted, min, max, keys: 0, reason: 'out_of_range' });
    }
    lastActedElement = el;
    el.focus({ preventScroll: true });
    const start = Date.now();
    let keys = 0;
    if (!sameRangeValue(now, wanted) && (sameRangeValue(wanted, min) || sameRangeValue(wanted, max))) {
      now = await pressAriaRangeKey(el, sameRangeValue(wanted, min) ? 'Home' : 'End');
      keys++;
    }
    // Increase and decrease keys, the usual pair first: Right/Left on a horizontal slider, Up/Down
    // on a vertical one or a spinbutton. The other pair is tried when the first does not move
    // the value toward the target (a widget that handles only one pair, or a right-to-left one).
    const vertical = role === 'spinbutton' || el.getAttribute('aria-orientation') === 'vertical';
    const pairs = vertical ? [['ArrowUp', 'ArrowDown'], ['ArrowRight', 'ArrowLeft']]
      : [['ArrowRight', 'ArrowLeft'], ['ArrowUp', 'ArrowDown']];
    let pair = 0, moved = false, away = false, reason = null;
    while (!sameRangeValue(now, wanted)) {
      if (now == null) { reason = 'stuck'; break; }
      if (keys >= ARIA_RANGE_MAX_KEYS) { reason = 'key_limit'; break; }
      if (Date.now() - start >= ARIA_RANGE_MAX_MS) { reason = 'time_limit'; break; }
      const up = wanted > now;
      const before = now;
      now = await pressAriaRangeKey(el, pairs[pair][up ? 0 : 1]);
      keys++;
      if (now != null && (up ? now > before : now < before)) {
        moved = true;
        if (!sameRangeValue(now, wanted) && (up ? now > wanted : now < wanted)) { reason = 'stepped_past'; break; }
      } else if (!moved && pair === 0 && now != null) {
        away = now !== before;
        pair = 1;
      } else {
        reason = away && !moved ? 'moved_away' : 'stuck';
        break;
      }
    }
    const state = { role, value: now, target: wanted, min, max, keys };
    if (reason) {
      return { ...actionError(`The ${role} stopped at ${now}, not ${wanted}`, 'value_not_reached',
        `Keys were pressed, so the ${role} may have changed. ${ARIA_RANGE_HINTS[reason]}`,
        { ...state, reason }), retryable: false };
    }
    return { ok: true, tag: el.tagName.toLowerCase(), ...state };
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
    // The background reads the fields of cross-origin frames through their own content scripts (#354).
    const readable = readableFrames();
    return { fields, ...(frames.length ? { unavailableFrames: frames } : {}),
      ...(readable.length ? { frames: readable } : {}) };
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

  // textSeen (#402): how often text occurred when a new_only wait began; text then counts as
  // appeared only once it occurs more often. Such reads report text_count, the occurrences now.
  function waitForText(text, textGone, timeout = 10000, diagnose = false, textSeen = null) {
    if ((text == null && textGone == null) ||
        [text, textGone].some(t => t != null && (typeof t !== 'string' || !t.length)) ||
        !Number.isFinite(timeout) || timeout < 0 ||
        (textSeen != null && (text == null || !Number.isInteger(textSeen) || textSeen < 0))) {
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
        const counted = textSeen == null ? {} : { text_count: visibleText.split(text).length - 1 };
        const appeared = text == null ||
          (textSeen == null ? visibleText.includes(text) : counted.text_count > textSeen);
        const disappeared = textGone == null || !visibleText.includes(textGone);
        const elapsed = Date.now() - start;
        if (appeared && disappeared) resolve({ ok: true, text, text_gone: textGone, elapsed, ...counted });
        else if (elapsed >= timeout) resolve({ ok: false, text, text_gone: textGone, elapsed, ...counted,
          error: `Timed out after ${timeout}ms waiting for ${textSeen ? 'new ' : ''}visible text`,
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

  // Firefox gives getBoundingClientRect, mozInnerScreenX/Y, and the fallback's screenX/Y and
  // outer/inner sizes all in the page's CSS pixels, and full page zoom scales those (#307): at
  // 150% one CSS pixel spans 1.5 unzoomed ones, so the viewport origin and the point inside it
  // both shrink by the zoom. Their sum times the tab's zoom (browser.tabs.getZoom, from the
  // background) is the desktop point at 100% zoom, which the OS backends take. OS display
  // scaling is a separate step that this does not cover.
  // frame: the ref of a cross-origin frame in this document, x/y a point in that frame's
  // viewport (#343). Such a frame's own mozInnerScreenX/Y are 0, so the background converts
  // its point here, in the document that shows the frame.
  // scroll=false (a trusted drag's destination, #351) measures the target where it is, so the
  // source point taken before stays valid; a target out of view then fails the hit test.
  function invalidScreenPoint(x, y, width, height) {
    return { error: 'The OS point must be finite and inside the page or frame viewport',
      code: 'invalid_coordinates', retryable: false,
      state: { viewportX: x, viewportY: y, width, height },
      hint: 'Use finite viewport coordinates inside the visible page, or a visible element ref; browser chrome is not a page target.' };
  }

  function screenViewportError(x, y, width, height) {
    return ![x, y, width, height].every(Number.isFinite) || width <= 0 || height <= 0 ||
      x < 0 || y < 0 || x >= width || y >= height ? invalidScreenPoint(x, y, width, height) : null;
  }

  // The existing frame geometry uses translation only. Never authorize OS input through a
  // scaled/rotated frame (or transformed ancestor) using that translation approximation.
  function transformedScreenFrame(frame) {
    for (let node = frame; node; node = flatParent(node)) {
      const style = styleOf(node);
      if ((style.transform && style.transform !== 'none') || (style.perspective && style.perspective !== 'none') ||
          ['translate', 'rotate', 'scale'].some(key => style[key] && style[key] !== 'none') ||
          (style.zoom && !['1', 'normal'].includes(String(style.zoom)))) {
        return { error: 'Transformed frame coordinates cannot authorize OS input', code: 'invalid_coordinates',
          retryable: false, hint: 'Use a target in an untransformed frame or a tab of your own opened at the frame URL.' };
      }
    }
    return null;
  }

  // Map a point in a same-origin document through each enclosing frame, checking every viewport.
  function screenPointInPage(x, y, doc) {
    for (let frame; ; doc = frame.ownerDocument) {
      const view = doc?.defaultView || window;
      const invalid = screenViewportError(x, y, view.innerWidth, view.innerHeight);
      if (invalid) return invalid;
      frame = hostFrame(doc);
      if (!frame) return { x, y };
      const transformed = transformedScreenFrame(frame);
      if (transformed) return transformed;
      const offset = frameContentOffset(frame);
      x += offset.x;
      y += offset.y;
    }
  }

  function screenPoint(selector, x, y, click, first, zoom, modifiers, frame, scroll = true) {
    // Viewport point (element centre or x/y) plus its desktop position for OS-level clicks.
    let el = null;
    if (frame) {
      el = resolveElement(frame);
      if (!el || !FRAME_TAGS.has(el.tagName)) return notFound();
      const style = styleOf(el);
      const invalid = screenViewportError(x, y,
        el.clientWidth - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0),
        el.clientHeight - (parseFloat(style.paddingTop) || 0) - (parseFloat(style.paddingBottom) || 0));
      if (invalid) return invalid;
      const transformed = transformedScreenFrame(el);
      if (transformed) return transformed;
      const offset = frameContentOffset(el);
      x += offset.x;
      y += offset.y;
    } else if (selector) {
      el = click ? resolveTarget(selector, null, { first, pointer: true }) : resolveElement(selector);
      if (!el) return notFound();
      if (scroll) el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
      const rect = el.getBoundingClientRect();
      x = rect.left + rect.width / 2;
      y = rect.top + rect.height / 2;
    }
    const mapped = screenPointInPage(x, y, el ? docOf(el) : document);
    if (mapped.error) return mapped;
    ({ x, y } = mapped);
    if (click) {
      if (el) {
        const elsewhere = targetNotAtPoint(el, x, y);
        if (elsewhere) return elsewhere;
      } else if (!elementAtPoint(x, y).el) {
        return actionError('No page content at the viewport point; no OS input was authorized',
          'target_not_at_point', 'Use a point on visible page content or a visible element ref.',
          { viewportX: x, viewportY: y, hit: null });
      }
    }
    const scale = Number.isFinite(zoom) && zoom > 0 ? zoom : 1;
    const viewportLeft = Number.isFinite(window.mozInnerScreenX)
      ? window.mozInnerScreenX
      : window.screenX + Math.max(0, (window.outerWidth - window.innerWidth) / 2);
    const viewportTop = Number.isFinite(window.mozInnerScreenY)
      ? window.mozInnerScreenY
      : window.screenY + Math.max(0, window.outerHeight - window.innerHeight);
    const screenX = (viewportLeft + x) * scale, screenY = (viewportTop + y) * scale;
    if (![screenX, screenY].every(Number.isFinite) || (zoom != null && (!Number.isFinite(zoom) || zoom <= 0))) {
      return invalidScreenPoint(x, y, window.innerWidth, window.innerHeight);
    }
    if (click) {
      agentTrustedClickUntil = Date.now() + AGENT_TRUSTED_CLICK_MS;
      agentTrustedClickKeys = new Set(Array.isArray(modifiers) ? modifiers : []);
    }
    return {
      title: document.title,
      viewportX: x,
      viewportY: y,
      screenX,
      screenY,
      zoom: scale,
      devicePixelRatio: window.devicePixelRatio || 1,
    };
  }

  // An OS click lands on whatever is at the point, so a trusted click refuses when that is
  // neither the target nor inside it (an overlay, a sticky header, a point off the page).
  // A hit in one of the target's labels counts: the browser sends that click to the control,
  // as with a custom checkbox whose input sits under its label.
  function targetNotAtPoint(el, x, y) {
    const hit = elementAtPoint(x, y).el;
    const accepted = new Set([el, ...(el.labels || [])]);
    for (let node = hit; node; node = parentAcrossFrames(node)) if (accepted.has(node)) return null;
    return actionError('The element at the target\'s center is not the target; the trusted click was not made',
      'target_not_at_point',
      'Close or scroll away what covers the target (state.hit), or pass x/y on a visible part of it.',
      { viewportX: x, viewportY: y, hit: hit ? targetSummary(hit) : null });
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

  // The frame id of a cross-origin frame the background reads through its own content script
  // (#343), else -1. Frames under 24×24 CSS pixels (tracking pixels, hidden helpers) show no
  // content and are not read.
  function readableFrameId(el) {
    if (!FRAME_TAGS.has(el.tagName) || frameAccess(el).reason !== 'cross_origin') return -1;
    const rect = el.getBoundingClientRect();
    if (rect.width < 24 || rect.height < 24) return -1;
    let frameId = -1;
    try { frameId = browser.runtime.getFrameId(el); } catch {}
    return frameId > 0 ? frameId : -1;
  }

  // Where a frame's viewport starts in the top viewport, as [x, y]. The background shifts the
  // bounds a cross-origin frame reports by it (#354).
  function frameTopOffset(frame) {
    const rect = viewportRect(frame);
    const style = styleOf(frame);
    return [rect.left + (frame.clientLeft || 0) + (parseFloat(style.paddingLeft) || 0),
      rect.top + (frame.clientTop || 0) + (parseFloat(style.paddingTop) || 0)];
  }

  // The rendered cross-origin frames browser_find and browser_forms read (#354), in the order
  // of searchedDocuments.
  function readableFrames() {
    const out = [];
    for (const doc of searchedDocuments()) {
      let frames = [];
      try { frames = queryTreeSelectorAll('iframe, frame', doc); } catch {}
      for (const frame of frames) {
        const frameId = isHidden(frame) ? -1 : readableFrameId(frame);
        if (frameId > 0) out.push({ ref: issueRef(frame), frameId, offset: frameTopOffset(frame) });
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
    return el.openOrClosedAssignedSlot || el.assignedSlot || el.parentElement ||
      (el.parentNode?.nodeType === 11 ? el.parentNode.host : null);
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
        // The light-DOM parent, not flatParent's slot: later steps search inside it, and a
        // slot has neither the slotted siblings as descendants nor a box to point at.
        const parent = el => el.parentElement || (el.parentNode?.nodeType === 11 ? el.parentNode.host : null);
        matches = [...new Set((matches || [document.documentElement]).map(parent).filter(Boolean))];
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

  // ── Trusted keyboard (#346, #347) ──
  // Whether the field a trusted keyboard command types into has the page's focus: the bridge
  // asks before every key and stops when it does not. sensitive tells it to keep the text out
  // of everything it reports.
  function focusState(selector, first) {
    const el = resolveTarget(selector, null, { first });
    if (!el) return notFound();
    const destination = fillDestination(el);
    const active = activeElement();
    const focused = !!active && (active === el || active === destination || el.contains?.(active) ||
      destination.contains?.(active));
    return { ok: true, focused, hidden: !!document.hidden, sensitive: isSensitiveField(destination) };
  }

  // The bridge is about to send OS keys for ms milliseconds: they are not the user's input.
  function agentInput(ms) {
    agentTrustedKeysUntil = Date.now() + Math.min(Math.max(Number(ms) || 0, 0), 60000);
    return { ok: true };
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
  // clicks the form's default button, or submits a form that has none and no other text
  // input. A disabled default button submits nothing. Both go by form ownership (#288).
  function implicitSubmit(el) {
    const form = el.form;
    if (!form) return { effect: null };
    const button = formSubmitControl(form);
    if (button) {
      if (button.disabled) return { effect: null };
      button.click();
      return { effect: 'implicitSubmit' };
    }
    const blocking = formOwnedControls(form, 'input').filter(input =>
      IMPLICIT_SUBMIT_TYPES.has(String(input.type || 'text').toLowerCase()));
    if (blocking.length > 1) return { effect: null };
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.submit();
    return { effect: 'implicitSubmit' };
  }

  // An editing command at an editor's selection; effect null when the editor did not change.
  function editorCommand(host, command, inputType) {
    return editOutcome(runEditCommand(host, command), inputType);
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
    if (text != null) return insertEditableText(host, text);
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
    const opts = keyEventInit(key, mods);
    const field = keyField(target);
    // As for a user's key, the field has focus, and an editor a caret: where it was if inside
    // the target, else at the target's end.
    if (field && isEditableRegion(field)) {
      const at = isEditableRegion(target) ? target : field;
      if ((docOf(at).defaultView || window).getSelection && !selectionInside(at)) placeEditableCaret(at);
    } else if (field && activeElement() !== field) {
      field.focus({ preventScroll: true });
    }
    const typed = text ?? typedText(opts.key, mods);
    const plainEnter = opts.key === 'Enter' && !mods.ctrl && !mods.alt && !mods.meta;
    const result = { ok: true, key: keyCombination(key, mods), target: target.tagName?.toLowerCase(), effect: null };
    let applies = target.dispatchEvent(new KeyboardEvent('keydown', opts));
    if (applies && (typed != null || plainEnter)) applies = target.dispatchEvent(new KeyboardEvent('keypress', opts));
    if (!applies) result.prevented = true;
    else if (field) Object.assign(result, isEditableRegion(field) ? editorKey(field, opts.key, mods, typed)
      : fieldKey(field, opts.key, mods, typed));
    const note = !applies || result.effect ? null : keyNote(target, field, opts.key, mods);
    if (note) result.note = note;
    target.dispatchEvent(new KeyboardEvent('keyup', opts));
    if (result.effect && result.effect !== 'implicitSubmit') lastFilledField = field;
    return result;
  }

  // keydown/keyup init for key with modifiers ({ctrl, shift, alt, meta}).
  function keyEventInit(key, mods) {
    return { ...keyboardKeyOptions(key), bubbles: true, cancelable: true, composed: true,
      ctrlKey: !!mods.ctrl, shiftKey: !!mods.shift, altKey: !!mods.alt, metaKey: !!mods.meta };
  }

  // repeat presses the key up to KEYPRESS_MAX_REPEAT times in one command (#311), a task apart
  // so the page renders between presses as between a user's. Each press resolves its target
  // again; the result is the last press's with repeat, or the first failure with pressed.
  const KEYPRESS_MAX_REPEAT = 100;

  async function keypressRepeated(selector, key, modifiers, text, first, repeat) {
    let result;
    for (let pressed = 0; pressed < repeat; pressed++) {
      if (pressed) await nextTask();
      result = keypress(selector, key, modifiers, text, first);
      if (result.error) return pressed ? { ...result, pressed } : result;
    }
    return { ...result, repeat };
  }

  function pressKey(selector, key, modifiers, text, first, repeat) {
    if (repeat == null || repeat === 1) return keypress(selector, key, modifiers, text, first);
    if (!Number.isInteger(repeat) || repeat < 1 || repeat > KEYPRESS_MAX_REPEAT) {
      return { error: `repeat must be an integer from 1 to ${KEYPRESS_MAX_REPEAT}` };
    }
    return keypressRepeated(selector, key, modifiers, text, first, repeat);
  }

  function doubleClick(selector, first, modifiers) {
    const el = resolveTarget(selector, null, { first, pointer: true });
    if (!el) return notFound();
    el.scrollIntoView({ behavior: 'instant', block: 'center' });
    const r = el.getBoundingClientRect();
    const init = { bubbles: true, cancelable: true, composed: true, view: el.ownerDocument?.defaultView || window,
      clientX: r.x + r.width/2, clientY: r.y + r.height/2, button: 0, ...modifierFlags(modifiers) };
    watchPopups(el);
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

  // The controls matching selector whose form owner is form, in tree order: its descendants
  // and the controls a form attribute associates with it, but not descendants whose form
  // attribute names another form (#288). Searched in the form's tree, which a form attribute
  // cannot leave; form.elements is not used because it omits <input type=image>.
  function formOwnedControls(form, selector) {
    const root = form.getRootNode?.() || docOf(form);
    return Array.from(root.querySelectorAll(selector)).filter(control => control.form === form);
  }

  // The form's default button: its first submit control by form ownership. A <button>
  // without a type (or with an unknown one) is a submit button, and its type property reads
  // "submit".
  function formSubmitControl(form) {
    return formOwnedControls(form, 'button, input').find(el =>
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
    movePointerTo(from, { view: from.ownerDocument?.defaultView || window, clientX: fx, clientY: fy });
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
