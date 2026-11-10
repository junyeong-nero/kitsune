"""Measure #499 design interface costs without a browser or model run.

    uv run --locked python -m scripts.flat_contract_size

Prints JSON only. Legacy metadata is loaded in fresh private-HOME subprocesses with
HTTP requests forbidden. Candidate definitions are expanded design fixtures, not
a claim of a registered runtime. Characters/UTF-8 bytes are NOT model tokens.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile

from scripts.flat_contract import DESIGN, tool_definitions
from scripts.tool_schema_size import first_paragraph

LEGACY_METADATA = '''import asyncio, json
from unittest.mock import patch
import httpx
with patch.object(httpx, "request", side_effect=AssertionError("metadata must not connect")), \\
     patch.object(httpx.Client, "request", side_effect=AssertionError("metadata must not connect")), \\
     patch.object(httpx.AsyncClient, "request", side_effect=AssertionError("metadata must not connect")):
    from kitsune import server
    async def metadata():
        listed = await server.mcp.list_tools()
        authorized = await server.mcp.all_tools()
        return {"listed": [tool.model_dump(mode="json", exclude_none=True) for tool in listed],
                "authorized": [tool.model_dump(mode="json", exclude_none=True) for tool in authorized.values()],
                "instructions": server.mcp._mcp_server.instructions}
    print(json.dumps(asyncio.run(metadata())))
'''


def compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def text_size(text):
    return {"chars": len(text), "utf8_bytes": len(text.encode("utf-8"))}


def definition_size(tools):
    definitions = [compact(tool) for tool in tools]
    first = [compact({**tool, "description": first_paragraph(tool.get("description", ""))}) for tool in tools]
    projection = [compact({key: tool[key] for key in ("name", "description", "inputSchema")}) for tool in tools]
    return {"count": len(tools), "sum_chars": sum(map(len, definitions)),
            "sum_utf8_bytes": sum(len(text.encode("utf-8")) for text in definitions),
            "array_chars": len(compact(tools)), "first_paragraph_sum_chars": sum(map(len, first)),
            "name_description_input_schema_sum_chars": sum(map(len, projection))}


def legacy_metadata(mode, caps="core", publish=None):
    with tempfile.TemporaryDirectory(prefix="kitsune-499-metadata-") as home:
        env = {key: value for key, value in os.environ.items() if not key.startswith("KITSUNE_")}
        env.update(HOME=home, KITSUNE_HOME=home, KITSUNE_TOKEN="metadata-test-token",
                   KITSUNE_MODE=mode, KITSUNE_CAPS=caps, KITSUNE_USER_TABS="read-only")
        if publish is not None:
            env["KITSUNE_PUBLISH"] = publish
        result = subprocess.run([sys.executable, "-c", LEGACY_METADATA], env=env, capture_output=True,
                                text=True, check=True)
        return json.loads(result.stdout)


def measure():
    rows = {}
    for name, mode, caps, publish in [
        ("legacy-core-act", "act", "core", None), ("legacy-core-read", "read", "core", None),
        ("legacy-core-draft", "draft", "core", None),
        ("legacy-gateway-core-act", "act", "all", "core"),
        ("legacy-gateway-only-act", "act", "all", ""),
    ]:
        metadata = legacy_metadata(mode, caps, publish)
        definitions = definition_size(metadata["listed"])
        instructions = text_size(metadata["instructions"])
        rows[name] = {"settings": {"mode": mode, "caps": caps, "publish": publish},
                      "definitions": definitions, "instructions": instructions,
                      "initial_component_sum_chars": definitions["sum_chars"] + instructions["chars"],
                      "authorized_definitions_not_initial_payload": definition_size(metadata["authorized"])}
    common = (DESIGN / "499-agent-guide.md").read_text()
    sdk = (DESIGN / "499-sdk-guide.md").read_text()
    for mode in ("act", "read", "draft"):
        for scripting in (False, True):
            name = f"flat-{'D' if scripting else 'C'}-{mode}"
            definitions = definition_size(tool_definitions(mode, scripting))
            rows[name] = {"definitions": definitions, "common_guide": text_size(common),
                          "sdk_guide": text_size(sdk if scripting else ""),
                          "initial_component_sum_chars": definitions["sum_chars"] + len(common) + (len(sdk) if scripting else 0)}
    code = {}
    for case in json.loads((DESIGN / "499-examples.json").read_text())["cases"]:
        source = "".join(case["python"])
        code[case["id"]] = {"source": text_size(source), "programs": len(case["python"]),
                            "documented_operations": len(case["direct"]),
                            "direct_request_json_sum_chars": sum(len(compact({"tool": step["tool"], "arguments": step["arguments"]}))
                                                                 for step in case["direct"]),
                            "script_request_json_sum_chars": sum(len(compact({"tool": "browser_script", "arguments": {"code": program}}))
                                                                 for program in case["python"])}
    references = {name: text_size((DESIGN / name).read_text())
                  for name in ("499-sdk.pyi", "499-sdk.d.ts", "499-examples.json")}
    artifacts = ["499-flat-schemas.json", "499-agent-guide.md", "499-sdk-guide.md", *references]
    return {"units": "characters and UTF-8 bytes; no tokenizer/model run", "rows": rows,
            "example_code_not_model_generated": code, "optional_reference_not_initial_context": references,
            "sha256": {name: hashlib.sha256((DESIGN / name).read_bytes()).hexdigest() for name in artifacts}}


if __name__ == "__main__":
    print(json.dumps(measure(), indent=2, ensure_ascii=False))
