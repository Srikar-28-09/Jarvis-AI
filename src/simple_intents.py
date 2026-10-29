"""Small deterministic answers and low-effort turn classification.

This module deliberately recognizes only closed, context-independent forms.
Unrecognized, contextual, and action-like requests continue through the normal
chat/agent path.
"""

from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext
from typing import Callable

from src.user_time import now_user_local

SIMPLE_REPLY_MAX_TOKENS = 192
DETAIL_REPLY_MIN_TOKENS = 1024


@dataclass(frozen=True)
class AnswerLengthPolicy:
    kind: str
    min_tokens: int
    instruction: str


_MARK_ANSWER_RE = re.compile(
    r"\b(?:(?P<marks>\d{1,2})\s*[- ]?\s*marks?\s+(?:answer|response|write[- ]?up)\b|"
    r"(?:answer|respond|write|explain|give(?:\s+me)?).{0,64}?\b(?P<marks_after>\d{1,2})\s*[- ]?\s*marks?\b)",
    re.IGNORECASE,
)
_EXAM_DETAIL_RE = re.compile(
    r"\b(?:detailed|comprehensive|thorough)\s+(?:exam\s+)?answer\b|"
    r"\b(?:exam\s+)?answer\s+in\s+detail\b",
    re.IGNORECASE,
)
_EXPLICIT_DETAIL_RE = re.compile(
    r"\b(?:in\s+detail|detailed|comprehensive|thorough|elaborate|expand)\b",
    re.IGNORECASE,
)
_CODE_REQUEST_RE = re.compile(
    r"\b(?:write|create|generate|debug|implement|fix)\b.{0,48}\b(?:code|program|script|function|class)\b",
    re.IGNORECASE,
)
_ACADEMIC_SECTION_PATTERNS = (
    ("definition", re.compile(r"\b(?:definition|define)\b", re.IGNORECASE)),
    ("architecture", re.compile(r"\barchitecture\b", re.IGNORECASE)),
    ("components", re.compile(r"\bcomponents?\b", re.IGNORECASE)),
    ("working", re.compile(r"\bworking\b|\bhow\s+it\s+works\b", re.IGNORECASE)),
    ("example", re.compile(r"\bexamples?\b", re.IGNORECASE)),
    ("advantages", re.compile(r"\badvantages?\b", re.IGNORECASE)),
    ("disadvantages", re.compile(r"\bdisadvantages?\b", re.IGNORECASE)),
    ("conclusion", re.compile(r"\bconclusions?\b", re.IGNORECASE)),
)


def _requested_academic_sections(text: str) -> list[str]:
    found = []
    for title, pattern in _ACADEMIC_SECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            found.append((match.start(), title))
    return [title for _, title in sorted(found)]


def requested_answer_format_instruction(message: str) -> str | None:
    """Return a small, turn-scoped instruction for explicit output formats."""
    text = " ".join(str(message or "").strip().split())
    instructions = []
    if re.search(r"\b(?:in|as|use)\s+(?:a\s+|an\s+)?table\b|\btable format\b", text, re.IGNORECASE):
        instructions.append(
            "Present the requested comparison in a table, with clear columns for each item "
            "and the comparison dimensions."
        )
    if re.search(r"\b(?:real[- ]world|practical) example\b", text, re.IGNORECASE):
        instructions.append(
            "Explain how the real-world example demonstrates the concept; do not only name a company."
        )
    if not instructions:
        return None
    return " ".join(instructions)


def answer_length_policy(message: str) -> AnswerLengthPolicy | None:
    """Recognize explicit answer-length requests without another model call."""
    text = " ".join(str(message or "").strip().split())
    mark_match = _MARK_ANSWER_RE.search(text)
    if mark_match:
        marks = int(mark_match.group("marks") or mark_match.group("marks_after"))
        if 1 <= marks <= 20:
            # A rough per-mark allowance provides room for a real exam answer
            # without imposing a fixed word count across subjects.
            token_budget = min(2560, max(384, marks * 128))
            requested_sections = _requested_academic_sections(text)
            section_instruction = (
                " Use these requested headings in the same order, and include every one: "
                + "; ".join(requested_sections)
                + "."
                if requested_sections
                else ""
            )
            instruction = (
                f"Answer the exact requested topic as a developed {marks}-mark exam response. "
                "Keep each requested section focused and concise while giving enough explanation "
                "for an exam answer. Do not replace the topic with a broader parent subject. "
                "When an example is requested, explain how it demonstrates the concept rather "
                "than only naming an organization. Avoid filler, repeated definitions, and padding."
                + section_instruction
            )
            return AnswerLengthPolicy("marks", token_budget, instruction)
    if _EXAM_DETAIL_RE.search(text) or _EXPLICIT_DETAIL_RE.search(text):
        return AnswerLengthPolicy(
            "detailed",
            DETAIL_REPLY_MIN_TOKENS,
            "The user explicitly requests a detailed answer. Develop the explanation with "
            "clear organization and relevant examples or distinctions. Stay on the exact "
            "requested topic and cover its requested scope without padding or repetition. "
            "When asked to expand an earlier answer, add meaningful detail instead of "
            "repeating the existing explanation.",
        )
    return None


_TIME_RE = re.compile(
    r"^(?:what(?:'s| is) (?:the )?(?:current )?time(?: is it)?(?: now| right now)?|"
    r"what time(?: is it)?(?: now| right now)?|current time|time now|tell me (?:the )?time(?: now| right now)?)"
    r"(?: in my timezone| locally)?[?.!]*$",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"^(?:what(?:'s| is) (?:today'?s )?(?:date|day)|"
    r"what day is it(?: today)?|what date is it(?: today)?|today'?s date)\s*[?.!]*$",
    re.IGNORECASE,
)
_GREETING_REPLIES = {
    "hi": "Hi, I’m here.",
    "hello": "Hello, I’m here.",
    "hey": "Hey, I’m here.",
    "good morning": "Good morning.",
    "good afternoon": "Good afternoon.",
    "good evening": "Good evening.",
    "thanks": "You’re welcome.",
    "thank you": "You’re welcome.",
    "thanks jarvis": "You’re welcome.",
}
_MATH_RE = re.compile(r"^[\d\s()+\-*/%.^]+$")
_MATH_PREFIX_RE = re.compile(r"^(?:(?:what is|what's|calculate|compute)\s+)?(.+?)\s*[?!.]*$", re.I)
_COMPLEXITY_RE = re.compile(
    r"\b(?:analy[sz]e|compare|contrast|derive|design|debug|detailed?|"
    r"explain|investigate|prove|reason|step[- ]by[- ]step|walk me through|"
    r"write|draft|plan|review|summari[sz]e|research|evaluate|tradeoffs?)\b"
    r"|\bwhy\b|\bhow (?:does|do|would|could|should|can)\b|"
    r"\bwhat if\b|\bsuppose\b|\bscenario\b|\bimpact\b",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r"\b(?:open|launch|close|click|type|send|delete|remove|clear|erase|reset|"
    r"save|overwrite|install|uninstall|create|make|edit|change|set|book|reserve|"
    r"purchase|buy|order|pay|post|publish|share|reply|forward|archive|mark|"
    r"schedule|remind|search|browse|navigate|visit|focus|manage|toggle|enable|"
    r"disable|switch|download|upload|run|execute|start|stop|turn on|turn off)\b",
    re.IGNORECASE,
)
_BOUNDED_EDUCATIONAL_RE = re.compile(
    r"\b(?:explain|describe)\s+(?:simply|briefly)\b|"
    r"\b(?:explain|describe)\s+in\s+(?:one|two|three|1|2|3)\s+"
    r"(?:(?:simple|short|brief)\s+)?sentences?\b|"
    r"\bin\s+(?:one|two|three|1|2|3)\s+(?:(?:simple|short|brief)\s+)?sentences?\b",
    re.IGNORECASE,
)
_EDUCATIONAL_PROMPT_RE = re.compile(
    r"^(?:what|who|where|when|which|define|explain|describe|give\s+(?:a\s+)?definition\b)",
    re.IGNORECASE,
)

_BINARY_OPERATORS: dict[type[ast.operator], Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}


def _arithmetic_value(expression: str) -> Decimal | None:
    if len(expression) > 80 or not _MATH_RE.fullmatch(expression):
        return None
    try:
        tree = ast.parse(expression.replace("^", "**"), mode="eval")
    except (SyntaxError, ValueError):
        return None
    nodes = list(ast.walk(tree))
    if len(nodes) > 32:
        return None

    def visit(node) -> Decimal:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Decimal(str(node.value))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow):
                if right != right.to_integral_value() or abs(right) > 10 or abs(left) > 1_000_000:
                    raise ValueError("power is out of range")
                return left ** int(right)
            operation = _BINARY_OPERATORS.get(type(node.op))
            if operation is None:
                raise ValueError("unsupported operator")
            return operation(left, right)
        raise ValueError("unsupported expression")

    try:
        with localcontext() as context:
            context.prec = 28
            result = visit(tree)
        if not result.is_finite() or abs(result) > Decimal("1e24"):
            return None
        return result
    except (ArithmeticError, DivisionByZero, InvalidOperation, ValueError, OverflowError):
        return None


def _format_number(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def answer_simple_request(message: str, *, now: datetime | None = None) -> str | None:
    """Return a concise deterministic reply, or ``None`` when not certain."""
    text = " ".join(str(message or "").strip().split())
    if not text or len(text) > 120:
        return None
    normalized = re.sub(r"[,?.!]+", " ", text.casefold()).strip()
    if normalized.endswith(" jarvis"):
        normalized = normalized[:-7].strip()

    if _TIME_RE.fullmatch(text):
        local = now_user_local() if now is None else now.astimezone(now_user_local().tzinfo)
        hour = local.strftime("%I").lstrip("0") or "12"
        return f"It’s {hour}:{local.minute:02d} {local.strftime('%p')}, Sir."
    if _DATE_RE.fullmatch(text):
        local = now_user_local() if now is None else now.astimezone(now_user_local().tzinfo)
        return f"It’s {local.strftime('%A, %B')} {local.day}, Sir."

    greeting = _GREETING_REPLIES.get(normalized)
    if greeting:
        return greeting

    match = _MATH_PREFIX_RE.fullmatch(text)
    expression = match.group(1).strip() if match else ""
    if expression and _MATH_RE.fullmatch(expression):
        result = _arithmetic_value(expression)
        return _format_number(result) if result is not None else None
    return None


def is_low_effort_conversation(message: str) -> bool:
    """Whether a Qwen-family model can answer without extended reasoning.

    This affects only model thinking mode. It never bypasses tool dispatch or
    action policy; action-like and explicitly complex prompts retain reasoning.
    """
    text = " ".join(str(message or "").strip().split())
    length_policy = answer_length_policy(text)
    intent_text = _MARK_ANSWER_RE.sub(" ", text) if length_policy and length_policy.kind == "marks" else text
    if not text or len(text) > 240 or _ACTION_RE.search(intent_text):
        return False

    if length_policy and length_policy.kind == "marks" and not _CODE_REQUEST_RE.search(text):
        complexity_matches = list(_COMPLEXITY_RE.finditer(text))
        # An exam-answer format by itself does not require extended reasoning.
        # Keep the normal reasoning path for analysis, proof, design, coding,
        # and tool/action requests.
        if (
            len(text.split()) <= 80
            and all(match.group(0).casefold() in {"explain", "write", "describe"} for match in complexity_matches)
        ):
            return True

    if len(text.split()) > 32:
        return False

    complexity_matches = list(_COMPLEXITY_RE.finditer(text))
    if not complexity_matches:
        return True

    # “Explain” normally keeps a request on the reasoning path. Allow it only
    # for a short educational question with an explicit simple/sentence bound;
    # all other complexity terms and action intents remain disqualifying.
    bounded_educational = (
        _EDUCATIONAL_PROMPT_RE.search(text) is not None
        and _BOUNDED_EDUCATIONAL_RE.search(text) is not None
        and all(match.group(0).casefold() in {"explain", "describe"} for match in complexity_matches)
    )
    return bounded_educational


def reply_token_limit(requested: int | None, message: str, *, preset_id=None) -> int | None:
    """Honor explicit length requests before applying the simple-answer cap."""
    length_policy = answer_length_policy(message)
    try:
        requested_value = int(requested or 0)
    except (TypeError, ValueError):
        requested_value = 0
    if length_policy:
        return max(requested_value, length_policy.min_tokens)
    if preset_id or not is_low_effort_conversation(message):
        return requested
    return min(requested_value, SIMPLE_REPLY_MAX_TOKENS) if requested_value > 0 else SIMPLE_REPLY_MAX_TOKENS

