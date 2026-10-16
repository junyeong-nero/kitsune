# Gmail Template Draft

Creates a Gmail compose draft with a subject and body, without adding recipients or sending.

Variables:
- `subject`
- `body`

```json
{
  "action": "navigate",
  "url": "https://mail.google.com/mail/u/2/#inbox"
}
```

```json
{
  "action": "waitForElement",
  "selector": "[role=\"main\"], div[role=\"button\"]",
  "timeout": 12000,
  "pollInterval": 100
}
```

```json
{
  "action": "click",
  "coords": {
    "x": 136,
    "y": 89
  }
}
```

```json
{
  "action": "waitForElement",
  "selector": "input[name=\"subjectbox\"]",
  "timeout": 8000,
  "pollInterval": 100
}
```

```json
{
  "action": "js",
  "code": "(() => {\n  const subjectText = {{jslit:subject}};\n  const bodyText = {{jslit:body}};\n  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };\n  const subjects = Array.from(document.querySelectorAll('input[name=\"subjectbox\"]')).filter(visible);\n  const subject = subjects.find(el => !el.value) || subjects[subjects.length - 1];\n  if (!subject) return JSON.stringify({ error: 'Visible Gmail subject field not found' });\n  const dialog = subject.closest('[role=\"dialog\"]') || subject.closest('table') || document;\n  const bodies = Array.from(dialog.querySelectorAll('[contenteditable=\"true\"][aria-label=\"Message Body\"]')).filter(visible);\n  const body = bodies.find(el => !el.innerText.trim()) || bodies[bodies.length - 1];\n  if (!body) return JSON.stringify({ error: 'Visible Gmail body field not found', subjects: subjects.length });\n  subject.focus();\n  subject.value = subjectText;\n  subject.dispatchEvent(new Event('input', { bubbles: true }));\n  subject.dispatchEvent(new Event('change', { bubbles: true }));\n  body.focus();\n  body.innerText = bodyText;\n  body.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertReplacementText', data: bodyText }));\n  body.dispatchEvent(new Event('change', { bubbles: true }));\n  return JSON.stringify({ ok: true, subject: subject.value, bodyLength: body.innerText.length, subjects: subjects.length });\n})()"
}
```
