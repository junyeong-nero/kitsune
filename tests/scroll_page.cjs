// Run with: node tests/scroll_page.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

function scrollable(id, overrides = {}) {
  return Object.assign({
    id, tagName: 'DIV', parentElement: null,
    scrollTop: 0, scrollLeft: 0, scrollHeight: 1600, clientHeight: 600,
    scrollWidth: 1800, clientWidth: 800,
    style: { overflowY: 'auto', overflowX: 'auto' }, calls: [],
    scrollTo(opts) {
      this.calls.push(opts);
      assert.equal(opts.behavior, 'instant', 'scrolling must complete before measuring');
      if (opts.top !== undefined) this.scrollTop = Math.max(0, Math.min(opts.top, this.scrollHeight - this.clientHeight));
      if (opts.left !== undefined) this.scrollLeft = Math.max(0, Math.min(opts.left, this.scrollWidth - this.clientWidth));
    },
    scrollBy(opts) {
      this.scrollTo({ ...opts, top: this.scrollTop + (opts.top || 0), left: this.scrollLeft + (opts.left || 0) });
    },
  }, overrides);
}

function setup({ maxY = 0, maxX = 0, center = null, elements = {} } = {}) {
  let listener;
  let centerChecks = 0;
  const root = scrollable('root', { scrollHeight: maxY + 600, scrollWidth: maxX + 800 });
  const window = {
    scrollX: 0, scrollY: 0, innerWidth: 800, innerHeight: 600, calls: [],
    scrollTo(opts) {
      this.calls.push(opts);
      assert.equal(opts.behavior, 'instant');
      if (opts.top !== undefined) this.scrollY = Math.max(0, Math.min(opts.top, maxY));
      if (opts.left !== undefined) this.scrollX = Math.max(0, Math.min(opts.left, maxX));
    },
    scrollBy(opts) {
      this.scrollTo({ ...opts, top: this.scrollY + (opts.top || 0), left: this.scrollX + (opts.left || 0) });
    },
    getComputedStyle: el => el.style,
  };
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: s => s }, window,
    document: {
      body: root, scrollingElement: root,
      querySelector: selector => elements[selector] || null,
      elementFromPoint(x, y) {
        assert.equal(x, 400);
        assert.equal(y, 300);
        centerChecks++;
        return center;
      },
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  return {
    window, centerChecks: () => centerChecks,
    send: (direction, amount, selector) => new Promise(resolve => {
      listener({ action: 'scroll', direction, amount, selector }, {}, result => resolve(JSON.parse(JSON.stringify(result))));
    }),
  };
}

(async () => {
  const unused = scrollable('unused');
  const page = setup({ maxY: 1000, maxX: 1000, center: unused });
  assert.deepEqual(await page.send('down', 900), {
    ok: true, scrollY: 900, scrollX: 0, scrolled_by: 900, scrolledPx: 900, target: 'window',
  });
  assert.equal((await page.send('down', 900)).scrolled_by, 100, 'report clamped movement, not requested amount');
  assert.equal((await page.send('up', 250)).scrolled_by, -250);
  assert.equal((await page.send('right', 350)).scrolled_by, 350);
  assert.equal((await page.send('left', 100)).scrolled_by, -100);
  assert.equal(page.centerChecks(), 0, 'do not use fallback when window moved');
  assert.equal(unused.scrollTop, 0);
  assert.equal((await page.send('top')).scrolled_by, -750);
  assert.equal((await page.send('bottom')).scrolled_by, 1000);

  const inner = scrollable('results');
  const leaf = scrollable('leaf', { scrollHeight: 600, scrollWidth: 800, parentElement: inner });
  const nested = setup({ center: leaf });
  const fallback = await nested.send('down', 900);
  assert.equal(fallback.target, '#results');
  assert.equal(fallback.scrolled_by, 900);
  assert.equal(fallback.scrollY, 0, 'window offset remains honest when container scrolled');
  assert.equal(inner.scrollTop, 900);
  assert.equal((await nested.send('up', 300)).scrolled_by, -300);
  assert.equal((await nested.send('bottom')).scrolled_by, 400, 'bottom uses container height');
  assert.equal((await nested.send('top')).scrolled_by, -1000);
  assert.equal((await nested.send('right', 700)).scrolled_by, 700);
  assert.equal((await nested.send('left', 200)).scrolled_by, -200);
  assert.equal(inner.scrollLeft, 500);

  const outer = scrollable('outer');
  const full = scrollable('full', { scrollTop: 1000, parentElement: outer });
  const atBoundary = setup({ maxY: 100, center: full });
  atBoundary.window.scrollY = 100;
  assert.equal((await atBoundary.send('down', 100)).target, '#outer', 'skip ancestors at their directional boundary');
  assert.equal(full.scrollTop, 1000);
  assert.equal(outer.scrollTop, 100);

  const hidden = scrollable('hidden', { style: { overflowY: 'hidden', overflowX: 'auto' }, parentElement: outer });
  const overflow = setup({ center: hidden });
  assert.equal((await overflow.send('down', 100)).target, '#outer', 'overflow hidden is not a fallback');
  assert.equal(hidden.scrollTop, 0);
  assert.equal((await overflow.send('right', 100)).target, '#hidden', 'use axis-specific overflow');

  const explicit = scrollable('explicit', { scrollHeight: 2600 });
  const targeted = setup({ maxY: 3000, center: unused, elements: { '#explicit': explicit } });
  assert.equal((await targeted.send('bottom', 1, '#explicit')).scrolled_by, 2000);
  assert.equal((await targeted.send('down', 500, '#explicit')).scrolled_by, 0);
  assert.equal((await targeted.send('top', 1, '#explicit')).scrolled_by, -2000);
  assert.equal((await targeted.send('down', 200, '#explicit')).target, '#explicit');
  assert.equal(targeted.window.scrollY, 0, 'selector scroll remains targeted');
  assert.equal(targeted.centerChecks(), 0, 'explicit target must not fall back elsewhere');

  const stuck = setup({ center: scrollable('stuck', { scrollTop: 1000 }) });
  const noOp = await stuck.send('down', 900);
  assert.equal(noOp.scrolled_by, 0);
  assert.equal(noOp.scrolledPx, 0, 'legacy field also reports actual movement');
  assert.equal(noOp.scrollY, 0);
  assert.equal(noOp.target, 'window');
  assert.equal((await setup().send('down', 900)).scrolled_by, 0, 'no center element is a valid no-op');
  const zero = setup({ center: unused });
  assert.equal((await zero.send('down', 0)).scrolled_by, 0);
  assert.equal(zero.centerChecks(), 0, 'zero amount does not trigger fallback');
  const defaults = setup({ maxY: 1000 });
  assert.equal((await defaults.send(undefined, undefined)).scrolled_by, 500);
  assert.equal((await defaults.send('down', 'not a number')).scrolled_by, 500);

  console.log('Scroll page window, container fallback, targeting, and no-op checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
