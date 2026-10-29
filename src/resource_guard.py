"""
src/resource_guard.py

System resource safety thresholds and observability policies for Jarvis/Odysseus.
Designed for 16 GB RAM / 6 GB VRAM laptop deployment.

Target Observability Thresholds:
- RAM Warning: >= 80% utilization
- RAM Critical: >= 90% utilization
- VRAM Warning: >= 80% utilization
- VRAM Critical: >= 90% utilization

Policy:
- When resources are elevated (>= Warning), new heavy model tasks and Computer Use tasks
  are queued or deferred rather than killed.
- Never terminate existing user tasks automatically.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Resource safety thresholds (observability & scheduling guidance)
RAM_WARNING_THRESHOLD: float = 0.80
RAM_CRITICAL_THRESHOLD: float = 0.90

VRAM_WARNING_THRESHOLD: float = 0.80
VRAM_CRITICAL_THRESHOLD: float = 0.90

# Default model context limits (KV-cache safety bounds)
DEFAULT_PLANNER_NUM_CTX: int = 4096
DEFAULT_PLANNER_NUM_PREDICT: int = 1024
DEFAULT_VISION_NUM_PREDICT: int = 200


def get_resource_policy_summary() -> Dict[str, Any]:
    """Retrieve the formal concurrency and resource policy definition."""
    return {
        "hardware_targets": {
            "ram_gb": 16.0,
            "vram_gb": 6.0,
        },
        "thresholds": {
            "ram_warning_pct": int(RAM_WARNING_THRESHOLD * 100),
            "ram_critical_pct": int(RAM_CRITICAL_THRESHOLD * 100),
            "vram_warning_pct": int(VRAM_WARNING_THRESHOLD * 100),
            "vram_critical_pct": int(VRAM_CRITICAL_THRESHOLD * 100),
        },
        "concurrency_policy": {
            "lightweight_tasks": "concurrent (web search, memory, reminders, notifications)",
            "qwen3_reasoning": "serialized (1 active generation stream, FIFO queue)",
            "qwen2_5_vl_vision": "on-demand single-task with immediate keep_alive: 0 eviction",
            "computer_use_controller": "strictly one active controller per desktop session",
            "model_residency": "Qwen3 warm in VRAM; vision model loaded on-demand and evicted post-inference",
        },
        "context_bounds": {
            "planner_num_ctx": DEFAULT_PLANNER_NUM_CTX,
            "planner_num_predict": DEFAULT_PLANNER_NUM_PREDICT,
            "vision_num_predict": DEFAULT_VISION_NUM_PREDICT,
        },
    }
