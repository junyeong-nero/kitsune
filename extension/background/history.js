// History and bookmark lookup (#140, docs/api-reference.md#history-and-bookmarks).
// Read-only. It needs the optional history/bookmarks permissions, which only the user grants,
// with the button on the options page (options/options.html); nothing here requests them.
// The bridge sends the policy scope (startTime, allowDomains, denyDomains). Results carry only
// URLs, titles, visit times, transitions and bookmark folders, never page contents, and every
// URL goes through redactHistoryUrl.

const HISTORY_REDACTED = '[redacted]';
const HISTORY_LIMIT_MAX = 100;
// Items read before the scope filter and the limit, so dropped or merged items leave room.
const HISTORY_SCAN_MAX = 1000;
const HISTORY_VISITS_MAX = 100;
const HISTORY_PERMISSION_HINT = 'Only the user can allow this: about:addons → Kitsune → Options (Preferences) → ' +
  '"Allow history and bookmarks" shows the browser\'s permission prompt. The agent cannot grant it; ask the user.';

// Sensitive-URL-parameter rules (docs/api-reference.md#history-url-redaction). A query or
// fragment parameter's value is replaced with [redacted] when a word of its name (split at
// punctuation and camelCase) is in SENSITIVE_URL_PARAM_WORDS, its whole name contains one of
// SENSITIVE_URL_NAME_PARTS, or its value looks like a credential (looksLikeCredential).
const SENSITIVE_URL_PARAM_WORDS = new Set([
  'token', 'secret', 'password', 'passwd', 'pwd', 'pass', 'auth', 'authorization', 'apikey', 'key',
  'sig', 'signature', 'hmac', 'session', 'sid', 'jwt', 'otp', 'code', 'ticket', 'nonce',
  'credential', 'credentials', 'saml', 'samlrequest', 'samlresponse', 'assertion',
]);
const SENSITIVE_URL_NAME_PARTS = ['token', 'secret', 'passw', 'sessid', 'sessionid', 'apikey', 'credential'];
const CREDENTIAL_VALUE_PATTERNS = [
  /^bearer\s/i,
  /^eyJ[\w-]{4,}\.[\w-]{4,}\.[\w-]*$/,                                   // JWT
  /^(?:gh[pousr]_|github_pat_|glpat-|xox[abprs]-|sk-|sk_live_|rk_live_|AKIA|ASIA|ya29\.|AIza)[\w.-]{8,}/,
  /^[A-Fa-f0-9]{32,}$/,                                                  // long hex: hashes, session ids
];

function sensitiveUrlParamName(name) {
  const lower = name.toLowerCase();
  if (SENSITIVE_URL_NAME_PARTS.some(part => lower.includes(part))) return true;
  const words = name.replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase().split(/[^a-z0-9]+/);
  return words.some(word => SENSITIVE_URL_PARAM_WORDS.has(word));
}

// Random-looking values: 32+ token characters mixing upper case, lower case and digits.
function looksLikeCredential(value) {
  if (CREDENTIAL_VALUE_PATTERNS.some(pattern => pattern.test(value))) return true;
  return value.length >= 32 && /^[\w\-.~+/=]+$/.test(value) &&
    /[a-z]/.test(value) && /[A-Z]/.test(value) && /\d/.test(value);
}

function decodeUrlPart(text) {
  try { return decodeURIComponent(text.replace(/\+/g, ' ')); } catch { return text; }
}

// name=value&... with sensitive values replaced; a bare credential-like part is replaced whole.
function redactUrlParams(text) {
  return text.split('&').map(part => {
    const equals = part.indexOf('=');
    if (equals < 0) return part && looksLikeCredential(decodeUrlPart(part)) ? HISTORY_REDACTED : part;
    const name = part.slice(0, equals), value = part.slice(equals + 1);
    if (!value) return part;
    return sensitiveUrlParamName(decodeUrlPart(name)) || looksLikeCredential(decodeUrlPart(value))
      ? `${name}=${HISTORY_REDACTED}` : part;
  }).join('&');
}

// A fragment is a route (#/inbox?token=...), parameters (#access_token=...&state=...) or a name.
function redactUrlFragment(fragment) {
  const query = fragment.indexOf('?');
  if (query >= 0) return fragment.slice(0, query + 1) + redactUrlParams(fragment.slice(query + 1));
  if (fragment.includes('=')) return redactUrlParams(fragment);
  return looksLikeCredential(decodeUrlPart(fragment)) ? HISTORY_REDACTED : fragment;
}

// The URL without user:password and with sensitive query and fragment values [redacted].
function redactHistoryUrl(raw) {
  let url;
  try { url = new URL(raw); } catch { return raw; }
  url.username = '';
  url.password = '';
  if (url.search.length > 1) url.search = redactUrlParams(url.search.slice(1));
  if (url.hash.length > 1) url.hash = redactUrlFragment(url.hash.slice(1));
  return url.href;
}

// Only http(s) pages inside the domain lists: deny wins; an empty allow list allows every
// domain; a listed domain also matches its subdomains. Other schemes (file:, data:, about:)
// never leave the browser, since they can hold local paths or page contents.
function inHistoryScope(raw, { allowDomains = [], denyDomains = [] }) {
  let host;
  try {
    const url = new URL(raw);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return false;
    host = url.hostname.toLowerCase();
  } catch {
    return false;
  }
  const covers = domain => host === domain || host.endsWith('.' + domain);
  if (denyDomains.some(covers)) return false;
  return !allowDomains.length || allowDomains.some(covers);
}

function historyLimit(limit) {
  const n = Math.floor(Number(limit));
  return Number.isFinite(n) ? Math.min(Math.max(n, 1), HISTORY_LIMIT_MAX) : 20;
}

const historyTime = ms => new Date(ms).toISOString();

// Checked before the API is touched: Firefox exposes browser.history/bookmarks only once granted.
async function missingHistoryPermission(permission) {
  const granted = await browser.permissions.contains({ permissions: [permission] }).catch(() => false);
  if (granted && browser[permission]) return null;
  return {
    error: `The user has not allowed Kitsune to read browser ${permission}`,
    code: 'permission_required', retryable: false, hint: HISTORY_PERMISSION_HINT, state: { permission },
  };
}

async function historySearch({ query = '', startTime = 0, limit, ...scope }) {
  const denied = await missingHistoryPermission('history');
  if (denied) return denied;
  const max = historyLimit(limit);
  let items;
  try {
    items = await browser.history.search({ text: String(query), startTime, maxResults: HISTORY_SCAN_MAX });
  } catch (e) {
    return { error: `History search failed: ${e.message}` };
  }
  // Redaction can map several URLs (different tokens) to one; they are listed once.
  const byUrl = new Map();
  for (const item of items) {
    if (!item.url || !(item.lastVisitTime >= startTime) || !inHistoryScope(item.url, scope)) continue;
    const url = redactHistoryUrl(item.url);
    const seen = byUrl.get(url);
    if (!seen) {
      byUrl.set(url, { url, title: item.title || '', lastVisit: item.lastVisitTime, visitCount: item.visitCount || 0 });
      continue;
    }
    seen.visitCount += item.visitCount || 0;
    if (item.lastVisitTime > seen.lastVisit) Object.assign(seen, { lastVisit: item.lastVisitTime, title: item.title || seen.title });
  }
  const found = [...byUrl.values()].sort((a, b) => b.lastVisit - a.lastVisit);
  const results = found.slice(0, max).map(item => ({
    url: item.url, title: item.title, last_visit: historyTime(item.lastVisit), visit_count: item.visitCount,
  }));
  return { ok: true, results, count: results.length, ...(found.length > max ? { truncated: true } : {}) };
}

// url may be a result of historySearch: a redacted URL resolves to every visited URL it stands for.
async function historyVisits({ url, startTime = 0, ...scope }) {
  const denied = await missingHistoryPermission('history');
  if (denied) return denied;
  if (typeof url !== 'string' || !inHistoryScope(url, scope)) {
    return { error: 'url must be an http(s) URL inside the history domain scope' };
  }
  const target = redactHistoryUrl(url);
  const visits = [];
  try {
    let urls = [url];
    if (target.includes(HISTORY_REDACTED)) {
      const items = await browser.history.search({ text: new URL(url).hostname, startTime, maxResults: HISTORY_SCAN_MAX });
      urls = items.map(item => item.url).filter(visited => visited && redactHistoryUrl(visited) === target);
    }
    for (const visited of urls) {
      for (const visit of await browser.history.getVisits({ url: visited })) {
        if (!(visit.visitTime >= startTime)) continue;
        // Firefox reports referringVisitId "-1" when it does not know the referrer.
        const referrer = visit.referringVisitId;
        visits.push({
          visit_time: visit.visitTime, transition: visit.transition, visit_id: visit.visitId,
          ...(referrer && referrer !== '-1' && referrer !== '0' ? { referring_visit_id: referrer } : {}),
        });
      }
    }
  } catch (e) {
    return { error: `History visit lookup failed: ${e.message}` };
  }
  visits.sort((a, b) => b.visit_time - a.visit_time);
  const kept = visits.slice(0, HISTORY_VISITS_MAX).map(visit => ({ ...visit, visit_time: historyTime(visit.visit_time) }));
  return { ok: true, url: target, visits: kept, count: kept.length, ...(visits.length > kept.length ? { truncated: true } : {}) };
}

// "Bookmarks Toolbar/Work" for a bookmark's parent folder id; the untitled root is left out.
async function bookmarkFolderPath(id, paths) {
  if (!id) return '';
  if (!paths.has(id)) {
    paths.set(id, (async () => {
      const [node] = await browser.bookmarks.get(id).catch(() => []);
      const parent = node?.parentId ? await bookmarkFolderPath(node.parentId, paths) : '';
      return node?.title ? (parent ? `${parent}/${node.title}` : node.title) : parent;
    })());
  }
  return await paths.get(id);
}

async function bookmarksSearch({ query = '', limit, ...scope }) {
  const denied = await missingHistoryPermission('bookmarks');
  if (denied) return denied;
  const max = historyLimit(limit);
  try {
    const nodes = query ? await browser.bookmarks.search(String(query)) : await browser.bookmarks.getRecent(HISTORY_SCAN_MAX);
    const found = nodes.filter(node => node.url && inHistoryScope(node.url, scope));
    const paths = new Map();
    const results = [];
    for (const node of found.slice(0, max)) {
      results.push({ url: redactHistoryUrl(node.url), title: node.title || '', folder: await bookmarkFolderPath(node.parentId, paths) });
    }
    return { ok: true, results, count: results.length, ...(found.length > max ? { truncated: true } : {}) };
  } catch (e) {
    return { error: `Bookmark search failed: ${e.message}` };
  }
}
