"""Inventory the existing public operation surfaces for #499, without connecting to a browser.

    uv run python scripts/toolset_inventory.py          # counts, not a proposed tool count
    uv run python scripts/toolset_inventory.py --json   # entries and implementation signatures

This reports registrations, not only the default tools/list. Python constructors,
namespace attributes and exception classes are not operations; their developer-only
connection/approval semantics must be covered by the contract document. HTTP methods
are separate entries. Container aliases are inventoried by their caller-facing names.
Nothing here registers the proposed surface, changes permissions, or executes scripts.
"""
from __future__ import annotations

import argparse
import ast
import inspect
import json
from pathlib import Path

from kitsune import browser, tools
from kitsune.capabilities import ACTION_GROUPS, OPEN_ROUTES, ROUTE_GROUPS

ROOT = Path(__file__).resolve().parent.parent


def _assigned(body, name):
    for node in body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise ValueError(f"Cannot find literal registration {name}; review the inventory extractor")


def _batch_actions(tree):
    """Read the explicit aliases and identity-name comprehension; never import the bridge daemon."""
    node = _assigned(tree.body, "BATCH_ACTION_MAP")
    aliases = {}
    for key, value in zip(node.keys, node.values):
        if key is not None:
            aliases[ast.literal_eval(key)] = ast.literal_eval(value)
        else:
            # The bridge declares **{action: action for action in (...literal names...)}.
            if (not isinstance(value, ast.DictComp) or len(value.generators) != 1
                    or ast.unparse(value.key) != ast.unparse(value.value)
                    or ast.unparse(value.key) != ast.unparse(value.generators[0].target)
                    or value.generators[0].ifs or value.generators[0].is_async):
                raise ValueError("Review changed BATCH_ACTION_MAP comprehension")
            aliases.update((name, name) for name in ast.literal_eval(value.generators[0].iter))
    # Match the bridge's _BATCH_ACTION_NAMES: aliases, plus bridge-handled operations.
    renamed = {name for alias, name in aliases.items() if alias != name}
    return {name: aliases.get(name, name) for name in set(aliases) | (set(ACTION_GROUPS) - renamed)}


def collect():
    inventory = {
        "tools": {name: {"group": spec.group, "modes": sorted(spec.modes) if spec.modes else None,
                          "implementation_signature": str(inspect.signature(spec.function))}
                  for name, spec in tools.TOOLS.items()},
        "gateway": {}, "python": {}, "http": {}, "container": {},
    }
    server = ast.parse((ROOT / "src/kitsune/server.py").read_text())
    gateway = _assigned(server.body, "GATEWAY_TOOLS")
    if not isinstance(gateway, ast.Call) or not isinstance(gateway.func, ast.Name) or gateway.func.id != "frozenset":
        raise ValueError("Review changed gateway registration")
    inventory["gateway"] = {name: {} for name in ast.literal_eval(gateway.args[0])}

    inventory["python"]["connect"] = {"signature": str(inspect.signature(browser.connect))}
    for class_name, cls in vars(browser).items():
        if not inspect.isclass(cls) or cls.__module__ != browser.__name__:
            continue
        for name, method in vars(cls).items():
            if not name.startswith("_") and inspect.isfunction(method):
                inventory["python"][f"{class_name}.{name}"] = {"signature": str(inspect.signature(method))}

    bridge = ast.parse((ROOT / "src/kitsune/bridge.py").read_text())
    handler = next(node for node in bridge.body if isinstance(node, ast.ClassDef) and node.name == "BridgeHandler")
    for method in handler.body:
        if not isinstance(method, ast.FunctionDef) or method.name not in {"do_GET", "do_POST"}:
            continue
        routes = _assigned(method.body, "routes")
        for key, value in zip(routes.keys, routes.values):
            path = ast.literal_eval(key)
            name = f"{method.name[3:]} {path}"
            if name in inventory["http"]:
                raise ValueError(f"Duplicate route registration: {name}")
            inventory["http"][name] = {"groups": sorted(ROUTE_GROUPS.get(path, ())),
                                      "capability_exempt": path in OPEN_ROUTES,
                                      "handler": ast.unparse(value)}
    inventory["container"] = {name: {"operation": operation, "groups": sorted(ACTION_GROUPS[operation])}
                              for name, operation in _batch_actions(bridge).items()}
    return {surface: dict(sorted(entries.items())) for surface, entries in inventory.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print every entry; no files are written")
    args = parser.parse_args()
    inventory = collect()
    print(json.dumps(inventory if args.json else {name: len(entries) for name, entries in inventory.items()},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
