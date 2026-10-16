# WordPress Elementor HTML

Extract HTML widget content from an Elementor page. The browser must already
be logged into WordPress admin. Runs in the active tab.

Variables:
- `site_url`: WordPress site URL without a trailing slash, e.g. `https://example.com`.
- `page_id`: Page/post ID.

Run with `browser_workflow("wp-html", {"site_url": "https://example.com", "page_id": 5719})`.
The last step returns a JSON string with `page_id`, `widget_count`, and `widgets`
(or an error); the workflow response contains all step results.

```json
{
  "action": "navigate",
  "url": "{{site_url}}/wp-admin/post.php?post={{page_id}}&action=elementor"
}
```

```json
{
  "action": "waitForResult",
  "code": "(() => { const d = document.querySelector('#elementor-preview-iframe')?.contentDocument; return d?.querySelector('[data-widget_type=\"html.default\"]') ? 'ready' : null; })()",
  "timeout": 30000
}
```

```json
{
  "action": "js",
  "code": "\n    (function() {\n        const iframe = document.querySelector('#elementor-preview-iframe');\n        if (!iframe) return JSON.stringify({error: 'No Elementor preview iframe found.'});\n        const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;\n        if (!iframeDoc) return JSON.stringify({error: 'Cannot access iframe document.'});\n        const widgets = iframeDoc.querySelectorAll('[data-widget_type=\"html.default\"]');\n        if (widgets.length === 0) return JSON.stringify({error: 'No HTML widgets found on this page.'});\n        const results = [];\n        for (const widget of widgets) {\n            const dataId = widget.getAttribute('data-id');\n            const container = widget.querySelector('.elementor-html');\n            const content = container ? container.innerHTML : widget.innerHTML;\n            results.push({widget_id: dataId, content: content.trim()});\n        }\n        return JSON.stringify({ok: true, page_id: {{jslit:page_id}}, widget_count: results.length, widgets: results});\n    })();\n    "
}
```
