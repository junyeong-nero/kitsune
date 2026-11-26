// Chrome build only (#589): the MV3 service worker loads the background scripts that Firefox's
// manifest lists as its background page.

'use strict';

importScripts('/chrome/compat.js', '/background/tool-approvals.js', '/background/background.js',
  '/background/history.js');

// Chrome suspends an idle service worker, and with it the reconnect timer: a worker suspended
// while the bridge is down would never reconnect. An open connection keeps it running, since
// the heartbeat exchanges a message every 10 s (Chrome 116+). The alarm wakes a suspended worker
// to try again (Chrome runs alarms at most every 30 s).
const RECONNECT_ALARM = 'kitsune-reconnect';
chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === RECONNECT_ALARM) connectUnlessConnected();
});
