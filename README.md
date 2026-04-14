# Crosswork CWM MCP — local HTTP bridge

This repository is for **setting up Cursor** to work with **Crosswork Workflow Manager (CWM) 2.1**’s MCP server: a small **local HTTP bridge** sits between Cursor and the CWM test environment so you can call CWM’s MCP tools over a stable URL, with **CAS/SSO JWT** handling and optional **Prefab workflow-app tools** merged into the same catalog.

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

## License

The upstream monorepo may apply its own license; if you publish **only** this folder as a new repo, add a `LICENSE` file consistent with your organization’s policy.
