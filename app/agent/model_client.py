from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from .schemas import InvestigationRequest


class ModelClientError(RuntimeError):
    """Provider or configuration error; no tool execution belongs here."""


class ResponseModelClient(Protocol):
    def create(
        self,
        *,
        instructions: str,
        input: Any,
        tools: list[dict[str, Any]],
        model: str,
        max_output_tokens: int,
        previous_response_id: str | None = None,
    ) -> Any: ...


class OpenAIModelClient:
    """Small adapter for the installed OpenAI Responses API.

    The SDK import is intentionally lazy: fixture mode and existing local
    workflows do not require the optional provider package or an API key.
    """

    def __init__(self, client: Any | None = None, model: str | None = None) -> None:
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-5.4")
        if client is not None:
            self.client = client
            return
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - depends on deployment env
            raise ModelClientError("OpenAI Python SDK is not installed") from exc
        if not os.environ.get("OPENAI_API_KEY"):
            raise ModelClientError("OPENAI_API_KEY is not configured")
        self.client = OpenAI()

    def create(
        self,
        *,
        instructions: str,
        input: Any,
        tools: list[dict[str, Any]],
        model: str,
        max_output_tokens: int,
        previous_response_id: str | None = None,
    ) -> Any:
        try:
            kwargs: dict[str, Any] = {
                "model": model or self.model,
                "instructions": instructions,
                "input": input,
                "tools": tools,
                "parallel_tool_calls": False,
                "max_output_tokens": max_output_tokens,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "policypulse_investigation_report",
                        "schema": _report_schema(),
                        "strict": True,
                    }
                },
            }
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id
            return self.client.responses.create(**kwargs)
        except Exception as exc:
            raise ModelClientError(f"OpenAI Responses request failed: {type(exc).__name__}") from exc


def _report_schema() -> dict[str, Any]:
    from .schemas import InvestigationReport

    return InvestigationReport.model_json_schema()


@dataclass
class FixtureFunctionCall:
    type: str
    call_id: str
    name: str
    arguments: str


@dataclass
class FixtureResponse:
    id: str
    output: list[Any]
    output_text: str = ""
    usage: Any | None = None


class FixtureModelClient:
    """Deterministic model substitute that chooses dependent tools from evidence."""

    def __init__(self, request: InvestigationRequest) -> None:
        self.request = request
        self.calls: list[dict[str, Any]] = []
        self.observed_tool_results: list[dict[str, Any]] = []

    def create(
        self,
        *,
        instructions: str,
        input: Any,
        tools: list[dict[str, Any]],
        model: str,
        max_output_tokens: int,
        previous_response_id: str | None = None,
    ) -> FixtureResponse:
        del instructions, tools, model, max_output_tokens, previous_response_id
        self._observe_tool_outputs(input)
        call_index = len(self.calls)
        if call_index == 0:
            call = self._call("get_policy_change", {"watch_id": self.request.policy_change_id})
            return FixtureResponse(id=str(uuid.uuid4()), output=[call], usage={"total_tokens": 20})

        if call_index == 1:
            watch_id = self._last_data_value("watch_id")
            call = self._call("get_policy_diff", {"watch_id": watch_id, "max_changed_lines": 20})
            return FixtureResponse(id=str(uuid.uuid4()), output=[call], usage={"total_tokens": 25})

        if call_index == 2 and self.request.validated_clinical_event is not None:
            watch_id = self._last_data_value("watch_id") or self.request.policy_change_id
            call = self._call(
                "get_documentation_checklist",
                {
                    "watch_id": watch_id,
                    "clinical_trigger": self.request.validated_clinical_event.model_dump(mode="json"),
                },
            )
            return FixtureResponse(id=str(uuid.uuid4()), output=[call], usage={"total_tokens": 30})

        return self._final_response()

    def _call(self, name: str, arguments: dict[str, Any]) -> FixtureFunctionCall:
        call = FixtureFunctionCall(
            type="function_call",
            call_id=f"fixture-call-{len(self.calls) + 1}",
            name=name,
            arguments=json.dumps(arguments),
        )
        self.calls.append({"name": name, "arguments": arguments, "call_id": call.call_id})
        return call

    def _observe_tool_outputs(self, input: Any) -> None:
        if not isinstance(input, list):
            return
        for item in input:
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                try:
                    self.observed_tool_results.append(json.loads(item.get("output", "{}")))
                except json.JSONDecodeError:
                    continue

    def _last_data_value(self, key: str) -> Any:
        for result in reversed(self.observed_tool_results):
            value = ((result.get("data") or {}).get(key))
            if value is not None:
                return value
        return self.request.policy_change_id

    def _final_response(self) -> FixtureResponse:
        sources = list(self.request.authorized_policy_sources)
        report = {
            "investigation_id": self.request.investigation_id,
            "status": "complete" if self.observed_tool_results else "insufficient_evidence",
            "answer": "Investigation completed from stored PolicyPulse evidence.",
            "source_ids": sources,
            "observations": ["The fixture model selected each subsequent tool from the prior tool result."],
            "hypotheses": ["The watched policy change may affect the validated clinical workflow."],
            "uncertainty": ["This fixture does not make a coverage or clinical determination."],
            "next_steps": ["Review the deterministic CRD/DTR outputs with an authorized workflow owner."],
            "tool_call_count": len(self.calls),
        }
        return FixtureResponse(id=str(uuid.uuid4()), output=[], output_text=json.dumps(report), usage={"total_tokens": 20})

