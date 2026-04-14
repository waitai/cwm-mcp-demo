#!/usr/bin/env python3
"""POST a workflow definition from a JSON file to CWM via the MCP bridge.
Usage: post_workflow_from_file.py <workflow.json>
File must have top-level "definition" (workflow object) and optional "wfTags" (array).
"""
import json
import os
import sys
import urllib.request

BRIDGE_URL = os.environ.get(
    "CWM_BRIDGE_URL",
    "http://127.0.0.1:9093/crosswork/cwm/v2/mcp",
).rstrip("/")


def main():
    if len(sys.argv) != 2:
        print("Usage: post_workflow_from_file.py <workflow.json>", file=sys.stderr)
        sys.exit(1)
    path = sys.argv[1]
    with open(path) as f:
        data = json.load(f)
    definition = data.get("definition")
    wf_tags = data.get("wfTags", ["mopActivity"])
    if not definition:
        print("File must contain 'definition' key.", file=sys.stderr)
        sys.exit(1)
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "post_workflow",
            "arguments": {
                "definition": definition,
                "wfTags": wf_tags,
            },
        },
    }
    body = json.dumps(req).encode()
    request = urllib.request.Request(
        BRIDGE_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            out = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        print(f"Request error: {e}", file=sys.stderr)
        sys.exit(1)
    err = out.get("error")
    if err:
        print("Error:", json.dumps(err, indent=2), file=sys.stderr)
        sys.exit(1)
    result = out.get("result", {})
    content = result.get("content", [])
    if content and isinstance(content[0], dict) and "text" in content[0]:
        print(content[0]["text"])
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
