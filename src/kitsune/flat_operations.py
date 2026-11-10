"""Strict leaf-operation admission for the opt-in flat coordinator (not yet published).

This module never imports a bridge, runs code, or grants browser authority. Native
adapters must still enforce live policy/ownership and attach trusted connection
metadata. The script runner is deliberately not a leaf operation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator, model_validator


def _json_integer(value):
    # JSON Schema's integer type includes 1.0, but not booleans, strings or fractions.
    # Canonicalize those JSON numbers before strict native-integer validation.
    return int(value) if type(value) is float and value.is_integer() else value


Integer = Annotated[int, BeforeValidator(_json_integer)]
TabId = Annotated[str, Field(pattern=r"^t_[A-Za-z0-9_-]+$", max_length=128)]
Selector = Annotated[str, Field(min_length=1, max_length=4096)]
URL = Annotated[str, Field(min_length=1, max_length=8192)]
Timeout = Annotated[Integer, Field(ge=1, le=60000)]
Modifier = Literal["Shift", "Control", "Alt", "Meta"]
QueryField = Literal["ref", "text", "href", "src", "name", "value", "checked", "type", "content", "role", "label"]


class OperationRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RefTarget(Arguments):
    ref: Annotated[str, Field(pattern=r"^e[0-9]+$", max_length=128)]


class SelectorTarget(Arguments):
    selector: Selector


Target = Union[RefTarget, SelectorTarget]


class TabsList(Arguments):
    action: Literal["list"] = "list"


class TabsOpen(Arguments):
    action: Literal["open"]
    url: URL = "about:blank"
    timeout_ms: Timeout = 15000


class TabArguments(Arguments):
    tab_id: TabId
    timeout_ms: Timeout = 10000


class TabsClose(TabArguments):
    action: Literal["close"]


class Navigate(TabArguments):
    action: Literal["goto"] = "goto"
    url: URL
    timeout_ms: Timeout = 15000


class History(TabArguments):
    action: Literal["back", "forward", "reload"]
    timeout_ms: Timeout = 15000


class Read(TabArguments):
    format: Literal["snapshot", "text"] = "snapshot"
    target: Target | None = None
    max_chars: Annotated[Integer, Field(ge=1, le=12000)] = 12000


class Query(TabArguments):
    selector: Selector
    fields: Annotated[list[QueryField], Field(min_length=1, max_length=11)] = ["text"]
    limit: Annotated[Integer, Field(ge=1, le=200)] = 50

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Duplicate query fields")
        return values


class Find(TabArguments):
    text: Selector
    limit: Annotated[Integer, Field(ge=1, le=100)] = 20


class Targeted(TabArguments):
    target: Target


class Modified(Targeted):
    modifiers: Annotated[list[Modifier], Field(max_length=4)] = []

    @field_validator("modifiers")
    @classmethod
    def unique_modifiers(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Duplicate modifiers")
        return values


class Click(Modified):
    button: Literal["left", "middle", "right"] = "left"
    click_count: Annotated[Integer, Field(ge=1, le=2)] = 1

    @model_validator(mode="after")
    def supported_double_click(self):
        if self.click_count == 2 and (self.button != "left" or self.modifiers):
            raise ValueError("Double-click requires left button and no modifiers")
        return self


class Input(Targeted):
    text: Annotated[str, Field(max_length=32768)]
    mode: Literal["replace", "type"] = "replace"


class Select(Targeted):
    values: Annotated[list[Annotated[str, Field(max_length=4096)]], Field(min_length=1, max_length=100)]
    by: Literal["value", "label"] = "value"

    @field_validator("values")
    @classmethod
    def unique_values(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Duplicate select values")
        return values


class Check(Targeted):
    checked: bool = True


class Press(Modified):
    key: Annotated[str, Field(min_length=1, max_length=64)]


class Drag(TabArguments):
    source: Target
    destination: Target


class Scroll(TabArguments):
    direction: Literal["up", "down", "left", "right", "top", "bottom"] = "down"
    amount_px: Annotated[Integer, Field(ge=1, le=10000)] | None = None
    target: Target | None = None

    @model_validator(mode="after")
    def amount_matches_direction(self):
        if self.direction in {"top", "bottom"} and self.amount_px is not None:
            raise ValueError("Top/bottom scrolls take no amount")
        return self


class TextCondition(Arguments):
    kind: Literal["text", "text_gone", "title"]
    value: Selector


class URLCondition(Arguments):
    kind: Literal["url"]
    value: URL


class TargetCondition(Arguments):
    kind: Literal["target"]
    target: Target


class IdleCondition(Arguments):
    kind: Literal["network_idle"]


class Wait(TabArguments):
    condition: Annotated[Union[TextCondition, URLCondition, TargetCondition, IdleCondition], Field(discriminator="kind")]


class Screenshot(TabArguments):
    scope: Literal["viewport", "element", "full_page"] = "viewport"
    target: Target | None = None

    @model_validator(mode="after")
    def target_matches_scope(self):
        if (self.scope == "element") != (self.target is not None):
            raise ValueError("Only element screenshots require a target")
        return self


class AskUser(Arguments):
    reason: Literal["sign_in", "human_check", "trusted_input", "other"]
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    tab_id: TabId | None = None
    timeout_ms: Timeout = 10000

    @model_validator(mode="after")
    def page_question_has_tab(self):
        if self.reason != "other" and self.tab_id is None:
            raise ValueError("Page questions require a tab")
        return self


class Resume(Arguments):
    call: Annotated[str, Field(min_length=1, max_length=128)]
    resume_token: Annotated[str, Field(min_length=1, max_length=256)]
    wait_ms: Annotated[Integer, Field(ge=0, le=120000)] = 30000


# Deliberately explicit: new legacy tools/routes do not widen this subset.
_MODELS = {
    "browser_read": Read, "browser_query": Query, "browser_find": Find,
    "browser_click": Click, "browser_input": Input, "browser_select": Select,
    "browser_check": Check, "browser_press": Press, "browser_hover": Targeted,
    "browser_drag": Drag, "browser_scroll": Scroll, "browser_wait": Wait,
    "browser_screenshot": Screenshot, "browser_request_tab": TabArguments,
    "browser_ask_user": AskUser, "browser_resume": Resume,
}
OPERATIONS = frozenset(_MODELS) | {"browser_tabs", "browser_navigate"}
_READ = frozenset({"browser_tabs", "browser_navigate", "browser_read", "browser_query", "browser_find",
                   "browser_scroll", "browser_wait", "browser_ask_user"})
_DRAFT = _READ | {"browser_input", "browser_select", "browser_check"}
MODE_OPERATIONS = {"read": _READ, "draft": _DRAFT, "act": OPERATIONS}


@dataclass(frozen=True)
class ValidatedOperation:
    name: str
    _model: Arguments

    def arguments(self) -> dict:
        """A fresh JSON-shaped copy; never share mutable caller input with a dispatch/resume."""
        result = self._model.model_dump(exclude_none=True)
        if self.name == "browser_scroll" and result["direction"] not in {"top", "bottom"}:
            result.setdefault("amount_px", 500)
        return result


def validate_operation(name: str, arguments: dict, *, mode: str) -> ValidatedOperation:
    """Validate a leaf request. mode is trusted caller state, never a request field."""
    if mode not in MODE_OPERATIONS:
        raise ValueError("Unknown flat caller mode")
    if not isinstance(name, str) or name not in OPERATIONS:
        raise OperationRefused("unknown_operation", "Not a supported flat browser operation")
    if name not in MODE_OPERATIONS[mode]:
        raise OperationRefused("mode_restricted", "The caller mode does not permit this operation")
    if type(arguments) is not dict or any(value is None for value in arguments.values()):
        raise OperationRefused("invalid_arguments", "Expected an argument object; omit optional fields instead of null")
    model = _MODELS.get(name)
    if name == "browser_tabs":
        action = arguments.get("action", "list")
        model = {"list": TabsList, "open": TabsOpen, "close": TabsClose}.get(action) if isinstance(action, str) else None
    elif name == "browser_navigate":
        action = arguments.get("action", "goto")
        model = Navigate if action == "goto" else History
    if model is None:
        raise OperationRefused("invalid_arguments", "Unknown operation action")
    try:
        parsed = model.model_validate(arguments)
    except ValidationError:
        # ValidationError includes the supplied input by default: never echo secrets or forged metadata.
        raise OperationRefused("invalid_arguments", "Arguments do not match the flat operation contract") from None
    if mode != "act" and (isinstance(parsed, (TabsClose, History))
                          or isinstance(parsed, Wait) and parsed.condition.kind not in {"text", "text_gone"}):
        raise OperationRefused("mode_restricted", "The caller mode does not permit this operation variant")
    return ValidatedOperation(name, parsed)
