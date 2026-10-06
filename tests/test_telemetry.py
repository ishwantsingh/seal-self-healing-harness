import json
from pathlib import Path
from types import SimpleNamespace

import mongomock

from evals.analyst.generator import load_scenarios, prepare_case
from harness.tools import AnalystTools
from self_heal.evaluation import EvaluationRunner
from self_heal.execution import RunExecutor
from self_heal.model import ModelReply, OpenRouterModel, ToolCall
from self_heal.settings import LangSmithConfig, load_config
from self_heal.storage import AtlasHistoryStore, HistoryError
from self_heal.table_store import AtlasTableStore
from self_heal.telemetry import LangSmithTelemetry, redact_trace_payload
from self_heal.evidence import safe_payload


FIXTURE = Path(__file__).resolve().parents[1] / "evals" / "analyst" / "data" / "small_inventory.json"
SCENARIOS = Path(__file__).resolve().parents[1] / "evals" / "analyst" / "scenarios.yaml"


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)

    def complete(self, messages, tools):
        return next(self.replies)


class FakeSpan:
    def __init__(self, name, kwargs):
        self.name = name
        self.kwargs = kwargs
        self.metadata = []
        self.tags = []
        self.outputs = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def add_metadata(self, metadata):
        self.metadata.append(metadata)

    def add_tags(self, tags):
        self.tags.extend(tags)

    def end(self, *, outputs):
        self.outputs.append(outputs)


class FakeTraceFactory:
    def __init__(self):
        self.spans = []

    def __call__(self, name, run_type="chain", **kwargs):
        span = FakeSpan(name, {"run_type": run_type, **kwargs})
        self.spans.append(span)
        return span


class FakeClient:
    def __init__(self, *, retrievable=True):
        self.retrievable = retrievable
        self.flushes = 0

    def flush(self, *, timeout):
        assert timeout == 10
        self.flushes += 1

    def list_runs(self, **kwargs):
        self.query = kwargs
        return [SimpleNamespace(id=kwargs["run_ids"][0])] if self.retrievable else []

    def get_run_url(self, *, run, project_name):
        return f"https://smith.test/{project_name}/{run.id}"


def test_display_evidence_redacts_sensitive_fields_and_trace_span_metadata():
    assert safe_payload({"sku": "A-100", "api_key": "sk-secret123456", "email": "private@example.com"}) == {
        "sku": "A-100", "api_key": "[REDACTED]", "email": "[REDACTED]"}
    from datetime import datetime, timedelta, timezone
    start = datetime.now(timezone.utc)
    span = SimpleNamespace(id="root", parent_run_id=None, name="analyst.run", run_type="chain",
                           start_time=start, end_time=start + timedelta(milliseconds=125),
                           error=None, extra={"metadata": {"model_id": "test-model"}},
                           total_tokens=42)
    tool = SimpleNamespace(id="tool", parent_run_id="root", name="tool.read_rows", run_type="tool",
                           start_time=start + timedelta(milliseconds=5),
                           end_time=start + timedelta(milliseconds=25), error=None, extra={},
                           total_tokens=None, inputs={"arguments": {"limit": 4, "api_key": "secret"}},
                           outputs={"result": {"rows": [{"sku": "A-100"}]}})
    class SpanClient:
        def list_runs(self, **kwargs):
            assert kwargs["trace_id"] == "root"
            return [span, tool]
    telemetry = LangSmithTelemetry(
        LangSmithConfig(enabled=True, api_key="ls-test", project="test", workspace_id=None),
        client_factory=lambda **kwargs: SpanClient(),
        trace_factory=FakeTraceFactory(), wrap_openai_fn=lambda client, **kwargs: client)
    spans = telemetry.trace_spans("root")
    assert spans[0]["name"] == "analyst.run"
    assert spans[0]["duration_ms"] == 125
    assert spans[0]["model"] == "test-model"
    assert spans[0]["tokens"] == 42
    assert spans[1]["arguments"]["api_key"] == "[REDACTED]"
    assert spans[1]["result_preview"]["rows"]["redacted_row_count"] == 1


def make_executor(*, enabled=True, retrievable=True, history=None):
    config = load_config()
    database = mongomock.MongoClient()["test"]
    tables = AtlasTableStore(database, config)
    tables.ensure_indexes()
    tables.materialize("small", json.loads(FIXTURE.read_text())["rows"])
    history = history or AtlasHistoryStore(database)
    history.ensure_indexes()
    trace_factory = FakeTraceFactory()
    fake_client = FakeClient(retrievable=retrievable)
    telemetry = LangSmithTelemetry(
        LangSmithConfig(enabled=enabled, api_key="ls-test-key", project="self-heal-test", workspace_id=None),
        client_factory=lambda **kwargs: fake_client,
        trace_factory=trace_factory,
        wrap_openai_fn=lambda client, **kwargs: client,
        retry_delay_seconds=0,
    )
    return (
        RunExecutor(history=history, telemetry=telemetry, config=config),
        tables,
        config,
        history,
        trace_factory,
        fake_client,
    )


def supported_model():
    return ScriptedModel(
        [
            ModelReply('{"metric":"available","filter_field":"warehouse","filter_value":"East"}', (), 10),
            ModelReply(
                None,
                (ToolCall("read-east", "read_rows", json.dumps({"limit": 4, "filter_field": "warehouse", "filter_value": "East"})),),
                10,
            ),
            ModelReply('{"value":18}', (), 10),
        ]
    )


def run_question(executor, tables, config, model, question):
    dataset = tables.dataset_info("small")
    return executor.run(
        model=model,
        tools=AnalystTools(tables.open_session("small"), config),
        dataset=dataset,
        invocation=question,
    )


def test_traced_run_links_one_id_across_agent_langsmith_and_atlas_with_tool_spans():
    executor, tables, config, history, trace_factory, client = make_executor()
    execution = run_question(
        executor,
        tables,
        config,
        supported_model(),
        "How many available units are in the East warehouse?",
    )
    assert execution.result.answer == {"value": 18}
    assert execution.trace.status == "available"
    assert execution.trace.trace_id == execution.result.run_id
    assert execution.trace.url.endswith(execution.result.run_id)
    assert client.flushes == 1
    root = next(span for span in trace_factory.spans if span.name == "analyst.run")
    assert root.kwargs["run_id"] == execution.result.run_id
    assert root.kwargs["metadata"]["dataset_id"] == "small"
    assert len([span for span in trace_factory.spans if span.name == "analyst.model"]) == 3
    assert [span.name for span in trace_factory.spans if span.name.startswith("tool.")] == ["tool.read_rows"]
    assert "outcome=answered" in root.tags
    record = history.get_run(execution.result.run_id)
    assert record["dataset"]["content_hash"] == tables.dataset_info("small").content_hash
    assert record["trace"]["root_id"] == execution.result.run_id
    assert record["resources"]["table_pages"] == execution.result.table_pages


def test_unsupported_question_is_a_capability_gap_trace_with_no_tool_span():
    executor, tables, config, history, trace_factory, _ = make_executor()
    execution = run_question(
        executor,
        tables,
        config,
        ScriptedModel([ModelReply('{"error":"Revenue is outside the inventory table contract"}', (), 10)]),
        "What is total revenue?",
    )
    assert execution.result.outcome == "unsupported"
    assert execution.result.limitation_kind == "capability_gap"
    assert execution.result.tool_calls == 0
    root = next(span for span in trace_factory.spans if span.name == "analyst.run")
    assert "outcome=unsupported" in root.tags
    assert "limitation_kind=capability_gap" in root.tags
    assert not [span for span in trace_factory.spans if span.name.startswith("tool.")]
    assert len([span for span in trace_factory.spans if span.name == "analyst.model"]) == 1
    record = history.get_run(execution.result.run_id)
    assert record["outcome"] == "unsupported"
    assert record["limitation_kind"] == "capability_gap"
    assert record["invocation"]["question"] == "What is total revenue?"
    assert [record["run_id"] for record in history.capability_gaps()] == [execution.result.run_id]


def test_redaction_removes_secrets_and_table_rows_before_a_trace_upload():
    raw = {
        "api_key": "sk-live-secret-value",
        "atlas_uri": "mongodb+srv://user:password@cluster.example/db",
        "result": {"rows": [{"sku": "A-100", "on_hand": 12}]},
        "message": {"content": json.dumps({"rows": [{"sku": "A-100", "on_hand": 12}]})},
        "answer": {"value": 18},
    }
    redacted = redact_trace_payload(raw)
    rendered = repr(redacted)
    assert "sk-live-secret-value" not in rendered
    assert "password" not in rendered
    assert "A-100" not in rendered
    assert redacted["result"]["rows"] == {"redacted_row_count": 1, "fields": ["on_hand", "sku"]}
    assert redacted["answer"] == {"redacted_answer_fields": ["value"]}


def test_disabled_or_unretrievable_traces_are_explicit_evidence_without_changing_answer():
    disabled_executor, tables, config, history, trace_factory, _ = make_executor(enabled=False)
    disabled = run_question(disabled_executor, tables, config, supported_model(), "How many available units are in East?")
    assert disabled.result.answer == {"value": 18}
    assert disabled.trace.status == "disabled"
    assert not trace_factory.spans
    assert "root_id" not in history.get_run(disabled.result.run_id)["trace"]

    incomplete_executor, tables, config, _, _, _ = make_executor(retrievable=False)
    incomplete = run_question(incomplete_executor, tables, config, supported_model(), "How many available units are in East?")
    assert incomplete.result.answer == {"value": 18}
    assert incomplete.trace.status == "incomplete"
    assert incomplete.trace.error_type == "LookupError"


def test_openrouter_client_is_wrapped_before_model_calls():
    trace_factory = FakeTraceFactory()
    wrapped = []
    fake_client = FakeClient()
    telemetry = LangSmithTelemetry(
        LangSmithConfig(enabled=True, api_key="ls-test-key", project="self-heal-test", workspace_id=None),
        client_factory=lambda **kwargs: fake_client,
        trace_factory=trace_factory,
        wrap_openai_fn=lambda client, **kwargs: wrapped.append((client, kwargs)) or client,
    )
    model = OpenRouterModel.__new__(OpenRouterModel)
    model.model = "openrouter-test"
    model.timeout_seconds = 45
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"metric":"on_hand"}', tool_calls=None))],
        usage=SimpleNamespace(total_tokens=2),
    )
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: completion)))
    traced = telemetry._instrument_model(
        model,
        client=fake_client,
        trace_factory=trace_factory,
        wrap_openai_fn=lambda client, **kwargs: wrapped.append((client, kwargs)) or client,
        metadata={"run_id": "run"},
        mark_incomplete=lambda exc: (_ for _ in ()).throw(exc),
    )
    assert traced.complete([{"role": "user", "content": "How many?"}], []).content == '{"metric":"on_hand"}'
    assert len(wrapped) == 1


def test_history_write_failure_does_not_change_a_completed_answer():
    class FailingHistory(AtlasHistoryStore):
        def start_run(self, record):
            raise HistoryError("offline")

    database = mongomock.MongoClient()["test"]
    failing = FailingHistory(database)
    executor, tables, config, _, _, _ = make_executor(history=failing)
    execution = run_question(executor, tables, config, supported_model(), "How many available units are in East?")
    assert execution.result.answer == {"value": 18}
    assert execution.history_status == "incomplete"


def test_traced_evaluation_persists_linked_case_trial_and_run_records():
    executor, tables, config, history, _, _ = make_executor()
    case = prepare_case(load_scenarios(SCENARIOS, config)["small-east-available"], config)
    trial = EvaluationRunner(tables, config, executor=executor, history=history).run_case(case, supported_model())
    assert trial.passed is True
    assert trial.history_status == "recorded"
    assert trial.case_id is not None
    assert trial.trace_id == trial.run_id
    assert history.get_run(trial.run_id)["case"]["case_id"] == trial.case_id
    assert history.eval_cases.count_documents({"_id": trial.case_id}) == 1
    evaluation = history.evaluations.find_one({"run_id": trial.run_id, "case_id": trial.case_id})
    assert evaluation["passed"] is True
    assert evaluation["trace_id"] == trial.trace_id
    assert evaluation["trial_id"] == trial.trial_id
