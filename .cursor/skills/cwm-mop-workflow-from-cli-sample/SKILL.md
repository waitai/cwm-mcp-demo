---
name: cwm-mop-workflow-from-cli-sample
description: Builds a CWM mopActivity DSL workflow from a sample CLI command and output (TextFSM + util-executor pattern), deploys it via the MCP bridge post_workflow, and runs a test job with post_job. Use when the user wants to automate validation of show command output, create a workflow from CLI samples, deploy workflows to Crosswork, or test workflows after deployment.
---

# CWM mopActivity workflow from CLI sample

End-to-end pattern used in this repo: **reference DSL** → **new workflow** → **TextFSM template** from sample output → **deploy** (`post_workflow`) → **test** (`post_job`).

## When to apply

- User provides (or will provide) a **CLI command** and **representative output** to validate or capture.
- Goal is a **registered CWM workflow** (`mopActivity`) callable via **`post_job`**.
- This repo’s **MCP HTTP bridge** is the transport. Scripts default to **`http://127.0.0.1:9093/crosswork/cwm/v2/mcp`** (override with **`CWM_BRIDGE_URL`**); match whatever port you pass to `mcp_http_bridge.py` (see root **README**).

## Reference workflow (copy structure)

Use an existing solution workflow as the **skeleton**—do not invent DSL shape from scratch.

If **`cisco-disk-space-cwm-sol.sw.json`** is not in this clone, follow the root **README** section **“Cursor skill: reference workflow”** to pull the definition from CWM **using MCP tools** through the bridge, or edit the table below (and prompts in **`docs/`**) to match another file you keep in-repo.

| Pattern | Reference file (repo root) |
|--------|----------------------------|
| Product-series gate + **util-executor-cwm-sol** + TextFSM inject | `cisco-disk-space-cwm-sol.sw.json` |
| Simpler NSO exec + foreach (no util template) | `command-capture` style in workflow exports / MOP JSON |

For **CLI parse + rak util**, mirror **disk-space**: `check-device-family` switch → `inject` (command + template + expressions) → `operation` with `subFlowRef` **util-executor-cwm-sol** `2.1.0` and `fromStateData` including **`vendor`: `Cisco Systems`**.

## Step 1 — Capture requirements

Collect explicitly (ask the user if missing):

1. **Workflow id/name** and **version** (e.g. `my-check-cwm-sol`, `2.1.0`).
2. **CLI command** (exact string sent to device).
3. **Sample output** (multi-line ok); note **IOS-XR** (timestamp lines before data) vs **IOS-XE** if relevant.
4. **What to validate**: presence of a line, numeric threshold, match to **device name**, etc.
5. **Product scope**: either **same allow-lists as `cisco-disk-space-cwm-sol`** (XR + XE `dataConditions`) or a deliberate subset—**do not drop the gate** if the reference uses it.

## Step 2 — TextFSM template

Author a **TextFSM-style** template string (same family as disk-space `inject.data.template`):

- **`Value` / `Value Required`** lines declare captured fields and regex fragments, e.g. `Value Required hostname (\\S+)`.
- **State** (usually **`Start`**) + **line rules** with `^` anchors, e.g. `^hostname ${hostname}`.
- JSON stores newlines as `\n` and backslashes doubled: `(\\S+)` in JSON → `(\S+)` in the template.

**Rule quality:** Base rules on the **user’s sample** (and one variant if they have it). If the user did not specify validation rules, **derive** minimal correct rules from the sample, then **state assumptions** in the workflow `description` and in chat.

Pair with **`expressions`** (jq on parsed **rows**) when the executor expects them—e.g. error if `( . | length == 0 )` after parse.

## Step 3 — Author the `.sw.json` file

1. Copy **states, timeouts, functions, errors, specVersion** pattern from `cisco-disk-space-cwm-sol.sw.json`.
2. Set **`id`**, **`name`**, **`description`**, **`dataInputSchema`** to match **inputs your states reference** (`app-data.device`, `app-data.resource`, optional `app-data.data` fields).
3. Add top-level **`wfTags`**: include **`mopActivity`** (and **`noExport`** if matching other solution workflows).
4. Validate JSON: `python3 -m json.tool <file>`.

## Step 4 — Deploy to CWM (`post_workflow`)

CWM MCP expects a wrapper object: **`definition`** (full DSL object **without** `wfTags`) + **`wfTags`** array.

```bash
# Build deploy payload (example pattern)
python3 -c "
import json, pathlib
p = pathlib.Path('YOUR_WORKFLOW.sw.json')
d = json.loads(p.read_text())
tags = d.pop('wfTags', ['mopActivity', 'noExport'])
pathlib.Path('tmp-deploy.json').write_text(json.dumps({'definition': d, 'wfTags': tags}, indent=2))
"
python3 scripts/post_workflow_from_file.py tmp-deploy.json
```

Requires **bridge reachable** and **`post_workflow`** tool on CWM MCP. Record returned **workflow registry id** if present.

## Step 5 — Test job (`post_job`)

1. **Known-good test data** (use if present in conversation, `tmp-*.json`, or repo docs—e.g. device **NCS540X-7**, product series **Cisco Network Convergence System 540 Series Routers**, resource **`cwm.sol.system.nso`**).
2. If **no** canonical test payload exists, **prompt the user** for: device **name**, **productSeries** (must match a branch in `check-device-family` if used), **resource**, and any **schema fields** (`app-data.data`).

Create a small JSON file:

```json
{
  "workflowName": "<id>",
  "workflowVersion": "<version>",
  "jobName": "test-<id>-<device>",
  "tags": ["mopActivity"],
  "data": { "app-data": { "device": { "name": "...", "productSeries": "..." }, "resource": "...", "data": {} } }
}
```

Run:

```bash
python3 scripts/run_job_from_input.py path/to/job.json
```

3. Follow up with **`get_mop_activity_job_status`** (`job_id` / `run_id`) or **`get_job_runs`** to confirm **engine status** and **business** success/failure message.

## Pitfalls (repo-specific)

- **`command-capture-cwm-sol`**: `commandCapture` lives at **top level** of job `data` next to `app-data`; **`app-data.device.vendor`** must be **`Cisco Systems`** or **`Juniper Networks`** for vendor routing—see workflow definition.
- **Util-executor workflows**: **vendor** is often fixed in `fromStateData` (**`Cisco Systems`** in disk-space pattern); product series is enforced by your **switch**, not by `vendor` alone.
- **Templates** must match what **util-executor** returns (**`stash.output`** shape / field names); adjust **`validate-*` jq** if keys differ in your environment.

## Optional deep dive

- [reference.md](reference.md) — deploy/job field checklist and file naming.
- [docs/cwm-mop-workflow-from-cli-sample-prompts-and-process.md](../../../docs/cwm-mop-workflow-from-cli-sample-prompts-and-process.md) — prompts and conversation arc that produced this skill.
