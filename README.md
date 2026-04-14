# Crosswork CWM MCP — local HTTP bridge

This repository is for **setting up Cursor** to work with **Crosswork Workflow Manager (CWM) 2.1**’s MCP server: a small **local HTTP bridge** sits between Cursor and the CWM test environment so you can call CWM’s MCP tools over a stable URL, with **CAS/SSO JWT** handling and optional **Prefab workflow-app tools** merged into the same catalog.

## Companion article

See the Cisco Community post [**From Copilot to MCP: Automating Crosswork Workflow Manager with AI**](https://community.cisco.com/t5/crosswork-automation-hub-blogs/from-copilot-to-mcp-automating-crosswork-workflow-manager-with/ba-p/5544745) for the full story: Copilot and agent-mode Python drivers, deterministic workflow generation, CWM MCP versus REST, Cursor MCP Apps (Prefab), and **why this local bridge exists** (CAS/JWT, JSON-RPC relay, optional merged workflow-app tools). The article also walks through this repository’s bridge pattern and includes a **nine-step MCP demo** (Cursor Agent) you can run when the bridge and CWM test environment are up—prerequisites, prompts, and the `cwm-mop-workflow-from-cli-sample` skill for the final step.

## What you get

| Piece | Purpose |
|--------|---------|
| `mcp_http_bridge.py` | Listens on `localhost`; forwards JSON-RPC to the cluster’s CWM MCP URL; refreshes JWT on 401 / expiry. |
| `workflow-apps/` | Bundled **cwm-workflow-apps** MCP stdio server (Prefab UI). When enabled, the bridge **merges** its three tools into `tools/list` and **proxies** `tools/call` / `resources/*` for those tools. |
| `scripts/` | Optional **stdlib** helpers to call `post_workflow` and `post_job` through the bridge (same JSON-RPC as Cursor). |
| `examples/cursor-mcp.json` | Minimal Cursor MCP snippet pointing at the bridge URL. |

## Requirements

- **Python 3.11+** for the bridge and helper scripts.
- **`uv`** recommended for the bundled `workflow-apps` package ([astral.sh/uv](https://docs.astral.sh/uv/)). The bridge starts workflow-apps with `uv run --project workflow-apps cwm-workflow-apps`.
- Network reachability from your laptop to **Crosswork** (HTTPS).

## Configuration (environment variables)

Set variables in the shell or in a **`.env`** file and load it before starting (for example `set -a && source .env && set +a` in bash).

| Variable | Required | Description |
|----------|----------|-------------|
| `CWM_CROSSWORK_BASE_URL` | **Yes** | Base URL of Crosswork, e.g. `https://cwms.example.com:443` (no trailing slash). |
| `CWM_CAS_USERNAME` | **Yes** | CAS user for JWT ticket flow. |
| `CWM_CAS_PASSWORD` | **Yes** | CAS password (use a secret manager in production; never commit). |
| `CWM_MCP_PATH` | No | Default `/crosswork/cwm/v2/mcp`. |
| `CWM_CAS_PATH` | No | Default `/crosswork/sso`. |
| `CWM_INVENTORY_QUERY_PATH` | No | Default `/crosswork/cwms/inventory/v1/devices/query` (used by bridge `get_inventory_devices`). |
| `CWM_SSL_VERIFY` | No | Default `false` (lab/self-signed). Set `true` when the cluster presents a trusted chain. |
| `CWM_SSO_EXTERNAL_PORT` | No | If CAS returns a `Location` with an **internal** port, set this to the **public** port (digits only) so the bridge can rewrite the TGT URL. |
| `CWM_BRIDGE_URL` | No | For `scripts/*.py` only; default `http://127.0.0.1:9093/crosswork/cwm/v2/mcp`. |

Copy **`.env.example`** to **`.env`**, fill in values, and keep **`.env` out of git** (see `.gitignore`).

## Install

```bash
cd cwm-mcp-demo
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Install workflow-apps dependencies (for `--with-workflow-apps`):

```bash
cd workflow-apps
uv sync
cd ..
```

## Run the bridge

The bridge defaults to **`--port 9092`** if you omit `--port` (see `mcp_http_bridge.py`). The commands below use **9093** so they align with **`CWM_BRIDGE_URL`** / **`examples/cursor-mcp.json`**.

**With Prefab / workflow-app tools merged** (recommended for the full mopActivity demo surface):

```bash
export CWM_CROSSWORK_BASE_URL="https://YOUR-HOST:PORT"
export CWM_CAS_USERNAME="YOUR_USER"
export CWM_CAS_PASSWORD="YOUR_PASSWORD"
# optional: export CWM_SSO_EXTERNAL_PORT=30603

python3 mcp_http_bridge.py --port 9093 --with-workflow-apps
```

**CWM-only** (no merged workflow-app tools):

```bash
python3 mcp_http_bridge.py --port 9093
```

Health check:

```bash
curl -s http://127.0.0.1:9093/
```

Point your MCP client at:

`http://127.0.0.1:9093/crosswork/cwm/v2/mcp`

See **`examples/cursor-mcp.json`** for Cursor.

When `--with-workflow-apps` is used, the bridge sets **`CWM_MCP_URL`** and **`CWM_BASE_URL`** for the subprocess so workflow-apps call **back into the same bridge** (loopback), not directly at Crosswork TLS from the subprocess.

## Cursor skill: reference workflow (`cwm-mop-workflow-from-cli-sample`)

The Cursor skill at **`.cursor/skills/cwm-mop-workflow-from-cli-sample/SKILL.md`** copies **states, timeouts, functions, errors, `specVersion`**, and related patterns from a **reference Serverless Workflow** file on disk. This repository does **not** ship Cisco solution workflows; you should **download that JSON with CWM MCP tools** through this bridge (or adjust the skill to a workflow you do commit).

### Default filename and location

By default the skill points at **`cisco-disk-space-cwm-sol.sw.json`** at the **repository root**. Prompts that use the skill (see **`docs/cwm-mop-workflow-from-cli-sample-prompts-and-process.md`**) assume the same name unless you change it.

### Before you run the skill the first time

1. **Run the bridge** and register the CWM MCP server in Cursor (see **`examples/cursor-mcp.json`**).
2. **Download the reference workflow using MCP only** (not the CWM Web UI): in Cursor, drive **`tools/list`** and **`tools/call`** against the bridge URL so you stay in the same JSON-RPC path the skill uses. Inspect **`tools/list`** for the exact read/export tool names on your build (they can differ by release), then call the tool that returns the **workflow definition** for **`cisco-disk-space-cwm-sol`** at the **version** you need (e.g. **2.1.0**), typically after listing **mopActivity** workflows with the appropriate filter arguments. Write the response body you need for local editing to **`cisco-disk-space-cwm-sol.sw.json`** at the repo root.

Normalize the saved JSON so it matches what the skill expects for a **skeleton** file: editable **DSL** with top-level **`wfTags`** such as **`mopActivity`** / **`noExport`** when mirroring solution workflows—see the skill and **`scripts/post_workflow_from_file.py`** for the **`definition` + `wfTags`** shape used at deploy time.

If the MCP response contains only the inner **`definition`**, merge it with the **`wfTags`** your CWM server expects before using the skill’s copy-as-skeleton flow, or follow the skill’s deploy step that wraps **`definition`** and **`wfTags`**.

### If the file is still missing or your skeleton has another name

Edit **`.cursor/skills/cwm-mop-workflow-from-cli-sample/SKILL.md`** in the **Reference workflow (copy structure)** table (and any other mentions of **`cisco-disk-space-cwm-sol.sw.json`**) so paths and workflow **id** / **version** match what you store locally. Update **`docs/cwm-mop-workflow-from-cli-sample-prompts-and-process.md`** §4 template line **Reference workflow:** if you use a different filename.

## Optional helper scripts

From the repo root of this folder, with the bridge running and env vars set:

```bash
export CWM_BRIDGE_URL="http://127.0.0.1:9093/crosswork/cwm/v2/mcp"
python3 scripts/post_workflow_from_file.py path/to/deploy.json
python3 scripts/run_job_from_input.py path/to/job.json
```

## Security notes

- Treat **`CWM_CAS_PASSWORD`** as a secret: environment, OS keychain, or CI secret store — not git.
- Default **`CWM_SSL_VERIFY=false`** disables TLS certificate verification (common in labs). Use **`true`** when you have proper PKI.
- Review Crosswork RBAC for the CAS user you automate with.
