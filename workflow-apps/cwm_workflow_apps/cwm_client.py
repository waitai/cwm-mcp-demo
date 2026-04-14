"""
Minimal CWM client for job submit and status.
- If CWM_MCP_URL is set: calls CWM via MCP (e.g. through the bridge).
- Else: uses REST with CWM_BASE_URL and CWM_BEARER_TOKEN or CAS credentials.
"""
import os
import re
import base64
import json
import threading
import time
import uuid
from typing import Any, Callable, Optional

import httpx

# Crosswork base URL (required for CAS login when CWM_BEARER_TOKEN is not set)
DEFAULT_BASE = os.environ.get("CWM_BASE_URL", "").strip()
CAS_PATH = "/crosswork/sso"
JOB_PATH = "/crosswork/cwm/v2/job"


def _ssl_verify() -> bool:
    return os.environ.get("CWM_SSL_VERIFY", "false").strip().lower() in ("1", "true", "yes", "on")

_token_lock = threading.Lock()
_bearer_token: Optional[str] = None
_token_expiry: float = 0


def _get_jwt_via_cas() -> bool:
    global _bearer_token, _token_expiry
    base = DEFAULT_BASE.rstrip("/")
    if not base:
        return False
    username = os.environ.get("CWM_CAS_USERNAME", "").strip()
    password = os.environ.get("CWM_CAS_PASSWORD", "").strip()
    if not username or not password:
        return False
    cas_url = f"{base}{CAS_PATH}"
    ext_port = os.environ.get("CWM_SSO_EXTERNAL_PORT", "").strip()
    try:
        r = httpx.post(
            f"{cas_url}/v1/tickets",
            data={"username": username, "password": password},
            verify=_ssl_verify(),
            timeout=30,
        )
        if r.status_code != 201:
            return False
        tgt_url = r.headers.get("Location") or r.text.strip()
        if ext_port:
            tgt_url = re.sub(r":\d+(/crosswork/sso/)", rf":{ext_port}\1", tgt_url)
        r2 = httpx.post(
            tgt_url,
            data={"service": f"{base}/app-dashboard"},
            verify=_ssl_verify(),
            timeout=30,
        )
        if r2.status_code != 200:
            return False
        token = r2.text.strip()
        if not token or len(token) < 50:
            return False
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            _token_expiry = float(payload.get("exp", 0))
        except Exception:
            _token_expiry = 0
        with _token_lock:
            _bearer_token = token
        return True
    except Exception:
        return False


def get_token() -> Optional[str]:
    token = os.environ.get("CWM_BEARER_TOKEN")
    if token:
        return token
    with _token_lock:
        t, exp = _bearer_token, _token_expiry
    if t and exp > 0 and time.time() < exp - 300:
        return t
    if _get_jwt_via_cas():
        with _token_lock:
            return _bearer_token
    return None


def _headers() -> dict:
    t = get_token()
    h = {"Content-Type": "application/json"}
    if t:
        h["Authorization"] = f"Bearer {t}"
    return h


def _call_cwm_via_mcp(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Call CWM via MCP (e.g. bridge URL). Returns the tool result content."""
    url = os.environ.get("CWM_MCP_URL", "").rstrip("/")
    if not url:
        return None
    req = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    r = httpx.post(url, json=req, timeout=60, verify=_ssl_verify())
    r.raise_for_status()
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"MCP response not JSON (status={r.status_code}, body={r.text[:200]!r})") from e
    if "error" in data:
        raise RuntimeError(data["error"].get("message", str(data["error"])))
    return data.get("result")


def post_job(
    workflow_name: str,
    workflow_version: str,
    data: dict,
    job_name: str = "",
    tags: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Start a job. Returns {jobId, runId} or raises."""
    mcp_url = os.environ.get("CWM_MCP_URL", "").strip()
    if mcp_url:
        result = _call_cwm_via_mcp(
            "post_job",
            {
                "workflowName": workflow_name,
                "workflowVersion": workflow_version,
                "data": data,
                **({"jobName": job_name} if job_name else {}),
                **({"tags": tags} if tags else {}),
            },
        )
        if isinstance(result, dict) and "jobId" in result:
            return result
        # CWM MCP can return result.result with jobId/runId
        if isinstance(result, dict) and "result" in result:
            inner = result.get("result")
            if isinstance(inner, dict) and "jobId" in inner:
                return inner
        def _parse_content_text(text: str) -> dict[str, Any]:
            text = (text or "").strip()
            if not text:
                return None
            # CWM may return error as "HTTP 400: {\"message\":\"...\",\"detail\":\"...\"}"
            if "HTTP " in text and ("message" in text or "detail" in text):
                idx = text.find("{")
                if idx >= 0:
                    try:
                        err = json.loads(text[idx:])
                        msg = err.get("message", "").strip()
                        detail = err.get("detail", "").strip()
                        if msg or detail:
                            raise RuntimeError(
                                f"CWM returned error: {msg}" + (f" — {detail}" if detail else "")
                            )
                    except json.JSONDecodeError:
                        pass
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"MCP post_job returned content that is not valid JSON (response snippet: {text[:200]!r})"
                ) from e

        if isinstance(result, dict) and "content" in result:
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                    parsed = _parse_content_text(item["text"])
                    if parsed is not None:
                        return parsed
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                    parsed = _parse_content_text(item["text"])
                    if parsed is not None:
                        return parsed
        raise RuntimeError(f"Unexpected MCP post_job result: {result}")

    base = DEFAULT_BASE.rstrip("/")
    url = f"{base}{JOB_PATH}"
    body: dict[str, Any] = {
        "workflowName": workflow_name,
        "workflowVersion": workflow_version,
        "data": data,
    }
    if job_name:
        body["jobName"] = job_name
    if tags:
        body["tags"] = tags
    r = httpx.post(url, json=body, headers=_headers(), verify=_ssl_verify(), timeout=60)
    r.raise_for_status()
    return r.json()


def get_job_run(job_id: str, run_id: str) -> dict[str, Any]:
    """Get job run details (status, duration, memo, etc.)."""
    mcp_url = os.environ.get("CWM_MCP_URL", "").strip()
    if mcp_url:
        result = _call_cwm_via_mcp("get_job_runs", {"jobId": job_id, "runId": run_id})
        # CWM MCP can return result.result with the run payload
        if isinstance(result, dict) and "result" in result:
            inner = result.get("result")
            if isinstance(inner, dict) and "workflowExecutionInfo" in inner:
                return inner
        if isinstance(result, dict) and "workflowExecutionInfo" in result:
            return result
        if isinstance(result, dict) and "content" in result:
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                    text = item["text"]
                    # Response may be "Operation GET ... completed successfully\nResponse:\n{json}"
                    if "Response:" in text:
                        text = text.split("Response:", 1)[-1].strip()
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        pass
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                    return json.loads(item["text"])
        raise RuntimeError(f"Unexpected MCP get_job_runs result: {result}")

    base = DEFAULT_BASE.rstrip("/")
    url = f"{base}{JOB_PATH}/{job_id}/runs/{run_id}"
    r = httpx.get(url, headers=_headers(), verify=_ssl_verify(), timeout=30)
    r.raise_for_status()
    return r.json()


def get_job_events(job_id: str, run_id: str) -> list[dict[str, Any]]:
    """Get job run history events (for workflow output payload). Returns list of event dicts."""
    mcp_url = os.environ.get("CWM_MCP_URL", "").strip()
    if mcp_url:
        result = _call_cwm_via_mcp("get_job_events", {"jobId": job_id, "runId": run_id})
        if result is None:
            return []
        # Unwrap result.result or result.content text (same pattern as get_job_run)
        if isinstance(result, dict) and "result" in result:
            inner = result.get("result")
            if isinstance(inner, dict) and "events" in inner:
                return inner.get("events") or []
            if isinstance(inner, list):
                return inner
        if isinstance(result, dict) and "events" in result:
            return result.get("events") or []
        if isinstance(result, dict) and "content" in result:
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                    text = item["text"]
                    if "Response:" in text:
                        text = text.split("Response:", 1)[-1].strip()
                    try:
                        data = json.loads(text)
                        if isinstance(data, dict):
                            return data.get("events", data.get("history", [])) or []
                        if isinstance(data, list):
                            return data
                    except json.JSONDecodeError:
                        pass
        if isinstance(result, list):
            return result
        # CWM may return content array with single text block containing JSON string
        if isinstance(result, dict):
            for key in ("content", "history", "eventHistory"):
                if key in result and isinstance(result[key], list):
                    return result[key]
        return []

    base = DEFAULT_BASE.rstrip("/")
    url = f"{base}{JOB_PATH}/{job_id}/runs/{run_id}/events"
    r = httpx.get(url, headers=_headers(), verify=_ssl_verify(), timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("events", data) if isinstance(data, dict) else data


def get_mop_activity_workflows() -> list[dict[str, Any]]:
    """Fetch workflows from CWM and return those with wfTags containing mopActivity (client-side filter).
    Returns list of dicts with name, version (and optionally workflowId)."""
    mcp_url = os.environ.get("CWM_MCP_URL", "").strip()
    if not mcp_url:
        return []
    result = _call_cwm_via_mcp("get_workflow", {"tags": ["mopActivity"]})
    if result is None:
        return []
    workflows: list[dict[str, Any]] = []
    if isinstance(result, list):
        workflows = result
    elif isinstance(result, dict) and "result" in result:
        inner = result.get("result")
        if isinstance(inner, list):
            workflows = inner
        elif isinstance(inner, dict) and "workflows" in inner:
            workflows = inner.get("workflows") or []
    elif isinstance(result, dict) and "content" in result:
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                text = item["text"]
                if "Response:" in text:
                    text = text.split("Response:", 1)[-1].strip()
                try:
                    data = json.loads(text)
                    workflows = data if isinstance(data, list) else (data.get("workflows") if isinstance(data, dict) else [])
                except json.JSONDecodeError:
                    pass
                break
    tag = "mopActivity"
    filtered = [w for w in workflows if isinstance(w, dict) and tag in (w.get("wfTags") or [])]
    filtered.sort(key=lambda w: (w.get("name", ""), w.get("version", "")))
    return filtered


def _parse_mcp_list_result(result: Any, list_key: str = "resources") -> list[str]:
    """Extract a list of strings from CWM MCP tool result (various shapes)."""
    if result is None:
        return []
    if isinstance(result, list):
        return [str(x) for x in result if x is not None and str(x).strip()]
    if isinstance(result, dict):
        if "content" in result:
            for item in (result.get("content") or []):
                if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                    text = (item.get("text") or "").strip()
                    if "Response:" in text:
                        text = text.split("Response:", 1)[-1].strip()
                    try:
                        data = json.loads(text)
                        if isinstance(data, list):
                            return [str(x) for x in data if x is not None and str(x).strip()]
                        if isinstance(data, dict):
                            lst = data.get(list_key) or data.get("result") or data.get("items") or []
                            return [str(x) for x in (lst if isinstance(lst, list) else []) if x is not None and str(x).strip()]
                    except (json.JSONDecodeError, TypeError):
                        pass
                    break
        inner = result.get("result", result)
        if isinstance(inner, list):
            return [str(x) for x in inner if x is not None and str(x).strip()]
        if isinstance(inner, dict):
            lst = inner.get(list_key) or inner.get("items") or inner.get("devices") or []
            return [str(x) for x in (lst if isinstance(lst, list) else []) if x is not None and str(x).strip()]
    return []


def get_cwm_resources() -> list[str]:
    """Fetch available NSO resource IDs from CWM via MCP (remote bridge). Returns list of resource ID strings."""
    mcp_url = os.environ.get("CWM_MCP_URL", "").strip()
    if not mcp_url:
        return []
    for tool in ("get_resources", "list_resources"):
        try:
            result = _call_cwm_via_mcp(tool, {})
            out = _parse_mcp_list_result(result, "resources")
            if out:
                return sorted(out)
        except Exception:
            continue
    return []


def get_nso_devices() -> list[str]:
    """Fetch device names from NSO. Uses CWM MCP tool if available (e.g. workflow that queries NSO), else empty."""
    mcp_url = os.environ.get("CWM_MCP_URL", "").strip()
    if not mcp_url:
        return []
    for tool in ("get_devices", "list_devices", "get_nso_devices"):
        try:
            result = _call_cwm_via_mcp(tool, {})
            out = _parse_mcp_list_result(result, "devices")
            if out:
                return sorted(out)
        except Exception:
            continue
    return []


INVENTORY_QUERY_PATH = "/crosswork/cwms/inventory/v1/devices/query"
_INVENTORY_QUERY_PAYLOAD = {"node": {"filterData": {"PageNum": 0, "PageSize": 500}}}


def _parse_inventory_from_mcp_result(result: Any) -> list[dict[str, Any]]:
    """Parse MCP result (from bridge get_inventory_devices) into list of {host_name, product_series, uuid}."""
    out: list[dict[str, Any]] = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and item.get("host_name"):
                out.append({
                    "host_name": (item.get("host_name") or "").strip(),
                    "product_series": (item.get("product_series") or "").strip() or "Unknown",
                    "uuid": (item.get("uuid") or "").strip(),
                })
        return out
    if isinstance(result, dict) and "content" in result:
        for part in result.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text" and "text" in part:
                try:
                    data = json.loads(part["text"])
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict) and item.get("host_name"):
                                out.append({
                                    "host_name": (item.get("host_name") or "").strip(),
                                    "product_series": (item.get("product_series") or "").strip() or "Unknown",
                                    "uuid": (item.get("uuid") or "").strip(),
                                })
                except (TypeError, json.JSONDecodeError):
                    pass
                break
    return out


def get_inventory_devices() -> list[dict[str, Any]]:
    """Fetch devices and inventory: first via bridge MCP (get_inventory_devices), else direct CWM Inventory API.
    Returns list of dicts with host_name, product_series (from product_info), and uuid."""
    # Prefer bridge (has JWT and can reach CWM inventory)
    mcp_url = os.environ.get("CWM_MCP_URL", "").strip()
    if mcp_url:
        try:
            result = _call_cwm_via_mcp("get_inventory_devices", {})
            devices = _parse_inventory_from_mcp_result(result)
            if devices:
                return devices
        except Exception:
            pass
    # Fallback: direct HTTP to CWM inventory (requires CWM_BASE_URL and CAS token)
    base = DEFAULT_BASE.rstrip("/")
    url = f"{base}{INVENTORY_QUERY_PATH}"
    token = get_token()
    if not token:
        return []
    try:
        r = httpx.post(
            url,
            json=_INVENTORY_QUERY_PAYLOAD,
            headers={"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {token}"},
            verify=_ssl_verify(),
            timeout=30,
        )
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    items = data.get("data") if isinstance(data, dict) else []
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
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


# Product series options from cisco-disk-space-cwm-sol workflow (check-device-family state under check-iosxr).
# When CWM exposes workflow state/options, this can be replaced by a runtime fetch.
PRODUCT_SERIES_OPTIONS = [
    "Cisco Network Convergence System 540 Series Routers",
    "Cisco IOS XR 9000 Series",
    "Cisco ASR 9000 Series",
    "Cisco NCS 5500 Series",
    "Cisco NCS 560 Series",
    "Cisco IOS XR 8000 Series",
]


def get_product_series() -> list[str]:
    """Return product series options (e.g. from cisco-disk-space check-iosxr check-device-family)."""
    return list(PRODUCT_SERIES_OPTIONS)


def _parse_result_payload(raw: Any) -> dict[str, Any] | None:
    """Parse a result field that may be dict, JSON string, or base64."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            try:
                return json.loads(base64.b64decode(raw).decode())
            except Exception:
                pass
    return None


def extract_workflow_output(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """From job events, extract the workflow result payload (e.g. Data with message/status)."""
    for event in reversed(events or []):
        # CWM uses workflowExecutionCompletedEventAttributes; Temporal docs use EventDetails
        details = (
            event.get("workflowExecutionCompletedEventAttributes")
            or event.get("workflowExecutionCompletedEventDetails")
            or event.get("workflowExecutionFailedEventAttributes")
            or event.get("workflowExecutionFailedEventDetails")
        )
        if details and isinstance(details, dict):
            result = details.get("result")
            if result is not None:
                parsed = _parse_result_payload(result)
                # If parsed is the raw payloads wrapper, decode it; don't return wrapper as-is
                if parsed and isinstance(parsed, dict) and "payloads" in parsed:
                    decoded = decode_output_payload(parsed)
                    if decoded is not None and decoded is not parsed:
                        return decoded
                if parsed:
                    return parsed
                # CWM/Temporal: result.payloads[0].data (may be dict, base64 string, or list of bytes)
                if isinstance(result, dict) and "payloads" in result:
                    payloads = result.get("payloads") or []
                    if isinstance(payloads, list) and payloads:
                        first = payloads[0] if isinstance(payloads[0], dict) else None
                        if first and "data" in first:
                            raw_data = first.get("data")
                            if isinstance(raw_data, dict):
                                return raw_data
                            if isinstance(raw_data, str):
                                try:
                                    return json.loads(base64.b64decode(raw_data).decode())
                                except Exception:
                                    pass
                            if isinstance(raw_data, list):
                                try:
                                    return json.loads(bytes(raw_data).decode())
                                except Exception:
                                    pass
        # Result at event top level
        if event.get("eventType") in ("WorkflowExecutionCompleted", "WORKFLOW_EXECUTION_COMPLETED", 2):
            parsed = _parse_result_payload(event.get("result"))
            if parsed:
                return parsed
        # Generic: any key containing 'result' or 'payload' or 'output'
        for key, val in event.items():
            if key and isinstance(val, (dict, str)) and (
                "result" in key.lower() or "payload" in key.lower() or "output" in key.lower()
            ):
                parsed = _parse_result_payload(val)
                if parsed and isinstance(parsed, dict):
                    return parsed
    return None


def _decode_payload_data(data: Any) -> dict[str, Any] | None:
    """Decode CWM/Temporal Payload 'data' (base64 string or list of int bytes) to JSON dict."""
    if data is None:
        return None
    if isinstance(data, str):
        try:
            return json.loads(base64.b64decode(data).decode())
        except Exception:
            pass
    if isinstance(data, list):
        try:
            return json.loads(bytes(data).decode())
        except Exception:
            pass
    return None


def decode_output_payload(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """If output is the raw payloads wrapper with base64 data, decode and return the inner payload for display."""
    if not raw or not isinstance(raw, dict):
        return raw
    payloads = raw.get("payloads")
    if not isinstance(payloads, list) or not payloads:
        return raw
    first = payloads[0] if isinstance(payloads[0], dict) else None
    if not first or "data" not in first:
        return raw
    decoded = _decode_payload_data(first.get("data"))
    return decoded if decoded is not None else raw


def extract_output_from_run(run: dict[str, Any]) -> dict[str, Any] | None:
    """Extract workflow output from run payload (memo/outputData, memo/result, etc.) when events lack it."""
    if not run:
        return None
    info = run.get("workflowExecutionInfo") or run.get("workflow_execution_info") or run
    memo = info.get("memo") or {}
    fields = memo.get("fields") or memo.get("Fields") or {}
    # CWM/Temporal: memo.fields values can be Payload with "data" as base64 string or list of ints (bytes)
    for key in ("outputData", "output", "result", "resultData", "Result", "Output"):
        if key not in fields:
            continue
        entry = fields[key]
        if isinstance(entry, dict) and "data" in entry:
            decoded = _decode_payload_data(entry.get("data"))
            if decoded:
                return decoded
        if isinstance(entry, dict) and ("Data" in entry or "message" in entry):
            return entry
        if isinstance(entry, dict):
            return entry
    search_attrs = info.get("searchAttributes") or info.get("search_attributes") or {}
    indexed = search_attrs.get("indexed_fields") or search_attrs.get("indexedFields") or search_attrs
    if isinstance(indexed, dict):
        for key in ("output", "result", "Output", "Result", "data", "Data"):
            if key not in indexed:
                continue
            val = indexed[key]
            if isinstance(val, dict) and "data" in val:
                decoded = _decode_payload_data(val.get("data"))
                if decoded:
                    return decoded
            parsed = _parse_result_payload(val)
            if parsed:
                return parsed
    return None
