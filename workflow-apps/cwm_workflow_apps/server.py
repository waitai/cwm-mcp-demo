"""
CWM Workflow MCP Apps: input form, job progress, and output UI for cfs-check-cwm-sol.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Optional

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
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
    Input,
    Button,
    Form,
    Separator,
    Select,
    SelectOption,
)
from prefab_ui.actions import ShowToast
from prefab_ui.actions.mcp import CallTool, SendMessage
from prefab_ui.app import PrefabApp

from cwm_workflow_apps.cwm_client import (
    post_job,
    get_job_run,
    get_job_events,
    extract_workflow_output,
    extract_output_from_run,
    decode_output_payload,
    get_mop_activity_workflows,
    get_cwm_resources,
    get_nso_devices,
    get_product_series,
    get_inventory_devices,
)
from cwm_workflow_apps.status_ui import _stash_cli_output_string

mcp = FastMCP("CWM Workflow Apps")

# --- App: job status and output ---


def _decode_memo_data(b64: str) -> Any:
    try:
        return json.loads(base64.b64decode(b64).decode())
    except Exception:
        return None


def _build_status_view(
    job_id: str, run_id: str, run: dict[str, Any], title: str = "MOP Activity Workflow Job Status"
) -> Column:
    """Build the Prefab status view (Job ID, Run ID, status, input, result, output) from run data."""
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
        result_message = "Job is still running. Refresh to see updated status."
    else:
        variant = "default"
        result_message = status

    output_payload: dict[str, Any] | None = None
    if status in ("WORKFLOW_EXECUTION_STATUS_COMPLETED", "WORKFLOW_EXECUTION_STATUS_FAILED"):
        try:
            events = get_job_events(job_id, run_id)
            output_payload = extract_workflow_output(events)
            if output_payload is None:
                output_payload = extract_output_from_run(run)
        except Exception:
            output_payload = None
        # Always show an Output block for completed/failed runs; use placeholder if API didn't return payload
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
            with Card():
                with CardContent():
                    Text("Output", css_class="font-medium")
                    Code(json.dumps(output_payload, indent=2), language="json")
            stash_text = _stash_cli_output_string(output_payload)
            if stash_text:
                with Card():
                    with CardContent():
                        Text("CLI output", css_class="font-medium")
                        Code(stash_text, language="text")
        Separator()
        Text("Refresh status", css_class="font-medium")
        Muted("Sends a message to the chat so you can run the status tool with the Job and Run IDs above.")
        Button(
            "Ask Cursor to refresh status",
            on_click=SendMessage(
                "Please run get_mop_activity_job_status with job_id {{ job_id }} and run_id {{ run_id }}."
            ),
        )
    return view


@mcp.tool(
    name="get_mop_activity_job_status",
    description="Track progress and view final output of a mop activity (or any workflow) job. Pass jobId and runId from the status card, or open with no args to enter them.",
    app=True,
)
def get_mop_activity_job_status(
    job_id: str = "",
    run_id: str = "",
) -> ToolResult | PrefabApp:
    """Show job status and result; if ids missing, show a small form to enter them."""
    if not job_id.strip() or not run_id.strip():
        with Column(gap=4, css_class="p-6") as view:
            Heading("Track workflow job status")
            Muted("Enter the job and run IDs from your workflow run (e.g. mop activity or CFS check).")
            with Form(
                on_submit=CallTool(
                    "get_mop_activity_job_status",
                    result_key="status_result",
                    on_success=ShowToast("Status loaded", variant="success"),
                    on_error=ShowToast("{{ $error }}", variant="error"),
                )
            ):
                Input(name="job_id", label="Job ID", required=True, placeholder="e.g. 7d0c9390-5663-4bbd-a497-...")
                Input(name="run_id", label="Run ID", required=True, placeholder="e.g. 019cd4be-f697-725c-...")
                Button("Load status")
        return ToolResult(
            content="Open the form above and enter job ID and run ID to track the job.",
            structured_content=PrefabApp(view=view),
        )

    try:
        run = get_job_run(job_id, run_id)
    except Exception as e:
        with Column(gap=4, css_class="p-6") as view:
            Heading("Job status")
            Badge("Error", variant="destructive")
            Text(str(e))
        return ToolResult(
            content=f"Failed to fetch job: {e}",
            structured_content=PrefabApp(view=view),
        )

    view = _build_status_view(job_id, run_id, run, title="MOP Activity Workflow Job Status")
    info = run.get("workflowExecutionInfo", {})
    status = info.get("status", "UNKNOWN")
    status_label = status.replace("WORKFLOW_EXECUTION_STATUS_", "").lower()
    result_message = "Workflow completed successfully." if status == "WORKFLOW_EXECUTION_STATUS_COMPLETED" else (
        "Workflow failed." if status == "WORKFLOW_EXECUTION_STATUS_FAILED" else (
            "Job is still running. Refresh to see updated status." if status == "WORKFLOW_EXECUTION_STATUS_RUNNING" else status
        )
    )
    return ToolResult(
        content=f"Status: {status_label}. {result_message}",
        structured_content=PrefabApp(view=view, state={"job_id": job_id, "run_id": run_id}),
    )


# --- MopActivity: run any mopActivity workflow and show status ---


@mcp.tool(
    name="run_mop_workflow_and_show_status",
    description="Run a mopActivity workflow (by name|version), with device name, product series, and resource; then return the job status view. Accepts either (workflow, device, resource, job_name) where device is 'host_name|product_series', or (workflow, device_name, product_series, resource, job_name).",
    app=True,
)
def run_mop_workflow_and_show_status(
    workflow: str,
    device_name: str = "",
    product_series: str = "",
    resource: str = "",
    job_name: str = "",
    device: str = "",
) -> ToolResult:
    """Run the selected mopActivity workflow and return the job status view."""
    if not workflow or "|" not in workflow:
        return ToolResult(
            content="Invalid workflow: expected 'workflowName|workflowVersion'.",
            structured_content=PrefabApp(view=Column(Text("Invalid workflow selection."))),
        )
    workflow_name, _, workflow_version = workflow.partition("|")
    workflow_name = workflow_name.strip()
    workflow_version = workflow_version.strip()
    if not workflow_name or not workflow_version:
        return ToolResult(
            content="Invalid workflow: name and version required.",
            structured_content=PrefabApp(view=Column(Text("Invalid workflow selection."))),
        )
    # Support form message: device = "host_name|product_series"
    if device and "|" in device:
        device_name, _, product_series = device.partition("|")
        device_name = device_name.strip()
        product_series = product_series.strip()
    if not device_name or not product_series or not resource:
        return ToolResult(
            content="Missing device name, product series, or resource. Use device (host_name|product_series) or device_name and product_series.",
            structured_content=PrefabApp(view=Column(Text("Invalid device or resource."))),
        )
    data = {
        "app-data": {
            "data": {},
            "device": {"name": device_name, "productSeries": product_series},
            "resource": resource,
        }
    }
    try:
        result = post_job(
            workflow_name=workflow_name,
            workflow_version=workflow_version,
            data=data,
            job_name=job_name or f"{workflow_name}-{device_name}",
            tags=["mopActivity"],
        )
    except Exception as e:
        with Column(gap=4, css_class="p-6") as err_view:
            Heading("Run mopActivity workflow")
            Badge("Error", variant="destructive")
            Text(str(e))
        return ToolResult(
            content=f"Failed to start job: {e}",
            structured_content=PrefabApp(view=err_view),
        )
    job_id = result["jobId"]
    run_id = result["runId"]
    try:
        run = get_job_run(job_id, run_id)
    except Exception as e:
        with Column(gap=4, css_class="p-6") as view:
            Heading("Workflow job started")
            Text("Job ID: " + job_id)
            Text("Run ID: " + run_id)
            Badge("Error loading status", variant="destructive")
            Text(str(e))
        return ToolResult(
            content=f"Job started (jobId={job_id}, runId={run_id}). Failed to load status: {e}",
            structured_content=PrefabApp(view=view),
        )
    view = _build_status_view(job_id, run_id, run, title="MOP Activity Workflow Job Status")
    info = run.get("workflowExecutionInfo", {})
    status = info.get("status", "UNKNOWN")
    status_label = status.replace("WORKFLOW_EXECUTION_STATUS_", "").lower()
    return ToolResult(
        content=f"Job started. Status: {status_label}. Job ID: {job_id}, Run ID: {run_id}.",
        structured_content=PrefabApp(view=view, state={"job_id": job_id, "run_id": run_id}),
    )


# --- App: mopActivity workflow form (list + device / productSeries / resource) ---


# Default options when CWM doesn't expose get_devices / get_resources (so dropdowns still show).
DEFAULT_DEVICE_OPTIONS = ["NCS540X-7", "NCS5500", "ASR9000"]
DEFAULT_RESOURCE_OPTIONS = ["cwm.sol.system.nso"]


@mcp.tool(
    name="mop_activity_workflow_form",
    description="Open a form to run any mopActivity workflow: choose workflow, device, product series, and resource. Click Run workflow to see the message to copy; paste it into the chat and run it to see the Job Status view.",
    app=True,
)
def mop_activity_workflow_form() -> ToolResult:
    """Show a form with mopActivity workflow list and device (from CWM inventory) + resource; product series is tied to selected device."""
    workflows = get_mop_activity_workflows()
    inventory_devices = get_inventory_devices()
    resources = get_cwm_resources() or DEFAULT_RESOURCE_OPTIONS
    # Device dropdown: value is "host_name|product_series" so product series is determined by device selection
    devices_for_dropdown = inventory_devices
    if not devices_for_dropdown:
        devices_for_dropdown = [
            {"host_name": d, "product_series": "Unknown", "uuid": ""}
            for d in (get_nso_devices() or DEFAULT_DEVICE_OPTIONS)
        ]
    with Column(gap=6, css_class="p-6") as view:
        Heading("Run mopActivity workflow")
        Muted("Select options below. Device and product series come from CWM inventory. Click Run workflow to send the message; run it in chat to execute.")
        Separator()
        with Form():
            Text("Workflow", css_class="font-medium")
            if workflows and all(w.get("name") and w.get("version") for w in workflows):
                with Select(name="workflow", placeholder="Choose a workflow...", required=True):
                    for w in workflows:
                        SelectOption(
                            value=f"{w['name']}|{w['version']}",
                            label=f"{w['name']} ({w['version']})",
                        )
            else:
                workflow_options = [f"{w.get('name', '')}|{w.get('version', '')}" for w in workflows if w.get("name") and w.get("version")]
                if workflow_options:
                    Muted("Available: " + ", ".join(workflow_options))
                Input(
                    name="workflow",
                    label="Workflow (name|version)",
                    required=True,
                    placeholder="e.g. cfs-check-cwm-sol|2.1.0",
                )
            Text("Device", css_class="font-medium")
            with Select(name="device", placeholder="Choose device (name and product series from CWM inventory)...", required=True):
                for dev in devices_for_dropdown:
                    host_name = dev.get("host_name", "")
                    product_series = dev.get("product_series", "")
                    value = f"{host_name}|{product_series}"
                    label = f"{host_name} ({product_series})" if product_series else host_name
                    SelectOption(value=value, label=label)
            Text("NSO resource ID", css_class="font-medium")
            with Select(name="resource", placeholder="Choose resource...", required=True):
                for r in resources:
                    SelectOption(value=r, label=r)
            Input(
                name="job_name",
                label="Job name (optional)",
                placeholder="e.g. check-nodes-NCS540X-7",
                value="",
            )
            Button(
                "Run workflow",
                on_click=SendMessage(
                    "Please run run_mop_workflow_and_show_status with workflow {{ workflow }}, device {{ device }}, resource {{ resource }}, job_name {{ job_name }}."
                ),
            )

        Muted("Clicking Run workflow sends a message to the chat (same as Ask Cursor to refresh status). Run that message to execute the workflow and see the Job Status view.")

    return ToolResult(
        content="Form to run any mopActivity workflow. Select workflow, enter device name, product series, and resource.",
        structured_content=PrefabApp(view=view),
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
