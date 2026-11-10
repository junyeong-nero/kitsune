"""Offline #499 schema/signature consistency helpers. NOT a browser SDK, broker or sandbox.

Only design fixtures are loaded. No runtime registration, bridge or browser is imported.
Reference expansion measures self-contained definitions, not deceptively small $ref stubs.
"""
from __future__ import annotations

from copy import deepcopy
import inspect
import json
from pathlib import Path

from jsonschema import Draft202012Validator

DESIGN = Path(__file__).resolve().parent.parent / "docs/design"


def specification():
    return json.loads((DESIGN / "499-flat-schemas.json").read_text())


def _expand(value, definitions):
    if isinstance(value, list):
        return [_expand(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        prefix = "#/$defs/"
        if not value["$ref"].startswith(prefix):
            raise ValueError("Only local design schema references are permitted")
        value = {**definitions[value["$ref"][len(prefix):]], **{k: v for k, v in value.items() if k != "$ref"}}
    return {key: _expand(item, definitions) for key, item in value.items()}


def tool_definitions(mode="act", scripting=True):
    if mode not in {"read", "draft", "act"}:
        raise ValueError("Unknown task mode")
    spec = specification()
    result = []
    for entry in spec["tools"]:
        if mode not in entry["modes"] or (entry["name"] == "browser_script" and not scripting):
            continue
        schema = _expand(entry["inputSchema"], spec["$defs"])
        if mode != "act":
            if entry["name"] in {"browser_tabs", "browser_navigate"}:
                allowed = {"list", "open"} if entry["name"] == "browser_tabs" else {"goto"}
                schema["oneOf"] = [branch for branch in schema["oneOf"]
                                   if branch["properties"]["action"].get("const") in allowed]
            elif entry["name"] == "browser_wait":
                condition = schema["properties"]["condition"]
                condition["oneOf"] = [branch for branch in condition["oneOf"]
                                      if branch["properties"]["kind"]["const"] in {"text", "text_gone"}]
        result.append({"name": entry["name"], "description": entry["description"], "inputSchema": schema,
                       "annotations": deepcopy(entry["annotations"])})
    return result


def normalize_call(name, arguments, mode="act"):
    """Validate then normalize documented defaults for comparing example request traces."""
    schemas = {tool["name"]: tool["inputSchema"] for tool in tool_definitions(mode)}
    if name not in schemas:
        raise ValueError(f"tool_not_available: {name} in {mode}")
    schema = schemas[name]
    Draft202012Validator(schema).validate(arguments)
    if name == "browser_script" and len(arguments["code"].encode("utf-8")) > 32768:
        raise ValueError("source_too_large: 32 KiB UTF-8")
    if "oneOf" in schema:
        schema = next(branch for branch in schema["oneOf"] if Draft202012Validator(branch).is_valid(arguments))
    normalized = deepcopy(arguments)
    for key, prop in schema.get("properties", {}).items():
        if key == "amount_px" and normalized.get("direction") in {"top", "bottom"}:
            continue
        if key not in normalized and "default" in prop:
            normalized[key] = deepcopy(prop["default"])
    Draft202012Validator(schemas[name]).validate(normalized)
    return {"tool": name, "arguments": normalized}


def sdk_signatures():
    # Trusted declaration file only: method bodies are ellipses, never browser operations.
    path = DESIGN / "499-sdk.pyi"
    namespace = {"__name__": "flat_design_declarations"}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    return {name: inspect.signature(method) for name, method in vars(namespace["Browser"]).items()
            if not name.startswith("_") and inspect.isfunction(method)}


def sdk_call(method, arguments, mode="act"):
    bindings = {method_name: (entry["name"], constants) for entry in specification()["tools"]
                for method_name, constants in entry["sdk"].items()}
    name, constants = bindings[method]
    bound = sdk_signatures()[method].bind(None, **arguments)
    bound.apply_defaults()
    wire = {key: value for key, value in bound.arguments.items() if key != "self" and value is not None}
    return normalize_call(name, {**wire, **constants}, mode)
