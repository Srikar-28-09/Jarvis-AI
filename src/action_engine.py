"""Validated, bounded plans for safe read-only autonomous task execution."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.secret_storage import decrypt, encrypt_plaintext
from src.tool_capabilities import ToolEffect, capabilities_for_tool

ACTION_GOAL_PREFIX = "jarvis-action-goal:v1:"
MAX_GOAL_CHARS = 4000
MAX_PLAN_STEPS = 12
MAX_PLAN_BYTES = 24000
_STEP_ID = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
_READ_ONLY_EFFECTS = frozenset({
    ToolEffect.READ_PUBLIC,
    ToolEffect.BROKERED_NETWORK_READ,
})
_APPROVAL_EFFECTS = frozenset({
    ToolEffect.WRITE_WORKSPACE,
    ToolEffect.WRITE_PRIVATE,
    ToolEffect.EXECUTE_CODE,
    ToolEffect.NETWORK_EGRESS,
    ToolEffect.EXTERNAL_SIDE_EFFECT,
    ToolEffect.UI_SIDE_EFFECT,
    ToolEffect.ADMIN_CHANGE,
    ToolEffect.DESTRUCTIVE,
})
_SECRET_VALUE = re.compile(
    r"(?i)\b(?:api[_ -]?key|password|passwd|secret|access[_ -]?token|"
    r"refresh[_ -]?token|authorization)\b\s*[:=]\s*\S+"
)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=48)
    objective: str = Field(min_length=1, max_length=600)
    dependencies: list[str] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)
    tools: list[str] = Field(min_length=1, max_length=5)
    expected_output: str = Field(min_length=1, max_length=400)
    verification: str = Field(min_length=1, max_length=300)
    timeout_seconds: int = Field(default=120, ge=1, le=300)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _STEP_ID.fullmatch(value):
            raise ValueError("step id must contain only letters, numbers, _ or -")
        return value

    @field_validator("tools", "dependencies")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("list entries must be unique")
        return values


class GeneratedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation: str = Field(min_length=1, max_length=800)
    clarification_question: str | None = Field(default=None, max_length=500)
    completion_criteria: list[str] = Field(min_length=1, max_length=5)
    steps: list[PlanStep] = Field(max_length=MAX_PLAN_STEPS)


def available_tool_catalog() -> list[dict[str, Any]]:
    """Expose only statically registered tools with known capability metadata."""
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    catalog = []
    seen = set()
    for schema in FUNCTION_TOOL_SCHEMAS:
        function = schema.get("function", {})
        name = function.get("name")
        if not isinstance(name, str) or name in seen:
            continue
        capability = capabilities_for_tool(name)
        if not capability.known:
            continue
        seen.add(name)
        catalog.append({
            "name": name,
            "effects": sorted(effect.value for effect in capability.effects),
        })
    return catalog


def parse_plan(raw: str | dict[str, Any]) -> GeneratedPlan:
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Planner response was not valid JSON.") from exc
    try:
        plan = GeneratedPlan.model_validate(raw)
    except ValidationError as exc:
        raise ValueError("Planner response did not match the plan schema.") from exc

    if len(plan.model_dump_json().encode("utf-8")) > MAX_PLAN_BYTES:
        raise ValueError("Generated plan exceeds the size limit.")
    if plan.clarification_question:
        if plan.steps:
            raise ValueError("Clarification plans cannot contain executable steps.")
        return plan
    if not plan.steps:
        raise ValueError("Plan must contain at least one step.")

    step_ids = [step.id for step in plan.steps]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("Plan contains duplicate step identifiers.")
    known_tools = {item["name"] for item in available_tool_catalog()}
    seen_steps: set[str] = set()
    for step in plan.steps:
        if not set(step.dependencies) <= set(step_ids):
            raise ValueError(f"Step {step.id} refers to an unknown dependency.")
        if step.id in step.dependencies:
            raise ValueError(f"Step {step.id} cannot depend on itself.")
        if not set(step.dependencies) <= seen_steps:
            raise ValueError("Steps must be ordered after their dependencies.")
        if not set(step.tools) <= known_tools:
            raise ValueError(f"Step {step.id} refers to an unsupported tool.")
        for tool in step.tools:
            capability = capabilities_for_tool(tool)
            if not capability.known or not capability.effects:
                raise ValueError(f"Tool {tool} has no trusted capability classification.")
        seen_steps.add(step.id)
    return plan


def step_effects(step: PlanStep) -> frozenset[ToolEffect]:
    effects: set[ToolEffect] = set()
    for tool in step.tools:
        effects.update(capabilities_for_tool(tool).effects)
    return frozenset(effects)


def plan_requires_approval(plan: GeneratedPlan) -> bool:
    return any(step_effects(step) & _APPROVAL_EFFECTS for step in plan.steps)


def plan_is_read_only(plan: GeneratedPlan) -> bool:
    return bool(plan.steps) and all(
        step_effects(step) and step_effects(step) <= _READ_ONLY_EFFECTS
        for step in plan.steps
    )


def format_checklist(plan: GeneratedPlan, completed: set[str] | None = None) -> str:
    completed = completed or set()
    return "\n".join(
        f"- [{'x' if step.id in completed else ' '}] {step.id}: {step.objective}"
        for step in plan.steps
    )


def encode_task_prompt(goal: str, plan: GeneratedPlan, *, goal_id: str | None = None) -> str:
    payload = {
        "version": 1,
        "goal_id": goal_id or uuid.uuid4().hex,
        "goal": goal.strip(),
        "plan": plan.model_dump(mode="json"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return ACTION_GOAL_PREFIX + encrypt_plaintext(json.dumps(payload, separators=(",", ":")))


def decode_task_prompt(value: str | None) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.startswith(ACTION_GOAL_PREFIX):
        return None
    from src.secret_storage import SecretStorageError

    try:
        payload = json.loads(decrypt(value[len(ACTION_GOAL_PREFIX):]))
    except (SecretStorageError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Stored action goal could not be authenticated.") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Stored action goal has an unsupported format.")
    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL_CHARS:
        raise ValueError("Stored action goal is invalid.")
    return payload


def preview_payload(goal_id: str, goal: str, plan: GeneratedPlan) -> dict[str, Any]:
    approval_required = plan_requires_approval(plan)
    return {
        "goal_id": goal_id,
        "goal": goal,
        "interpretation": plan.interpretation,
        "clarification_question": plan.clarification_question,
        "completion_criteria": plan.completion_criteria,
        "steps": [
            {
                **step.model_dump(),
                "effects": sorted(effect.value for effect in step_effects(step)),
                "approval_required": bool(step_effects(step) & _APPROVAL_EFFECTS),
            }
            for step in plan.steps
        ],
        "requires_approval": approval_required,
        "executable_here": plan_is_read_only(plan) and not approval_required,
    }


async def generate_plan(goal: str, owner: str | None) -> GeneratedPlan:
    if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL_CHARS:
        raise ValueError(f"Goal must be 1-{MAX_GOAL_CHARS} characters.")
    if _SECRET_VALUE.search(goal):
        raise ValueError("Remove credentials or secret values before planning.")

    from core.database import Session as DbSession, SessionLocal
    from src.endpoint_resolver import build_headers
    from src.llm_core import llm_call_async
    from src.task_endpoint import resolve_task_endpoint

    db = SessionLocal()
    try:
        query = db.query(DbSession).filter(
            DbSession.endpoint_url.isnot(None),
            DbSession.model.isnot(None),
        )
        query = (
            query.filter(DbSession.owner == owner)
            if owner
            else query.filter(DbSession.owner.is_(None))
        )
        session = query.order_by(DbSession.last_accessed.desc()).first()
        fallback_url = session.endpoint_url if session else None
        fallback_model = session.model if session else None
        fallback_headers = session.headers if session else None
    finally:
        db.close()

    url, model, headers = resolve_task_endpoint(
        fallback_url=fallback_url,
        fallback_model=fallback_model,
        fallback_headers=fallback_headers,
        owner=owner,
    )
    if not url or not model:
        raise RuntimeError("No explicitly configured planning model is available.")

    from src.interactive_gate import wait_for_interactive_quiet

    catalog = available_tool_catalog()
    await wait_for_interactive_quiet("action goal planning")
    response = await llm_call_async(
        url=url,
        model=model,
        headers=headers or build_headers(None, url),
        messages=[
            {
                "role": "system",
                "content": (
                    "Create a concise execution plan for the user's goal. Return "
                    "only JSON with keys interpretation, clarification_question, "
                    "completion_criteria, and steps. Each step must have id, "
                    "objective, dependencies, tools, expected_output, verification, "
                    "timeout_seconds. Dependencies must refer only to earlier steps. "
                    "Use the supplied tool names only; list tools that Jarvis should "
                    "use, never invent arguments or shell commands. If materially "
                    "ambiguous, return a clarification_question and an empty steps "
                    "array. Keep plans within 12 steps. Do not include secrets.\n\n"
                    f"Registered tools and trusted effects:\n"
                    f"{json.dumps(catalog, separators=(',', ':'))}"
                ),
            },
            {"role": "user", "content": goal.strip()},
        ],
        temperature=0.1,
        max_tokens=2200,
        timeout=60,
        workload="background",
    )
    return parse_plan(response)
