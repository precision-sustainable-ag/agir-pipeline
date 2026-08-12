"""
Low-level Globus CLI helpers for endpoint-to-endpoint transfers.

This module does not plan transfers or write SQLite state. It turns generic
transfer requests into Globus CLI calls and returns structured results for
callers such as input staging and result synchronization to persist.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class TransferRequest:
    """One endpoint-to-endpoint transfer understood by the Globus CLI."""

    src_endpoint: str
    dst_endpoint: str
    src_path: str
    dst_path: str
    label: Optional[str] = None


@dataclass(frozen=True)
class TransferSubmitResult:
    status: str
    globus_task_id: Optional[str]
    details: str
    command: list[str]


@dataclass(frozen=True)
class TransferPollResult:
    status: str
    globus_status: Optional[str]
    details: str
    command: list[str]


def build_input_staging_label(stage: str, batch_id: str) -> str:
    """Construct a stable label for one input staging transfer."""
    return f"agir:{stage}:{batch_id}:input_stage"


def build_transfer_command(
    request: TransferRequest,
    *,
    recursive: bool = True,
    sync_level: Optional[str] = "mtime",
    label: Optional[str] = None,
) -> list[str]:
    """Build a ``globus transfer`` command for a generic transfer request."""
    cmd = [
        "globus",
        "transfer",
        f"{request.src_endpoint}:{request.src_path}",
        f"{request.dst_endpoint}:{request.dst_path}",
    ]
    if recursive:
        cmd.append("--recursive")
    if sync_level:
        cmd += ["--sync-level", sync_level]
    resolved_label = label if label is not None else request.label
    if resolved_label:
        cmd += ["--label", resolved_label]
    return cmd


def parse_globus_task_id(output: str) -> Optional[str]:
    """Extract a task id from Globus JSON or human-readable CLI output."""
    text = (output or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        task_id = payload.get("task_id") or payload.get("task_id_text")
        if task_id:
            return str(task_id)

    match = re.search(r"Task ID:\s*([a-f0-9-]+)", text, re.IGNORECASE)
    return match.group(1) if match else None


def parse_globus_task_state(output: str) -> Optional[str]:
    """Extract a Globus task state from JSON or human-readable CLI output."""
    text = (output or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        state = payload.get("status") or payload.get("state")
        if state:
            return str(state).upper()

    for pattern in (r"Status:\s*([A-Z_]+)", r"Task Status:\s*([A-Z_]+)"):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def map_globus_state_to_transfer_status(globus_state: Optional[str]) -> str:
    """Map Globus task state to the pipeline's transfer status vocabulary."""
    state = (globus_state or "").upper()
    if state in {"ACTIVE", "IN_PROGRESS", "RUNNING", "RETRYING"}:
        return "active"
    if state in {"SUCCEEDED", "SUCCESS"}:
        return "completed"
    if state in {"FAILED"}:
        return "failed"
    if state in {"CANCELED", "CANCELLED"}:
        return "canceled"
    return "submitted"


def map_globus_state_to_staging_status(globus_state: Optional[str]) -> str:
    """Backward-compatible alias for input-staging callers."""
    return map_globus_state_to_transfer_status(globus_state)


def _combined_output(exc: subprocess.CalledProcessError) -> str:
    return ((exc.stderr or "") + "\n" + (exc.stdout or "")).strip() or str(exc)


def submit_transfer(
    request: TransferRequest,
    *,
    dry_run: bool = False,
    recursive: bool = True,
    sync_level: Optional[str] = "mtime",
    runner: CommandRunner = subprocess.run,
) -> TransferSubmitResult:
    """
    Submit one Globus transfer request.

    In dry-run mode no command is executed; a synthetic task id is returned so
    callers can exercise downstream status handling without contacting Globus.
    """
    command = build_transfer_command(
        request,
        recursive=recursive,
        sync_level=sync_level,
    )

    if dry_run:
        task_id = f"dry-run-{uuid.uuid4()}"
        return TransferSubmitResult("submitted", task_id, "[DRY-RUN] " + " ".join(command), command)

    try:
        proc = runner(
            command + ["--format", "json"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        return TransferSubmitResult("failed", None, _combined_output(exc), command)

    output = (proc.stdout or "").strip()
    task_id = parse_globus_task_id(output)
    status = "submitted" if task_id else "failed"
    details = output or "Globus transfer submitted without stdout"
    if task_id is None:
        details = f"Could not parse Globus task id from output: {details}"
    return TransferSubmitResult(status, task_id, details, command)


def build_task_show_command(globus_task_id: str) -> list[str]:
    """Build a ``globus task show`` command."""
    return ["globus", "task", "show", globus_task_id]


def poll_task(
    globus_task_id: str,
    *,
    dry_run: bool = False,
    runner: CommandRunner = subprocess.run,
) -> TransferPollResult:
    """Poll one Globus task and map its state to a staged-input status."""
    command = build_task_show_command(globus_task_id)
    if dry_run or globus_task_id.startswith("dry-run-"):
        return TransferPollResult("completed", "SUCCEEDED", "DRY-RUN SUCCEEDED", command)

    try:
        proc = runner(
            command + ["--format", "json"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        return TransferPollResult("failed", None, _combined_output(exc), command)

    output = (proc.stdout or "").strip()
    globus_state = parse_globus_task_state(output)
    status = map_globus_state_to_transfer_status(globus_state)
    details = output or "Globus task show returned no stdout"
    return TransferPollResult(status, globus_state, details, command)


def submit_many(
    requests: Sequence[TransferRequest],
    *,
    dry_run: bool = False,
    recursive: bool = True,
    sync_level: Optional[str] = "mtime",
    runner: CommandRunner = subprocess.run,
) -> list[TransferSubmitResult]:
    """Submit multiple generic transfer requests in order."""
    return [
        submit_transfer(
            request,
            dry_run=dry_run,
            recursive=recursive,
            sync_level=sync_level,
            runner=runner,
        )
        for request in requests
    ]
