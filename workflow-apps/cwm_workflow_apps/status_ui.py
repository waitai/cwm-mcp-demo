"""
Standalone web UI to view CFS check job status using the same Prefab layout as the MCP tool.
Run: uv run python -m cwm_workflow_apps.status_ui
Then open http://127.0.0.1:8765/?job_id=...&run_id=...
"""
from __future__ import annotations

import base64
import json
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from prefab_ui.components import (
    Column,
    Row,
    Heading,
    Text,
    Muted,
    Badge,
    Card,
    CardContent,
    Code,
    Separator,
)
from prefab_ui.app import PrefabApp

from cwm_workflow_apps.cwm_client import (
    get_job_run,
    get_job_events,
    extract_workflow_output,
    extract_output_from_run,
    decode_output_payload,
    post_job,
    get_mop_activity_workflows,
)

# Default port for the status viewer
DEFAULT_PORT = 8765


def _decode_memo_data(b64: str):
    try:
        return json.loads(base64.b64decode(b64).decode())
    except Exception:
        return None


def _ensure_output_decoded(payload: dict | None) -> dict | None:
    """Decode raw payloads[0].data (base64) in-place so browser always shows readable JSON."""
    if not payload or not isinstance(payload, dict):
        return payload
    payloads = payload.get("payloads") or payload.get("Payloads")
    if not isinstance(payloads, list) or not payloads:
        return payload
    first = payloads[0] if isinstance(payloads[0], dict) else None
    if not first:
        return payload
    data = first.get("data") or first.get("Data")
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
    return payload


def _stash_cli_output_string(payload: dict) -> str | None:
    """Extract stash array from Data.stash (or payload.stash) and return as one string with newlines normalized for display."""
    if not payload or not isinstance(payload, dict):
        return None
    data = payload.get("Data") or payload.get("data") or {}
    if not isinstance(data, dict):
        return None
    stash = data.get("stash") or payload.get("stash")
    if not isinstance(stash, list) or not stash:
        return None
    parts = []
    for item in stash:
        if isinstance(item, str) and item.strip():
            parts.append(item.replace("\r\n", "\n").replace("\r", "\n").strip())
        elif isinstance(item, dict) and (item.get("data") or item.get("text")):
            s = item.get("data") or item.get("text") or ""
            if isinstance(s, str) and s.strip():
                parts.append(s.replace("\r\n", "\n").replace("\r", "\n").strip())
    return "\n\n".join(parts) if parts else None


# Generic workflow type names from CWM that don't identify the actual workflow (e.g. "DSL" for DSL-based workflows)
_WORKFLOW_TYPE_GENERIC = frozenset({"dsl", "temporal", "workflow", "unknown"})


def _humanize_workflow_name(name: str) -> str:
    """Turn workflow identifier into a short display label (e.g. cisco-disk-space -> Cisco disk space)."""
    if not name or not name.strip():
        return name
    s = name.strip().replace("-", " ").replace("_", " ")
    return s.title() if s else name


def _workflow_title_from_run(run: dict) -> str:
    """Derive a display title from run: prefer job name, else execution.workflowId, else workflowExecutionInfo.type.name."""
    info = run.get("workflowExecutionInfo") or run.get("workflow_execution_info") or {}
    # 1) Job name we set when starting is e.g. "cisco-disk-space-NCS540X-7" -> workflow is prefix before last "-"
    job_name = (
        run.get("jobName") or run.get("job_name")
        or info.get("jobName") or info.get("job_name")
    )
    if not job_name and isinstance(run.get("job"), dict):
        job_name = run["job"].get("jobName") or run["job"].get("name") or run["job"].get("job_name")
    if not job_name and isinstance(run.get("jobRun"), dict):
        job_name = run["jobRun"].get("jobName") or run["jobRun"].get("name") or run["jobRun"].get("job_name")
    if isinstance(job_name, str) and job_name.strip():
        parts = job_name.strip().split("-")
        if len(parts) >= 2:
            workflow_part = "-".join(parts[:-1])
            if workflow_part:
                return f"{_humanize_workflow_name(workflow_part)} job status"
        return f"{_humanize_workflow_name(job_name)} job status"
    # 2) execution.workflowId sometimes holds a readable workflow id (if not a UUID)
    execution = info.get("execution") or info.get("Execution") or {}
    if isinstance(execution, dict):
        wf_id = (execution.get("workflowId") or execution.get("workflow_id") or "").strip()
        if wf_id and not _looks_like_uuid(wf_id):
            return f"{_humanize_workflow_name(wf_id)} job status"
    # 3) Fall back to workflow type name from CWM (e.g. type.name) unless it's generic
    t = info.get("type")
    type_name = None
    if isinstance(t, dict) and t.get("name"):
        type_name = (t.get("name") or "").strip()
    elif isinstance(t, str) and t.strip():
        type_name = t.strip()
    if type_name and type_name.lower() not in _WORKFLOW_TYPE_GENERIC:
        return f"{_humanize_workflow_name(type_name)} job status"
    return "Workflow job status"


def _looks_like_uuid(s: str) -> bool:
    """True if s looks like a UUID (e.g. 019cd4df-15be-7e94-...)."""
    if len(s) < 30:
        return False
    return bool(re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", s))


def build_status_view(job_id: str, run_id: str) -> str:
    """Fetch job run and return self-contained HTML with the Prefab status UI."""
    try:
        run = get_job_run(job_id, run_id)
    except Exception as e:
        with Column(gap=4, css_class="p-6") as view:
            Heading("Workflow job status")
            Badge("Error", variant="destructive")
            Text(str(e))
        return PrefabApp(view=view).html()

    title = _workflow_title_from_run(run)
    info = run.get("workflowExecutionInfo", {})
    status = info.get("status", "UNKNOWN")
    start = info.get("startTime", "")
    close = info.get("closeTime", "")
    duration = info.get("executionDuration", "")
    memo = info.get("memo", {}).get("fields", {})
    input_b64 = memo.get("inputData", {}).get("data")
    input_data = _decode_memo_data(input_b64) if input_b64 else None

    status_label = status.replace("WORKFLOW_EXECUTION_STATUS_", "").lower()
    if status == "WORKFLOW_EXECUTION_STATUS_COMPLETED":
        variant = "success"
        result_message = "Workflow completed successfully."
    elif status == "WORKFLOW_EXECUTION_STATUS_FAILED":
        variant = "destructive"
        result_message = "Workflow failed."
    elif status == "WORKFLOW_EXECUTION_STATUS_RUNNING":
        variant = "default"
        result_message = "Job is still running. This page will refresh every 5 seconds until the job completes."
    else:
        variant = "default"
        result_message = status

    output_payload = None
    if status in ("WORKFLOW_EXECUTION_STATUS_COMPLETED", "WORKFLOW_EXECUTION_STATUS_FAILED"):
        try:
            events = get_job_events(job_id, run_id)
            output_payload = extract_workflow_output(events)
            if output_payload is None:
                output_payload = extract_output_from_run(run)
        except Exception:
            output_payload = None
        if output_payload is None:
            output_payload = (
                {"Data": {"message": "Workflow completed successfully. (Output payload not available from CWM API.)", "status": "success"}}
                if status == "WORKFLOW_EXECUTION_STATUS_COMPLETED"
                else {"Data": {"message": "Workflow failed. (Output payload not available from CWM API.)", "status": "failed"}}
            )
        # Decode raw payloads[0].data (base64) so Output shows readable JSON
        output_payload = decode_output_payload(output_payload) or output_payload

    with Column(gap=4, css_class="p-6") as view:
        Heading(title)
        with Row(gap=2, align="center"):
            Badge(status_label, variant=variant)
            if duration:
                Muted(f"Duration: {duration}")
        Separator()
        with Card():
            with CardContent():
                Text("Job ID", css_class="font-medium")
                Muted(job_id)
                Text("Run ID", css_class="font-medium")
                Muted(run_id)
        if start:
            Muted(f"Started: {start}")
        if close:
            Muted(f"Closed: {close}")
        if input_data:
            with Card():
                with CardContent():
                    Text("Input", css_class="font-medium")
                    Code(json.dumps(input_data, indent=2), language="json")
        Separator()
        Text("Result", css_class="font-medium")
        Text(result_message)
        if output_payload:
            # Decode raw payloads[0].data (base64) so browser always shows readable JSON (local decode so it works even if server wasn't restarted)
            display_output = _ensure_output_decoded(output_payload) or decode_output_payload(output_payload) or output_payload
            with Card():
                with CardContent():
                    Text("Output", css_class="font-medium")
                    Code(json.dumps(display_output, indent=2), language="json")
            stash_text = _stash_cli_output_string(display_output)
            if stash_text:
                with Card():
                    with CardContent():
                        Text("CLI output", css_class="font-medium")
                        Code(stash_text, language="text")

    html = PrefabApp(view=view).html()
    # Auto-refresh every 5 seconds while job is still running so user sees final result
    if status == "WORKFLOW_EXECUTION_STATUS_RUNNING":
        if "</head>" in html:
            html = html.replace(
                "</head>",
                '<meta http-equiv="refresh" content="5"><meta name="description" content="Refreshing every 5s until job completes."></head>',
            )
    return html


def build_run_form_html() -> str:
    """Form to run the CFS check workflow (device name, resource), then redirect to status."""
    return """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Run CFS check workflow</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 32rem; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.25rem; }
  a { color: #0066cc; }
  label { display: block; margin-top: 0.75rem; font-weight: 500; }
  input { width: 100%; padding: 0.5rem; margin-top: 0.25rem; box-sizing: border-box; }
  button { margin-top: 1rem; padding: 0.5rem 1rem; cursor: pointer; }
</style>
</head>
<body>
  <h1>Run CFS check workflow</h1>
  <p>Checks the sanity of the configuration file system on a device. Submit to start the job and see status.</p>
  <form method="post" action="/run">
    <label>Device name</label>
    <input name="device_name" placeholder="e.g. NCS540X-7" required>
    <label>Resource</label>
    <input name="resource" placeholder="e.g. cwm.sol.system.nso" required>
    <label>Job name (optional)</label>
    <input name="job_name" placeholder="e.g. cfs-check-NCS540X-7">
    <button type="submit">Run workflow</button>
  </form>
  <p style="margin-top: 1.5rem;"><a href="/run-mop">Run any mopActivity workflow</a> | <a href="/">← View status</a></p>
</body>
</html>"""


def build_run_mop_form_html() -> str:
    """Form to run any mopActivity workflow: workflow dropdown + device, product series, resource."""
    try:
        workflows = get_mop_activity_workflows()
    except Exception:
        workflows = []
    options = "".join(
        f'<option value="{w.get("name", "")}|{w.get("version", "")}">{w.get("name", "")} ({w.get("version", "")})</option>'
        for w in workflows if isinstance(w, dict) and w.get("name") and w.get("version")
    )
    if not options:
        options = "<option value=''>-- No workflows (set CWM_MCP_URL?) --</option>"
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Run mopActivity workflow</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 32rem; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.25rem; }}
  a {{ color: #0066cc; }}
  label {{ display: block; margin-top: 0.75rem; font-weight: 500; }}
  input, select {{ width: 100%; padding: 0.5rem; margin-top: 0.25rem; box-sizing: border-box; }}
  button {{ margin-top: 1rem; padding: 0.5rem 1rem; cursor: pointer; }}
</style>
</head>
<body>
  <h1>Run mopActivity workflow</h1>
  <p>Select a workflow and enter device name, product series, and resource. Then submit to start the job.</p>
  <form method="post" action="/run-mop">
    <label>Workflow</label>
    <select name="workflow" required>
      <option value="">-- Choose workflow --</option>
      {options}
    </select>
    <label>Device name</label>
    <input name="device_name" value="NCS540X-7" required>
    <label>Product series</label>
    <input name="product_series" value="Cisco Network Convergence System 540 Series Routers" required>
    <label>Resource</label>
    <input name="resource" value="cwm.sol.system.nso" required>
    <label>Job name (optional)</label>
    <input name="job_name" value="" placeholder="e.g. check-nodes-NCS540X-7">
    <button type="submit">Run workflow</button>
  </form>
  <p style="margin-top: 1.5rem;"><a href="/run">Run CFS check only</a> | <a href="/">← View status</a></p>
</body>
</html>"""


def build_index_html() -> str:
    """Simple form to enter job_id and run_id, then redirect to status page."""
    return """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>CFS check status</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 32rem; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.25rem; }
  a { color: #0066cc; }
  label { display: block; margin-top: 0.75rem; font-weight: 500; }
  input { width: 100%; padding: 0.5rem; margin-top: 0.25rem; box-sizing: border-box; }
  button { margin-top: 1rem; padding: 0.5rem 1rem; cursor: pointer; }
</style>
</head>
<body>
  <h1>Workflow job status</h1>
  <p><a href="/run">Run CFS check</a> | <a href="/run-mop">Run mopActivity workflow</a> — or enter job/run IDs below.</p>
  <form method="get" action="/">
    <label>Job ID</label>
    <input name="job_id" placeholder="e.g. 171a9bcd-7406-4b32-..." required>
    <label>Run ID</label>
    <input name="run_id" placeholder="e.g. 019cd4df-15be-7e94-..." required>
    <button type="submit">View status</button>
  </form>
</body>
</html>"""


def _parse_post_form(raw: bytes) -> dict[str, str]:
    """Parse application/x-www-form-urlencoded body."""
    qs = parse_qs(raw.decode("utf-8", errors="replace"))
    return {k: (v[0] if v else "").strip() for k, v in qs.items()}


def _normalize_path(path: str) -> str:
    """Return path without query/fragment, normalized (no trailing slash, or '/' for root)."""
    p = path.split("?")[0].split("#")[0]
    return p.rstrip("/") or "/"


class StatusUIHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = _normalize_path(parsed.path)
        if path == "/run":
            html = build_run_form_html()
        elif path in ("/run-mop", "/run_mop"):
            html = build_run_mop_form_html()
        elif path == "/":
            qs = parse_qs(parsed.query)
            job_id = (qs.get("job_id") or [""])[0].strip()
            run_id = (qs.get("run_id") or [""])[0].strip()
            if job_id and run_id:
                html = build_status_view(job_id, run_id)
            else:
                html = build_index_html()
        else:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self):
        parsed = urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        form = _parse_post_form(body)

        path = _normalize_path(parsed.path)
        if path == "/run":
            device_name = form.get("device_name", "").strip()
            resource = form.get("resource", "").strip()
            job_name = form.get("job_name", "").strip()
            if not device_name or not resource:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Missing device_name or resource. <a href='/run'>Back</a>")
                return
            try:
                result = post_job(
                    workflow_name="cfs-check-cwm-sol",
                    workflow_version="2.1.0",
                    data={"app-data": {"device": {"name": device_name}, "resource": resource}},
                    job_name=job_name or f"cfs-check-{device_name}",
                    tags=["cfs-check", "mopActivity"],
                )
                job_id = result.get("jobId", "")
                run_id = result.get("runId", "")
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"Error starting job: {e!s}. <a href='/run'>Back</a>".encode("utf-8"))
                return
            self.send_response(302)
            self.send_header("Location", f"/?job_id={job_id}&run_id={run_id}")
            self.end_headers()
            return

        if path in ("/run-mop", "/run_mop"):
            workflow = form.get("workflow", "").strip()
            device_name = form.get("device_name", "").strip()
            product_series = form.get("product_series", "").strip()
            resource = form.get("resource", "").strip()
            job_name = form.get("job_name", "").strip()
            if not workflow or "|" not in workflow or not device_name or not product_series or not resource:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Missing workflow, device name, product series, or resource. <a href='/run-mop'>Back</a>")
                return
            wf_name, _, wf_version = workflow.partition("|")
            wf_name, wf_version = wf_name.strip(), wf_version.strip()
            if not wf_name or not wf_version:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Invalid workflow selection. <a href='/run-mop'>Back</a>")
                return
            try:
                result = post_job(
                    workflow_name=wf_name,
                    workflow_version=wf_version,
                    data={
                        "app-data": {
                            "data": {},
                            "device": {"name": device_name, "productSeries": product_series},
                            "resource": resource,
                        }
                    },
                    job_name=job_name or f"{wf_name}-{device_name}",
                    tags=["mopActivity"],
                )
                job_id = result.get("jobId", "")
                run_id = result.get("runId", "")
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"Error starting job: {e!s}. <a href='/run-mop'>Back</a>".encode("utf-8"))
                return
            self.send_response(302)
            self.send_header("Location", f"/?job_id={job_id}&run_id={run_id}")
            self.end_headers()
            return

        self.send_error(404)

    def log_message(self, format, *args):
        print(f"[Status UI] {args[0]}", flush=True)


def main(port: int = DEFAULT_PORT) -> None:
    server = HTTPServer(("127.0.0.1", port), StatusUIHandler)
    print(f"Status UI: http://127.0.0.1:{port}/")
    print("  Run CFS check: http://127.0.0.1:{port}/run")
    print("  Run mopActivity workflow: http://127.0.0.1:{port}/run-mop")
    print("  View status: use form on index or ?job_id=...&run_id=...")
    server.serve_forever()


def _parse_args():
    import argparse
    parser = argparse.ArgumentParser(description="CFS check job status UI (Prefab)")
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT})",
    )
    return parser.parse_args()


def cli() -> None:
    """Entry point for the cwm-workflow-status-ui console script."""
    args = _parse_args()
    main(port=args.port)


if __name__ == "__main__":
    cli()
