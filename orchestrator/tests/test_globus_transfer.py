from __future__ import annotations

import subprocess

from orchestrator.globus_transfer import (
    TransferRequest,
    build_batch_stdin,
    build_input_staging_label,
    build_task_show_command,
    build_transfer_command,
    map_globus_state_to_staging_status,
    parse_globus_task_id,
    parse_globus_task_state,
    poll_task,
    submit_many,
    submit_transfer,
)


def make_request() -> TransferRequest:
    return TransferRequest(
        src_endpoint="juno-uuid",
        dst_endpoint="ceres-uuid",
        src_path="/LTS/project/dash_agir/semifield-upload/NC_2025-01-01",
        dst_path="/90daydata/dash_agir/semifield-upload/NC_2025-01-01",
    )


def test_build_input_staging_label() -> None:
    assert build_input_staging_label("raw_to_jpg", "NC_2025-01-01") == (
        "agir:raw_to_jpg:NC_2025-01-01:input_stage"
    )


def test_build_transfer_command() -> None:
    request = make_request()

    assert build_transfer_command(request, label="label-1") == [
        "globus",
        "transfer",
        "juno-uuid:/LTS/project/dash_agir/semifield-upload/NC_2025-01-01",
        "ceres-uuid:/90daydata/dash_agir/semifield-upload/NC_2025-01-01",
        "--recursive",
        "--sync-level",
        "mtime",
        "--label",
        "label-1",
    ]


def make_batch_request() -> TransferRequest:
    return TransferRequest(
        src_endpoint="atlas-uuid",
        dst_endpoint="ceres-uuid",
        src_path="/90daydata/dash_agir/semifield-developed-images/MD_2026-03-19/images",
        dst_path="/90daydata/dash_agir/semifield-developed-images/MD_2026-03-19/images",
        file_names=("img_001.jpg", "img_002.jpg"),
    )


def test_build_transfer_command_uses_batch_mode_for_sampled_files() -> None:
    request = make_batch_request()

    assert build_transfer_command(request, label="label-1") == [
        "globus",
        "transfer",
        "atlas-uuid",
        "ceres-uuid",
        "--batch",
        "-",
        "--sync-level",
        "mtime",
        "--label",
        "label-1",
    ]


def test_build_batch_stdin_pairs_each_sampled_file() -> None:
    request = make_batch_request()

    assert build_batch_stdin(request) == (
        "/90daydata/dash_agir/semifield-developed-images/MD_2026-03-19/images/img_001.jpg"
        " /90daydata/dash_agir/semifield-developed-images/MD_2026-03-19/images/img_001.jpg\n"
        "/90daydata/dash_agir/semifield-developed-images/MD_2026-03-19/images/img_002.jpg"
        " /90daydata/dash_agir/semifield-developed-images/MD_2026-03-19/images/img_002.jpg\n"
    )


def test_submit_transfer_pipes_batch_stdin_for_sampled_files() -> None:
    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout='{"task_id": "task-456"}', stderr="")

    result = submit_transfer(make_batch_request(), runner=fake_runner)

    assert result.status == "submitted"
    assert calls[0][1]["input"] == build_batch_stdin(make_batch_request())


def test_parse_globus_task_id_and_state() -> None:
    assert parse_globus_task_id('{"task_id": "abc-123"}') == "abc-123"
    assert parse_globus_task_id("Task ID: deadbeef-1234") == "deadbeef-1234"
    assert parse_globus_task_id("no task here") is None

    assert parse_globus_task_state('{"status": "SUCCEEDED"}') == "SUCCEEDED"
    assert parse_globus_task_state("Status: ACTIVE") == "ACTIVE"
    assert parse_globus_task_state("no status here") is None


def test_map_globus_state_to_staging_status() -> None:
    assert map_globus_state_to_staging_status("ACTIVE") == "active"
    assert map_globus_state_to_staging_status("SUCCEEDED") == "completed"
    assert map_globus_state_to_staging_status("FAILED") == "failed"
    assert map_globus_state_to_staging_status("CANCELED") == "canceled"
    assert map_globus_state_to_staging_status("UNKNOWN") == "submitted"


def test_submit_transfer_uses_runner_and_parses_task_id() -> None:
    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout='{"task_id": "task-123"}', stderr="")

    result = submit_transfer(make_request(), runner=fake_runner)

    assert result.status == "submitted"
    assert result.globus_task_id == "task-123"
    assert calls[0][0][0][-2:] == ["--format", "json"]
    assert calls[0][1]["check"] is True


def test_submit_transfer_failure_returns_structured_failure() -> None:
    def fake_runner(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], output="out", stderr="err")

    result = submit_transfer(make_request(), runner=fake_runner)

    assert result.status == "failed"
    assert result.globus_task_id is None
    assert "err" in result.details


def test_dry_run_submit_and_poll_do_not_call_runner() -> None:
    def exploding_runner(*args, **kwargs):
        raise AssertionError("runner should not be called")

    submitted = submit_transfer(make_request(), dry_run=True, runner=exploding_runner)
    assert submitted.status == "submitted"
    assert submitted.globus_task_id is not None
    assert submitted.globus_task_id.startswith("dry-run-")

    polled = poll_task(submitted.globus_task_id, runner=exploding_runner)
    assert polled.status == "completed"
    assert polled.globus_status == "SUCCEEDED"


def test_poll_task_maps_json_status() -> None:
    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout='{"status": "ACTIVE"}', stderr="")

    result = poll_task("task-123", runner=fake_runner)

    assert build_task_show_command("task-123") == ["globus", "task", "show", "task-123"]
    assert result.status == "active"
    assert result.globus_status == "ACTIVE"
    assert calls[0][0][0] == ["globus", "task", "show", "task-123", "--format", "json"]


def test_submit_many_submits_in_order() -> None:
    requests = [make_request(), make_request()]

    def fake_runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout='{"task_id": "task-123"}', stderr="")

    results = submit_many(requests, runner=fake_runner)

    assert [result.status for result in results] == ["submitted", "submitted"]
    assert [result.globus_task_id for result in results] == ["task-123", "task-123"]
