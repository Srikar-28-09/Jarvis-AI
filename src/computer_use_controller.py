"""
src/computer_use_controller.py

Safe Closed-Loop Computer-Use / GUI Agent Controller for Jarvis.

Orchestrates the loop:
  User Goal
     ↓
  Screenshot Capture
     ↓
  Qwen2.5-VL Vision Interpretation (active windows, UI elements, coordinates, text)
     ↓
  Qwen3:8b Planning (selects exactly ONE action or finish)
     ↓
  Safety & Sensitive-Action Gate (bounds check, allowlist, confirmation if sensitive)
     ↓
  Single Action Execution
     ↓
  Post-Action Screenshot Capture
     ↓
  Qwen2.5-VL Vision Verification (verifies expected visual state change)
     ↓
  Repeat until completion or safety/budget stop

Security & Safety Principles:
- Strictly ONE discrete action per loop cycle.
- Hard action budget, iteration budget, and wall-clock timeout.
- Dynamic screen coordinate boundary validation.
- Sensitive-action detection and confirmation gate (payments, emails/messages, deletion, credentials, OTPs).
- Absolute prohibition of shell, PowerShell, cmd, registry modifications, and credential theft.
- No blind guessing when vision is unconfident.
- Automatic stop on repeated failures or verification failures.
- Structured iteration logging with credential/secret redaction.
- Non-leaking UI progress streaming.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import urllib.error
import urllib.parse
import urllib.request

from host_bridge.actions import KEY_ALLOWLIST, get_screen_bounds, normalize_app_name
from host_bridge.config import APPLICATION_ALLOWLIST, DANGEROUS_URL_SCHEMES, SAFE_URL_SCHEMES
from src.tool_capabilities import ToolEffect, canonicalize_navigation_url, capabilities_for_tool
from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, _PLAN_MODE_KNOWN_MUTATORS
from src.vision_analyzer import (
    DEFAULT_OLLAMA_URL,
    DEFAULT_VISION_MODEL,
    analyze_and_verify,
    analyze_screenshot,
    check_model_resident,
    get_ollama_base_url,
    normalize_ollama_base_url,
    unload_model_sync,
)

def is_computer_use_enabled() -> bool:
    """Check if safe closed-loop computer use is enabled via environment or config."""
    val = os.environ.get("COMPUTER_USE_ENABLED", "true").strip().lower()
    return val in ("true", "1", "yes", "on", "enabled")

def get_computer_use_handlers() -> Dict[str, Callable]:
    """Retrieve executable handlers for computer control tools cleanly and directly."""
    from src.agent_tools.computer_control_tools import (
        OpenApplicationTool,
        OpenUrlTool,
        ClickTool,
        DoubleClickTool,
        TypeTextTool,
        PressKeyTool,
        ScrollTool,
        TakeScreenshotTool,
    )
    return {
        "open_application": OpenApplicationTool().execute,
        "open_url": OpenUrlTool().execute,
        "click": ClickTool().execute,
        "double_click": DoubleClickTool().execute,
        "type_text": TypeTextTool().execute,
        "press_key": PressKeyTool().execute,
        "scroll": ScrollTool().execute,
        "take_screenshot": TakeScreenshotTool().execute,
        "screenshot": TakeScreenshotTool().execute,
    }

logger = logging.getLogger("src.computer_use_controller")

# ---------------------------------------------------------------------------
# Constants & Allowable Actions
# ---------------------------------------------------------------------------

ALLOWED_COMPUTER_USE_ACTIONS: Set[str] = {
    "open_application",
    "open_url",
    "click",
    "double_click",
    "type_text",
    "press_key",
    "scroll",
    "take_screenshot",
    "screenshot",
    "finish",
}

COMPUTER_USE_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": "Launch a Windows application from the allowlist (e.g. Notepad, Chrome, VS Code, Calculator, File Explorer, Windows Terminal).",
            "parameters": {
                "type": "object",
                "properties": {
                    "application": {"type": "string", "description": "Application name to launch (e.g. 'notepad')"}
                },
                "required": ["application"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open a safe http:// or https:// URL in the default browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Valid http:// or https:// URL"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click at specific screen coordinates (x, y) on the Windows desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate in pixels"},
                    "y": {"type": "integer", "description": "Y coordinate in pixels"}
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "double_click",
            "description": "Double click at specific screen coordinates (x, y) on the Windows desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate in pixels"},
                    "y": {"type": "integer", "description": "Y coordinate in pixels"}
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into the currently focused window or element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type (max 500 characters)"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a key from allowlist (ENTER, TAB, ESCAPE, BACKSPACE, DELETE, SPACE, UP, DOWN, LEFT, RIGHT).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Key name to press (e.g. 'ENTER')"}
                },
                "required": ["key"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the mouse wheel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
                    "amount": {"type": "integer", "description": "Scroll steps between 1 and 50"}
                },
                "required": ["direction", "amount"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Capture a real screenshot of the current desktop screen via the Host Bridge. Note: The current desktop is already captured and analyzed in CURRENT SCREEN STATE; call finish with your summary description if your goal was to inspect or describe the screen.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Capture a real screenshot of the current desktop screen via the Host Bridge (alias for take_screenshot). Note: The current desktop is already captured and analyzed in CURRENT SCREEN STATE; call finish with your summary description if your goal was to inspect or describe the screen.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Signal that the user goal has been fully satisfied and verified on screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Summary explaining how the goal was accomplished"}
                },
                "required": ["summary"]
            }
        }
    }
]

DEFAULT_PLANNER_MODEL = "qwen3:8b"

# Sensitive keyword patterns that require explicit user confirmation
SENSITIVE_PATTERNS = {
    "purchase_or_payment": [
        r"\b(buy|purchase|checkout|pay|payment|order|subscribe|billing|cart)\b",
        r"\b(credit\s*card|debit\s*card|cvv|expir)\b",
    ],
    "send_message_or_email": [
        r"\b(send\s*(?:email|message|sms|tweet|dm)|submit\s*form|post\s*(?:comment|update|tweet|message)|publish)\b",
    ],
    "delete_data_or_files": [
        r"\b(delete|remove|erase|wipe|format|drop\s*table|purge|rm\s+-rf|destroy|overwrite|truncate|clobber)\b",
    ],
    "security_or_account": [
        r"\b(change\s*password|reset\s*password|api[_\s]*key|private[_\s]*key|secret|credential|oauth|2fa|mfa)\b",
    ],
    "passwords_or_otps": [
        r"\b(password|passcode|pin|otp|verification\s*code|one-time\s*password|auth\s*code)\b",
    ],
    "financial": [
        r"\b(bank|wire\s*transfer|crypto|bitcoin|ethereum|wallet|wire|funds\s*transfer)\b",
    ],
}

# Forbidden command terms in text or arguments
FORBIDDEN_EXEC_TERMS = [
    "powershell",
    "cmd.exe",
    "cmd /c",
    "bash",
    "sh -c",
    "rmdir /s",
    "regedit",
    "reg.exe",
    "certutil",
    "vssadmin",
]


# ---------------------------------------------------------------------------
# Configuration & State Models
# ---------------------------------------------------------------------------

@dataclass
class ComputerUseConfig:
    """Configurable safety limits and model parameters for closed-loop execution."""
    max_actions: int = 15
    max_iterations: int = 8
    max_duration_seconds: float = 180.0
    max_consecutive_failures: int = 3
    max_repeated_identical_actions: int = 2
    planner_model: str = DEFAULT_PLANNER_MODEL
    vision_model: str = DEFAULT_VISION_MODEL
    ollama_url: str = DEFAULT_OLLAMA_URL
    keep_alive: str = "5m"
    require_confirmation_for_sensitive: bool = True
    plan_mode: bool = False
    confidence_threshold: float = 0.6
    action_pause_seconds: float = 0.5
    planner_num_predict: int = 384
    planner_num_ctx: int = 4096
    planner_timeout_seconds: float = 90.0


@dataclass
class StepRecord:
    """Record of a single cycle in the closed-loop computer use state machine."""
    iteration: int
    timestamp: float
    screenshot_file: Optional[str] = None
    vision_summary: Dict[str, Any] = field(default_factory=dict)
    planner_thought: str = ""
    proposed_action: Dict[str, Any] = field(default_factory=dict)
    is_sensitive: bool = False
    sensitive_reason: Optional[str] = None
    confirmation_status: str = "none"  # "none", "approved", "rejected", "pending"
    tool_result: Dict[str, Any] = field(default_factory=dict)
    post_screenshot_file: Optional[str] = None
    verification: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    planning_latency_seconds: float = 0.0
    vision_latency_seconds: float = 0.0
    action_latency_seconds: float = 0.0
    verification_latency_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class ComputerUseState:
    """Complete audit state of a closed-loop computer-use session."""
    goal: str
    session_id: str
    status: str = "IDLE"  # IDLE, RUNNING, AWAITING_CONFIRMATION, COMPLETED, FAILED, STOPPED_BY_BUDGET, STOPPED_BY_TIMEOUT, VERIFICATION_FAILED, SENSITIVE_ACTION_BLOCKED, STOPPED_BY_PLAN_MODE
    iteration: int = 0
    action_count: int = 0
    consecutive_failures: int = 0
    start_time: float = 0.0
    elapsed_seconds: float = 0.0
    current_screenshot: Optional[Dict[str, Any]] = None
    previous_screenshot: Optional[Dict[str, Any]] = None
    last_proposed_action: Optional[Dict[str, Any]] = None
    last_executed_action: Optional[Dict[str, Any]] = None
    last_action_result: Optional[Dict[str, Any]] = None
    last_verification: Optional[Dict[str, Any]] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    pending_confirmation: Optional[Dict[str, Any]] = None
    approved_action: Optional[Dict[str, Any]] = None
    summary: Optional[str] = None
    error: Optional[str] = None
    model_swap_count: int = 0
    total_planning_seconds: float = 0.0
    total_vision_seconds: float = 0.0
    total_action_seconds: float = 0.0
    total_verification_seconds: float = 0.0
    total_screenshot_seconds: float = 0.0
    total_image_transfer_seconds: float = 0.0
    total_image_encoding_seconds: float = 0.0
    total_model_load_seconds: float = 0.0
    total_model_inference_seconds: float = 0.0
    total_model_transition_seconds: float = 0.0
    total_approval_wait_seconds: float = 0.0
    approval_retry_count: int = 0
    total_model_swap_seconds: float = 0.0
    last_active_model: Optional[str] = None
    planned_action_queue: List[Dict[str, Any]] = field(default_factory=list)
    current_visual_data: Dict[str, Any] = field(default_factory=dict)
    model_invocations: List[Dict[str, Any]] = field(default_factory=list)
    # FIX #1: Track the last action that failed verification to prevent repeats
    last_failed_action: Optional[Dict[str, Any]] = None
    last_failed_action_os_succeeded: bool = False
    # Goal-level final visual verification status
    final_visual_verification_passed: bool = False
    # Duplicate prevention safety invariants
    launched_applications: Set[str] = field(default_factory=set)
    # Tracks attempts, including failures/timeouts, so one run never retries a launch.
    application_launch_attempts: Set[str] = field(default_factory=set)
    # URL actions are tracked separately from application launches. Each URL
    # remains reserved for this run after dispatch, including uncertain outcomes.
    navigation_attempts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    action_signature_counts: Dict[str, int] = field(default_factory=dict)
    active_application: Optional[str] = None
    active_hwnd: Optional[int] = None
    active_pid: Optional[int] = None
    type_text_retry_count: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)



# ---------------------------------------------------------------------------
# Redaction & Logging Helpers
# ---------------------------------------------------------------------------

def redact_sensitive_data(text: Any) -> Any:
    """Scrub sensitive credentials, tokens, and authorization headers from logs and UI streams."""
    sensitive_keys = {"password", "passwd", "pwd", "token", "secret", "otp", "pin", "cvv", "api_key", "apikey", "authorization", "image_base64"}
    if isinstance(text, dict):
        redacted_dict = {}
        for k, v in text.items():
            if str(k).lower() in sensitive_keys:
                redacted_dict[k] = "[REDACTED]"
            else:
                redacted_dict[k] = redact_sensitive_data(v)
        return redacted_dict
    if isinstance(text, list):
        return [redact_sensitive_data(item) for item in text]
    if not isinstance(text, str):
        return text

    scrubbed = text
    # Bearer tokens
    scrubbed = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]{10,}", "Bearer [REDACTED]", scrubbed, flags=re.IGNORECASE)
    # Generic key=value or key:value
    scrubbed = re.sub(
        r"(\b(?:key|token|secret|password|passwd|pwd|otp|pin|cvv|auth)\b\s*[:=]\s*)(['\"][^'\"]*['\"]|[^\s,;]+)",
        r"\1[REDACTED]",
        scrubbed,
        flags=re.IGNORECASE,
    )
    return scrubbed


# ---------------------------------------------------------------------------
# Sensitive-Action & Safety Classification
# ---------------------------------------------------------------------------

def is_sensitive_action(action: str, params: Dict[str, Any], goal: str) -> Tuple[bool, Optional[str]]:
    """Determine if a proposed action or goal touches sensitive user data or operations."""
    action_corpus = f"{action} {json.dumps(params)}".lower()
    goal_text = str(goal or "").lower()

    # Respect explicit negative constraints in the user goal. Split on a
    # contrastive conjunction so "don't delete X, but delete Y" still flags Y.
    positive_goal_clauses = re.split(r"\b(?:but|however|except)\b|[.!?;]", goal_text)
    positive_goal = " ".join(
        clause for clause in positive_goal_clauses
        if not re.search(r"\b(?:do\s+not|don't|never|must\s+not|mustn't|should\s+not|shouldn't|will\s+not|avoid|without|no)\b", clause)
    )
    text_corpus = f"{action_corpus} {positive_goal}"

    # Explicit credential theft/inference attempt -> immediate safety violation
    if any(re.search(pat, text_corpus, re.IGNORECASE) for pat in SENSITIVE_PATTERNS["passwords_or_otps"]):
        if any(term in text_corpus for term in ["password", "otp", "pin", "passcode", "cvv", "2fa", "verification code"]):
            return True, "Operation involves password, PIN, or OTP credential handling"

    # Purchases / Payments
    for pat in SENSITIVE_PATTERNS["purchase_or_payment"]:
        if re.search(pat, text_corpus, re.IGNORECASE):
            return True, "Operation involves financial purchase, payment, or checkout"

    # Sending messages / forms / emails
    for pat in SENSITIVE_PATTERNS["send_message_or_email"]:
        if re.search(pat, text_corpus, re.IGNORECASE):
            return True, "Operation involves sending external communications or submitting forms"

    # File/data deletion
    # Destructive tokens in the actual action/arguments always remain gated.
    # Goal wording is considered only after explicit negative clauses above
    # have been removed.
    for pat in SENSITIVE_PATTERNS["delete_data_or_files"]:
        if re.search(pat, action_corpus, re.IGNORECASE) or re.search(pat, positive_goal, re.IGNORECASE):
            return True, "Operation involves deleting data or files"

    # Security/account modifications
    for pat in SENSITIVE_PATTERNS["security_or_account"]:
        if re.search(pat, text_corpus, re.IGNORECASE):
            return True, "Operation modifies security settings or authentication tokens"

    # Financial operations
    for pat in SENSITIVE_PATTERNS["financial"]:
        if re.search(pat, text_corpus, re.IGNORECASE):
            return True, "Operation involves banking or financial account access"

    return False, None


def normalize_action_payload(action: Optional[str], params: Optional[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """Normalize an action name and its parameters into a canonical representation for robust comparison."""
    norm_act = str(action or "").strip().lower()
    if not isinstance(params, dict):
        return norm_act, {}

    if norm_act == "open_application":
        raw_app = str(params.get("application") or params.get("app_name") or params.get("app") or params.get("name") or "").strip()
        app_clean = raw_app.lower()
        if app_clean.endswith(".exe"):
            app_clean = app_clean[:-4].strip()
        app_entry = APPLICATION_ALLOWLIST.get(app_clean)
        if app_entry:
            app_clean = normalize_app_name(str(app_entry.get("name") or app_clean))
        return "open_application", {"application": app_clean}

    if norm_act in ("click", "double_click"):
        try:
            x = int(params.get("x", 0))
            y = int(params.get("y", 0))
            return norm_act, {"x": x, "y": y}
        except (ValueError, TypeError):
            return norm_act, dict(params)

    if norm_act == "type_text":
        return "type_text", {"text": str(params.get("text", ""))}

    if norm_act == "press_key":
        return "press_key", {"key": str(params.get("key", "")).strip().upper()}

    if norm_act == "scroll":
        try:
            amount = int(params.get("amount", 5))
        except (ValueError, TypeError):
            amount = 5
        return "scroll", {
            "direction": str(params.get("direction", "")).strip().lower(),
            "amount": amount,
        }

    if norm_act == "open_url":
        return "open_url", {"url": str(params.get("url", "")).strip()}

    if norm_act in ("take_screenshot", "screenshot"):
        query = str(params.get("query") or params.get("prompt") or params.get("question") or "").strip()
        return "take_screenshot", {"query": query} if query else {}

    if norm_act == "finish":
        return "finish", {"summary": str(params.get("summary", "")).strip()}

    return norm_act, {k: str(v).strip().lower() if isinstance(v, str) else v for k, v in params.items()}


def _requested_application_keys(goal: str) -> Set[str]:
    """Return canonical allowlisted applications explicitly named in the goal."""
    goal_text = str(goal or "").casefold()
    matches: List[Tuple[int, str, str]] = []
    for alias in sorted(APPLICATION_ALLOWLIST, key=len, reverse=True):
        alias_text = str(alias).casefold().strip()
        if not alias_text:
            continue
        for match in re.finditer(rf"(?<![\w]){re.escape(alias_text)}(?![\w])", goal_text):
            prefix = goal_text[max(0, match.start() - 28):match.start()]
            if re.search(r"\b(?:do\s+not|don't|never|avoid|not|no)\s+(?:(?:open|launch|start|run)\s+)?$", prefix):
                continue
            entry = APPLICATION_ALLOWLIST[alias]
            matches.append((match.start(), alias_text, normalize_app_name(str(entry.get("name") or alias_text))))

    specific_apps = {app_key for _, alias, app_key in matches if alias != "browser"}
    return {
        app_key for _, alias, app_key in matches
        if alias != "browser" or not specific_apps
    }


def are_actions_equivalent(
    action1: Optional[str],
    params1: Optional[Dict[str, Any]],
    action2: Optional[str],
    params2: Optional[Dict[str, Any]],
) -> bool:
    """Compare two actions for semantic equivalence using normalized parameters."""
    if not action1 or not action2:
        return False
    norm_act1, norm_p1 = normalize_action_payload(action1, params1)
    norm_act2, norm_p2 = normalize_action_payload(action2, params2)
    return norm_act1 == norm_act2 and norm_p1 == norm_p2


def validate_action_payload(action: str, params: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Strictly validate action name and parameters against security rules."""
    if action not in ALLOWED_COMPUTER_USE_ACTIONS:
        return False, f"Action '{action}' is not permitted in computer-use mode"

    if not isinstance(params, dict):
        return False, f"Parameters for '{action}' must be a JSON object"

    # Check for forbidden execution strings in any string argument
    params_str = json.dumps(params).lower()
    for term in FORBIDDEN_EXEC_TERMS:
        if term in params_str:
            return False, f"Forbidden command execution term '{term}' detected in parameters"

    # Coordinate validation for click and double_click
    if action in ("click", "double_click"):
        x = params.get("x")
        y = params.get("y")
        if x is None or y is None:
            return False, f"'{action}' requires integer 'x' and 'y' coordinates"
        if not isinstance(x, int) or not isinstance(y, int) or isinstance(x, bool) or isinstance(y, bool):
            return False, f"Coordinates must be integers, got x={type(x).__name__}, y={type(y).__name__}"
        
        # Validate against screen bounds
        screen_w, screen_h = get_screen_bounds()
        if not (0 <= x < screen_w and 0 <= y < screen_h):
            return False, f"Coordinates ({x}, {y}) are outside screen bounds (width={screen_w}, height={screen_h})"

    # URL validation for open_url
    elif action == "open_url":
        url = params.get("url")
        if not isinstance(url, str) or not url.strip():
            return False, "'open_url' requires a non-empty 'url' string"
        parsed = urllib.parse.urlparse(url.strip())
        scheme = (parsed.scheme or "").lower()
        if scheme in DANGEROUS_URL_SCHEMES:
            return False, f"URL scheme '{scheme}:' is explicitly forbidden for security reasons"
        if scheme not in SAFE_URL_SCHEMES or not parsed.netloc:
            return False, f"'open_url' requires a valid http:// or https:// URL with a host, got '{url}'"

    # Text validation for type_text
    elif action == "type_text":
        text = params.get("text")
        if not isinstance(text, str) or not text:
            return False, "'type_text' requires a non-empty 'text' string"
        if len(text) > 500:
            return False, f"'type_text' length ({len(text)}) exceeds maximum of 500 characters"

    # Key validation for press_key
    elif action == "press_key":
        key = params.get("key")
        if not isinstance(key, str) or not key:
            return False, "'press_key' requires a non-empty 'key' string"
        if key.upper() not in KEY_ALLOWLIST:
            return False, f"Key '{key}' is not in the allowed virtual key list"

    # Scroll validation
    elif action == "scroll":
        direction = str(params.get("direction", "")).lower()
        amount = params.get("amount")
        if direction not in ("up", "down"):
            return False, f"Scroll direction must be 'up' or 'down', got '{direction}'"
        if not isinstance(amount, int) or isinstance(amount, bool) or not (1 <= amount <= 50):
            return False, f"Scroll amount must be an integer between 1 and 50, got {amount}"

    return True, None


def is_deterministic_app_workflow(goal: str) -> bool:
    """Narrow, safe check for simple deterministic application workflows (Phase 6).

    Recognizes requests like 'Open Notepad and type Hello Jarvis' or 'Launch Calculator'
    where the initial action is deterministic and does not require screen understanding.
    """
    if not goal or not isinstance(goal, str):
        return False
    lower = goal.lower().strip()
    pattern = r"^(open|launch|start|run)\s+(notepad|calculator|calc|terminal|cmd|powershell|edge|chrome)\b"
    return bool(re.search(pattern, lower))


def is_goal_requiring_visual_confirmation(goal: str) -> bool:
    """Check if the user goal explicitly requests visual inspection or confirmation.

    Examples:
    - 'Verify that Notepad has the text hello'
    - 'Check if the window opened'
    - 'Confirm the button is green'
    - 'Read what is on the screen'
    """
    if not goal or not isinstance(goal, str):
        return False
    lower = goal.lower()
    visual_patterns = [
        r"\b(verify|inspect|check\s*(?:if|whether|visual)|confirm\s*(?:visible|that|text))\b",
        r"\b(read\s*(?:screen|text|what)|what\s*is\s*on|see\s*if|look\s*at|find\s*text)\b",
        r"\b(is\s*(?:it|the\s*text)\s*visible|screenshot)\b",
    ]
    return any(re.search(pat, lower) for pat in visual_patterns)


def is_deterministically_verifiable(
    action: str,
    params: Dict[str, Any],
    tool_result: Dict[str, Any],
    goal: Optional[str] = None,
) -> Tuple[bool, str]:
    """Determine whether an action's success can be verified deterministically via OS/Host Bridge
    without invoking the expensive Qwen2.5-VL vision specialist during intermediate loop steps.

    Safety Invariants:
    1. Failed / non-zero exit code actions NEVER bypass vision.
    2. Sensitive actions (payments, emails, deletions, passwords/auth, finance) NEVER bypass vision.
    3. Ambiguous GUI interactions (click, double_click, scroll) ALWAYS require immediate vision.
    4. Deterministic OS actions (open_application with verified HWND/PID, write_text dispatch,
       press_key dispatch) with valid exit_code=0 bypass intermediate vision. Final visual verification
       is deferred to the finish checkpoint when required by the goal.

    Returns:
        (is_deterministic, reason_or_description)
    """
    if not isinstance(tool_result, dict):
        return False, "Invalid tool result structure"

    # Rule 1: OS failure or error code cannot be deterministically verified as a success
    exit_code = tool_result.get("exit_code")
    if exit_code is not None and exit_code != 0:
        return False, f"OS action returned non-zero exit code ({exit_code})"
    if not tool_result.get("success", True) and not tool_result.get("focused") and not tool_result.get("already_running"):
        return False, "OS tool result indicated failure"
    if tool_result.get("error"):
        return False, f"OS tool result reported error: {tool_result.get('error')}"

    # Rule 2: Sensitive actions require visual verification
    is_sens, sens_reason = is_sensitive_action(action, params, goal or "")
    if is_sens:
        return False, f"Sensitive action requires visual verification: {sens_reason}"

    # Rule 3: Action classification (Deterministic OS actions vs. Ambiguous/GUI)
    if action == "open_application":
        app_name = params.get("application", "Application") if isinstance(params, dict) else "Application"
        hwnd = tool_result.get("hwnd")
        pid = tool_result.get("pid")
        focused = tool_result.get("focused")
        already_running = tool_result.get("already_running", False)
        already_satisfied = tool_result.get("already_satisfied", False)

        # Invariant: Process spawn alone without confirmed window/focus is NOT deterministic verification.
        has_window_evidence = (hwnd is not None) or (focused is True) or already_running or already_satisfied
        if not has_window_evidence or focused is False:
            return False, f"open_application for '{app_name}' spawned process (PID={pid}) but window HWND/focus was not established"

        status_parts = []
        if already_running or already_satisfied:
            status_parts.append("already running")
        else:
            status_parts.append("launched")
        if focused:
            status_parts.append("focused")
        status_desc = " & ".join(status_parts) if status_parts else "active"

        return True, f"Application '{app_name}' {status_desc} via OS Host Bridge (hwnd={hwnd}, pid={pid})"

    elif action in ("write_text", "type_text"):
        text = params.get("text", "") if isinstance(params, dict) else ""
        text_len = len(text)
        # Invariant 1: SendInput dispatch alone without target window focus or keyboard focus is NOT application-visible success
        if tool_result.get("focused") is False or tool_result.get("keyboard_focus_established") is False:
            target = params.get("application") or params.get("target_app") or tool_result.get("target") or "target application"
            return False, f"Text input ({text_len} chars) dispatched via SendInput but {target} was not verified in foreground / input focus"

        # Invariant 2: Explicitly distinguish DISPATCHED from DELIVERED / VERIFIED
        if tool_result.get("dispatched", 1) == 0:
            return False, f"Text input ({text_len} chars) failed: no characters were dispatched via OS SendInput"
        if tool_result.get("delivered") is False:
            return False, f"Text input ({text_len} chars) failed delivery verification to target application"

        fg_title = tool_result.get("foreground_title") or tool_result.get("title") or "focused window"
        hwnd = tool_result.get("hwnd")
        hwnd_str = f" (hwnd={hwnd})" if hwnd is not None else ""
        ctrl = tool_result.get("control")
        ctrl_str = f", control={ctrl}" if ctrl else ""
        verif_mode = " (verified)" if tool_result.get("delivery_verified") else ""
        return True, f"Text input ({text_len} chars) delivered to foreground window '{fg_title}'{hwnd_str}{ctrl_str}{verif_mode}"

    elif action == "press_key":
        key = params.get("key", "") if isinstance(params, dict) else ""
        return True, f"Key '{key}' dispatched deterministically via OS SendInput"

    elif action in ("take_screenshot", "screenshot"):
        w = tool_result.get("width", 1920)
        h = tool_result.get("height", 1080)
        fp = tool_result.get("file_path")
        fp_str = f" saved to {fp}" if fp else ""
        return True, f"Desktop screenshot captured ({w}x{h}){fp_str}"

    elif action in ("click", "double_click"):
        return False, f"Mouse action '{action}' requires visual verification to confirm UI reaction"

    elif action == "scroll":
        return False, "Scroll action requires visual verification of viewport change"

    elif action == "finish":
        return True, "Task finish signal is deterministic"

    else:
        return False, f"Action '{action}' is not classified as deterministically verifiable"


# ---------------------------------------------------------------------------
# Computer Use Controller
# ---------------------------------------------------------------------------

class ComputerUseController:
    """Safe Closed-Loop Computer-Use / GUI Agent Controller."""

    def __init__(
        self,
        config: Optional[ComputerUseConfig] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
        navigation_guard: Optional[Any] = None,
    ) -> None:
        self.config = config or ComputerUseConfig()
        self.progress_callback = progress_callback
        self.navigation_guard = navigation_guard

    async def _release_planner_for_vision(self, state: ComputerUseState) -> None:
        """Keep the 8B planner and 3B vision model from competing for 6 GB VRAM."""
        if state.last_active_model != self.config.planner_model:
            return
        started = time.perf_counter()
        await asyncio.to_thread(unload_model_sync, self.config.planner_model, self.config.ollama_url)
        elapsed = time.perf_counter() - started
        state.total_model_transition_seconds += elapsed
        logger.info("[Computer Use] Planner unload before vision: %.3fs", elapsed)

    async def _notify_progress(self, stage: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """Forward lifecycle progress event safely to progress_callback."""
        if not self.progress_callback:
            return
        safe_msg = redact_sensitive_data(message)
        payload: Dict[str, Any] = {
            "stage": stage,
            "tail": safe_msg,
            "message": safe_msg,
        }
        if extra:
            payload.update(redact_sensitive_data(extra))
        try:
            if inspect.iscoroutinefunction(self.progress_callback):
                await self.progress_callback(payload)
            else:
                self.progress_callback(payload)
        except Exception as e:
            logger.debug("Failed to deliver computer-use progress event: %s", e)

    def _finalize_metrics(self, state: ComputerUseState) -> None:
        """Compute summary timing and model swap metrics."""
        state.elapsed_seconds = time.time() - state.start_time
        state.metrics = {
            "total_planning_seconds": round(state.total_planning_seconds, 3),
            "total_vision_seconds": round(state.total_vision_seconds, 3),
            "total_action_seconds": round(state.total_action_seconds, 3),
            "total_verification_seconds": round(state.total_verification_seconds, 3),
            "total_screenshot_seconds": round(state.total_screenshot_seconds, 3),
            "total_image_transfer_seconds": round(state.total_image_transfer_seconds, 3),
            "total_image_encoding_seconds": round(state.total_image_encoding_seconds, 3),
            "total_model_load_seconds": round(state.total_model_load_seconds, 3),
            "total_model_inference_seconds": round(state.total_model_inference_seconds, 3),
            "total_model_transition_seconds": round(state.total_model_transition_seconds, 3),
            "total_approval_wait_seconds": round(state.total_approval_wait_seconds, 3),
            "approval_retry_count": state.approval_retry_count,
            "total_model_swap_seconds": round(state.total_model_swap_seconds, 3),
            "total_elapsed_seconds": round(state.elapsed_seconds, 3),
            "model_swap_count": state.model_swap_count,
            "iterations": state.iteration,
            "action_count": state.action_count,
            "status": state.status,
            "model_invocations": state.model_invocations,
        }

    def build_planner_messages(
        self,
        goal: str,
        history: List[Dict[str, Any]],
        visual_data: Dict[str, Any],
    ) -> List[Dict[str, str]]:
        """Construct minimal prompt messages for the Qwen3 planner."""
        # Build concise execution history summary (last 3 max)
        history_summary = []
        for h in history[-3:]:
            step_num = h.get("iteration", 1)
            action = h.get("proposed_action", {}).get("action", "none")
            params = h.get("proposed_action", {}).get("params", {})
            verified = h.get("verification", {}).get("verified", False)
            desc = h.get("verification", {}).get("description", "")
            tool_res = h.get("tool_result", {}) or {}
            tool_out = tool_res.get("output") or tool_res.get("error") or h.get("error") or ""
            history_summary.append(
                f"- Step {step_num}: `{action}`({json.dumps(params)}) -> Result: {tool_out} | Verified: {verified} ({desc})"
            )
        history_text = "\n".join(history_summary) if history_summary else "None (Starting execution)."

        # Format visual observation concisely
        obs = visual_data.get("observations", [])
        windows = visual_data.get("windows", [])
        coords = visual_data.get("coordinates", [])
        text_labels = visual_data.get("readable_text", []) or visual_data.get("visible_text", [])

        visual_summary: Dict[str, Any] = {
            "observations": obs,
            "windows": windows,
            "detected_elements": coords,
            "readable_text": text_labels,
        }
        if visual_data.get("screenshot_info"):
            visual_summary["screenshot_info"] = visual_data["screenshot_info"]

        # FIX #2: Warn planner about actions that already failed so it chooses alternatives
        failed_action_warning = ""
        for h in history[-3:]:
            prev_act = h.get("proposed_action", {})
            prev_verified = h.get("verification", {}).get("verified", False)
            if not prev_verified and prev_act.get("action") == "open_application":
                app_name = prev_act.get("params", {}).get("application", "")
                tool_exit = h.get("tool_result", {}).get("exit_code", 1)
                if tool_exit == 0:
                    failed_action_warning += (
                        f"\nIMPORTANT: open_application('{app_name}') already launched the process successfully "
                        f"but it did NOT become the foreground window. Do NOT call open_application('{app_name}') again. "
                        f"Instead, try clicking on its taskbar icon to bring it to the foreground, or use a different approach.\n"
                    )
            elif not prev_verified and prev_act.get("action") in ("take_screenshot", "screenshot"):
                err_detail = (
                    (h.get("tool_result", {}) or {}).get("error")
                    or h.get("error")
                    or (h.get("verification", {}) or {}).get("description")
                    or "screenshot capture failed"
                )
                failed_action_warning += (
                    f"\nIMPORTANT: take_screenshot() failed with error: {err_detail}. "
                    f"Do NOT fabricate a description of the screen.\n"
                )

        if history:
            last_h = history[-1]
            last_act_name = last_h.get("proposed_action", {}).get("action")
            last_verif = last_h.get("verification", {}).get("verified", False)
            if last_act_name in ("take_screenshot", "screenshot") and last_verif:
                failed_action_warning += (
                    "\nIMPORTANT: take_screenshot() was just executed and succeeded. "
                    "Its captured screenshot metadata and visual findings are included in CURRENT SCREEN STATE "
                    "and RECENT STEP HISTORY above. Do NOT call take_screenshot() again unless another action "
                    "has changed the screen; proceed with the next action or call finish with your summary.\n"
                )

        desktop_context = (
            "\nDESKTOP SCREEN CONTEXT:\n"
            "- The desktop screen was already captured and analyzed by the Vision Specialist (qwen2.5vl:3b) for this step, and its findings are shown in CURRENT SCREEN STATE above.\n"
            "- If the goal is to take a screenshot and/or describe what is visible on the desktop, you already have the actual live screen observations in CURRENT SCREEN STATE. Do NOT say no screenshot tool is available. Synthesize the findings from CURRENT SCREEN STATE and call finish with your summary description.\n"
            "- If you need a fresh capture or additional screen refresh, you may emit take_screenshot (or screenshot).\n"
        )

        user_content = (
            f"GOAL: {goal}\n\n"
            f"CURRENT SCREEN STATE:\n{json.dumps(visual_summary, indent=2)}\n\n"
            f"RECENT STEP HISTORY:\n{history_text}\n"
            f"{failed_action_warning}\n"
            f"{desktop_context}\n"
            "Keep reasoning brief and immediately emit the single next tool action or sequence of actions to accomplish the goal, or call finish if complete."
        )

        return [{"role": "user", "content": user_content}]

    def _call_planner_sync(
        self,
        messages: List[Dict[str, str]],
        timeout: float = 120.0,
    ) -> Dict[str, Any]:
        """Synchronously query the primary planner model (Qwen3:8b) over Ollama."""
        residency_started = time.perf_counter()
        was_resident = check_model_resident(self.config.planner_model, self.config.ollama_url)
        residency_check_seconds = time.perf_counter() - residency_started
        t0 = time.perf_counter()

        payload = {
            "model": self.config.planner_model,
            "messages": messages,
            "tools": COMPUTER_USE_TOOL_SCHEMAS,
            "stream": False,
            "keep_alive": self.config.keep_alive,
            "options": {
                "temperature": 0.0,
                "num_predict": self.config.planner_num_predict,
                "num_ctx": self.config.planner_num_ctx,
            },
        }

        base = normalize_ollama_base_url(self.config.ollama_url)
        url = f"{base}/api/chat"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                msg = data.get("message", {})
                content = msg.get("content", "")
                thinking = msg.get("thinking", "")
                tool_calls = msg.get("tool_calls", [])
                latency = round(time.perf_counter() - t0, 3)
                load_seconds = float(data.get("load_duration", 0) or 0) / 1_000_000_000
                prompt_eval_seconds = float(data.get("prompt_eval_duration", 0) or 0) / 1_000_000_000
                eval_seconds = float(data.get("eval_duration", 0) or 0) / 1_000_000_000
                inference_seconds = (
                    prompt_eval_seconds + eval_seconds
                    if prompt_eval_seconds + eval_seconds > 0
                    else max(0.0, latency - load_seconds)
                )
                return {
                    "success": True,
                    "content": content,
                    "tool_calls": tool_calls,
                    "thinking": thinking,
                    "latency_seconds": latency,
                    "was_resident": was_resident,
                    "residency_check_seconds": round(residency_check_seconds, 3),
                    "model_load_seconds": round(load_seconds, 3),
                    "model_inference_seconds": round(inference_seconds, 3),
                    "prompt_eval_duration_ns": data.get("prompt_eval_duration", 0),
                    "eval_duration_ns": data.get("eval_duration", 0),
                    "keep_alive": self.config.keep_alive,
                    "model": self.config.planner_model,
                }
        except urllib.error.HTTPError as e:
            return {
                "success": False,
                "error": f"Planner HTTP {e.code}: {e.reason}",
                "latency_seconds": round(time.time() - t0, 3),
                "was_resident": was_resident,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Planner query failed: {e}",
                "latency_seconds": round(time.time() - t0, 3),
                "was_resident": was_resident,
            }

    async def plan_next_action(
        self,
        state: ComputerUseState,
        visual_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Ask Qwen3 to decide the next GUI action(s) based on current state and vision."""
        messages = self.build_planner_messages(state.goal, state.history, visual_data)
        t_plan_start = time.time()
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(
                    self._call_planner_sync,
                    messages,
                    timeout=self.config.planner_timeout_seconds,
                ),
                timeout=self.config.planner_timeout_seconds + 1.0,
            )
        except asyncio.TimeoutError:
            res = {
                "success": False,
                "error": f"Planner exceeded {self.config.planner_timeout_seconds:.0f}s timeout",
                "latency_seconds": self.config.planner_timeout_seconds,
                "was_resident": False,
                "model": self.config.planner_model,
            }
        plan_latency = time.time() - t_plan_start

        # Record model lifecycle instrumentation
        model_name = res.get("model", self.config.planner_model)
        actual_latency = res.get("latency_seconds", plan_latency)
        was_resident = res.get("was_resident", False)
        model_load_seconds = float(res.get("model_load_seconds") or 0.0)
        model_inference_seconds = float(res.get("model_inference_seconds") or actual_latency)
        state.total_model_load_seconds += model_load_seconds
        state.total_model_inference_seconds += model_inference_seconds

        if state.last_active_model is not None and state.last_active_model != model_name:
            state.model_swap_count += 1
            if not was_resident:
                state.total_model_swap_seconds += actual_latency
        elif state.last_active_model is None and not was_resident:
            state.model_swap_count += 1
            state.total_model_swap_seconds += actual_latency
        state.last_active_model = model_name

        state.model_invocations.append({
            "model": model_name,
            "phase": "planner",
            "start_time": t_plan_start,
            "end_time": time.time(),
            "latency": actual_latency,
            "keep_alive": self.config.keep_alive,
            "was_resident": was_resident,
            "model_load_seconds": model_load_seconds,
            "model_inference_seconds": model_inference_seconds,
            "residency_check_seconds": res.get("residency_check_seconds", 0.0),
            "total_task_elapsed_seconds": round(time.time() - state.start_time, 3),
        })
        logger.info(
            "[Iteration %d] Planner timing: total=%.3fs load=%.3fs inference=%.3fs resident=%s",
            state.iteration, actual_latency, model_load_seconds, model_inference_seconds, was_resident,
        )

        if not res.get("success"):
            return {
                "success": False,
                "error": res.get("error", "Failed to query planner"),
                "action": "none",
                "latency_seconds": actual_latency,
            }

        # Handle native tool calls if returned
        tool_calls = res.get("tool_calls", [])
        if tool_calls:
            actions_list = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                fn_name = fn.get("name", "").strip()
                fn_args = fn.get("arguments", {})
                if isinstance(fn_args, str):
                    try:
                        fn_args = json.loads(fn_args)
                    except Exception:
                        fn_args = {}
                actions_list.append({
                    "action": fn_name,
                    "params": fn_args if isinstance(fn_args, dict) else {},
                    "expected_outcome": f"Perform {fn_name}",
                })

            first_act = actions_list[0]
            remaining = actions_list[1:]
            thought_text = res.get("thinking", "") or res.get("content", "") or f"Executing tool {first_act['action']}"
            short_thought = thought_text.strip().split("\n")[0][:200]
            return {
                "success": True,
                "thought": short_thought,
                "action": first_act["action"],
                "params": first_act["params"],
                "expected_outcome": first_act["expected_outcome"],
                "planned_steps": remaining,
                "raw_response": str(tool_calls),
                "latency_seconds": actual_latency,
            }

        raw_content = res.get("content", "").strip()

        # Strip thinking tags (<think>...</think>) from reasoning models
        clean_text = re.sub(r"<think>[\s\S]*?</think>", "", raw_content, flags=re.DOTALL).strip()

        # Strip markdown code blocks
        if "```" in clean_text:
            m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean_text)
            if m:
                clean_text = m.group(1).strip()

        # Find first matching JSON object
        json_match = re.search(r"\{[\s\S]*\}", clean_text)
        json_str = json_match.group(0) if json_match else clean_text

        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, dict) and "action" in parsed:
                planned_steps = parsed.get("planned_steps") or parsed.get("next_actions") or []
                return {
                    "success": True,
                    "thought": parsed.get("thought", ""),
                    "action": str(parsed.get("action", "")).strip(),
                    "params": parsed.get("params", {}) if isinstance(parsed.get("params"), dict) else {},
                    "expected_outcome": parsed.get("expected_outcome", ""),
                    "planned_steps": planned_steps if isinstance(planned_steps, list) else [],
                    "raw_response": raw_content,
                    "latency_seconds": actual_latency,
                }
        except Exception as e:
            logger.warning("Failed to parse JSON planner response: %s (clean_text: %s)", e, clean_text[:150])

        # Resilient fallback if planner returned non-JSON text
        return {
            "success": False,
            "error": f"Could not parse structured planner action from: {raw_content[:200]}",
            "action": "none",
            "raw_response": raw_content,
            "latency_seconds": actual_latency,
        }

    async def capture_screenshot(self) -> Dict[str, Any]:
        """Capture a fresh screenshot via the registered TakeScreenshotTool."""
        t0 = time.time()
        handlers = get_computer_use_handlers()
        handler = handlers.get("take_screenshot")
        if not handler:
            return {"success": False, "error": "take_screenshot tool handler is not registered"}

        try:
            res = await handler("")
            dt = time.time() - t0
            if res.get("exit_code") == 0 and res.get("image_base64"):
                return {
                    "success": True,
                    "file_path": res.get("file_path"),
                    "image_base64": res.get("image_base64"),
                    "width": res.get("width", 1920),
                    "height": res.get("height", 1080),
                    "image_bytes": res.get("image_bytes"),
                    "host_capture_seconds": res.get("host_capture_seconds"),
                    "jpeg_encode_seconds": res.get("jpeg_encode_seconds"),
                    "base64_encode_seconds": res.get("base64_encode_seconds"),
                    "bridge_round_trip_seconds": res.get("bridge_round_trip_seconds"),
                    "latency_seconds": round(dt, 3),
                }
            return {
                "success": False,
                "error": res.get("error", "Screenshot capture failed"),
                "bridge_available": res.get("bridge_available", True),
                "latency_seconds": round(dt, 3),
            }
        except Exception as e:
            return {"success": False, "error": f"Error capturing screenshot: {e}", "latency_seconds": round(time.time() - t0, 3)}

    async def execute_single_action(
        self,
        action: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a validated single action via the agent tool registry."""
        if action == "finish":
            return {"exit_code": 0, "output": f"Task completed: {params.get('summary', 'Done')}"}

        handlers = get_computer_use_handlers()
        handler = handlers.get(action)
        if not handler:
            return {"exit_code": 1, "error": f"Tool '{action}' is not registered in handlers"}

        try:
            # Pass as JSON string
            payload_str = json.dumps(params)
            res = await handler(payload_str)
            return res
        except Exception as e:
            logger.exception("Exception executing tool '%s': %s", action, e)
            return {
                "exit_code": 1,
                "error": f"Exception executing {action}: {e}",
                "execution_status_uncertain": action == "open_url",
            }

    def log_step(self, step: StepRecord, state: ComputerUseState) -> None:
        """Produce clean, structured, redacted iteration log."""
        safe_thought = redact_sensitive_data(step.planner_thought)
        safe_action = redact_sensitive_data(step.proposed_action)
        safe_tool_res = redact_sensitive_data(step.tool_result)
        safe_verif = redact_sensitive_data(step.verification)

        logger.info(
            "\n[CLOSED-LOOP ITERATION %d] (Actions: %d/%d, Elapsed: %.1fs, Iteration: %.3fs)\n"
            "  Screenshot: %s\n"
            "  Planner Thought: %s\n"
            "  Proposed Action: %s\n"
            "  Sensitive: %s (Reason: %s, Status: %s)\n"
            "  Tool Result: exit=%s | %s\n"
            "  Verification: verified=%s (conf=%.2f) | %s\n"
            "  Next State: %s",
            step.iteration,
            state.action_count,
            self.config.max_actions,
            time.time() - state.start_time,
            time.time() - step.timestamp,
            step.screenshot_file or "(none)",
            safe_thought,
            safe_action,
            step.is_sensitive,
            step.sensitive_reason,
            step.confirmation_status,
            safe_tool_res.get("exit_code"),
            safe_tool_res.get("output") or safe_tool_res.get("error"),
            safe_verif.get("verified"),
            safe_verif.get("confidence", 0.0),
            safe_verif.get("description"),
            state.status,
        )

    async def step(
        self,
        state: ComputerUseState,
        confirmed: bool = False,
    ) -> Tuple[ComputerUseState, bool]:
        """Execute one complete cycle of the closed-loop state machine.

        Returns (updated_state, should_continue).
        """
        # Feature flag check
        if not is_computer_use_enabled():
            state.status = "FAILED"
            state.error = "Computer use is disabled via COMPUTER_USE_ENABLED feature flag"
            await self._notify_progress("failed", state.error)
            self._finalize_metrics(state)
            return state, False

        # Plan mode top-level check
        if self.config.plan_mode:
            state.status = "STOPPED_BY_PLAN_MODE"
            state.error = "Computer use is blocked in plan mode (mutating action prohibited)"
            await self._notify_progress("plan_mode_blocked", state.error)
            self._finalize_metrics(state)
            return state, False

        if state.start_time <= 0.0:
            state.start_time = time.time()

        state.iteration += 1
        state.elapsed_seconds = time.time() - state.start_time
        step_rec = StepRecord(iteration=state.iteration, timestamp=time.time())

        # 1. Check Limits (Wall clock timeout, iterations, actions)
        if state.elapsed_seconds > self.config.max_duration_seconds:
            state.status = "STOPPED_BY_TIMEOUT"
            state.error = f"Exceeded maximum wall-clock duration of {self.config.max_duration_seconds}s"
            await self._notify_progress("timeout", state.error)
            self._finalize_metrics(state)
            return state, False

        if state.iteration > self.config.max_iterations:
            state.status = "STOPPED_BY_BUDGET"
            state.error = f"Exceeded maximum iterations ({self.config.max_iterations})"
            await self._notify_progress("budget_exhausted", state.error)
            self._finalize_metrics(state)
            return state, False

        if state.action_count >= self.config.max_actions:
            state.status = "STOPPED_BY_BUDGET"
            state.error = f"Exceeded maximum allowed actions budget ({self.config.max_actions})"
            await self._notify_progress("budget_exhausted", state.error)
            self._finalize_metrics(state)
            return state, False

        approved_action = state.approved_action

        # 2. Capture Screenshot (reuse if screen has not changed)
        if approved_action:
            # The original proposal already has a screenshot and observation.
            # Resume that exact approved action without replanning or recapturing.
            screenshot_data = state.current_screenshot or {"success": True, "image_base64": ""}
            step_rec.screenshot_file = screenshot_data.get("file_path")
        elif state.current_screenshot is not None and not state.consecutive_failures and state.last_verification and state.last_verification.get("verified", False):
            # Screen was already captured post-action and visually verified; safe to reuse
            screenshot_data = state.current_screenshot
            step_rec.screenshot_file = screenshot_data.get("file_path")
        else:
            logger.info("[Iteration %d] Capturing desktop screenshot...", state.iteration)
            await self._notify_progress("screenshot_capturing", f"Capturing desktop screenshot for iteration {state.iteration}...")
            t_ss_start = time.time()
            screenshot_data = await self.capture_screenshot()
            ss_latency = time.time() - t_ss_start
            state.total_screenshot_seconds += ss_latency
            bridge_rtt = screenshot_data.get("bridge_round_trip_seconds")
            host_capture = screenshot_data.get("host_capture_seconds")
            jpeg_encode = float(screenshot_data.get("jpeg_encode_seconds") or 0.0)
            base64_encode = float(screenshot_data.get("base64_encode_seconds") or 0.0)
            image_transfer = max(
                0.0,
                float(bridge_rtt or 0.0) - float(host_capture or 0.0) - jpeg_encode - base64_encode,
            )
            if bridge_rtt is not None and host_capture is not None:
                state.total_image_transfer_seconds += image_transfer
            state.total_image_encoding_seconds += jpeg_encode + base64_encode
            logger.info(
                "[Iteration %d] Screenshot timing: bridge_total=%.3fs host_capture=%.3fs jpeg_encode=%.3fs base64_encode=%.3fs transfer=%.3fs image_bytes=%s",
                state.iteration, ss_latency,
                float(host_capture or 0.0),
                jpeg_encode,
                base64_encode,
                image_transfer,
                screenshot_data.get("image_bytes"),
            )
            if not screenshot_data.get("success"):
                state.consecutive_failures += 1
                step_rec.error = f"Screenshot capture failed: {screenshot_data.get('error')}"
                state.history.append(step_rec.to_dict())
                state.status = "FAILED"
                state.error = f"Screenshot capture failed: {screenshot_data.get('error')}"
                self.log_step(step_rec, state)
                await self._notify_progress("failed", state.error)
                self._finalize_metrics(state)
                return state, False

            state.previous_screenshot = state.current_screenshot
            state.current_screenshot = screenshot_data
            step_rec.screenshot_file = screenshot_data.get("file_path")
            await self._notify_progress("screenshot_captured", f"Captured desktop screenshot ({screenshot_data.get('width', 0)}x{screenshot_data.get('height', 0)})")

        # 3. Vision Specialist Analysis (avoid redundant call when screen state has not changed)
        if approved_action:
            visual_data = state.current_visual_data
            step_rec.vision_summary = visual_data
            step_rec.vision_latency_seconds = 0.0
        elif state.current_visual_data and state.last_verification and state.last_verification.get("verified", False) and not state.consecutive_failures:
            visual_data = state.current_visual_data
            step_rec.vision_summary = visual_data
            step_rec.vision_latency_seconds = 0.0
        elif state.iteration == 1 and is_deterministic_app_workflow(state.goal) and not is_goal_requiring_visual_confirmation(state.goal) and not state.consecutive_failures:
            # Phase 6: Simple task fast-path.
            # Avoid expensive Qwen2.5-VL wakeup on iteration 1 when the goal is a deterministic app workflow.
            visual_data = {
                "observations": ["Desktop ready. Planning initial deterministic action."],
                "windows": [],
                "coordinates": [],
                "readable_text": [],
            }
            state.current_visual_data = visual_data
            step_rec.vision_summary = visual_data
            step_rec.vision_latency_seconds = 0.0
            logger.info("[Iteration 1] Fast-path: Bypassing initial Vision Specialist for deterministic workflow '%s'", state.goal)
        else:
            logger.info("[Iteration %d] Vision Specialist (%s) analyzing screen state...", state.iteration, self.config.vision_model)
            await self._notify_progress("vision_analyzing", f"Vision specialist ({self.config.vision_model}) analyzing screen state...")
            await self._release_planner_for_vision(state)
            t_vis_start = time.time()
            vision_res = await analyze_screenshot(
                image_base64=screenshot_data["image_base64"],
                query="List all currently open and visible application windows, buttons, input fields, coordinates, and readable text on screen.",
                model=self.config.vision_model,
                ollama_url=self.config.ollama_url,
                keep_alive="0",  # Evict vision model immediately; planner needs VRAM
            )
            vis_latency = time.time() - t_vis_start
            step_rec.vision_latency_seconds = vis_latency
            state.total_vision_seconds += vis_latency

            # Record model lifecycle instrumentation
            was_resident = vision_res.get("was_resident", False)
            model_load_seconds = float(vision_res.get("model_load_seconds") or 0.0)
            model_inference_seconds = float(vision_res.get("model_inference_seconds") or vis_latency)
            state.total_model_load_seconds += model_load_seconds
            state.total_model_inference_seconds += model_inference_seconds
            if state.last_active_model is not None and state.last_active_model != self.config.vision_model:
                state.model_swap_count += 1
                if not was_resident:
                    state.total_model_swap_seconds += vis_latency
            elif state.last_active_model is None and not was_resident:
                state.model_swap_count += 1
                state.total_model_swap_seconds += vis_latency
            state.last_active_model = self.config.vision_model

            state.model_invocations.append({
                "model": self.config.vision_model,
                "phase": "vision_observation",
                "start_time": t_vis_start,
                "end_time": time.time(),
                "latency": vis_latency,
                "model_load_seconds": model_load_seconds,
                "model_inference_seconds": model_inference_seconds,
                "residency_check_seconds": vision_res.get("residency_check_seconds", 0.0),
                "keep_alive": "0",
                "was_resident": was_resident,
                "total_task_elapsed_seconds": round(time.time() - state.start_time, 3),
            })

            if not vision_res.get("success"):
                state.consecutive_failures += 1
                step_rec.error = f"Vision analysis failed: {vision_res.get('error')}"
                state.history.append(step_rec.to_dict())
                if state.consecutive_failures >= self.config.max_consecutive_failures:
                    state.status = "FAILED"
                    state.error = f"Consecutive vision analysis failures: {vision_res.get('error')}"
                    await self._notify_progress("failed", state.error)
                    self._finalize_metrics(state)
                    return state, False
                state.status = "FAILED"
                state.error = f"Vision analysis failed: {vision_res.get('error')}"
                self.log_step(step_rec, state)
                self._finalize_metrics(state)
                return state, False

            visual_data = vision_res.get("structured", {})
            state.current_visual_data = visual_data
            step_rec.vision_summary = visual_data
            await self._notify_progress("vision_analyzed", "Screen analysis complete")

            logger.info(
                "[Iteration %d] Vision timing: total=%.3fs load=%.3fs inference=%.3fs resident=%s",
                state.iteration, vis_latency, model_load_seconds, model_inference_seconds, was_resident,
            )

        # 4. Planner Reasoning (Qwen3:8b) - avoid calling Qwen3 when planned decision is already sufficient
        can_use_queue = (
            bool(state.planned_action_queue)
            and state.last_verification is not None
            and state.last_verification.get("verified", False) is True
            and state.consecutive_failures == 0
        )

        if approved_action:
            proposed_action_name = approved_action.get("action")
            proposed_params = dict(approved_action.get("params") or {})
            expected_outcome = approved_action.get("expected_outcome", f"Perform {proposed_action_name}")
            step_rec.planner_thought = "Resuming the exact action approved by the user."
            step_rec.proposed_action = {"action": proposed_action_name, "params": proposed_params}
            state.last_proposed_action = step_rec.proposed_action
            await self._notify_progress("plan_decided", f"Executing approved action: {proposed_action_name}")
        elif can_use_queue:
            next_action_item = state.planned_action_queue.pop(0)
            proposed_action_name = next_action_item.get("action")
            proposed_params = next_action_item.get("params", {})
            expected_outcome = next_action_item.get("expected_outcome", f"Perform {proposed_action_name}")
            step_rec.planner_thought = next_action_item.get("thought", f"Executing planned step: {proposed_action_name}")
            step_rec.proposed_action = {"action": proposed_action_name, "params": proposed_params}
            state.last_proposed_action = step_rec.proposed_action
            step_rec.planning_latency_seconds = 0.0
            await self._notify_progress("plan_decided", f"Planned action (from active plan): {proposed_action_name}")
        else:
            state.planned_action_queue.clear()
            logger.info("[Iteration %d] Planner (%s) deciding next action...", state.iteration, self.config.planner_model)
            await self._notify_progress("planning", f"Planner ({self.config.planner_model}) deciding next GUI action...")
            t_plan_start = time.time()
            plan_res = await self.plan_next_action(state, visual_data)
            plan_latency = time.time() - t_plan_start
            step_rec.planning_latency_seconds = plan_latency
            state.total_planning_seconds += plan_latency

            if not plan_res.get("success"):
                state.consecutive_failures += 1
                step_rec.error = f"Planner failed: {plan_res.get('error')}"
                state.history.append(step_rec.to_dict())
                state.status = "FAILED"
                state.error = f"Planner failed to generate a valid action: {plan_res.get('error', 'invalid planner output')}"
                await self._notify_progress("failed", state.error)
                self._finalize_metrics(state)
                return state, False

            proposed_action_name = plan_res.get("action")
            proposed_params = plan_res.get("params", {})
            expected_outcome = plan_res.get("expected_outcome", "")
            step_rec.planner_thought = plan_res.get("thought", "")
            step_rec.proposed_action = {"action": proposed_action_name, "params": proposed_params}
            state.last_proposed_action = step_rec.proposed_action

            if plan_res.get("planned_steps"):
                state.planned_action_queue = list(plan_res["planned_steps"])

            await self._notify_progress("plan_decided", f"Planned action: {proposed_action_name}")

        # 5. Goal Complete Check (finish)
        if proposed_action_name == "finish":

            # If the user goal explicitly requires visual confirmation, execute a final-state visual check
            if is_goal_requiring_visual_confirmation(state.goal) and not getattr(state, "final_visual_verification_passed", False):
                logger.info("[Iteration %d] Goal requires final visual verification before finish. Waking Vision Specialist (%s)...", state.iteration, self.config.vision_model)
                await self._notify_progress("verifying", f"Performing final visual goal verification: {state.goal}")

                t_verif_ss_start = time.time()
                final_ss = await self.capture_screenshot()
                final_ss_latency = time.time() - t_verif_ss_start
                state.total_screenshot_seconds += final_ss_latency

                if final_ss.get("success"):
                    step_rec.post_screenshot_file = final_ss.get("file_path")
                    state.current_screenshot = final_ss

                    verif_query = f"Verify that the user goal is fully accomplished on screen: {state.goal}"
                    await self._release_planner_for_vision(state)
                    t_verif_start = time.time()
                    combined_res = await analyze_and_verify(
                        image_base64=final_ss["image_base64"],
                        expected_change=verif_query,
                        prior_context=f"Final goal verification. Summary: {proposed_params.get('summary', '')}",
                        observation_query=f"Verify screen state for goal: {state.goal}",
                        model=self.config.vision_model,
                        ollama_url=self.config.ollama_url,
                        keep_alive="0",
                    )
                    verif_latency = time.time() - t_verif_start
                    step_rec.verification_latency_seconds = verif_latency
                    state.total_verification_seconds += verif_latency
                    if not combined_res.get("success"):
                        state.status = "FAILED"
                        state.error = f"Final visual verification failed: {combined_res.get('error', 'vision model returned no result')}"
                        step_rec.error = state.error
                        state.history.append(step_rec.to_dict())
                        self.log_step(step_rec, state)
                        await self._notify_progress("failed", state.error)
                        self._finalize_metrics(state)
                        return state, False
                    state.total_model_load_seconds += float(combined_res.get("model_load_seconds") or 0.0)
                    state.total_model_inference_seconds += float(combined_res.get("model_inference_seconds") or verif_latency)

                    # Record model lifecycle instrumentation
                    if state.last_active_model is not None and state.last_active_model != self.config.vision_model:
                        state.model_swap_count += 1
                        state.total_model_swap_seconds += verif_latency
                    elif state.last_active_model is None:
                        state.model_swap_count += 1
                        state.total_model_swap_seconds += verif_latency
                    state.last_active_model = self.config.vision_model

                    state.model_invocations.append({
                        "model": self.config.vision_model,
                        "phase": "vision_final_goal_verification",
                        "start_time": t_verif_start,
                        "end_time": time.time(),
                        "latency": verif_latency,
                        "keep_alive": "0",
                        "was_resident": combined_res.get("was_resident", False),
                        "model_load_seconds": combined_res.get("model_load_seconds", 0.0),
                        "model_inference_seconds": combined_res.get("model_inference_seconds", verif_latency),
                        "total_task_elapsed_seconds": round(time.time() - state.start_time, 3),
                    })

                    state.total_model_load_seconds += float(combined_res.get("model_load_seconds") or 0.0)
                    state.total_model_inference_seconds += float(combined_res.get("model_inference_seconds") or verif_latency)

                    verif_data = combined_res.get("verification", {})
                    is_verified = bool(verif_data.get("verified", False))
                    verif_res = {
                        "success": combined_res.get("success", False),
                        "verified": is_verified,
                        "confidence": verif_data.get("confidence", 0.0),
                        "description": verif_data.get("description", ""),
                        "state_change_observed": verif_data.get("state_change_observed", False),
                        "active_window": verif_data.get("active_window", ""),
                        "visible_text": verif_data.get("visible_text", []),
                        "latency_seconds": verif_latency,
                        "was_resident": False,
                        "keep_alive": "0",
                        "model": self.config.vision_model,
                        "raw_response": combined_res.get("raw_response", ""),
                    }
                    step_rec.verification = verif_res
                    state.last_verification = verif_res

                    if is_verified:
                        state.final_visual_verification_passed = True
                        state.status = "COMPLETED"
                        state.summary = proposed_params.get("summary", "Task completed and visually verified.")
                        step_rec.tool_result = {"exit_code": 0, "output": state.summary}
                        state.history.append(step_rec.to_dict())
                        self.log_step(step_rec, state)
                        await self._notify_progress("completed", state.summary)
                        self._finalize_metrics(state)
                        return state, False
                    else:
                        # Final visual verification failed -> trigger recovery
                        state.consecutive_failures += 1
                        state.planned_action_queue.clear()
                        step_rec.error = f"Final goal visual verification failed: {verif_data.get('description')}"
                        logger.warning("[Iteration %d] Final visual verification failed: %s", state.iteration, verif_data.get("description"))
                        state.current_visual_data = combined_res.get("observation", {})
                        state.history.append(step_rec.to_dict())
                        self.log_step(step_rec, state)
                        if state.consecutive_failures >= self.config.max_consecutive_failures:
                            state.status = "VERIFICATION_FAILED"
                            state.error = f"Final goal verification failed repeatedly: {verif_data.get('description')}"
                            await self._notify_progress("failed", state.error)
                            self._finalize_metrics(state)
                            return state, False
                        await self._notify_progress("verification_failed", f"Final verification failed: {verif_data.get('description')}")
                        return state, True
                else:
                    state.consecutive_failures += 1
                    step_rec.error = f"Screenshot capture failed for final verification: {final_ss.get('error')}"
                    state.history.append(step_rec.to_dict())
                    if state.consecutive_failures >= self.config.max_consecutive_failures:
                        state.status = "FAILED"
                        state.error = step_rec.error
                        await self._notify_progress("failed", state.error)
                        self._finalize_metrics(state)
                        return state, False
                    return state, True

            # Standard completion when no final visual verification is required (or already passed)
            state.status = "COMPLETED"
            state.summary = proposed_params.get("summary", "Task completed successfully.")
            step_rec.tool_result = {"exit_code": 0, "output": state.summary}
            finish_desc = f"Goal completed by planner (active window: '{state.active_application}', HWND: {state.active_hwnd})." if state.active_application else "Goal completed by planner."
            step_rec.verification = {"verified": True, "confidence": 1.0, "description": finish_desc}
            state.history.append(step_rec.to_dict())
            self.log_step(step_rec, state)
            await self._notify_progress("completed", state.summary)
            self._finalize_metrics(state)
            return state, False

        # 6. Safety & Sensitive Operation Validation
        # A. Schema validation
        valid, schema_err = validate_action_payload(proposed_action_name, proposed_params)
        if not valid:
            state.consecutive_failures += 1
            state.planned_action_queue.clear()
            step_rec.error = f"Security/Schema rejection: {schema_err}"
            state.history.append(step_rec.to_dict())
            if state.consecutive_failures >= self.config.max_consecutive_failures:
                state.status = "FAILED"
                state.error = schema_err
                await self._notify_progress("failed", state.error)
                self._finalize_metrics(state)
                return state, False
            return state, True

        if proposed_action_name == "open_application":
            app_value = str(
                proposed_params.get("application")
                or proposed_params.get("app_name")
                or proposed_params.get("app")
                or proposed_params.get("name")
                or ""
            ).strip()
            app_key = normalize_action_payload("open_application", {"application": app_value})[1]["application"]
            requested_apps = _requested_application_keys(state.goal)
            if not app_key or app_key not in requested_apps:
                state.status = "FAILED"
                state.error = (
                    f"Planner proposed opening '{app_value or 'an unspecified application'}', "
                    "which the user did not request. No application was launched."
                )
                state.planned_action_queue.clear()
                step_rec.error = state.error
                state.history.append(step_rec.to_dict())
                await self._notify_progress("failed", state.error)
                self._finalize_metrics(state)
                return state, False

        # B. Plan mode check
        if self.config.plan_mode and proposed_action_name in _PLAN_MODE_KNOWN_MUTATORS:
            state.status = "STOPPED_BY_PLAN_MODE"
            state.error = f"Mutating action '{proposed_action_name}' blocked in plan mode"
            state.planned_action_queue.clear()
            step_rec.error = state.error
            state.history.append(step_rec.to_dict())
            await self._notify_progress("plan_mode_blocked", state.error)
            self._finalize_metrics(state)
            return state, False

        # C. Sensitive action check
        is_sens, sens_reason = is_sensitive_action(proposed_action_name, proposed_params, state.goal)
        step_rec.is_sensitive = is_sens
        step_rec.sensitive_reason = sens_reason

        exact_approval = bool(
            confirmed and approved_action
            and are_actions_equivalent(
                approved_action.get("action"), approved_action.get("params"),
                proposed_action_name, proposed_params,
            )
        )
        if is_sens and self.config.require_confirmation_for_sensitive and not exact_approval:
            state.status = "AWAITING_CONFIRMATION"
            state.pending_confirmation = {
                "action": proposed_action_name,
                "params": proposed_params,
                "reason": sens_reason,
                "thought": step_rec.planner_thought,
            }
            step_rec.confirmation_status = "pending"
            state.history.append(step_rec.to_dict())
            self.log_step(step_rec, state)
            await self._notify_progress("awaiting_confirmation", f"Awaiting user approval for sensitive operation: {sens_reason}")
            self._finalize_metrics(state)
            return state, False

        step_rec.confirmation_status = "approved" if exact_approval else "not_needed"
        if exact_approval:
            state.approved_action = None

        if proposed_action_name == "open_url":
            requested_url = str(proposed_params.get("url", "")).strip()
            url_key = canonicalize_navigation_url(requested_url)
            previous_navigation = state.navigation_attempts.get(url_key) if url_key else None
            if previous_navigation:
                previous_status = previous_navigation.get("status", "in_flight")
                if previous_status == "completed":
                    if (
                        state.last_failed_action
                        and are_actions_equivalent(
                            state.last_failed_action.get("action"),
                            state.last_failed_action.get("params"),
                            proposed_action_name,
                            proposed_params,
                        )
                        and state.last_failed_action_os_succeeded
                    ):
                        state.status = "FAILED"
                        state.error = (
                            f"Navigation to '{requested_url}' completed, but its follow-up verification failed. "
                            "Stopped without repeating the browser action."
                        )
                        state.planned_action_queue.clear()
                        step_rec.error = state.error
                        state.history.append(step_rec.to_dict())
                        await self._notify_progress("failed", state.error)
                        self._finalize_metrics(state)
                        return state, False
                    # Treat an already completed navigation as satisfied, while
                    # ensuring screenshot/focus/verification failures never
                    # dispatch the same browser action again.
                    tool_res = {
                        "success": True,
                        "exit_code": 0,
                        "action": "open_url",
                        "url": requested_url,
                        "already_satisfied": True,
                        "duplicate_action": True,
                        "message": "This URL was already opened during this request; no second navigation was dispatched.",
                    }
                    verif_res = {
                        "success": True,
                        "verified": True,
                        "confidence": 1.0,
                        "description": "The requested URL navigation already completed in this run.",
                        "state_change_observed": False,
                        "latency_seconds": 0.0,
                        "model": "os_deterministic",
                        "raw_response": "navigation_already_completed",
                    }
                    step_rec.tool_result = tool_res
                    step_rec.verification = verif_res
                    state.last_action_result = tool_res
                    state.last_verification = verif_res
                    state.consecutive_failures = 0
                    state.last_failed_action = None
                    state.last_failed_action_os_succeeded = False
                    state.history.append(step_rec.to_dict())
                    self.log_step(step_rec, state)
                    await self._notify_progress("verified", tool_res["message"])
                    return state, True
                state.status = "FAILED"
                state.error = (
                    f"Navigation to '{requested_url}' was already attempted with status '{previous_status}'. "
                    "Stopped without retrying to prevent duplicate browser actions."
                )
                state.planned_action_queue.clear()
                step_rec.error = state.error
                state.history.append(step_rec.to_dict())
                await self._notify_progress("failed", state.error)
                self._finalize_metrics(state)
                return state, False

        # D. Check repeated identical action failures (legacy N-repetition check)
        if len(state.history) >= self.config.max_repeated_identical_actions:
            recent_same = True
            for prev_h in state.history[-self.config.max_repeated_identical_actions:]:
                prev_act = prev_h.get("proposed_action", {})
                if not are_actions_equivalent(
                    prev_act.get("action"),
                    prev_act.get("params"),
                    proposed_action_name,
                    proposed_params,
                ) or prev_h.get("verification", {}).get("verified", False):
                    recent_same = False
                    break
            if recent_same:
                state.status = "FAILED"
                state.error = f"Repeated identical failing action '{proposed_action_name}' attempted {self.config.max_repeated_identical_actions} times"
                state.planned_action_queue.clear()
                step_rec.error = state.error
                state.history.append(step_rec.to_dict())
                await self._notify_progress("failed", state.error)
                self._finalize_metrics(state)
                return state, False

            # Prevent infinite consecutive take_screenshot loops even when each capture succeeds
            if proposed_action_name in ("take_screenshot", "screenshot") and all(
                prev_h.get("proposed_action", {}).get("action") in ("take_screenshot", "screenshot")
                for prev_h in state.history[-self.config.max_repeated_identical_actions:]
            ):
                state.status = "STOPPED_BY_REPEATED_ACTION"
                state.error = (
                    f"Repeated 'take_screenshot' action attempted "
                    f"{self.config.max_repeated_identical_actions + 1} times consecutively without progress"
                )
                state.planned_action_queue.clear()
                step_rec.error = state.error
                state.history.append(step_rec.to_dict())
                await self._notify_progress("failed", state.error)
                self._finalize_metrics(state)
                return state, False

        # E. FIX #1: Prevent identical re-execution of the LAST failed action
        #    If the semantically equivalent action+params just failed verification,
        #    block it immediately rather than executing it again (which would e.g. launch
        #    another Notepad window).
        if state.last_failed_action is not None:
            if are_actions_equivalent(
                state.last_failed_action.get("action"),
                state.last_failed_action.get("params"),
                proposed_action_name,
                proposed_params,
            ):
                if proposed_action_name in ("type_text", "write_text"):
                    # Phase 5: Controlled single retry for deterministic keyboard input failures
                    if state.type_text_retry_count >= 1:
                        state.status = "FAILED"
                        state.error = (
                            f"Deterministic input action '{proposed_action_name}' failed "
                            f"delivery verification after controlled retry. Stopping to prevent indefinite retry loop."
                        )
                        state.planned_action_queue.clear()
                        step_rec.error = state.error
                        state.history.append(step_rec.to_dict())
                        await self._notify_progress("failed", state.error)
                        self._finalize_metrics(state)
                        return state, False
                    else:
                        state.type_text_retry_count += 1
                        logger.warning(
                            "[Iteration %d] Allowing at most ONE immediate controlled retry for deterministic input '%s' (attempt %d/1)...",
                            state.iteration, proposed_action_name, state.type_text_retry_count,
                        )
                        # Explicitly re-verify and re-focus target
                        if state.active_application:
                            proposed_params["application"] = state.active_application
                        if state.active_hwnd:
                            proposed_params["hwnd"] = state.active_hwnd
                elif state.last_failed_action_os_succeeded:
                    # The OS-level action succeeded before but verification failed.
                    # Retrying will just create duplicates (e.g. another Notepad window).
                    state.status = "FAILED"
                    state.error = (
                        f"Action '{proposed_action_name}' with params {json.dumps(proposed_params)} "
                        f"previously succeeded at OS level but failed visual verification. "
                        f"Re-executing would create duplicates. "
                        f"No safe foreground recovery mechanism available."
                    )
                    state.planned_action_queue.clear()
                    step_rec.error = state.error
                    state.history.append(step_rec.to_dict())
                    await self._notify_progress("failed", state.error)
                    self._finalize_metrics(state)
                    return state, False
                else:
                    # OS-level action also failed — allow one retry but increment failures
                    logger.warning(
                        "[Iteration %d] Retrying previously failed action '%s' (OS action also failed previously)",
                        state.iteration, proposed_action_name,
                    )

        # F. HARD SAFETY GUARD: Prevent duplicate application launches within the same run
        if proposed_action_name == "open_application":
            raw_app = normalize_action_payload("open_application", proposed_params)[1]["application"]

            if raw_app and raw_app in state.application_launch_attempts and raw_app not in state.launched_applications:
                state.status = "FAILED"
                state.error = (
                    f"Application launch for '{raw_app}' was already attempted in this run and did not complete. "
                    "Stopped without retrying to prevent duplicate launches."
                )
                state.planned_action_queue.clear()
                step_rec.error = state.error
                state.history.append(step_rec.to_dict())
                await self._notify_progress("failed", state.error)
                self._finalize_metrics(state)
                return state, False

            if raw_app and raw_app in state.launched_applications:
                logger.info(
                    "[Iteration %d] Application '%s' already launched/satisfied in this run; skipping duplicate execution.",
                    state.iteration, raw_app,
                )
                tool_res = {
                    "success": True,
                    "action": "open_application",
                    "application": raw_app,
                    "already_satisfied": True,
                    "already_running": True,
                    "focused": True,
                    "exit_code": 0,
                    "reason": "application_already_launched_this_run",
                    "message": f"Application '{raw_app}' is already running and active for this run.",
                }
                step_rec.tool_result = tool_res
                state.last_action_result = tool_res
                verif_res = {
                    "success": True,
                    "verified": True,
                    "confidence": 1.0,
                    "description": f"Application '{raw_app}' already satisfied in this run.",
                    "state_change_observed": False,
                    "active_window": raw_app,
                    "visible_text": (state.last_verification or {}).get("visible_text", []),
                    "latency_seconds": 0.0,
                    "was_resident": False,
                    "keep_alive": "0",
                    "model": "os_deterministic",
                    "raw_response": "already_satisfied",
                }
                step_rec.verification = verif_res
                state.last_verification = verif_res
                state.consecutive_failures = 0
                state.last_failed_action = None
                state.history.append(step_rec.to_dict())
                self.log_step(step_rec, state)
                await self._notify_progress("verified", f"Application '{raw_app}' already active in this run")
                return state, True

        # G. HARD SAFETY GUARD: Prevent runaway repeated action signature loops
        act_sig = f"{proposed_action_name}:{json.dumps(proposed_params, sort_keys=True)}"
        state.action_signature_counts[act_sig] = state.action_signature_counts.get(act_sig, 0) + 1
        if state.action_signature_counts[act_sig] > 3:
            state.status = "STOPPED_BY_REPEATED_ACTION"
            state.error = f"Hard safety stop: Action '{proposed_action_name}' with identical parameters attempted {state.action_signature_counts[act_sig]} times"
            state.planned_action_queue.clear()
            step_rec.error = state.error
            state.history.append(step_rec.to_dict())
            await self._notify_progress("failed", state.error)
            self._finalize_metrics(state)
            return state, False

        # 7. Execute Exactly ONE Action
        state.action_count += 1
        state.last_executed_action = step_rec.proposed_action
        if proposed_action_name == "open_application":
            app_key = normalize_action_payload("open_application", proposed_params)[1]["application"]
            if app_key:
                # Mark before dispatch: even a timeout or lost response is an attempt.
                state.application_launch_attempts.add(app_key)
        elif proposed_action_name == "open_url":
            url_key = canonicalize_navigation_url(proposed_params.get("url"))
            if url_key:
                if self.navigation_guard is not None:
                    claimed, previous_navigation = self.navigation_guard.claim_navigation(
                        proposed_params.get("url")
                    )
                    if not claimed:
                        state.status = "FAILED"
                        state.error = (
                            f"Navigation to '{proposed_params.get('url')}' was already attempted in this request "
                            f"(status: {previous_navigation.get('status', 'in_flight')}). "
                            "Stopped without dispatching it again."
                        )
                        state.planned_action_queue.clear()
                        step_rec.error = state.error
                        state.history.append(step_rec.to_dict())
                        await self._notify_progress("failed", state.error)
                        self._finalize_metrics(state)
                        return state, False
                # Reserve before dispatch so concurrent/repeated planner output
                # cannot issue another browser navigation while this is in flight.
                state.navigation_attempts[url_key] = {
                    "url": str(proposed_params.get("url", "")),
                    "status": "in_flight",
                }

        # Re-focus target window when preceding action was open_application or controller knows target application
        if proposed_action_name in ("type_text", "write_text"):
            if not proposed_params.get("application") and state.active_application:
                proposed_params["application"] = state.active_application
            if not proposed_params.get("hwnd") and state.active_hwnd:
                proposed_params["hwnd"] = state.active_hwnd

        logger.info(
            "[Iteration %d] Executing action '%s' with %s (Thought: %s)...",
            state.iteration,
            proposed_action_name,
            json.dumps(proposed_params),
            step_rec.planner_thought,
        )
        await self._notify_progress("action_executing", f"Executing action '{proposed_action_name}'...")
        t_act_start = time.time()
        tool_res = await self.execute_single_action(proposed_action_name, proposed_params)
        act_latency = time.time() - t_act_start
        step_rec.action_latency_seconds = act_latency
        state.total_action_seconds += act_latency

        step_rec.tool_result = tool_res
        state.last_action_result = tool_res
        if proposed_action_name == "open_url":
            url_key = canonicalize_navigation_url(proposed_params.get("url"))
            if url_key:
                if tool_res.get("execution_status_uncertain"):
                    nav_status = "uncertain"
                elif tool_res.get("success") or tool_res.get("exit_code") == 0:
                    nav_status = "completed"
                else:
                    nav_status = "failed"
                state.navigation_attempts[url_key]["status"] = nav_status
                if self.navigation_guard is not None:
                    self.navigation_guard.finish_navigation(proposed_params.get("url"), nav_status)
        await self._notify_progress("action_executed", f"Action '{proposed_action_name}' executed (exit_code={tool_res.get('exit_code', 0)})")

        if proposed_action_name == "open_application" and (tool_res.get("exit_code") == 0 or tool_res.get("success") or tool_res.get("already_running")):
            raw_app = normalize_action_payload("open_application", {
                "application": tool_res.get("application") or proposed_params.get("application")
                or proposed_params.get("app_name") or proposed_params.get("name")
            })[1]["application"]
            if raw_app:
                state.launched_applications.add(raw_app)
                state.active_application = raw_app
                if tool_res.get("hwnd"):
                    state.active_hwnd = tool_res.get("hwnd")
                if tool_res.get("pid"):
                    state.active_pid = tool_res.get("pid")

        if tool_res.get("exit_code") != 0:
            state.consecutive_failures += 1
            state.planned_action_queue.clear()
            # FIX #1: Record this as a failed action (OS-level failure)
            state.last_failed_action = {"action": proposed_action_name, "params": proposed_params}
            state.last_failed_action_os_succeeded = False
            err_msg = str(tool_res.get("error") or "Unknown action execution failure")
            step_rec.error = f"Action execution failed: {err_msg}"
            step_rec.verification = {
                "success": False,
                "verified": False,
                "confidence": 0.0,
                "description": f"Action '{proposed_action_name}' failed: {err_msg}",
            }
            state.last_verification = step_rec.verification
            state.history.append(step_rec.to_dict())
            if proposed_action_name == "open_application" and tool_res.get("execution_status_uncertain"):
                state.status = "FAILED"
                state.error = (
                    f"Application launch status is uncertain for '{proposed_params.get('application', 'application')}'; "
                    "the Host Bridge response was lost. Stopped without retrying to avoid a duplicate launch."
                )
                step_rec.error = state.error
                state.history[-1] = step_rec.to_dict()
                await self._notify_progress("failed", state.error)
                self._finalize_metrics(state)
                return state, False
            if proposed_action_name == "open_url" and tool_res.get("execution_status_uncertain"):
                state.status = "FAILED"
                state.error = (
                    f"Navigation status is uncertain for '{proposed_params.get('url', 'URL')}'; "
                    "stopped without retrying to prevent duplicate browser actions."
                )
                step_rec.error = state.error
                state.history[-1] = step_rec.to_dict()
                await self._notify_progress("failed", state.error)
                self._finalize_metrics(state)
                return state, False
            if state.consecutive_failures >= self.config.max_consecutive_failures:
                state.status = "FAILED"
                state.error = f"Consecutive action failures: {err_msg}"
                await self._notify_progress("failed", state.error)
                self._finalize_metrics(state)
                return state, False
            return state, True

        # Brief pause to allow UI update before post-screenshot (skip for non-mutating take_screenshot)
        if self.config.action_pause_seconds > 0 and proposed_action_name not in ("take_screenshot", "screenshot"):
            await asyncio.sleep(self.config.action_pause_seconds)

        # 8. Verification: Fast-path deterministic OS verification vs. Vision Specialist
        is_det, det_desc = is_deterministically_verifiable(
            action=proposed_action_name,
            params=proposed_params,
            tool_result=tool_res,
            goal=state.goal,
        )

        if is_det:
            # Fast-path: Verified via OS Host Bridge evidence, bypassing Qwen2.5-VL to keep Qwen3 resident
            logger.info("[Iteration %d] Fast-path deterministic OS verification: %s", state.iteration, det_desc)
            await self._notify_progress("verified", f"Deterministic verification: {det_desc}")

            verif_res = {
                "success": True,
                "verified": True,
                "confidence": 0.95,
                "description": det_desc,
                "state_change_observed": proposed_action_name not in ("take_screenshot", "screenshot"),
                "active_window": tool_res.get("active_window", "") or (proposed_params.get("application", "") if proposed_action_name == "open_application" else (state.last_verification or {}).get("active_window", "")),
                "visible_text": (state.last_verification or {}).get("visible_text", []),
                "latency_seconds": 0.0,
                "was_resident": False,
                "keep_alive": "0",
                "model": "os_deterministic",
                "raw_response": det_desc,
            }
            step_rec.verification = verif_res
            step_rec.verification_latency_seconds = 0.0
            state.last_verification = verif_res
            state.consecutive_failures = 0
            state.last_failed_action = None
            state.last_failed_action_os_succeeded = False

            if proposed_action_name in ("take_screenshot", "screenshot"):
                # Reuse the screenshot just captured by execute_single_action("take_screenshot")
                # instead of performing a duplicate post-action capture.
                state.total_screenshot_seconds += act_latency
                prev_ss_b64 = (screenshot_data or {}).get("image_base64")
                new_ss_b64 = tool_res.get("image_base64") or prev_ss_b64 or ""
                ss_w = tool_res.get("width", (state.current_screenshot or {}).get("width", 1920))
                ss_h = tool_res.get("height", (state.current_screenshot or {}).get("height", 1080))
                ss_fp = tool_res.get("file_path") or (state.current_screenshot or {}).get("file_path")
                post_screenshot = {
                    "success": True,
                    "file_path": ss_fp,
                    "image_base64": new_ss_b64,
                    "width": ss_w,
                    "height": ss_h,
                    "latency_seconds": round(act_latency, 3),
                }
                step_rec.post_screenshot_file = ss_fp
                state.previous_screenshot = state.current_screenshot
                state.current_screenshot = post_screenshot

                # If the planner-requested screenshot yielded a new image (or visual data was not yet analyzed),
                # analyze it once so the planner receives fresh visual observations on the next step.
                if tool_res.get("image_base64") and (
                    not state.current_visual_data
                    or tool_res.get("image_base64") != prev_ss_b64
                ):
                    await self._release_planner_for_vision(state)
                    t_vis_ss = time.time()
                    vis_ss_res = await analyze_screenshot(
                        image_base64=tool_res["image_base64"],
                        query="List all currently open and visible application windows, buttons, input fields, coordinates, and readable text on screen.",
                        model=self.config.vision_model,
                        ollama_url=self.config.ollama_url,
                        keep_alive="0",
                    )
                    vis_ss_latency = time.time() - t_vis_ss
                    step_rec.verification_latency_seconds = vis_ss_latency
                    state.total_vision_seconds += vis_ss_latency
                    state.total_model_load_seconds += float(vis_ss_res.get("model_load_seconds") or 0.0)
                    state.total_model_inference_seconds += float(vis_ss_res.get("model_inference_seconds") or vis_ss_latency)
                    if not vis_ss_res.get("success"):
                        state.status = "FAILED"
                        state.error = f"Screenshot vision analysis failed: {vis_ss_res.get('error', 'vision model returned no result')}"
                        step_rec.error = state.error
                        state.history.append(step_rec.to_dict())
                        self.log_step(step_rec, state)
                        self._finalize_metrics(state)
                        return state, False
                    if vis_ss_res.get("success") and isinstance(vis_ss_res.get("structured"), dict):
                        state.current_visual_data = dict(vis_ss_res["structured"])

                existing_obs = list(state.current_visual_data.get("observations", []))
                ss_summary = tool_res.get("output") or det_desc
                if ss_summary and ss_summary not in existing_obs:
                    existing_obs.append(ss_summary)
                state.current_visual_data = {
                    "observations": existing_obs,
                    "windows": state.current_visual_data.get("windows", []),
                    "coordinates": state.current_visual_data.get("coordinates", []),
                    "readable_text": state.current_visual_data.get("readable_text", []),
                    "screenshot_info": {
                        "width": ss_w,
                        "height": ss_h,
                        "file_path": ss_fp,
                        "summary": ss_summary,
                    },
                }
            else:
                # Capture post-action screenshot for audit trail
                t_post_start = time.time()
                post_screenshot = await self.capture_screenshot()
                post_latency = time.time() - t_post_start
                state.total_screenshot_seconds += post_latency
                if post_screenshot.get("success"):
                    step_rec.post_screenshot_file = post_screenshot.get("file_path")
                    state.current_screenshot = post_screenshot

                # Update visual observations for next iteration's context
                if proposed_action_name == "open_application" and proposed_params.get("application"):
                    app_title = proposed_params.get("application")
                    state.current_visual_data = {
                        "observations": [f"Application '{app_title}' launched/active on desktop (OS verified)."],
                        "windows": [{"title": app_title, "state": "active"}],
                        "coordinates": state.current_visual_data.get("coordinates", []),
                        "readable_text": state.current_visual_data.get("readable_text", []),
                    }
                elif proposed_action_name in ("write_text", "type_text") and proposed_params.get("text"):
                    typed = proposed_params.get("text")
                    prev_obs = list(state.current_visual_data.get("observations", []))
                    prev_obs.append(f"Typed text: '{typed}'")
                    state.current_visual_data = {
                        "observations": prev_obs[-5:],
                        "windows": state.current_visual_data.get("windows", []),
                        "coordinates": state.current_visual_data.get("coordinates", []),
                        "readable_text": state.current_visual_data.get("readable_text", []),
                    }
        else:
            # Full visual verification path via Qwen2.5-VL
            logger.info("[Iteration %d] Capturing post-action screenshot for visual verification...", state.iteration)
            t_post_start = time.time()
            post_screenshot = await self.capture_screenshot()
            post_latency = time.time() - t_post_start
            state.total_screenshot_seconds += post_latency

            if post_screenshot.get("success"):
                step_rec.post_screenshot_file = post_screenshot.get("file_path")
                state.current_screenshot = post_screenshot

                # Combined Vision Verification + Observation (Qwen2.5-VL)
                verif_prompt = expected_outcome or f"Action {proposed_action_name} executed successfully"
                logger.info("[Iteration %d] Vision (%s) verify+observe '%s'...", state.iteration, self.config.vision_model, verif_prompt)
                await self._notify_progress("verifying", f"Verifying and observing: {verif_prompt}...")
                await self._release_planner_for_vision(state)
                t_verif_start = time.time()
                combined_res = await analyze_and_verify(
                    image_base64=post_screenshot["image_base64"],
                    expected_change=verif_prompt,
                    prior_context=f"Action: {proposed_action_name}, Params: {json.dumps(proposed_params)}",
                    observation_query="List all visible application windows, buttons, input fields, coordinates, and readable text.",
                    model=self.config.vision_model,
                    ollama_url=self.config.ollama_url,
                    keep_alive="0",  # Evict immediately; planner needs VRAM next iteration
                )
                verif_latency = time.time() - t_verif_start
                step_rec.verification_latency_seconds = verif_latency
                state.total_verification_seconds += verif_latency
                if not combined_res.get("success"):
                    state.status = "FAILED"
                    state.error = f"Visual verification failed: {combined_res.get('error', 'vision model returned no result')}"
                    step_rec.error = state.error
                    state.history.append(step_rec.to_dict())
                    self.log_step(step_rec, state)
                    await self._notify_progress("failed", state.error)
                    self._finalize_metrics(state)
                    return state, False

                # Record model lifecycle instrumentation
                if state.last_active_model is not None and state.last_active_model != self.config.vision_model:
                    state.model_swap_count += 1
                    state.total_model_swap_seconds += verif_latency
                elif state.last_active_model is None:
                    state.model_swap_count += 1
                    state.total_model_swap_seconds += verif_latency
                state.last_active_model = self.config.vision_model

                state.model_invocations.append({
                    "model": self.config.vision_model,
                    "phase": "vision_combined_verify_observe",
                    "start_time": t_verif_start,
                    "end_time": time.time(),
                    "latency": verif_latency,
                        "model_load_seconds": combined_res.get("model_load_seconds", 0.0),
                        "model_inference_seconds": combined_res.get("model_inference_seconds", verif_latency),
                    "keep_alive": "0",
                        "was_resident": combined_res.get("was_resident", False),
                    "total_task_elapsed_seconds": round(time.time() - state.start_time, 3),
                })

                logger.info(
                    "[Iteration %d] Verification timing: total=%.3fs load=%.3fs inference=%.3fs",
                    state.iteration, verif_latency,
                    float(combined_res.get("model_load_seconds") or 0.0),
                    float(combined_res.get("model_inference_seconds") or verif_latency),
                )

                # Extract verification result (backward-compatible format)
                verif_data = combined_res.get("verification", {})
                verif_res = {
                    "success": combined_res.get("success", False),
                    "verified": verif_data.get("verified", False),
                    "confidence": verif_data.get("confidence", 0.0),
                    "description": verif_data.get("description", ""),
                    "state_change_observed": verif_data.get("state_change_observed", False),
                    "active_window": verif_data.get("active_window", ""),
                    "visible_text": verif_data.get("visible_text", []),
                    "latency_seconds": verif_latency,
                    "was_resident": False,
                    "keep_alive": "0",
                    "model": self.config.vision_model,
                    "raw_response": combined_res.get("raw_response", ""),
                }

                step_rec.verification = verif_res
                state.last_verification = verif_res
                await self._notify_progress("verified", f"Visual verification: {'Success' if verif_res.get('verified') else 'Failed'} (confidence: {verif_res.get('confidence', 0):.2f})")

                if not verif_res.get("verified", False):
                    state.consecutive_failures += 1
                    state.planned_action_queue.clear()
                    # FIX #1: Record this as a failed action with OS success info
                    state.last_failed_action = {"action": proposed_action_name, "params": proposed_params}
                    state.last_failed_action_os_succeeded = (tool_res.get("exit_code") == 0)
                    logger.warning(
                        "Verification failed for step %d (%s): %s (os_succeeded=%s)",
                        state.iteration,
                        proposed_action_name,
                        verif_res.get("description"),
                        state.last_failed_action_os_succeeded,
                    )
                    # Clear stale visual data — force fresh observation on retry
                    state.current_visual_data = {}
                    if state.consecutive_failures >= self.config.max_consecutive_failures:
                        state.status = "VERIFICATION_FAILED"
                        state.error = f"Verification failed repeatedly: {verif_res.get('description')}"
                        state.history.append(step_rec.to_dict())
                        self.log_step(step_rec, state)
                        await self._notify_progress("failed", state.error)
                        self._finalize_metrics(state)
                        return state, False
                else:
                    # Reset consecutive failures on successful verified action
                    state.consecutive_failures = 0
                    # FIX #1: Clear last_failed_action on success
                    state.last_failed_action = None
                    state.last_failed_action_os_succeeded = False
                    # Store combined observation for the NEXT iteration's vision-skip
                    obs_data = combined_res.get("observation", {})
                    obs_text = verif_data.get("description", "")
                    active_win = verif_data.get("active_window", "")
                    state.current_visual_data = {
                        "observations": obs_data.get("observations", [obs_text] if obs_text else []),
                        "windows": obs_data.get("windows", [{"title": active_win, "state": "active"}] if active_win else []),
                        "coordinates": obs_data.get("coordinates", []),
                        "readable_text": obs_data.get("readable_text", verif_data.get("visible_text", [])),
                    }
            else:
                step_rec.verification = {
                    "verified": False,
                    "confidence": 0.0,
                    "description": "Could not capture post-action screenshot for verification",
                }
                state.planned_action_queue.clear()

        state.history.append(step_rec.to_dict())
        self.log_step(step_rec, state)

        # Continue loop
        return state, True

    async def run(
        self,
        goal: str,
        initial_state: Optional[ComputerUseState] = None,
        confirmed: bool = False,
    ) -> ComputerUseState:
        """Run the closed-loop computer use orchestrator until completion, pause, or failure."""
        if initial_state and initial_state.status == "AWAITING_CONFIRMATION" and confirmed:
            state = initial_state
            state.status = "RUNNING"
            state.pending_confirmation = None
        else:
            state = ComputerUseState(
                goal=goal,
                session_id=f"session_{int(time.time() * 1000)}",
                status="RUNNING",
                start_time=time.time(),
            )

        logger.info("Starting safe closed-loop computer-use session for goal: %s", goal)
        await self._notify_progress("started", f"Starting computer use for goal: {goal}")

        while state.status == "RUNNING":
            state, should_continue = await self.step(state, confirmed=confirmed)
            if not should_continue:
                break

        self._finalize_metrics(state)

        # Planner inference keeps the 8B model resident for follow-up turns;
        # release it when the run ends. Vision calls use keep_alive=0.
        if state.status in ("COMPLETED", "FAILED", "CANCELLED", "VERIFICATION_FAILED") and state.last_active_model == self.config.planner_model:
            unload_started = time.perf_counter()
            await asyncio.to_thread(unload_model_sync, self.config.planner_model, self.config.ollama_url)
            unload_elapsed = time.perf_counter() - unload_started
            state.total_model_transition_seconds += unload_elapsed
            self._finalize_metrics(state)
            logger.info("[Computer Use] Planner unload after run: %.3fs", unload_elapsed)

        logger.info(
            "Closed-loop session %s ended with status: %s (Total actions: %d, Elapsed: %.1fs, Swaps: %d)",
            state.session_id,
            state.status,
            state.action_count,
            state.elapsed_seconds,
            state.model_swap_count,
        )

        return state
