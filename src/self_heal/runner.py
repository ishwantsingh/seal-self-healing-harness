"""Docker boundary and supervisor-owned model/table bridge for generated code."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from harness.agent import RunResult, logistics_capability_request
from harness.logistics import LogisticsTools
from harness.tools import AnalystTools
from evals.analyst.oracle import OracleError, _validate_task
from self_heal.contracts import (
    build_run_completion_patch, build_run_start_record, new_run_id, trace_metadata, utc_now,
)
from self_heal.execution import RunExecution
from self_heal.model import ChatModel
from self_heal.settings import AnalystConfig
from self_heal.storage import AtlasHistoryStore
from self_heal.table_store import AtlasTableStore, DatasetInfo
from self_heal.logistics_store import DatasetBundleInfo, LogisticsDatasetStore
from self_heal.telemetry import LangSmithTelemetry
from self_heal.evidence import EvidenceTools, run_evidence


class RunnerError(RuntimeError):
    pass


class CandidateRunner:
    def __init__(
        self, *, store: AtlasTableStore | None, config: AnalystConfig, telemetry: LangSmithTelemetry,
        history: AtlasHistoryStore | None, image: str = "self-heal-runner:local",
        repository: Path | None = None, logistics: LogisticsDatasetStore | None = None,
    ) -> None:
        self.store, self.logistics = store, logistics
        self.config, self.telemetry, self.history = config, telemetry, history
        self.image = image
        self.repository = (repository or Path(__file__).resolve().parents[2]).resolve()

    def dataset_info(self, dataset: DatasetInfo | DatasetBundleInfo) -> DatasetInfo | DatasetBundleInfo:
        """Re-read immutable metadata through the store that owns this input."""

        input_kind = getattr(dataset, "input_kind", "inventory_table")
        if input_kind == "logistics_bundle":
            if self.logistics is None:
                raise RunnerError("Logistics candidate execution is not configured")
            return self.logistics.dataset_info(dataset.dataset_id)
        if input_kind == "inventory_table":
            if self.store is None:
                raise RunnerError("Inventory candidate execution is not configured")
            return self.store.dataset_info(dataset.dataset_id)
        raise RunnerError("Candidate input kind is not supported")

    def image_identity(self) -> str:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self.image],
            capture_output=True, text=True, check=False, timeout=15,
        )
        if result.returncode or not result.stdout.strip().startswith("sha256:"):
            raise RunnerError("Runner image is unavailable; run `self-heal runner build` and start Docker")
        return result.stdout.strip()

    def build_image(self) -> str:
        result = subprocess.run(
            ["docker", "build", "-t", self.image, "-f", "Dockerfile", "."],
            cwd=self.repository, capture_output=True, text=True, check=False, timeout=300,
        )
        if result.returncode:
            raise RunnerError("Docker image build failed: " + result.stderr[-500:])
        return self.image_identity()

    def run(
        self, *, source: Path, source_commit: str, dataset: DatasetInfo | DatasetBundleInfo,
        invocation: dict[str, Any] | str, model: ChatModel,
        case_id: str | None = None, case_exposure: str | None = None,
        workflow_revision_id: str | None = None,
    ) -> RunExecution:
        image_digest = self.image_identity()
        source = source.resolve()
        harness_dir = source / "harness"
        if not harness_dir.is_dir() or harness_dir.is_symlink():
            raise RunnerError("Candidate harness directory is unavailable")
        if any(path.is_symlink() for path in harness_dir.rglob("*")):
            raise RunnerError("Candidate harness contains a symlink")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source,
                              capture_output=True, text=True, check=False, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=source,
                               capture_output=True, text=True, check=False, timeout=5)
        if head.returncode or dirty.returncode or head.stdout.strip() != source_commit or dirty.stdout.strip():
            raise RunnerError("Candidate source does not match its pinned clean commit")
        input_kind = getattr(dataset, "input_kind", "inventory_table")
        if input_kind == "logistics_bundle":
            if self.logistics is None:
                raise RunnerError("Logistics candidate execution is not configured")
            verified = self.dataset_info(dataset)
            table = self.logistics.open_session(dataset.dataset_id)
            audited_tools = EvidenceTools(LogisticsTools(table, self.config))
        elif input_kind == "inventory_table":
            if self.store is None:
                raise RunnerError("Inventory candidate execution is not configured")
            verified = self.dataset_info(dataset)
            table = self.store.open_session(dataset.dataset_id)
            audited_tools = EvidenceTools(AnalystTools(table, self.config))
        else:
            raise RunnerError("Candidate input kind is not supported")
        if verified != dataset:
            raise RunnerError("Dataset identity changed before execution")
        run_id, started_at = new_run_id(), utc_now()
        status = "not_configured" if self.history is None else "recording"
        history_error = None
        started = False
        if self.history:
            try:
                self.history.start_run(build_run_start_record(
                    run_id=run_id, invocation=invocation, dataset=dataset, config=self.config,
                    model=model, started_at=started_at, case_id=case_id,
                    case_exposure=case_exposure, source_commit=source_commit,
                    runner_image_digest=image_digest,
                    workflow_revision_id=workflow_revision_id,
                ))
                started = True
            except Exception as exc:
                status, history_error = "incomplete", type(exc).__name__

        result, trace = self.telemetry.execute(
            run_id=run_id, invocation=invocation,
            metadata=trace_metadata(
                run_id=run_id, dataset=dataset, invocation=invocation,
                config=self.config, model=model, source_commit=source_commit,
                runner_image_digest=image_digest,
            ),
            model=model, tools=audited_tools, started_at=started_at,
            execute=lambda traced_model, traced_tools: self._execute_container(
                harness_dir, invocation, run_id, table, traced_model, traced_tools, input_kind=input_kind,
            ),
        )
        if self.history and started:
            try:
                self.history.finish_run(
                    run_id, build_run_completion_patch(result=result, trace=trace, completed_at=utc_now(),
                                                       evidence=run_evidence(table, audited_tools))
                )
                status = "recorded"
            except Exception as exc:
                status, history_error = "incomplete", type(exc).__name__
        return RunExecution(result=result, trace=trace, history_status=status, history_error=history_error)

    def _execute_container(
        self, harness_dir, invocation, run_id, table, model, tools, *, input_kind: str | None = None,
    ) -> RunResult:
        limits = self.config.limits
        input_kind = input_kind or getattr(table, "input_kind", "inventory_table")
        if input_kind not in {"inventory_table", "logistics_bundle"}:
            raise RunnerError("Candidate input kind is not supported")
        command = [
            "docker", "run", "--rm", "-i", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
            "--memory", "256m", "--cpus", "1", "--user", "65534:65534",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--mount", f"type=bind,src={harness_dir},dst=/candidate/harness,readonly",
            self.image,
        ]
        begun = time.monotonic()
        model_calls = tool_calls = tokens = observed_tools = 0
        payload = None
        error = None
        process = None
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, bufsize=0)
            assert process.stdin and process.stdout
            self._send(process, {"run_id": run_id, "invocation": invocation, "input_kind": input_kind, "config": {
                "metrics": self.config.metrics, "filter_fields": self.config.filter_fields,
                "group_fields": self.config.group_fields, "table_schema": self.config.table_schema,
                "limits": asdict(limits),
            }})
            pending = b""
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while payload is None:
                    remaining = limits.max_elapsed_seconds - (time.monotonic() - begun)
                    if remaining <= 0:
                        raise RunnerError("Task time budget exceeded")
                    if not selector.select(remaining):
                        raise RunnerError("Task time budget exceeded")
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        raise RunnerError("Candidate process exited without a result")
                    pending += data
                    if len(pending) > 2_000_000:
                        raise RunnerError("Candidate bridge message exceeded size limit")
                    while b"\n" in pending and payload is None:
                        line, pending = pending.split(b"\n", 1)
                        request = json.loads(line)
                        kind = request.get("type")
                        try:
                            if kind == "model":
                                if model_calls >= limits.max_model_calls:
                                    raise RunnerError("Model-call budget exceeded")
                                messages, definitions = request.get("messages"), request.get("tools")
                                if not isinstance(messages, list) or not isinstance(definitions, list):
                                    raise RunnerError("Invalid model request")
                                reply = model.complete(messages, definitions)
                                model_calls += 1
                                tool_calls += len(reply.tool_calls)
                                tokens += reply.total_tokens
                                if tool_calls > limits.max_tool_calls or tokens > limits.max_total_tokens:
                                    raise RunnerError("Model/tool/token budget exceeded")
                                value = {"content": reply.content, "total_tokens": reply.total_tokens,
                                         "tool_calls": [asdict(call) for call in reply.tool_calls]}
                            elif kind == "table":
                                if input_kind != "inventory_table":
                                    raise RunnerError("Inventory table access is unavailable for this input")
                                operation, arguments = request.get("operation"), request.get("arguments")
                                if not isinstance(arguments, dict):
                                    raise RunnerError("Invalid table request")
                                if operation == "inspect_table" and not arguments:
                                    value = table.inspect_table()
                                elif operation == "read_rows" and set(arguments) <= {
                                    "cursor", "limit", "filter_field", "filter_value"
                                }:
                                    value = table.read_rows(**arguments)
                                elif operation == "completed_scan" and set(arguments) == {
                                    "filter_field", "filter_value"
                                }:
                                    value = table.completed_scan(**arguments)
                                else:
                                    raise RunnerError("Table operation is not permitted")
                            elif kind == "logistics":
                                if input_kind != "logistics_bundle":
                                    raise RunnerError("Logistics access is unavailable for this input")
                                operation, arguments = request.get("operation"), request.get("arguments")
                                if not isinstance(arguments, dict):
                                    raise RunnerError("Invalid logistics request")
                                if operation == "inspect_catalog" and not arguments:
                                    value = table.inspect_catalog()
                                elif operation == "inspect_relation" and set(arguments) == {"relation"} and isinstance(arguments["relation"], str):
                                    value = table.inspect_relation(**arguments)
                                elif operation == "read_shipments" and set(arguments) <= {
                                    "warehouse_number", "relative_day", "limit", "cursor"
                                }:
                                    value = table.read_shipments(**arguments)
                                else:
                                    raise RunnerError("Logistics operation is not permitted")
                            elif kind == "tool_result":
                                observed_tools += 1
                                if input_kind == "logistics_bundle":
                                    tool_calls = observed_tools
                                    if tool_calls > limits.max_tool_calls:
                                        raise RunnerError("Tool-call budget exceeded")
                                elif observed_tools > tool_calls:
                                    raise RunnerError("Unmatched candidate tool envelope")
                                name, arguments, value = request.get("name"), request.get("arguments"), request.get("result")
                                if not isinstance(name, str) or not isinstance(arguments, dict) or not isinstance(value, dict):
                                    raise RunnerError("Invalid tool envelope")
                                record = getattr(tools, "record_remote_tool", None)
                                if callable(record):
                                    record(name, arguments, value)
                                value = None
                            elif kind == "result":
                                payload = request.get("value")
                                value = None
                            else:
                                raise RunnerError("Unknown candidate bridge request")
                            self._send(process, {"ok": True, "value": value})
                        except Exception as exc:
                            self._send(process, {"ok": False, "error": str(exc)[:200]})
                            if isinstance(exc, RunnerError):
                                raise
            process.wait(timeout=2)
        except (OSError, ValueError, subprocess.SubprocessError, RunnerError) as exc:
            error = f"Isolated candidate failure: {type(exc).__name__}: {str(exc)[:120]}"
        finally:
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=2)

        elapsed = round(time.monotonic() - begun, 3)
        if not isinstance(payload, dict):
            payload = {}
        outcome = payload.get("outcome")
        answer = payload.get("answer")
        interpreted = payload.get("interpreted_task")
        if error is None and outcome == "answered":
            if not isinstance(answer, dict) or not isinstance(interpreted, dict):
                error = "Candidate returned an invalid answer envelope"
            else:
                if input_kind == "logistics_bundle":
                    expected = _logistics_task_for(invocation)
                    if expected is None or interpreted != expected:
                        error = "Candidate returned a task outside the protected logistics contract"
                    if error is None and not _valid_answer(answer, grouped=False):
                        error = "Candidate returned an invalid answer shape"
                    if error is None and observed_tools == 0:
                        error = "Candidate did not execute an observed logistics tool"
                    if error is None and not table.completed_shipment_scan(
                        interpreted["warehouse_number"], interpreted["relative_day"],
                    ):
                        error = "Required shipment rows were not fully read"
                else:
                    try:
                        _validate_task(interpreted, self.config)
                    except OracleError:
                        error = "Candidate returned a task outside the protected contract"
                    if error is None and isinstance(invocation, dict) and interpreted != invocation:
                        error = "Candidate changed the requested structured task"
                    if error is None and not _valid_answer(answer, grouped=interpreted.get("group_by") is not None):
                        error = "Candidate returned an invalid answer shape"
                    if error is None and not table.completed_scan(interpreted.get("filter_field"), interpreted.get("filter_value")):
                        error = "Required table rows were not fully read"
        if outcome not in {"answered", "unsupported", "error"}:
            error = error or "Candidate returned an invalid outcome"
        if elapsed > limits.max_elapsed_seconds:
            error = "Task time budget exceeded"
        return RunResult(
            run_id=run_id, answer=answer if not error and isinstance(answer, dict) else None,
            error=error or (payload.get("error") if outcome == "error" else None),
            outcome="error" if error else outcome,
            interpreted_task=interpreted if isinstance(interpreted, dict) else None,
            model_calls=model_calls, tool_calls=tool_calls, total_tokens=tokens,
            elapsed_seconds=elapsed, table_pages=table.pages_read, table_bytes=table.bytes_read,
            limitation_kind=payload.get("limitation_kind") if outcome == "unsupported" else None,
            limitation_reason=payload.get("limitation_reason") if outcome == "unsupported" else None,
        )

    @staticmethod
    def _send(process: subprocess.Popen, message: dict[str, Any]) -> None:
        assert process.stdin
        process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        process.stdin.flush()


def _valid_answer(answer: dict[str, Any], *, grouped: bool) -> bool:
    if grouped:
        groups = answer.get("groups")
        return (set(answer) == {"groups"} and isinstance(groups, dict)
                and list(groups) == sorted(groups)
                and all(isinstance(key, str) and type(value) is int for key, value in groups.items()))
    return set(answer) == {"value"} and type(answer.get("value")) is int


def _logistics_task_for(invocation: dict[str, Any] | str) -> dict[str, Any] | None:
    """Return the one canonical task a logistics candidate may execute."""

    if isinstance(invocation, str):
        request = logistics_capability_request(invocation)
        if request is None:
            return None
        return {key: request[key] for key in (
            "operation", "warehouse_number", "relative_day", "threshold",
        )}
    required = {"operation", "warehouse_number", "relative_day", "threshold"}
    if set(invocation) != required:
        return None
    if (invocation["operation"] != "count_customers_with_shipment_count_gt"
            or type(invocation["warehouse_number"]) is not int or invocation["warehouse_number"] < 1
            or invocation["relative_day"] != "yesterday"
            or type(invocation["threshold"]) is not int or invocation["threshold"] < 0):
        return None
    return dict(invocation)
