# Test Workflow
Navigates to a URL and extracts the page title and text.

## Steps

Navigate to the target URL:
```json
{"action": "navigate", "url": "{{url}}"}
```

Wait for page to load:
```json
{"action": "sleep", "ms": 2000}
```

Get page info:
```json
{"action": "pageInfo"}
```

Get page text:
```json
{"action": "pageText"}
```
