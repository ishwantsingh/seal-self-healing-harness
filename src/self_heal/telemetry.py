"""Thin, trusted LangSmith instrumentation for a fixed model/tool harness.

Atlas keeps compact supervisor evidence. LangSmith receives detailed nested
traces only after this module redacts secrets, table rows, and aggregate
answers. A tracing failure never changes the agent's measured outcome.
"""

from __future__ import annotations

import copy
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

from harness.agent import RunResult
from harness.tools import AnalystTools
from self_heal.model import ChatModel, ModelReply, OpenRouterModel
from self_heal.settings import LangSmithConfig


REDACTION_VERSION = "v1"
_SECRET_KEY_PARTS = (
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
    "connection_string",
    "atlas_uri",
    "mongo_uri",
)
_URI_CREDENTIALS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)
_KEY_LIKE_VALUE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|lsv2_[A-Za-z0-9_-]{8,})\b")


def redact_trace_payload(payload: Any) -> Any:
    """Recursively remove secrets and raw table/answer values before upload.

    Wrapped OpenAI calls include messages as JSON strings, so those strings are
    parsed when possible before being returned to LangSmith.
    """

    return _redact(payload, parent_key=None)


def _redact(value: Any, *, parent_key: str | None) -> Any:
    key = parent_key.lower() if parent_key else ""
    if any(part in key for part in _SECRET_KEY_PARTS):
        return "[REDACTED]"
    if key == "rows":
        return _row_summary(value)
    if key == "row":
        return _single_row_summary(value)
    if key == "answer":
        return _answer_summary(value)
    if isinstance(value, dict):
        if set(value) == {"value"} and isinstance(value.get("value"), (int, float)):
            return {"value": "[REDACTED]"}
        if set(value) == {"groups"} and isinstance(value.get("groups"), dict):
            return {"groups": {name: "[REDACTED]" for name in value["groups"]}}
        return {str(item_key): _redact(item, parent_key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, parent_key=parent_key) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _row_summary(rows: Any) -> dict[str, Any]:
    if not isinstance(rows, list):
        return {"redacted_row_count": None, "fields": []}
    fields = sorted({field for row in rows if isinstance(row, dict) for field in row})
    return {"redacted_row_count": len(rows), "fields": fields}


def _single_row_summary(row: Any) -> dict[str, Any]:
    return {"redacted_row": True, "fields": sorted(row) if isinstance(row, dict) else []}


def _answer_summary(answer: Any) -> dict[str, Any]:
    if isinstance(answer, dict):
        return {"redacted_answer_fields": sorted(answer)}
    return {"redacted_answer": answer is not None}


def _redact_string(value: str) -> str:
    # Tool results are serialized into OpenAI message content. Redact their
    # nested rows and final answers while retaining the shape of the exchange.
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        decoded = None
    if isinstance(decoded, (dict, list)):
        cleaned = _redact(decoded, parent_key=None)
        if cleaned != decoded:
            return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    value = _URI_CREDENTIALS.sub(r"\g<scheme>[REDACTED]@", value)
    return _KEY_LIKE_VALUE.sub("[REDACTED]", value)


@dataclass(frozen=True)
class TraceEvidence:
    trace_id: str | None
    url: str | None
    status: str
    project: str | None
    started_at: datetime | None
    checked_at: datetime | None
    error_type: str | None = None
    redaction_version: str = REDACTION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.trace_id,
            "url": self.url,
            "status": self.status,
            "project": self.project,
            "started_at": self.started_at,
            "checked_at": self.checked_at,
            "error_type": self.error_type,
            "redaction_version": self.redaction_version,
        }


class TraceFactory(Protocol):
    def __call__(self, name: str, run_type: str = "chain", **kwargs: Any) -> Any: ...


class LangSmithClientFactory(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


class TracedModel:
    """Model proxy that gives scripted and OpenRouter models a logical child span."""

    def __init__(
        self,
        model: ChatModel,
        *,
        trace_factory: TraceFactory,
        client: Any,
        project: str,
        metadata: dict[str, Any],
        mark_incomplete: Callable[[Exception], None],
    ) -> None:
        self._model = model
        self._trace_factory = trace_factory
        self._client = client
        self._project = project
        self._metadata = metadata
        self._mark_incomplete = mark_incomplete

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        started_model = False
        completed_model = False
        reply: ModelReply | None = None
        try:
            context = self._trace_factory(
                "analyst.model",
                run_type="chain",
                client=self._client,
                project_name=self._project,
                inputs={"messages": messages, "tools": tools},
                metadata=self._metadata,
                enabled=True,
            )
            with context as span:
                started_model = True
                reply = self._model.complete(messages, tools)
                completed_model = True
                _end_span(
                    span,
                    {
                        "content": reply.content,
                        "tool_calls": [
                            {"id": call.id, "name": call.name, "arguments": call.arguments}
                            for call in reply.tool_calls
                        ],
                        "total_tokens": reply.total_tokens,
                    },
                )
                return reply
        except Exception as exc:
            if completed_model:
                self._mark_incomplete(exc)
                assert reply is not None
                return reply
            if not started_model:
                self._mark_incomplete(exc)
                return self._model.complete(messages, tools)
            raise


class TracedTools:
    """Delegating tool proxy that preserves the harness's exact tool surface."""

    def __init__(
        self,
        tools: AnalystTools,
        *,
        trace_factory: TraceFactory,
        client: Any,
        project: str,
        metadata: dict[str, Any],
        mark_incomplete: Callable[[Exception], None],
    ) -> None:
        self._tools = tools
        self._trace_factory = trace_factory
        self._client = client
        self._project = project
        self._metadata = metadata
        self._mark_incomplete = mark_incomplete

    @property
    def table(self) -> Any:
        return self._tools.table

    def definitions(self) -> list[dict[str, Any]]:
        return self._tools.definitions()

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started_tool = False
        completed_tool = False
        result: dict[str, Any] | None = None
        try:
            context = self._trace_factory(
                f"tool.{name}",
                run_type="tool",
                client=self._client,
                project_name=self._project,
                inputs={"name": name, "arguments": arguments},
                metadata=self._metadata,
                enabled=True,
            )
            with context as span:
                started_tool = True
                result = self._tools.execute(name, arguments)
                completed_tool = True
                _end_span(span, {"result": result})
                return result
        except Exception as exc:
            if completed_tool:
                self._mark_incomplete(exc)
                assert result is not None
                return result
            if not started_tool:
                self._mark_incomplete(exc)
                return self._tools.execute(name, arguments)
            raise

    def record_remote_tool(self, name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        """Record an isolated candidate's tool envelope without executing it again."""
        record = getattr(self._tools, "record_remote_tool", None)
        if callable(record):
            record(name, arguments, result)
        try:
            context = self._trace_factory(
                f"tool.{name}", run_type="tool", client=self._client,
                project_name=self._project, inputs={"name": name, "arguments": arguments},
                metadata=self._metadata, enabled=True,
            )
            with context as span:
                _end_span(span, {"result": result})
        except Exception as exc:
            self._mark_incomplete(exc)


def _end_span(span: Any, outputs: dict[str, Any]) -> None:
    end = getattr(span, "end", None)
    if callable(end):
        end(outputs=outputs)


def _add_metadata(span: Any, metadata: dict[str, Any]) -> None:
    add = getattr(span, "add_metadata", None)
    if callable(add):
        add(metadata)


def _add_tags(span: Any, tags: list[str]) -> None:
    add = getattr(span, "add_tags", None)
    if callable(add):
        add(tags)


class LangSmithTelemetry:
    """Owns optional LangSmith setup and turns failures into explicit evidence."""

    def __init__(
        self,
        config: LangSmithConfig,
        *,
        client_factory: LangSmithClientFactory | None = None,
        trace_factory: TraceFactory | None = None,
        wrap_openai_fn: Callable[..., Any] | None = None,
        verification_attempts: int = 3,
        retry_delay_seconds: float = 1,
    ) -> None:
        if verification_attempts < 1 or retry_delay_seconds < 0:
            raise ValueError("Trace verification settings must be nonnegative")
        self._config = config
        self._client_factory = client_factory
        self._trace_factory = trace_factory
        self._wrap_openai_fn = wrap_openai_fn
        self._verification_attempts = verification_attempts
        self._retry_delay_seconds = retry_delay_seconds

    def read_redacted_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """Fetch only the incident tree needed for diagnosis, then redact again."""
        if not self._config.enabled or not self._config.api_key:
            raise LookupError("LangSmith trace access is not configured")
        client_factory, _, _ = self._dependencies()
        client = client_factory(api_key=self._config.api_key, workspace_id=self._config.workspace_id,
                                timeout_ms=10000)
        runs = list(client.list_runs(project_name=self._config.project, trace_id=trace_id, limit=100))
        if not runs or not any(str(getattr(run, "id", "")) == trace_id for run in runs):
            raise LookupError("Incident trace is unavailable")
        return [redact_trace_payload({
            "name": getattr(run, "name", None), "run_type": getattr(run, "run_type", None),
            "inputs": getattr(run, "inputs", None), "outputs": getattr(run, "outputs", None),
            "error": getattr(run, "error", None),
        }) for run in runs]

    def trace_spans(self, trace_id: str) -> list[dict[str, Any]]:
        """Return display metadata only; trace inputs and outputs stay redacted and collapsed."""
        if not self._config.enabled or not self._config.api_key:
            return []
        client_factory, _, _ = self._dependencies()
        client = client_factory(api_key=self._config.api_key, workspace_id=self._config.workspace_id,
                                timeout_ms=10000)
        spans = []
        for run in client.list_runs(project_name=self._config.project, trace_id=trace_id, limit=100):
            start, end = getattr(run, "start_time", None), getattr(run, "end_time", None)
            metadata = getattr(run, "extra", None) or {}
            is_tool = str(getattr(run, "name", "")).startswith("tool.")
            spans.append({
                "id": str(getattr(run, "id", "")),
                "parent_id": str(getattr(run, "parent_run_id", "")) if getattr(run, "parent_run_id", None) else None,
                "name": getattr(run, "name", None),
                "type": getattr(run, "run_type", None),
                "start_time": start,
                "duration_ms": round((end - start).total_seconds() * 1000, 1) if start and end else None,
                "status": "error" if getattr(run, "error", None) else ("completed" if end else "running"),
                "error": redact_trace_payload(getattr(run, "error", None)),
                "model": redact_trace_payload((metadata.get("metadata") or {}).get("model_id") or
                                              (metadata.get("metadata") or {}).get("model")),
                "tokens": (getattr(run, "total_tokens", None) or
                           (getattr(run, "prompt_tokens", None) or 0) +
                           (getattr(run, "completion_tokens", None) or 0) or None),
                **({"arguments": redact_trace_payload((getattr(run, "inputs", None) or {}).get("arguments")),
                    "result_preview": redact_trace_payload((getattr(run, "outputs", None) or {}).get("result"))}
                   if is_tool else {}),
            })
        return sorted(spans, key=lambda span: str(span["start_time"] or ""))

    def execute(
        self,
        *,
        run_id: str,
        invocation: dict[str, Any] | str,
        metadata: dict[str, Any],
        model: ChatModel,
        tools: AnalystTools,
        execute: Callable[[ChatModel, AnalystTools], RunResult],
        started_at: datetime,
    ) -> tuple[RunResult, TraceEvidence]:
        if not self._config.enabled:
            return execute(model, tools), TraceEvidence(
                trace_id=None,
                url=None,
                status="disabled",
                project=self._config.project,
                started_at=None,
                checked_at=None,
            )
        if not self._config.api_key:
            return execute(model, tools), TraceEvidence(
                trace_id=run_id,
                url=None,
                status="incomplete",
                project=self._config.project,
                started_at=started_at,
                checked_at=datetime.now(started_at.tzinfo),
                error_type="MissingLangSmithApiKey",
            )

        try:
            client_factory, trace_factory, wrap_openai_fn = self._dependencies()
            client = client_factory(
                api_key=self._config.api_key,
                workspace_id=self._config.workspace_id,
                anonymizer=redact_trace_payload,
                timeout_ms=10000,
            )
        except Exception as exc:
            return execute(model, tools), self._incomplete(run_id, started_at, exc)

        failures: list[Exception] = []

        def mark_incomplete(exc: Exception) -> None:
            failures.append(exc)

        traced_model = self._instrument_model(
            model,
            client=client,
            trace_factory=trace_factory,
            wrap_openai_fn=wrap_openai_fn,
            metadata=metadata,
            mark_incomplete=mark_incomplete,
        )
        traced_tools = TracedTools(
            tools,
            trace_factory=trace_factory,
            client=client,
            project=self._config.project,
            metadata=metadata,
            mark_incomplete=mark_incomplete,
        )
        result: RunResult | None = None
        began_execution = False
        completed_execution = False
        try:
            context = trace_factory(
                "analyst.run",
                run_type="chain",
                run_id=run_id,
                project_name=self._config.project,
                client=client,
                inputs={"run_id": run_id, "invocation": _trace_invocation(invocation)},
                metadata=metadata,
                tags=["self-heal", "phase=3", f"task_family={metadata['task_family']}"],
                enabled=True,
            )
            with context as root:
                began_execution = True
                result = execute(traced_model, traced_tools)
                completed_execution = True
                final_metadata = {
                    "outcome": result.outcome,
                    "limitation_kind": result.limitation_kind,
                    "limitation_reason": result.limitation_reason,
                    "resource_summary": {
                        "model_calls": result.model_calls,
                        "tool_calls": result.tool_calls,
                        "total_tokens": result.total_tokens,
                        "elapsed_seconds": result.elapsed_seconds,
                        "table_pages": result.table_pages,
                        "table_bytes": result.table_bytes,
                    },
                }
                _add_metadata(root, final_metadata)
                tags = [f"outcome={result.outcome}"]
                if result.limitation_kind:
                    tags.append(f"limitation_kind={result.limitation_kind}")
                _add_tags(root, tags)
                _end_span(root, _trace_result(result))
        except Exception as exc:
            if completed_execution:
                mark_incomplete(exc)
            elif not began_execution:
                mark_incomplete(exc)
                result = execute(model, tools)
            else:
                # The agent is designed to contain model/tool errors. If an
                # unexpected supervisor exception escapes it, report a normal
                # agent-shaped error rather than retrying an unknown task.
                mark_incomplete(exc)
                result = RunResult(
                    run_id=run_id,
                    answer=None,
                    error=f"Supervisor execution failure: {type(exc).__name__}",
                    outcome="error",
                    interpreted_task=None,
                    model_calls=0,
                    tool_calls=0,
                    total_tokens=0,
                    elapsed_seconds=0,
                    table_pages=tools.table.pages_read,
                    table_bytes=tools.table.bytes_read,
                )
        assert result is not None
        return result, self._verify(
            client=client,
            run_id=run_id,
            started_at=started_at,
            failure=failures[0] if failures else None,
        )

    def _dependencies(self) -> tuple[LangSmithClientFactory, TraceFactory, Callable[..., Any]]:
        if self._client_factory and self._trace_factory and self._wrap_openai_fn:
            return self._client_factory, self._trace_factory, self._wrap_openai_fn
        from langsmith import Client, trace
        from langsmith.wrappers import wrap_openai

        return self._client_factory or Client, self._trace_factory or trace, self._wrap_openai_fn or wrap_openai

    def _instrument_model(
        self,
        model: ChatModel,
        *,
        client: Any,
        trace_factory: TraceFactory,
        wrap_openai_fn: Callable[..., Any],
        metadata: dict[str, Any],
        mark_incomplete: Callable[[Exception], None],
    ) -> ChatModel:
        traced_base = model
        if isinstance(model, OpenRouterModel):
            try:
                traced_base = copy.copy(model)
                traced_base.client = wrap_openai_fn(
                    model.client,
                    tracing_extra={
                        "client": client,
                        "project_name": self._config.project,
                        "metadata": metadata,
                    },
                )
            except Exception as exc:
                mark_incomplete(exc)
                traced_base = model
        return TracedModel(
            traced_base,
            trace_factory=trace_factory,
            client=client,
            project=self._config.project,
            metadata=metadata,
            mark_incomplete=mark_incomplete,
        )

    def _verify(
        self, *, client: Any, run_id: str, started_at: datetime, failure: Exception | None
    ) -> TraceEvidence:
        checked_at = datetime.now(started_at.tzinfo)
        url: str | None = None
        verification_error: Exception | None = failure
        try:
            flush = getattr(client, "flush", None)
            if callable(flush):
                flush(timeout=10)
            matches: list[Any] = []
            for attempt in range(self._verification_attempts):
                matches = list(
                    client.list_runs(
                        project_name=self._config.project,
                        run_ids=[run_id],
                        is_root=True,
                        limit=1,
                    )
                )
                if matches:
                    break
                if attempt + 1 < self._verification_attempts and self._retry_delay_seconds:
                    time.sleep(self._retry_delay_seconds)
            if not matches:
                raise LookupError("LangSmith root trace was not retrievable")
            get_url = getattr(client, "get_run_url", None)
            if callable(get_url):
                url = get_url(run=matches[0], project_name=self._config.project)
        except Exception as exc:
            verification_error = verification_error or exc
        return TraceEvidence(
            trace_id=run_id,
            url=url,
            status="available" if verification_error is None else "incomplete",
            project=self._config.project,
            started_at=started_at,
            checked_at=checked_at,
            error_type=type(verification_error).__name__ if verification_error else None,
        )

    def _incomplete(self, run_id: str, started_at: datetime, error: Exception) -> TraceEvidence:
        return TraceEvidence(
            trace_id=run_id,
            url=None,
            status="incomplete",
            project=self._config.project,
            started_at=started_at,
            checked_at=datetime.now(started_at.tzinfo),
            error_type=type(error).__name__,
        )


def _trace_invocation(invocation: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(invocation, str):
        return {"kind": "question", "question": invocation}
    return {"kind": "task", "task": dict(invocation)}


def _trace_result(result: RunResult) -> dict[str, Any]:
    return {
        "outcome": result.outcome,
        "error": result.error,
        "answer_shape": sorted(result.answer) if result.answer else None,
        "limitation_kind": result.limitation_kind,
        "limitation_reason": result.limitation_reason,
    }
