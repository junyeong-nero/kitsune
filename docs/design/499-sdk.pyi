"""Proposed injected SDK; declarations only, NOT a working SDK or security boundary.

Methods return the common Result or raise BrowserOperationError(result=...).
None on an optional SDK argument means omit it; direct JSON does not accept null.
The broker applies schema defaults (scroll amount only for cardinal directions).
No constructor, connection, raw request, policy or nested runner is provided.
"""
from __future__ import annotations
from typing import Any, Literal, TypedDict

TabId = str
Modifier = Literal["Shift", "Control", "Alt", "Meta"]
QueryField = Literal["ref", "text", "href", "src", "name", "value", "checked", "type", "content", "role", "label"]

class RefTarget(TypedDict):
    ref: str

class SelectorTarget(TypedDict):
    selector: str

Target = RefTarget | SelectorTarget

class TextCondition(TypedDict):
    kind: Literal["text", "text_gone", "url", "title"]
    value: str

class TargetCondition(TypedDict):
    kind: Literal["target"]
    target: Target

class IdleCondition(TypedDict):
    kind: Literal["network_idle"]

Condition = TextCondition | TargetCondition | IdleCondition

class OperationErrorData(TypedDict):
    code: str
    message: str
    retryable: bool
    hint: str
    state: dict[str, Any]

class _RequiredResult(TypedDict):
    ok: bool
    call: str
    operation: str
    outcome: Literal["not_dispatched", "dispatched", "observed", "unknown"]
    data: dict[str, Any]
    truncated: bool

class Result(_RequiredResult, total=False):
    tab_id: TabId
    error: OperationErrorData

class BrowserOperationError(Exception):
    result: Result

class Browser:
    async def tabs(self) -> Result: ...
    async def new_tab(self, *, url: str = "about:blank", timeout_ms: int = 15000) -> Result: ...
    async def close_tab(self, *, tab_id: TabId, timeout_ms: int = 10000) -> Result: ...
    async def navigate(self, *, tab_id: TabId, url: str, timeout_ms: int = 15000) -> Result: ...
    async def back(self, *, tab_id: TabId, timeout_ms: int = 15000) -> Result: ...
    async def forward(self, *, tab_id: TabId, timeout_ms: int = 15000) -> Result: ...
    async def reload(self, *, tab_id: TabId, timeout_ms: int = 15000) -> Result: ...
    async def read(self, *, tab_id: TabId, format: Literal["snapshot", "text"] = "snapshot",
                   target: Target | None = None, max_chars: int = 12000, timeout_ms: int = 10000) -> Result: ...
    async def query(self, *, tab_id: TabId, selector: str, fields: list[QueryField] | None = None,
                    limit: int = 50, timeout_ms: int = 10000) -> Result: ...
    async def find(self, *, tab_id: TabId, text: str, limit: int = 20, timeout_ms: int = 10000) -> Result: ...
    async def click(self, *, tab_id: TabId, target: Target, button: Literal["left", "middle", "right"] = "left",
                    click_count: Literal[1, 2] = 1, modifiers: list[Modifier] | None = None,
                    timeout_ms: int = 10000) -> Result: ...
    async def input(self, *, tab_id: TabId, target: Target, text: str,
                    mode: Literal["replace", "type"] = "replace", timeout_ms: int = 10000) -> Result: ...
    async def select(self, *, tab_id: TabId, target: Target, values: list[str],
                     by: Literal["value", "label"] = "value", timeout_ms: int = 10000) -> Result: ...
    async def check(self, *, tab_id: TabId, target: Target, checked: bool = True, timeout_ms: int = 10000) -> Result: ...
    async def press(self, *, tab_id: TabId, target: Target, key: str,
                    modifiers: list[Modifier] | None = None, timeout_ms: int = 10000) -> Result: ...
    async def hover(self, *, tab_id: TabId, target: Target, timeout_ms: int = 10000) -> Result: ...
    async def drag(self, *, tab_id: TabId, source: Target, destination: Target, timeout_ms: int = 10000) -> Result: ...
    async def scroll(self, *, tab_id: TabId, direction: Literal["up", "down", "left", "right", "top", "bottom"] = "down",
                     amount_px: int | None = None, target: Target | None = None, timeout_ms: int = 10000) -> Result: ...
    async def wait(self, *, tab_id: TabId, condition: Condition, timeout_ms: int = 10000) -> Result: ...
    async def screenshot(self, *, tab_id: TabId, scope: Literal["viewport", "element", "full_page"] = "viewport",
                         target: Target | None = None, timeout_ms: int = 10000) -> Result: ...
    async def request_tab(self, *, tab_id: TabId, timeout_ms: int = 10000) -> Result: ...
    async def ask_user(self, *, reason: Literal["sign_in", "human_check", "trusted_input", "other"], message: str,
                       tab_id: TabId | None = None, timeout_ms: int = 10000) -> Result: ...
    async def resume(self, *, call: str, resume_token: str, wait_ms: int = 30000) -> Result: ...

browser: Browser

def emit(value: Any) -> None: ...  # JSON-compatible selected output; not success evidence.
def emit_image(handle: str) -> None: ...  # Handle from this run's screenshot, not a path/base64 string.
