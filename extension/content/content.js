// Claude Bridge for Zen - Content Script
// Handles all DOM interaction on web pages

(function() {
  'use strict';
  const CONTENT_VERSION = 16;
  if (window.__claudeBridgeVersion >= CONTENT_VERSION) return;
  window.__claudeBridgeVersion = CONTENT_VERSION;

  // Element reference map (capped to prevent memory leaks in long sessions)
  const REF_MAP_LIMIT = 10000;
  const refMap = {};
  let refCounter = 0;

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
      'getPageText':           () => getPageText(msg.selector),
      'getAccessibilityTree':  () => getAccessibilityTree(msg.depth || 6),
      'getFormFields':         () => getFormFields(),
      'click':                 () => clickElement(msg.selector, msg.coords),
      'type':                  () => typeText(msg.selector, msg.text, msg.clear),
      'setEditableContent':    () => setEditableContent(msg.selector, msg.value, msg.format || 'text', msg.clear !== false),
      'scroll':                () => scrollPage(msg.direction, msg.amount, msg.selector),
      'hover':                 () => hoverElement(msg.selector, msg.coords),
      'fill':                  () => fillFieldSafe(msg.selector, msg.value),
      'find':                  () => findElements(msg.query),
      'executeJS':             () => executeJS(msg.code),
      'highlight':             () => highlightElement(msg.selector),
      'clearHighlight':        () => clearHighlight(),
      'waitForElement':        () => waitForElement(msg.selector, msg.timeout, msg.pollInterval),
      'waitForResult':         () => waitForResult(msg.code, msg.timeout, msg.pollInterval),
      // ── 1.4.0 additions ──
      'query':                 () => queryElements(msg.selector, msg.fields, msg.limit),
      'getHTML':               () => getHTML(msg.selector),
      'getLinks':              () => getLinks(msg.internalOnly),
      'getImages':             () => getImages(),
      'getMeta':               () => getMeta(),
      'getStructuredData':     () => getStructuredData(),
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
    if (!handler) return false;

    try {
      const result = handler();
      if (result && typeof result.then === 'function') {
        // Async handler — keep message channel open
        result.then(r => sendResponse(r)).catch(e => sendResponse({ error: e.message }));
        return true;
      }
      sendResponse(result);
    } catch (e) {
      sendResponse({ error: e.message });
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
    pruneRefMap();
    refCounter = 0;
    
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

      const ref = `r${refCounter++}`;
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
      if (el.value && isInteractive) node.value = String(el.value).slice(0, 80);
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

  // ── Find Elements ──
  function findElements(query) {
    pruneRefMap();
    const q = query.toLowerCase();
    const results = [];

    // Try as CSS selector
    try {
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
    const ref = `r${refCounter++}`;
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
      [el.value, 2],
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
    if (!el) return { error: 'Element not found' };

    if (coords) {
      el = nearestInteractiveAncestor(el) || el;
    } else {
      el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'center' });
    }

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

    for (const type of ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
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

    return { ok: true, tag: el.tagName.toLowerCase(), text: (el.textContent || '').trim().slice(0, 60) };
  }

  // ── Type ──
  function typeText(selector, text, clear) {
    const el = resolveElement(selector);
    if (!el) return { error: 'Element not found' };

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
    const el = resolveElement(selector);
    if (!el) return { error: 'Element not found' };

    const target = getEditableTarget(el);
    if (!target) return { error: 'Element is not editable' };

    target.focus();
    const text = String(value ?? '');

    if (target.isContentEditable || target.contentEditable === 'true') {
      if (clear) {
        if (format === 'html') {
          target.innerHTML = text;
        } else {
          target.innerText = text;
        }
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
    const target = selector ? resolveElement(selector) : null;
    // amount is pixels; keep this aligned with the MCP tool contract.
    const parsed = Number(amount);
    const dist = Number.isFinite(parsed) ? Math.round(parsed) : 500;
    const opts = { behavior: 'smooth' };

    if (direction === 'top') {
      (target || window).scrollTo({ top: 0, ...opts });
    } else if (direction === 'bottom') {
      (target || window).scrollTo({ top: document.body.scrollHeight, ...opts });
    } else {
      const deltas = { up: { top: -dist }, down: { top: dist }, left: { left: -dist }, right: { left: dist } };
      (target || window).scrollBy({ ...deltas[direction || 'down'], ...opts });
    }

    return { ok: true, scrollY: window.scrollY, scrollX: window.scrollX, scrolledPx: dist };
  }

  // ── Hover ──
  function hoverElement(selector, coords) {
    const el = resolveElement(selector, coords);
    if (!el) return { error: 'Element not found' };

    const rect = el.getBoundingClientRect();
    const x = rect.x + rect.width / 2;
    const y = rect.y + rect.height / 2;

    for (const type of ['mouseenter', 'mouseover', 'mousemove']) {
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, clientX: x, clientY: y }));
    }

    return { ok: true, tag: el.tagName.toLowerCase() };
  }

  // ── Fill Form ──
  async function fillFieldSafe(selector, value) {
    let result;
    try { result = fillField(selector, value); } catch(e) { result = { error: e.message }; }
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
    if (!el) return { error: 'Element not found' };

    // Auto-pierce shadow DOM: if element isn't a standard input, look inside shadow root
    const target = getFillTarget(el);

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

  function fillViaPageContext(selector, value) {
    // Last resort: inject script into page context to pierce closed shadow DOM
    // Uses JSON.stringify to safely embed user input and prevent template injection
    return new Promise((resolve) => {
      const script = document.createElement('script');
      const callbackName = '__claudeFill_' + Date.now();
      const safeSelector = JSON.stringify(selector);
      const safeValue = JSON.stringify(String(value));
      const safeCb = JSON.stringify(callbackName);
      script.textContent = '(function(){' +
        'var el=document.querySelector(' + safeSelector + ');' +
        'if(!el){window[' + safeCb + ']="not found";return;}' +
        'var target=el.shadowRoot?(el.shadowRoot.querySelector("input,textarea,select")||el):(el.querySelector("input,textarea,select")||el);' +
        'target.focus();' +
        'var proto=target.tagName==="TEXTAREA"?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;' +
        'var setter=Object.getOwnPropertyDescriptor(proto,"value");' +
        'if(setter&&setter.set)setter.set.call(target,' + safeValue + ');' +
        'else target.value=' + safeValue + ';' +
        'target.dispatchEvent(new Event("input",{bubbles:true}));' +
        'target.dispatchEvent(new Event("change",{bubbles:true}));' +
        'window[' + safeCb + ']="ok:"+target.tagName;' +
      '})();';
      document.documentElement.appendChild(script);
      script.remove();
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

      const ref = `r${refCounter++}`;
      refMap[ref] = el;

      fields.push({
        ref,
        tag: el.tagName.toLowerCase(),
        type: el.type || 'text',
        name: el.name || '',
        id: el.id || '',
        placeholder: el.placeholder || '',
        value: (el.value || '').slice(0, 100),
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

  // ── Wait For Element ──
  function waitForElement(selector, timeout, pollInterval) {
    const maxWait = timeout || 10000;
    const interval = pollInterval || 200;
    const start = Date.now();

    return new Promise((resolve) => {
      function poll() {
        const el = document.querySelector(selector);
        if (el && !isHidden(el)) {
          const elapsed = Date.now() - start;
          const ref = `r${refCounter++}`;
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

  // ── Highlight ──
  let highlightOverlay = null;

  function highlightElement(selector) {
    clearHighlight();
    const el = resolveElement(selector);
    if (!el) return { error: 'Element not found' };

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
    if (selector.startsWith('r') && refMap[selector]) return refMap[selector];
    
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
    if (el.onclick || el.hasAttribute('tabindex') || el.contentEditable === 'true') return true;
    const role = el.getAttribute('role');
    if (role && ['button', 'link', 'textbox', 'checkbox', 'radio', 'combobox', 'tab', 'menuitem', 'switch', 'slider'].includes(role)) return true;
    return false;
  }

  function inferRole(el) {
    const map = { 'A': 'link', 'BUTTON': 'button', 'INPUT': el.type || 'textbox', 'SELECT': 'combobox', 'TEXTAREA': 'textbox' };
    return map[el.tagName] || '';
  }

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
    let nodes;
    try { nodes = document.querySelectorAll(selector); } catch (e) { return { error: 'Invalid selector: ' + e.message }; }
    const cap = Math.min(limit || QUERY_LIMIT, QUERY_LIMIT);
    const want = (fields && fields.length) ? fields : DEFAULT_FIELDS;
    const items = [];
    for (let i = 0; i < nodes.length && items.length < cap; i++) {
      const el = nodes[i];
      const row = { tag: el.tagName.toLowerCase() };
      for (const f of want) {
        try {
          if (f === 'text') row.text = (el.innerText || el.textContent || '').trim().slice(0, 400);
          else if (f === 'html') row.html = el.innerHTML.slice(0, 4000);
          else if (f === 'href') row.href = el.href || el.getAttribute('href') || undefined;
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
            for (const att of el.attributes) a[att.name] = att.value.slice(0, 200);
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
    if (!el) return { error: 'Element not found' };
    const outer = el.outerHTML || '';
    const truncated = outer.length > 100000;
    return { html: outer.slice(0, 100000), length: outer.length, ...(truncated && { truncated: true }) };
  }

  function getBounds(selector) {
    const el = resolveElement(selector);
    if (!el) return { error: 'Element not found' };
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
    if (!el) return { error: 'Element not found' };
    const cs = window.getComputedStyle(el);
    if (properties && properties.length) {
      const out = {};
      for (const p of properties) out[p] = cs.getPropertyValue(p);
      return { computed: out };
    }
    // No props specified → return commonly-needed ones
    const common = ['display','position','visibility','opacity','width','height','color','background-color','font-size','font-family','font-weight','margin','padding','border','z-index','overflow','cursor'];
    const out = {};
    for (const p of common) out[p] = cs.getPropertyValue(p);
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
      const links = el.querySelectorAll('a').length;
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
    const root = selector ? document.querySelector(selector) : document.body;
    if (!root) return { error: 'Element not found' };
    function walk(node, depth) {
      if (node.nodeType === 3) return node.textContent.replace(/\s+/g, ' ');
      if (node.nodeType !== 1) return '';
      const tag = node.tagName.toLowerCase();
      if (['script','style','noscript','svg','iframe','nav','aside','footer','header','form'].includes(tag)) return '';
      const children = () => Array.from(node.childNodes).map(c => walk(c, depth+1)).join('');
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
            out += '- ' + Array.from(li.childNodes).map(c => walk(c, depth+1)).join('').trim() + '\n';
          }
          return out + '\n';
        }
        case 'ol': {
          let out = '\n', i = 1;
          for (const li of node.querySelectorAll(':scope > li')) {
            out += (i++) + '. ' + Array.from(li.childNodes).map(c => walk(c, depth+1)).join('').trim() + '\n';
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
    const md = walk(root, 0).replace(/\n{3,}/g, '\n\n').trim();
    const truncated = md.length > 80000;
    return { markdown: md.slice(0, 80000), length: md.length, ...(truncated && { truncated: true }) };
  }

  // ── Interaction additions ──
  function selectOption(selector, value, byText) {
    const el = resolveElement(selector);
    if (!el) return { error: 'Element not found' };
    if (el.tagName !== 'SELECT') return { error: 'Element is not a <select>' };
    let matched = null;
    for (const opt of el.options) {
      if (byText ? (opt.text === value || opt.text.trim() === String(value).trim()) : opt.value === value) {
        matched = opt; break;
      }
    }
    if (!matched) return { error: `No matching option for ${byText ? 'text' : 'value'}: ${value}` };
    el.value = matched.value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('input', { bubbles: true }));
    return { ok: true, value: el.value, text: matched.text };
  }

  function checkBox(selector, checked) {
    const el = resolveElement(selector);
    if (!el) return { error: 'Element not found' };
    if (el.type !== 'checkbox' && el.type !== 'radio') return { error: 'Element is not a checkbox/radio' };
    el.checked = !!checked;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return { ok: true, checked: el.checked };
  }

  function focusEl(selector) {
    const el = resolveElement(selector);
    if (!el) return { error: 'Element not found' };
    el.focus();
    return { ok: true, active: document.activeElement === el };
  }

  function blurEl(selector) {
    const el = selector ? resolveElement(selector) : document.activeElement;
    if (!el) return { error: 'Element not found' };
    if (typeof el.blur === 'function') el.blur();
    return { ok: true };
  }

  function keypress(selector, key, modifiers, text) {
    const target = selector ? resolveElement(selector) : (document.activeElement || document.body);
    if (!target) return { error: 'No target' };
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
    if (!el) return { error: 'Element not found' };
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
    return { ok: true };
  }

  function submitForm(selector) {
    let form;
    if (selector) {
      const el = resolveElement(selector);
      if (!el) return { error: 'Element not found' };
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
          el.value = value;
          el.dispatchEvent(new Event('change', { bubbles: true }));
          results[key] = { ok: true, tag: 'select', value: el.value };
        } else if (el.type === 'checkbox' || el.type === 'radio') {
          el.checked = !!value;
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
    if (!from) return { error: 'from element not found' };
    if (!to) return { error: 'to element not found' };
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
