// Issue #222. Run with: node tests/screenshot_capture.cjs (no npm/browser dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const copy = value => JSON.parse(JSON.stringify(value));
const event = () => ({ addListener() {}, removeListener() {} });

function harness() {
  const captures = [], encodings = [], operations = [], targets = [];
  const tab = { id: 2, url: 'https://example.test/', width: 100, height: 100 };
  const context = vm.createContext({
    console: { log() {}, error() {} },
    WebSocket: class {},
    setTimeout: (fn, delay) => { if (delay <= 160) queueMicrotask(fn); return 1; }, clearTimeout() {},
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
    browser: {
      tabs: {
        onCreated: event(), onUpdated: event(), onRemoved: event(),
        get: async () => tab, query: async () => [tab],
        captureTab: async (id, options) => {
          captures.push([id, copy(options)]);
          return `data:image/${options.format};base64,/9j/2Q==`;
        },
      },
      storage: { local: { get: async () => ({}), set: async () => {} } },
      runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
    },
    OffscreenCanvas: class {
      constructor(width, height) { Object.assign(this, { width, height }); }
      getContext() { return { drawImage() {}, fillRect() {} }; }
      async convertToBlob(options) {
        encodings.push(copy(options));
        return { type: options.type, arrayBuffer: async () => Uint8Array.from([255, 216, 255, 217]).buffer };
      }
    },
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  context.loadDataUrl = async () => ({ width: 100, height: 100 });
  context.forwardToContent = async (action, params) => {
    operations.push(action);
    if (action === 'markScreenshotTarget') {
      targets.push(params.selector);
      return { selector: '[data-kitsune-capture-1]', attribute: 'data-kitsune-capture-1' };
    }
    if (action === 'captureMeasure') return { x: 0, y: 0, w: 40, h: 30, vw: 100, vh: 100, sy: 0 };
    if (action === 'fullPageMetrics') return { viewportWidth: 100, viewportHeight: 100, docWidth: 100, docHeight: 100 };
    if (action === 'captureScroll') return { sy: 0 };
    return { ok: true };
  };
  return { context, captures, encodings, operations, targets };
}

async function formats() {
  for (const format of ['png', 'jpeg']) {
    const h = harness();
    const viewport = await h.context.dispatchCommand({ action: 'screenshot', tabId: 2, format });
    assert.ok(viewport.dataUrl.startsWith(`data:image/${format};base64,`));
    assert.deepEqual(h.captures, [[2, { format, quality: 90 }]]);
    const element = await h.context.dispatchCommand({ action: 'elementScreenshot', tabId: 2, selector: 's1e0', format });
    assert.ok(element.dataUrl?.startsWith(`data:image/${format};base64,`), JSON.stringify(element));
    assert.equal(element.complete, true);
    assert.equal(h.operations.at(-1), 'unmarkScreenshotTarget');
    const fullPage = await h.context.dispatchCommand({ action: 'fullPageScreenshot', tabId: 2, format });
    assert.ok(fullPage.dataUrl?.startsWith(`data:image/${format};base64,`), JSON.stringify(fullPage));
    assert.equal(h.operations.at(-1), 'restoreFixed');
    assert.deepEqual(h.encodings, [{ type: `image/${format}`, quality: 0.9 }, { type: `image/${format}`, quality: 0.9 }]);
    assert.ok(h.captures.slice(1).every(([, options]) => options.format === 'png'), 'stitch lossless segments before final encoding');
  }
  const h = harness();
  assert.ok((await h.context.captureScreenshot({ tabId: 2 })).dataUrl.startsWith('data:image/png;'));
  assert.deepEqual(h.captures, [[2, { format: 'png', quality: 90 }]]);
  assert.match((await h.context.captureScreenshot({ format: 'gif' })).error, /format must be png or jpeg/);
  assert.equal(h.captures.length, 1, 'invalid format does not capture');
}

async function selectors() {
  for (const selector of ['#shadow-button', 'text=Capture', 'role=button[name="Capture"]',
    'button:has-text("Capture") >> nth=0', 's1e0']) {
    const h = harness();
    const result = await h.context.elementScreenshot(2, selector, 1, false, 'png');
    assert.ok(result.dataUrl, JSON.stringify(result));
    assert.deepEqual(h.targets, [selector], 'all target spellings resolve before measurement');
    assert.equal(h.operations.at(-1), 'unmarkScreenshotTarget');
  }
  const h = harness();
  const unsupported = { error: 'Unsupported selector', code: 'unsupported_selector', retryable: false,
    hint: 'Pass text= or role=', state: { selector: 'xpath=//button' } };
  h.context.forwardToContent = async () => unsupported;
  assert.deepEqual(copy(await h.context.elementScreenshot(2, 'xpath=//button', 1, false, 'png')), unsupported);
  assert.equal(h.captures.length, 0, 'selector failure dispatches no capture');
}

async function diagnostics() {
  const h = harness();
  for (const cause of [new Error('An unexpected error occurred'), new TypeError('Capture surface lost'), 'plain rejection']) {
    h.context.browser.tabs.captureTab = async () => { throw cause; };
    const result = copy(await h.context.captureScreenshot({ tabId: 2 }));
    assert.equal(result.code, 'screenshot_failed');
    assert.equal(result.state.stage, 'tabs.captureTab');
    assert.equal(result.state.tabId, 2);
    assert.equal(result.state.cause, cause.message || cause);
    assert.equal(result.retryable, false, 'an unknown failure is not claimed safe to retry');
    assert.match(result.hint, /wait.*before retrying/);
    assert.match(result.hint, /do not retry blindly/);
    assert.equal(result.dataUrl, undefined);
  }
  for (const failureAt of ['captureMeasure', 'captureScrollIntoView', 'captureScroll']) {
    const h = harness();
    const forward = h.context.forwardToContent;
    h.context.forwardToContent = async (action, params) => {
      if (action === failureAt) return { error: 'Content connection lost', code: 'connection_lost', retryable: true, hint: 'Reconnect the extension.' };
      return forward(action, params);
    };
    const result = copy(await (failureAt === 'captureScroll'
      ? h.context.fullPageScreenshot(2, false, 'jpeg') : h.context.elementScreenshot(2, 's1e0', 1, false, 'jpeg')));
    assert.equal(result.code, 'connection_lost', JSON.stringify(result));
    assert.equal(result.retryable, true);
    assert.equal(result.state.stage, failureAt);
    assert.match(result.error, /Content connection lost/);
    assert.equal(result.hint, 'Reconnect the extension.');
    assert.equal(result.dataUrl, undefined);
    if (failureAt !== 'captureScroll') assert.equal(h.operations.at(-1), 'unmarkScreenshotTarget');
  }
  {
    const h = harness();
    h.context.OffscreenCanvas.prototype.convertToBlob = async () => { throw new Error('Canvas encoding unavailable'); };
    const result = await h.context.elementScreenshot(2, 's1e0', 1, false, 'jpeg');
    assert.equal(result.state.stage, 'encode element image');
    assert.match(result.error, /Canvas encoding unavailable/);
    assert.equal(h.operations.at(-1), 'unmarkScreenshotTarget');
  }
  {
    const h = harness();
    h.context.OffscreenCanvas.prototype.convertToBlob = async () => ({ type: 'image/png' });
    const result = await h.context.fullPageScreenshot(2, false, 'jpeg');
    assert.match(result.error, /encoder returned image\/png instead of image\/jpeg/);
    assert.equal(result.state.stage, 'encode full-page image');
    assert.equal(result.dataUrl, undefined, 'unexpected encoder fallback is never mislabeled as JPEG');
    assert.equal(h.operations.at(-1), 'restoreFixed');
  }
}

(async () => {
  await formats();
  await diagnostics();
  await selectors();
  console.log('screenshot capture: ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
