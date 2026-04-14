# Reference: deploy, job payload, files

## `post_workflow` file shape

```json
{
  "definition": {
    "id": "...",
    "name": "...",
    "specVersion": "0.8",
    "start": "...",
    "states": [],
    "version": "2.1.0",
    "functions": [],
    "errors": [],
    "timeouts": {},
    "description": "...",
    "dataInputSchema": {}
  },
  "wfTags": ["mopActivity", "noExport"]
}
```

Do **not** put `wfTags` inside `definition` for this script; `scripts/post_workflow_from_file.py` sends `wfTags` as a separate MCP argument.

## `post_job` file shape

- `workflowName` / `workflowVersion`: must match registered workflow.
- `data`: must satisfy **`dataInputSchema`** and any **jq** in states (e.g. required `app-data.data` object—even `{}`).

## Suggested temp files (gitignore if desired)

| File | Role |
|------|------|
| `tmp-<workflow>-deploy.json` | Output of “pop wfTags → definition wrapper” |
| `tmp-<workflow>-job.json` | `post_job` body |

## Environment

- **`CWM_BRIDGE_URL`** overrides the bridge URL for **`scripts/post_workflow_from_file.py`** and **`scripts/run_job_from_input.py`**; default in this repo is `http://127.0.0.1:9093/crosswork/cwm/v2/mcp`.

## Origin and prompt history

For the conversation that led to this skill, example prompts, and a reusable full-cycle template, see [prompts and process](../../../docs/cwm-mop-workflow-from-cli-sample-prompts-and-process.md).

## Example filenames (from the original session)

These are **not** required to be committed; add them if you want a concrete worked example in-tree.

- Workflow: `cisco-hostname-verify-cwm-sol.sw.json`
- Deploy: `tmp-cisco-hostname-verify-deploy.json`
- Job: `tmp-cisco-hostname-verify-job.json`
