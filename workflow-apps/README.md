# CWM Workflow Apps

MCP server that provides **Prefab UI apps** for the **cfs-check-cwm-sol** workflow:

1. **Input form** – Collect device name and NSO resource, then start the job.
2. **Job progress & output** – Track status and view result for a job by job ID and run ID.

## Tools

| Tool | Description |
|------|-------------|
| `get_mop_activity_job_status` | Track a mop activity (or any workflow) job: pass `job_id` and `run_id` to see status, duration, and result. Call with no args to open a small form to enter the IDs. |
| `run_cfs_check_and_show_status` | Run the CFS check workflow and return the job status view in one call. |
| `mop_activity_workflow_form` | Opens a form to run any mopActivity workflow; submit runs the job and shows status. |
| `run_mop_workflow_and_show_status` | Run a mopActivity workflow (name\|version, device, product series, resource) and return the job status view. |

## Configuration

The server talks to the same CWM host as your MCP bridge. Set one of:

- **`CWM_BEARER_TOKEN`** – JWT for the CWM API, or
- **`CWM_CAS_USERNAME`** and **`CWM_CAS_PASSWORD`** – to obtain a token via CAS (no default credentials in this bundle).

Optional:

- **`CWM_BASE_URL`** – Crosswork base URL (required for CAS unless you only use `CWM_BEARER_TOKEN`).
- **`CWM_SSL_VERIFY`** – `true` / `false` (default `false` for lab TLS).
- **`CWM_SSO_EXTERNAL_PORT`** – public port for CAS `Location` rewrite when needed.

## Run locally

```bash
cd workflow-apps
uv run cwm-workflow-apps
```

Cursor is configured to run this server via `uv run --project /path/to/workflow-apps cwm-workflow-apps`.

---

## Testing the workflow apps

Use the **CWM remote MCP bridge with workflow-apps** so all workflow-app tools are available through one endpoint.

### 1. Prerequisites

- Bridge running **with** workflow-apps:
  ```bash
  python3 mcp_http_bridge.py --port 9092 --with-workflow-apps
  ```
- In Cursor: MCP server **cwm-remote-mcp-bridge** points to `http://localhost:9092/crosswork/cwm/v2/mcp`.

### 2. Test from Cursor (recommended)

**Option A – Form to start a job**

1. In chat, ask: *“Open the mopActivity workflow form”* or *“Use mop_activity_workflow_form”*.
2. When the form appears, select **cfs-check-cwm-sol** (or another workflow), then fill device name, product series, NSO resource ID, and optional job name.
3. Click **Run workflow**. You should see the job status view (Job ID, Run ID, and output).

**Option B – Start job by tool call**

Ask: *“Run the CFS check workflow for device NCS540X-7 and resource cwm.sol.system.nso.”*  
The agent will call `run_cfs_check_and_show_status` and return the job status view (job ID, run ID, and output).

**Option C – Check job status**

1. After you have a job ID and run ID, ask: *“Get the status for job &lt;jobId&gt; run &lt;runId&gt;”* or *“Use get_mop_activity_job_status with job_id … and run_id …”*.
2. Or ask: *“Open get_mop_activity_job_status”* and enter the IDs in the form.

You should see status (running/completed/failed), duration, and a short result message.

### 3. Test from the command line (curl)

**Start a job and get status**

```bash
curl -s -X POST http://127.0.0.1:9092/crosswork/cwm/v2/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "tools/call",
    "params": {
      "name": "run_cfs_check_and_show_status",
      "arguments": {
        "device_name": "NCS540X-7",
        "resource": "cwm.sol.system.nso"
      }
    }
  }'
```

Save the `jobId` and `runId` from the response.

**Get job status**

```bash
# Replace JOB_ID and RUN_ID with the values from above
curl -s -X POST http://127.0.0.1:9092/crosswork/cwm/v2/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "2",
    "method": "tools/call",
    "params": {
      "name": "get_mop_activity_job_status",
      "arguments": {
        "job_id": "JOB_ID",
        "run_id": "RUN_ID"
      }
    }
  }'
```

### 4. Example values

| Field        | Example value          |
|-------------|-------------------------|
| Device name | `NCS540X-7`            |
| Resource    | `cwm.sol.system.nso`   |
| Job name    | `cfs-check-NCS540X-7` (optional) |

---

## Troubleshooting: Form / status UI not showing in Cursor

If the CFS check form, **mopActivity workflow form**, or status view only show as plain text in Cursor (no Prefab form/UI):

**Why:** Cursor often does not render Prefab UI for **HTTP** MCP servers. The bridge sends `_meta.ui.resourceUri` and the Prefab renderer correctly; if you only see text, it's a Cursor/transport limitation.

1. **Use the stdio workflow-apps server for the form**  
   In Cursor’s MCP settings you have both **cwm-remote-mcp-bridge** (HTTP) and **cwm-workflow-apps** (stdio). Some Cursor versions only render Prefab UI when the tool comes from a **stdio** MCP server.  
   - In the chat/Composer where you want the form, select the **cwm-workflow-apps** server (not the bridge).  
   - Run **mop_activity_workflow_form** (no arguments). The form may then render.  
   - Keep the **bridge** running with `--with-workflow-apps` and set **CWM_MCP_URL** for the stdio server (e.g. `http://127.0.0.1:9092/crosswork/cwm/v2/mcp`) so workflow-apps can call CWM via the bridge.

3. **Reconnect MCP after restarting the bridge**  
   After starting or restarting the bridge, disconnect and reconnect the MCP server in Cursor (or restart Cursor) so it refetches `tools/list` and `resources/list`. Otherwise Cursor may use a cached list that doesn’t include the Prefab renderer.

4. **Run the form from the MCP Tools panel**  
   Try opening the form from Cursor’s MCP / Tools panel (run **mop_activity_workflow_form** with no arguments there). Some builds only render Prefab UI when the tool is run from the tools UI rather than from chat.

5. **Use the browser UI**  
   If Cursor never shows the Prefab form/status, use the [browser status UI](#view-job-status-in-the-prefab-ui-browser) (form and status at http://127.0.0.1:8765/ with the bridge and status UI server running).

### Prefab UI used to work, now it doesn't

If you used to see Prefab (forms, tables, cards) in Cursor and now only see text:

- **Which server are you using?** If you switched to the **HTTP bridge** (e.g. cwm-remote-mcp-bridge) for workflow or NSO tools, try switching back to the **stdio** server for that tool (**cwm-workflow-apps** or **cisco-nso-mcp-server**). Cursor often only renders Prefab for stdio MCP.
- **Reconnect MCP**  
  In Cursor: **Settings → MCP** → find the server → **Disconnect**, then **Connect** (or restart Cursor). This forces Cursor to refetch `tools/list` and `resources/list` so it rediscover the Prefab renderer.
- **Run from the Tools panel**  
  Open **Tools** (wrench), pick the stdio server, run the app tool (e.g. **get_service_types**, **mop_activity_workflow_form**) from there. Some Cursor builds only render Prefab when the tool is invoked from the Tools UI.
- **Cursor update**  
  A Cursor update can change MCP or Prefab behavior. Check release notes; if nothing helps, use the [browser UI](#view-job-status-in-the-prefab-ui-browser) for workflow status and NSO data.

---

## View job status in the Prefab UI (browser)

Cursor only shows the text part of tool results, not the Prefab form/status UI. To see the **same status UI** (badge, cards, duration, result) in a browser:

1. **Start the MCP bridge** with workflow-apps (so job data can be fetched):
   ```bash
   python3 mcp_http_bridge.py --port 9092 --with-workflow-apps
   ```

2. **Start the status UI server** from the `cwm-workflow-apps` directory:
   ```bash
   cd cwm-workflow-apps
   CWM_MCP_URL=http://127.0.0.1:9092/crosswork/cwm/v2/mcp uv run cwm-workflow-status-ui
   ```
   If port 8765 is already in use (`Address already in use`), either free it or pick another port:
   - **Use another port:** `uv run cwm-workflow-status-ui --port 8766` (then open http://127.0.0.1:8766/)
   - **Free 8765:** `lsof -ti :8765 | xargs kill -9` (macOS), then run the command above again.

3. **Open in your browser:**
   - **Index (form):** http://127.0.0.1:8765/
   - **Status for a job:** http://127.0.0.1:8765/?job_id=JOB_ID&run_id=RUN_ID

   Use the form on the index page to enter job and run IDs, or paste the URL with `job_id` and `run_id` query params. The page renders the same Prefab layout as the MCP tool (status badge, Job ID/Run ID cards, duration, result message).
