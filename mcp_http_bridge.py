#!/usr/bin/env python3
"""
HTTP bridge proxy for the CWM MCP server.
Runs a local HTTP server that forwards requests to the HTTPS MCP server,
handling SSL verification, Bearer token authentication, and automatic
token refresh transparently.

Configuration: set environment variables (see README.md and .env.example).
Never commit real credentials.

Usage:
    export CWM_CROSSWORK_BASE_URL=https://crosswork.example.com:443
    export CWM_CAS_USERNAME=...
    export CWM_CAS_PASSWORD=...
    ./mcp_http_bridge.py [--port 9093] [--with-workflow-apps]

Then configure MCP clients to connect to:
    http://localhost:9093/crosswork/cwm/v2/mcp

Token is auto-refreshed on startup and whenever a 401 is received.
"""
import sys
import json
import argparse
import signal
import threading
import time
import base64
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import requests
import urllib3
import subprocess
import os
from socketserver import ThreadingMixIn

# Disable SSL warnings for self-signed certificates (typical in lab); set CWM_SSL_VERIFY=true for stricter TLS
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Populated by load_bridge_config() — no embedded host or secrets
REMOTE_HOST = ""
REMOTE_MCP_URL = ""
CAS_URL = ""
INVENTORY_QUERY_URL = ""
CAS_USERNAME = ""
CAS_PASSWORD = ""
SSL_VERIFY = False
SSO_EXTERNAL_PORT = ""


def _env_bool(key: str, default: str = "false") -> bool:
    return os.environ.get(key, default).strip().lower() in ("1", "true", "yes", "on")


def _require_env(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        print(f"[MCP Bridge] Missing required environment variable: {key}", file=sys.stderr)
        print("[MCP Bridge] See README.md and .env.example.", file=sys.stderr)
        sys.exit(1)
    return v


def load_bridge_config():
    """Load Crosswork URL and CAS credentials from the environment."""
    global REMOTE_HOST, REMOTE_MCP_URL, CAS_URL, INVENTORY_QUERY_URL, CAS_USERNAME, CAS_PASSWORD
    global SSL_VERIFY, SSO_EXTERNAL_PORT

    REMOTE_HOST = _require_env("CWM_CROSSWORK_BASE_URL").rstrip("/")
    mcp_path = os.environ.get("CWM_MCP_PATH", "/crosswork/cwm/v2/mcp").strip()
    if not mcp_path.startswith("/"):
        mcp_path = "/" + mcp_path
    REMOTE_MCP_URL = REMOTE_HOST + mcp_path

    cas_path = os.environ.get("CWM_CAS_PATH", "/crosswork/sso").strip()
    if not cas_path.startswith("/"):
        cas_path = "/" + cas_path
    CAS_URL = REMOTE_HOST + cas_path.rstrip("/")

    inv_path = os.environ.get(
        "CWM_INVENTORY_QUERY_PATH",
        "/crosswork/cwms/inventory/v1/devices/query",
    ).strip()
    if not inv_path.startswith("/"):
        inv_path = "/" + inv_path
    INVENTORY_QUERY_URL = REMOTE_HOST + inv_path

    CAS_USERNAME = _require_env("CWM_CAS_USERNAME")
    CAS_PASSWORD = _require_env("CWM_CAS_PASSWORD")
    SSL_VERIFY = _env_bool("CWM_SSL_VERIFY", "false")
    SSO_EXTERNAL_PORT = os.environ.get("CWM_SSO_EXTERNAL_PORT", "").strip()

# Token state (thread-safe)
_token_lock = threading.Lock()
_bearer_token = None
_token_expiry = 0  # epoch seconds

# Prefab UI: Cursor renders tool result when tool has _meta.ui.resourceUri and can load this resource
PREFAB_RENDERER_URI = "ui://prefab/renderer.html"

# Workflow-apps proxy: tool names and definitions for tools/list merge
# Include _meta.ui.resourceUri so Cursor renders structuredContent as Prefab UI (same as stdio workflow-apps).
WORKFLOW_APP_TOOLS = {
    "get_mop_activity_job_status",
    "mop_activity_workflow_form",
    "run_mop_workflow_and_show_status",
}
_UI_META = {"_meta": {"ui": {"resourceUri": PREFAB_RENDERER_URI}}}
WORKFLOW_APP_TOOL_DEFS = [
    {
        "name": "get_mop_activity_job_status",
        "description": "Track progress and view final output of a mop activity (or any workflow) job. Pass job_id and run_id from the status card, or open with no args to enter them.",
        "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}, "run_id": {"type": "string"}}},
        "annotations": {"title": "MOP activity job status", "readOnlyHint": True},
        **_UI_META,
    },
    {
        "name": "mop_activity_workflow_form",
        "description": "Open a form to run any mopActivity workflow. Choose workflow, device, product series, and resource. Clicking Run workflow sends a message to the chat (it does not call a tool). Run that message to execute the workflow and see the Job Status view.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"title": "Run mopActivity workflow", "readOnlyHint": False},
        **_UI_META,
    },
    {
        "name": "run_mop_workflow_and_show_status",
        "description": "Run a mopActivity workflow (workflow name|version) with device and resource; return job status view. Pass device as 'host_name|product_series' (from inventory form) or device_name and product_series separately.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow": {"type": "string"},
                "device": {"type": "string", "description": "Device from form: host_name|product_series"},
                "device_name": {"type": "string"},
                "product_series": {"type": "string"},
                "resource": {"type": "string"},
                "job_name": {"type": "string"},
            },
            "required": ["workflow", "resource"],
        },
        "annotations": {"title": "Run mopActivity workflow and show status", "readOnlyHint": True},
        **_UI_META,
    },
]
# Set by main() for subprocess and bridge URL
_bridge_mcp_url = None
_workflow_apps_proc = None
_workflow_apps_lock = threading.Lock()


def _inject_prefab_meta_on_tool_result(resp):
    """Ensure tools/call results for workflow-app tools include _meta.ui.resourceUri.

    tools/list already advertises _meta for these tools, but the stdio server often omits
    _meta on the JSON-RPC *result* body. Some MCP clients (including Cursor builds) only
    attach the Prefab renderer when this field is present on the call result, not only
    on the tool definition.
    """
    if not isinstance(resp, dict) or "result" not in resp:
        return resp
    result = resp["result"]
    if not isinstance(result, dict) or result.get("isError"):
        return resp
    if "structuredContent" not in result:
        return resp
    existing = result.get("_meta")
    if not isinstance(existing, dict):
        existing = {}
    ui = existing.get("ui")
    if not isinstance(ui, dict):
        ui = {}
    if not ui.get("resourceUri"):
        ui = {**ui, "resourceUri": PREFAB_RENDERER_URI}
        result["_meta"] = {**existing, "ui": ui}
    return resp


def _drain_workflow_apps_stderr(proc):
    """Read workflow-apps stderr so the subprocess never blocks on a full PIPE buffer."""
    try:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            if line:
                log(f"[workflow-apps] {line.rstrip()}")
    except Exception:
        pass


def log(message):
    """Log to stderr; never block the HTTP handler on a full stdout pipe (e.g. IDE-captured terminals)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [MCP Bridge] {message}\n"
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except (BlockingIOError, BrokenPipeError, OSError):
        pass


def get_jwt_token():
    """Authenticate via CAS and return a fresh JWT token.
    Two-step process: get TGT, then exchange for JWT (service ticket)."""
    global _bearer_token, _token_expiry

    log("Refreshing JWT token...")
    try:
        # Step 1: Get TGT (Ticket Granting Ticket)
        tgt_response = requests.post(
            f"{CAS_URL}/v1/tickets",
            data={"username": CAS_USERNAME, "password": CAS_PASSWORD},
            verify=SSL_VERIFY,
            timeout=30
        )
        if tgt_response.status_code != 201:
            log(f"✗ TGT request failed: {tgt_response.status_code} {tgt_response.text[:200]}")
            return False

        tgt_url = tgt_response.headers.get("Location", "")
        if not tgt_url:
            # Try to extract from response body
            tgt_url = tgt_response.text.strip()

        # Optional: CAS may return Location with an internal port; rewrite to public port
        if SSO_EXTERNAL_PORT:
            tgt_url = re.sub(r':\d+(/crosswork/sso/)', rf':{SSO_EXTERNAL_PORT}\1', tgt_url)

        log(f"  TGT obtained: ...{tgt_url[-30:]}")

        # Step 2: Exchange TGT for JWT
        jwt_response = requests.post(
            tgt_url,
            data={"service": f"{REMOTE_HOST}/app-dashboard"},
            verify=SSL_VERIFY,
            timeout=30
        )
        if jwt_response.status_code != 200:
            log(f"✗ JWT request failed: {jwt_response.status_code} {jwt_response.text[:200]}")
            return False

        token = jwt_response.text.strip()
        if not token or len(token) < 50:
            log(f"✗ Invalid JWT received (length={len(token)})")
            return False

        # Decode JWT to get expiry (no verification needed, just reading claims)
        try:
            payload_b64 = token.split('.')[1]
            # Add padding
            payload_b64 += '=' * (4 - len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = payload.get('exp', 0)
            exp_str = datetime.fromtimestamp(exp).strftime("%H:%M:%S") if exp else "unknown"
        except Exception:
            exp = 0
            exp_str = "unknown"

        with _token_lock:
            _bearer_token = token
            _token_expiry = exp

        log(f"  JWT refreshed successfully (expires: {exp_str})")
        return True

    except requests.exceptions.RequestException as e:
        log(f"✗ Token refresh error: {e}")
        return False


def get_token():
    """Get the current token, refreshing if expired or missing."""
    global _bearer_token, _token_expiry

    with _token_lock:
        token = _bearer_token
        expiry = _token_expiry

    # Refresh if no token, or within 5 minutes of expiry
    now = time.time()
    if not token or (expiry > 0 and now >= expiry - 300):
        log(f"Token {'expired' if token else 'missing'}, refreshing...")
        if get_jwt_token():
            with _token_lock:
                return _bearer_token
        # If refresh failed but we have an old token, try it anyway
        if token:
            log("Using existing token despite refresh failure")
            return token
        return None

    return token


def _is_sso_redirect(response):
    """Check if the response is an SSO login redirect (token rejected)."""
    if response.status_code in (302, 303):
        location = response.headers.get("Location", "")
        if "/crosswork/sso/login" in location:
            return True
    # CWM gateway sometimes returns 404 with SSO login path in body
    if response.status_code == 404:
        try:
            body_text = response.text[:500]
            if "/crosswork/sso/login" in body_text:
                return True
        except Exception:
            pass
    return False


def _do_post(token, request_data, body):
    """Send a single POST request to the remote MCP server."""
    return requests.post(
        REMOTE_MCP_URL,
        json=request_data if isinstance(request_data, dict) else None,
        data=body if not isinstance(request_data, dict) else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        verify=SSL_VERIFY,
        timeout=120,
        allow_redirects=False
    )


def forward_to_remote(body, request_data):
    """Forward request to remote server, retry with fresh token on auth failure.
    
    Handles both HTTP 401 and SSO login redirects (302/404) as auth failures.
    Retries up to 3 times with fresh tokens on transient SSO redirect issues.
    """
    token = get_token()
    if not token:
        return None, "No valid token available"

    max_retries = 3
    for attempt in range(max_retries):
        response = _do_post(token, request_data, body)

        # Check for auth failure: 401 or SSO redirect
        if response.status_code == 401 or _is_sso_redirect(response):
            reason = f"HTTP {response.status_code}"
            if _is_sso_redirect(response):
                reason = "SSO redirect (token rejected)"
            log(f"Got {reason} — refreshing token and retrying ({attempt + 1}/{max_retries})...")
            
            if get_jwt_token():
                with _token_lock:
                    token = _bearer_token
                if attempt < max_retries - 1:
                    import time
                    time.sleep(1)  # Brief pause before retry
                    continue
                # Last attempt with fresh token
                response = _do_post(token, request_data, body)
            else:
                log("Token refresh failed")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(2)
                    continue
                break
        else:
            # Success or non-auth error — return immediately
            break

    return response, None


# One-time MCP handshake so workflow-apps accepts tools/call
_workflow_apps_initialized = False


def _ensure_workflow_apps_initialized():
    """Send MCP Initialize + notifications/initialized to subprocess so it accepts tools/call."""
    global _workflow_apps_proc, _workflow_apps_initialized
    if _workflow_apps_proc is None or _workflow_apps_initialized:
        return
    with _workflow_apps_lock:
        if _workflow_apps_initialized:
            return
        try:
            init_req = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "mcp-bridge", "version": "1.0"},
                    "capabilities": {},
                },
            }
            _workflow_apps_proc.stdin.write(json.dumps(init_req) + "\n")
            _workflow_apps_proc.stdin.flush()
            init_resp = _workflow_apps_proc.stdout.readline()
            if not init_resp:
                return
            json.loads(init_resp)  # ensure valid
            notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            _workflow_apps_proc.stdin.write(json.dumps(notif) + "\n")
            _workflow_apps_proc.stdin.flush()
            _workflow_apps_initialized = True
            log("  Workflow-apps MCP session initialized")
        except (BrokenPipeError, ValueError, OSError) as e:
            log(f"Workflow-apps init error: {e}")


def _fetch_inventory_devices_bridge():
    """Fetch device list from CWM Inventory API using bridge token. Returns list of {host_name, product_series, uuid} or None."""
    token = get_token()
    if not token:
        return None
    try:
        r = requests.post(
            INVENTORY_QUERY_URL,
            json={"node": {"filterData": {"PageNum": 0, "PageSize": 500}}},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
            verify=SSL_VERIFY,
            timeout=30,
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    items = data.get("data") if isinstance(data, dict) else []
    if not isinstance(items, list):
        return None
    out = []
    for node in items:
        if not isinstance(node, dict):
            continue
        host_name = (node.get("host_name") or "").strip()
        product_info = node.get("product_info")
        product_series = ""
        if isinstance(product_info, dict):
            product_series = (product_info.get("product_series") or "").strip()
        uuid_val = (node.get("uuid") or "").strip()
        if host_name:
            out.append({"host_name": host_name, "product_series": product_series or "Unknown", "uuid": uuid_val})
    return out


def _call_workflow_apps(request_data):
    """Send MCP request to workflow-apps subprocess via stdio; return JSON-RPC response dict or None."""
    global _workflow_apps_proc
    if _workflow_apps_proc is None:
        return None
    _ensure_workflow_apps_initialized()
    req_line = json.dumps(request_data) + "\n"
    with _workflow_apps_lock:
        try:
            _workflow_apps_proc.stdin.write(req_line)
            _workflow_apps_proc.stdin.flush()
            out = _workflow_apps_proc.stdout.readline()
            if not out:
                return None
            return json.loads(out)
        except (BrokenPipeError, ValueError, OSError) as e:
            log(f"Workflow-apps subprocess error: {e}")
            return None


class MCPBridgeHandler(BaseHTTPRequestHandler):
    """HTTP handler that forwards MCP requests to the remote HTTPS server; optionally merges and proxies workflow-app tools.

    We avoid socket.settimeout on the accepted socket and custom Connection headers: together
    with BaseHTTPRequestHandler's keep-alive loop that can look like curl hanging on GET /
    to localhost (especially with IPv6 / localhost name resolution quirks).
    """

    def do_POST(self):
        """Forward POST to CWM, or intercept tools/list (merge) and tools/call (route workflow-app tools)."""
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            request_data = json.loads(body)
            method = request_data.get('method', 'unknown')
            log(f"→ Received: {method} (id={request_data.get('id')})")
        except json.JSONDecodeError:
            request_data = None
            method = 'unknown'

        try:
            # Intercept tools/call get_inventory_devices: bridge fetches from CWM inventory (workflow-apps calls this via MCP)
            if isinstance(request_data, dict) and method == "tools/call":
                params = request_data.get("params") or {}
                name = params.get("name") or ""
                if name == "get_inventory_devices":
                    log("  Handling get_inventory_devices (CWM inventory API)")
                    devices = _fetch_inventory_devices_bridge()
                    req_id = request_data.get("id")
                    payload = devices if devices is not None else []
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
                    response_body = json.dumps(resp).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response_body)))
                    self.end_headers()
                    self.wfile.write(response_body)
                    return
            # Intercept tools/call for workflow-app tools when proxy is enabled
            if isinstance(request_data, dict) and method == "tools/call" and _workflow_apps_proc is not None:
                params = request_data.get("params") or {}
                name = params.get("name") or ""
                if name in WORKFLOW_APP_TOOLS:
                    log(f"  Proxying tool to workflow-apps: {name}")
                    resp = _call_workflow_apps(request_data)
                    if resp is not None:
                        _inject_prefab_meta_on_tool_result(resp)
                        response_body = json.dumps(resp).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(response_body)))
                        self.end_headers()
                        self.wfile.write(response_body)
                        return
                    log("  Workflow-apps no response, falling back to CWM")

            # Intercept resources/read for Prefab renderer so Cursor can load the UI iframe
            if isinstance(request_data, dict) and method == "resources/read" and _workflow_apps_proc is not None:
                params = request_data.get("params") or {}
                uri = (params.get("uri") or "").strip()
                if uri == PREFAB_RENDERER_URI or uri.endswith("renderer.html"):
                    log(f"  Proxying resource to workflow-apps: {uri}")
                    resp = _call_workflow_apps(request_data)
                    if resp is not None and "error" not in resp:
                        response_body = json.dumps(resp).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(response_body)))
                        self.end_headers()
                        self.wfile.write(response_body)
                        return
                    log("  Workflow-apps resource no response")

            # Intercept resources/list to merge Prefab renderer so Cursor can discover it
            if isinstance(request_data, dict) and method == "resources/list" and _workflow_apps_proc is not None:
                wa_resp = _call_workflow_apps(request_data)
                if wa_resp is not None and "error" not in wa_resp:
                    result = wa_resp.get("result")
                    resources = result.get("resources", result) if isinstance(result, dict) else result
                    if isinstance(resources, list) and any(
                        (r.get("uri") or "").endswith("renderer.html") for r in resources if isinstance(r, dict)
                    ):
                        log("  Proxying resources/list from workflow-apps (Prefab renderer present)")
                        response_body = json.dumps(wa_resp).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(response_body)))
                        self.end_headers()
                        self.wfile.write(response_body)
                        return
                # Forward to CWM and merge in Prefab renderer from workflow-apps if we have it
                response, error = forward_to_remote(body, request_data)
                if not error and response.status_code == 200 and wa_resp is not None and "error" not in wa_resp:
                    try:
                        cwm_result = response.json()
                        result = cwm_result.get("result")
                        resources = result.get("resources", result) if isinstance(result, dict) else result
                        if not isinstance(resources, list):
                            resources = []
                        has_renderer = any(
                            (r.get("uri") or "").endswith("renderer.html") for r in resources if isinstance(r, dict)
                        )
                        if not has_renderer:
                            wa_result = wa_resp.get("result")
                            wa_resources = wa_result.get("resources", wa_result) if isinstance(wa_result, dict) else wa_result
                            if isinstance(wa_resources, list):
                                for r in wa_resources:
                                    if isinstance(r, dict) and (r.get("uri") or "").endswith("renderer.html"):
                                        resources = list(resources) + [r]
                                        log("  Merged Prefab renderer into resources/list")
                                        break
                            if isinstance(result, dict):
                                cwm_result["result"] = {**result, "resources": resources}
                            response_body = json.dumps(cwm_result).encode()
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(response_body)))
                            self.end_headers()
                            self.wfile.write(response_body)
                            return
                    except (KeyError, TypeError, json.JSONDecodeError):
                        pass
                if not error:
                    response_body = response.content
                    self.send_response(response.status_code)
                    for key, value in response.headers.items():
                        if key.lower() in ("content-type", "content-length"):
                            self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(response_body)
                    return
                log(f"  resources/list CWM forward error: {error}")
                error_response = json.dumps({
                    "jsonrpc": "2.0",
                    "id": request_data.get("id"),
                    "error": {"code": -32603, "message": str(error)},
                }).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(error_response)))
                self.end_headers()
                self.wfile.write(error_response)
                return

            # Intercept tools/list to merge workflow-app tools when proxy is enabled
            if isinstance(request_data, dict) and method == "tools/list" and _workflow_apps_proc is not None:
                response, error = forward_to_remote(body, request_data)
                if not error and response.status_code == 200:
                    try:
                        cwm_result = response.json()
                        result = cwm_result.get("result")
                        tools = result.get("tools", result) if isinstance(result, dict) else result
                        if not isinstance(tools, list):
                            tools = []
                        tools = list(tools) + list(WORKFLOW_APP_TOOL_DEFS)
                        if isinstance(result, dict):
                            cwm_result["result"] = {**result, "tools": tools}
                        else:
                            cwm_result["result"] = tools
                        response_body = json.dumps(cwm_result).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(response_body)))
                        self.end_headers()
                        self.wfile.write(response_body)
                        return
                    except (KeyError, TypeError, json.JSONDecodeError):
                        pass
                # fall through to normal forward if merge failed

            response, error = forward_to_remote(body, request_data)

            if error:
                log(f"✗ {error}")
                error_response = json.dumps({
                    "jsonrpc": "2.0",
                    "id": request_data.get("id") if isinstance(request_data, dict) else None,
                    "error": {"code": -32603, "message": f"Bridge error: {error}"}
                }).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(error_response)))
                self.end_headers()
                self.wfile.write(error_response)
                return

            response_body = response.content
            log(f"← Response: {response.status_code} ({len(response_body)} bytes)")

            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() in ('content-type', 'content-length'):
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response_body)

        except requests.exceptions.RequestException as e:
            log(f"✗ Error forwarding to remote: {e}")
            error_response = json.dumps({
                "jsonrpc": "2.0",
                "id": request_data.get("id") if isinstance(request_data, dict) else None,
                "error": {"code": -32603, "message": f"Bridge error: {str(e)}"}
            }).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_response)))
            self.end_headers()
            self.wfile.write(error_response)

        except Exception as e:
            log(f"✗ Unexpected error in POST handler: {e!r}")
            try:
                error_response = json.dumps({
                    "jsonrpc": "2.0",
                    "id": request_data.get("id") if isinstance(request_data, dict) else None,
                    "error": {"code": -32603, "message": "Bridge internal error"},
                }).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(error_response)))
                self.end_headers()
                self.wfile.write(error_response)
            except Exception:
                pass

    def _health_json_body(self) -> bytes:
        # Lock-free snapshot: health must never block behind token refresh / POST handlers
        # (which can hold _token_lock or stall on network). Values may be momentarily stale.
        has_token = _bearer_token is not None
        expiry = _token_expiry
        exp_str = datetime.fromtimestamp(expiry).strftime("%Y-%m-%d %H:%M:%S") if expiry else "none"
        return json.dumps({
            "status": "ok",
            "remote": REMOTE_MCP_URL,
            "token_valid": has_token,
            "token_expires": exp_str
        }).encode()

    def do_GET(self):
        """Health check on any path (same behavior as legacy hardcoded bridge).

        Some HTTP MCP clients issue GET on the MCP URL to verify reachability; returning
        405 there breaks those clients even though JSON-RPC still uses POST.
        """
        self.close_connection = True
        try:
            response = self._health_json_body()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            try:
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        except Exception as e:
            log(f"do_GET error: {e!r}")
            try:
                err = json.dumps({"status": "error", "message": "health handler failed"}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            except Exception:
                pass

    def do_HEAD(self):
        """HEAD returns health metadata Content-Length without a body (legacy-friendly)."""
        response = self._health_json_body()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()

    def do_OPTIONS(self):
        """Respond to preflight / capability probes."""
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.end_headers()

    def log_message(self, format, *args):
        """Suppress default HTTP log messages (we have our own logging)."""
        pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a thread so subprocess callbacks don't deadlock."""

    # Default backlog (5) is too small for MCP clients that open several connections at once;
    # overflow shows up as clients stuck in SYN_SENT while the server is already LISTENing.
    request_queue_size = 256
    # Avoid blocking process exit on hung handler threads.
    daemon_threads = True


def main():
    global _bridge_mcp_url, _workflow_apps_proc
    parser = argparse.ArgumentParser(description="HTTP bridge proxy for Crosswork MCP server")
    parser.add_argument('--port', type=int, default=9093, help='Port to listen on (default: 9093)')
    parser.add_argument(
        '--host',
        type=str,
        default='127.0.0.1',
        help='Bind address (default 127.0.0.1). Use 0.0.0.0 only if a client cannot reach loopback.',
    )
    parser.add_argument('--with-workflow-apps', action='store_true', help='Merge and proxy workflow-app tools (Prefab) via bundled workflow-apps')
    parser.add_argument('--workflow-apps-dir', type=str, default=None, help='Path to workflow-apps project with pyproject.toml (default: ./workflow-apps next to this script)')
    args = parser.parse_args()
    load_bridge_config()

    _bridge_mcp_url = f"http://127.0.0.1:{args.port}/crosswork/cwm/v2/mcp"

    log("Authenticating with CAS server...")
    if not get_jwt_token():
        log("⚠ Warning: Initial token fetch failed. Will retry on first request.")

    if args.with_workflow_apps:
        wf_dir = args.workflow_apps_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow-apps")
        if os.path.isdir(wf_dir) and os.path.isfile(os.path.join(wf_dir, "pyproject.toml")):
            env = os.environ.copy()
            # Shell may still export VIRTUAL_ENV from another repo after `source …/activate`;
            # `uv run --project workflow-apps` then warns it does not match that project’s .venv.
            env.pop("VIRTUAL_ENV", None)
            env.pop("VIRTUAL_ENV_PROMPT", None)
            env["CWM_MCP_URL"] = _bridge_mcp_url
            env["CWM_BASE_URL"] = REMOTE_HOST
            try:
                _workflow_apps_proc = subprocess.Popen(
                    ["uv", "run", "--project", wf_dir, "cwm-workflow-apps"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    text=True,
                    bufsize=1,
                )
                threading.Thread(
                    target=_drain_workflow_apps_stderr,
                    args=(_workflow_apps_proc,),
                    daemon=True,
                    name="workflow-apps-stderr",
                ).start()
                log(f"Started workflow-apps subprocess (CWM_MCP_URL={_bridge_mcp_url})")
            except Exception as e:
                log(f"⚠ Could not start workflow-apps: {e}")
                _workflow_apps_proc = None
        else:
            log(f"⚠ --with-workflow-apps: dir not found or no pyproject.toml: {wf_dir}")
            _workflow_apps_proc = None

    server = ThreadedHTTPServer((args.host, args.port), MCPBridgeHandler)

    def handle_shutdown(signum, frame):
        log("Shutting down...")
        if _workflow_apps_proc is not None:
            try:
                _workflow_apps_proc.terminate()
                _workflow_apps_proc.wait(timeout=3)
            except Exception:
                pass
        server.shutdown()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    log(f"MCP HTTP Bridge started on http://{args.host}:{args.port}")
    log(f"Forwarding to: {REMOTE_MCP_URL}")
    log(f"Configure MCP clients to use: http://localhost:{args.port}/crosswork/cwm/v2/mcp")
    if _workflow_apps_proc is not None:
        log("Workflow-apps: merged (get_mop_activity_job_status, mop_activity_workflow_form, run_mop_workflow_and_show_status); requests go through MCP")
    log("Token auto-refresh: enabled (on 401 or expiry)")
    log("Press Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if _workflow_apps_proc is not None:
            try:
                _workflow_apps_proc.terminate()
            except Exception:
                pass
        log("Bridge stopped")


if __name__ == "__main__":
    main()
