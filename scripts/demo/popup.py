"""Kitsune's toolbar popup in the demo Zen, driven as the user would through the browser's debugger.

The popup opens the way a toolbar click opens it (the browser action's openPopup), and its buttons
are clicked in the popup's own page, so popup.js sends its real messages. The same privileged
scripts drive the popup in tests/test_real_browser_handoff.py.
"""
import json

from common import STATE
from real_browser_harness import EXTENSION_ID, RemoteDebugger

ACTION = f"""const {{ExtensionParent}} = ChromeUtils.importESModule('resource://gre/modules/ExtensionParent.sys.mjs');
  const policy = WebExtensionPolicy.getByID({json.dumps(EXTENSION_ID)});
  const action = ExtensionParent.apiManager.global.browserActionFor(policy.extension);
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const url = `moz-extension://${{policy.mozExtensionHostname}}/popup/popup.html`;
  const find = () => [...win.document.querySelectorAll('browser')].find(b => b.currentURI?.spec === url);
  const shown = () => {{ const b = find(); return b?.closest('panel')?.state === 'open' ? b : null; }};
  const sleep = ms => new Promise(resolve => win.setTimeout(resolve, ms));"""

# Zen keeps an extension's button in the extensions panel until it is pinned to the toolbar.
PIN = f"""(async () => {{
  {ACTION}
  const {{CustomizableUI}} = win;
  CustomizableUI.addWidgetToArea(action.widget.id, CustomizableUI.AREA_NAVBAR);
  return CustomizableUI.getPlacementOfWidget(action.widget.id);
}})()"""

BADGE = f"""(async () => {{
  {ACTION}
  return action.action.getProperty(null, 'badgeText') || '';
}})()"""

# Opens the popup unless it is open, then clicks the button labelled %s (none for null). The popup opens only once
# Zen is the active app, and openPopup's promise can outlive a popup that never shows, so the script focuses Zen
# and asks again while no panel is showing. Zen sometimes lays the popup out 32 px wide, or flips between that
# and its real width; such a popup is closed and opened again until it keeps its width.
CLICK = f"""(async () => {{
  {ACTION}
  const label = %s;
  const opening = () => ['open', 'showing'].includes(find()?.closest('panel')?.state);
  const wide = () => (shown()?.getBoundingClientRect().width ?? 0) >= 200;
  const steady = async () => {{
    for (let j = 0; j < 8; j++) {{ if (!wide()) return false; await sleep(100); }}
    return true;
  }};
  if (!shown()) win.focus();
  for (let i = 0; i < 6 && !(shown() && await steady()); i++) {{
    if (shown()) {{ shown().closest('panel').hidePopup(); await sleep(500); }}
    await sleep(400);
    if (!opening()) action.openPopup(win);
    for (let j = 0; j < 15 && !shown(); j++) await sleep(100);
    for (let j = 0; j < 10 && shown() && !wide(); j++) await sleep(100);
  }}
  const popup = shown();
  if (!popup || !wide()) throw new Error('the toolbar popup did not open at its width');
  if (label === null) return true;
  const name = 'kitsune-demo-popup-' + Math.random();
  const reply = new Promise(resolve => popup.messageManager.addMessageListener(name, m => resolve(m.data)));
  popup.messageManager.loadFrameScript('data:,' + encodeURIComponent(`(() => {{
    const button = [...content.document.querySelectorAll('button')]
      .find(b => b.textContent === ${{JSON.stringify(label)}} && !b.disabled);
    if (button) button.click();
    sendAsyncMessage(${{JSON.stringify(name)}}, Boolean(button));
  }})()`), false);
  return await reply;
}})()"""

CLOSE = f"""(async () => {{
  {ACTION}
  find()?.closest('panel')?.hidePopup();
  return true;
}})()"""


def _run(script):
    debugger = RemoteDebugger(json.loads(STATE.read_text())["rdp_port"])
    debugger.sock.settimeout(60)  # CLICK may reopen the popup a few times
    try:
        return debugger.evaluate_in_parent(script)
    finally:
        debugger.close()


def pin():
    return _run(PIN)


def badge():
    return _run(BADGE)


def show():
    return _run(CLICK % "null")


def click(label):
    """Open the popup if needed and click its button labelled label; False if there is none."""
    return _run(CLICK % json.dumps(label))


def close():
    return _run(CLOSE)
