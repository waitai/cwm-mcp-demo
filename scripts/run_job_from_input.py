#!/usr/bin/env python3
"""Run a CWM workflow job via the MCP bridge using a JSON input file.
Usage: run_job_from_input.py <input.json>
Input file must have: workflowName, workflowVersion, data; optional: jobName, tags.
Example: run_job_from_input.py ../create-fleet-upgrade-job-mcp-input.json
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
        print("Usage: run_job_from_input.py <input.json>", file=sys.stderr)
        sys.exit(1)
    path = sys.argv[1]
    with open(path) as f:
        payload = json.load(f)
    workflow_name = payload.get("workflowName")
    workflow_version = payload.get("workflowVersion")
    data = payload.get("data")
    if not workflow_name or not workflow_version or data is None:
        print("Input must contain workflowName, workflowVersion, and data.", file=sys.stderr)
        sys.exit(1)
    arguments = {
        "workflowName": workflow_name,
        "workflowVersion": workflow_version,
        "data": data,
    }
    if payload.get("jobName"):
        arguments["jobName"] = payload["jobName"]
    if payload.get("tags") is not None:
        arguments["tags"] = payload["tags"]
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "post_job",
            "arguments": arguments,
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
