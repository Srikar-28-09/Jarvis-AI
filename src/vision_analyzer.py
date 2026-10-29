"""
src/vision_analyzer.py

Dedicated visual analyzer service for Jarvis Vision Phase.
Provides structured visual understanding using qwen2.5vl:3b while
ensuring Qwen3 remains the primary agent and VRAM is aggressively freed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger("src.vision_analyzer")

DEFAULT_VISION_MODEL = "qwen2.5vl:3b"


def _ollama_inference_seconds(body: Dict[str, Any], wall_seconds: float) -> float:
    """Use Ollama's server-side prompt/eval durations when available."""
    prompt_ns = float(body.get("prompt_eval_duration", 0) or 0)
    eval_ns = float(body.get("eval_duration", 0) or 0)
    measured = prompt_ns + eval_ns
    if measured > 0:
        return round(measured / 1_000_000_000, 3)
    load_seconds = float(body.get("load_duration", 0) or 0) / 1_000_000_000
    return round(max(0.0, wall_seconds - load_seconds), 3)


def get_ollama_base_url() -> str:
    """Resolve the default Ollama base URL with robust fallback and Docker awareness."""
    env_url = (os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_URL") or "").strip()
    if env_url:
        return normalize_ollama_base_url(env_url)
    
    in_docker = False
    try:
        in_docker = os.path.exists("/.dockerenv")
        if not in_docker and os.path.exists("/proc/1/cgroup"):
            with open("/proc/1/cgroup", "r", encoding="utf-8") as f:
                cg = f.read()
            in_docker = any(m in cg for m in ("docker", "containerd", "kubepods"))
    except Exception:
        in_docker = False
    
    if in_docker:
        return "http://host.docker.internal:11434"

    # Outside Docker, check localhost then docker bridge gateway
    for candidate in ["http://localhost:11434", "http://172.17.0.1:11434", "http://127.0.0.1:11434"]:
        try:
            req = urllib.request.Request(f"{candidate}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=0.5):
                return candidate
        except Exception:
            continue

    return "http://localhost:11434"


def normalize_ollama_base_url(raw_url: Optional[str]) -> str:
    """Ensure an Ollama base URL has a valid scheme, host, and no trailing API subpaths."""
    if not raw_url or not str(raw_url).strip():
        return get_ollama_base_url()
    
    url = str(raw_url).strip()
    if url.startswith("/") or "://" not in url:
        if "://" not in url and any(url.startswith(h) for h in ("localhost", "127.0.0.1", "host.docker.internal", "0.0.0.0")):
            url = f"http://{url}"
        else:
            return get_ollama_base_url()

    url = url.rstrip("/")
    if url.endswith("/v1/chat/completions"):
        url = url[:-len("/v1/chat/completions")].rstrip("/")
    elif url.endswith("/chat/completions"):
        url = url[:-len("/chat/completions")].rstrip("/")
    elif url.endswith("/api/chat"):
        url = url[:-len("/api/chat")].rstrip("/")
    elif url.endswith("/api/generate"):
        url = url[:-len("/api/generate")].rstrip("/")
    elif url.endswith("/api/tags"):
        url = url[:-len("/api/tags")].rstrip("/")
    elif url.endswith("/api/ps"):
        url = url[:-len("/api/ps")].rstrip("/")
    elif url.endswith("/v1"):
        url = url[:-len("/v1")].rstrip("/")
    elif url.endswith("/api"):
        url = url[:-len("/api")].rstrip("/")

    return url or get_ollama_base_url()


DEFAULT_OLLAMA_URL = get_ollama_base_url()
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB limit for raw images



def clean_base64_image(image_input: str) -> str:
    """Normalize base64 string by removing data URI headers and whitespace.

    Raises ValueError if the input is empty or contains invalid base64 characters.
    """
    if not image_input or not isinstance(image_input, str):
        raise ValueError("Image input must be a non-empty string")

    cleaned = image_input.strip()
    if cleaned.startswith("data:image/") and ";base64," in cleaned:
        cleaned = cleaned.split(";base64,", 1)[1].strip()

    # Validate base64 structure
    try:
        decoded = base64.b64decode(cleaned, validate=True)
        if len(decoded) == 0:
            raise ValueError("Decoded image content is empty")
        if len(decoded) > MAX_IMAGE_BYTES:
            raise ValueError(f"Image exceeds maximum allowable size of {MAX_IMAGE_BYTES} bytes")
    except Exception as e:
        raise ValueError(f"Invalid base64 image data: {e}")

    return cleaned


def check_model_resident(model: str, ollama_url: Optional[str] = None) -> bool:
    """Check whether a model is currently resident in Ollama via /api/ps."""
    try:
        base = normalize_ollama_base_url(ollama_url or DEFAULT_OLLAMA_URL)
        url = f"{base}/api/ps"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=0.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = data.get("models", [])
            for m in models:
                name = m.get("name", "")
                if model in name or name in model:
                    return True
            return False
    except Exception:
        return False


def unload_model_sync(model: str, ollama_url: Optional[str] = None, timeout: float = 3.0) -> bool:
    """Explicitly unload a model from Ollama VRAM by passing keep_alive=0."""
    try:
        base = normalize_ollama_base_url(ollama_url or DEFAULT_OLLAMA_URL)
        url = f"{base}/api/generate"
        payload = {"model": model, "keep_alive": 0}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def build_vision_prompt(query: Optional[str] = None) -> str:
    """Build a structured visual analysis prompt."""
    base_instructions = (
        "Analyze the provided screenshot carefully and thoroughly. Return a valid JSON object with the following schema:\n"
        "{\n"
        '  "observations": ["Detailed bullet point observations of everything visible on screen"],\n'
        '  "windows": [\n'
        '    {"title": "Window or application title", "state": "active|inactive|minimized", "region": "left|right|center|full|top|bottom"}\n'
        "  ],\n"
        '  "coordinates": [\n'
        '    {"element": "name or description of element", "box_2d": [ymin, xmin, ymax, xmax]}\n'
        "  ],\n"
        '  "readable_text": ["Any prominent clearly readable text labels, titles, or messages"]\n'
        "}\n"
    )
    if query and query.strip():
        base_instructions += f"\nUser specific question to focus on: {query.strip()}\n"

    base_instructions += "Return only valid JSON without extra conversational preamble."
    return base_instructions


def parse_structured_vision_output(raw_output: str) -> Dict[str, Any]:
    """Parse structured JSON from model response, with resilient fallbacks."""
    if not raw_output or not isinstance(raw_output, str):
        return {
            "observations": ["No visual output generated"],
            "windows": [],
            "coordinates": [],
            "raw_text": "",
        }

    text = raw_output.strip()
    # Strip thinking tags if present
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.DOTALL).strip()

    # Strip code fences if present
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            text = match.group(1).strip()

    # Extract first JSON object or array
    json_match = re.search(r"[\{\[][\s\S]*[\}\]]", text)
    if json_match:
        text = json_match.group(0)
    elif text.strip().startswith("{") or text.strip().startswith("["):
        text = text.strip()

    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        # Attempt resilient closure of truncated JSON
        for suffix in ["}", "\n}", "]}", "\n]}", '"]}', '"\n]}']:
            try:
                p = json.loads(text + suffix)
                if isinstance(p, (dict, list)):
                    parsed = p
                    break
            except Exception:
                pass

    if isinstance(parsed, dict):
        # Ensure required keys exist
        if "observations" not in parsed:
            parsed["observations"] = [raw_output.strip()]
        if "windows" not in parsed:
            parsed["windows"] = []
        if "coordinates" not in parsed:
            parsed["coordinates"] = []
        return parsed
    elif isinstance(parsed, list):
        return {
            "observations": [f"Visual elements detected: {len(parsed)}"],
            "windows": parsed,
            "coordinates": [],
            "raw_text": raw_output,
        }

    # Fallback to structured wrapper around freeform text
    return {
        "observations": [raw_output.strip()],
        "windows": [],
        "coordinates": [],
        "raw_text": raw_output.strip(),
    }


def analyze_screenshot_sync(
    image_base64: str,
    query: Optional[str] = None,
    model: str = DEFAULT_VISION_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    keep_alive: Any = "5m",
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """Synchronously call Ollama with a screenshot and extract visual observations.

    The ``keep_alive`` parameter controls how long Ollama keeps the model
    resident after inference.  Pass ``"0"`` to free VRAM immediately.
    """
    try:
        cleaned_b64 = clean_base64_image(image_base64)
    except ValueError as e:
        return {
            "success": False,
            "error": f"Image validation error: {e}",
            "reason": "malformed_image",
        }

    prompt = build_vision_prompt(query)
    messages = [
        {
            "role": "user",
            "content": prompt,
            "images": [cleaned_b64],
        }
    ]

    payload = {
        "model": model,
        "messages": messages,
        "format": "json",
        "stream": False,
        "keep_alive": keep_alive,
        "options": {
            "temperature": 0.0,
            "num_predict": 512,
        },
    }

    residency_started = time.perf_counter()
    was_resident = check_model_resident(model, ollama_url)
    residency_check_seconds = time.perf_counter() - residency_started
    t0 = time.perf_counter()

    base = normalize_ollama_base_url(ollama_url or DEFAULT_OLLAMA_URL)
    url = f"{base}/api/chat"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            content = body.get("message", {}).get("content", "")
            structured = parse_structured_vision_output(content)
            latency = round(time.perf_counter() - t0, 3)
            load_seconds = float(body.get("load_duration", 0) or 0) / 1_000_000_000
            return {
                "success": True,
                "model": model,
                "structured": structured,
                "raw_response": content,
                "latency_seconds": latency,
                "was_resident": was_resident,
                "residency_check_seconds": round(residency_check_seconds, 3),
                "model_load_seconds": round(load_seconds, 3),
                "model_inference_seconds": _ollama_inference_seconds(body, latency),
                "keep_alive": keep_alive,
                "eval_count": body.get("eval_count", 0),
                "prompt_eval_duration_ns": body.get("prompt_eval_duration", 0),
                "eval_duration_ns": body.get("eval_duration", 0),
            }
    except urllib.error.HTTPError as e:
        err_msg = f"Ollama HTTP {e.code}: {e.reason}"
        try:
            err_body = e.read().decode("utf-8")
            err_json = json.loads(err_body)
            err_msg = err_json.get("error", err_msg)
        except Exception:
            pass
        return {
            "success": False,
            "error": err_msg,
            "status_code": e.code,
            "reason": "ollama_error",
            "latency_seconds": round(time.perf_counter() - t0, 3),
            "was_resident": was_resident,
        }
    except (urllib.error.URLError, TimeoutError, ConnectionRefusedError, OSError) as e:
        return {
            "success": False,
            "error": f"Ollama vision service unavailable: {e}",
            "reason": "service_unavailable",
        }
    except Exception as e:
        logger.exception("Unexpected error in vision analysis: %s", e)
        return {
            "success": False,
            "error": f"Vision analysis failed: {e}",
            "reason": "unexpected_error",
        }


async def analyze_screenshot(
    image_base64: str,
    query: Optional[str] = None,
    model: str = DEFAULT_VISION_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    keep_alive: Any = "5m",
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """Asynchronously analyze a screenshot with the dedicated vision model."""
    return await asyncio.to_thread(
        analyze_screenshot_sync,
        image_base64=image_base64,
        query=query,
        model=model,
        ollama_url=ollama_url,
        keep_alive=keep_alive,
        timeout=timeout,
    )


def build_verification_prompt(expected_change: str, prior_context: Optional[str] = None) -> str:
    """Build a targeted visual verification prompt."""
    prompt = (
        "You are a visual verification specialist verifying the outcome of a GUI action.\n"
        f"Expected change to verify: {expected_change}\n"
    )
    if prior_context and prior_context.strip():
        prompt += f"Context of action taken: {prior_context.strip()}\n"
    prompt += (
        "\nCarefully inspect the screenshot to determine if the expected change actually occurred.\n"
        "Return a valid JSON object with the following schema:\n"
        "{\n"
        '  "verified": true|false,\n'
        '  "confidence": 0.0 to 1.0,\n'
        '  "description": "Clear explanation of what is visually observed regarding the expected change",\n'
        '  "state_change_observed": true|false,\n'
        '  "active_window": "Window or application title",\n'
        '  "visible_text": ["Key visible text labels or content"]\n'
        "}\n"
        "Return only valid JSON without extra conversational preamble."
    )
    return prompt


def parse_verification_output(raw_output: str) -> Dict[str, Any]:
    """Parse structured verification JSON from model response."""
    if not raw_output or not isinstance(raw_output, str):
        return {
            "verified": False,
            "confidence": 0.0,
            "description": "No visual output generated",
            "state_change_observed": False,
            "active_window": "",
            "visible_text": [],
        }

    text = raw_output.strip()
    # Strip thinking tags if present
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.DOTALL).strip()

    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            text = match.group(1).strip()

    json_match = re.search(r"\{[\s\S]*\}", text)
    if json_match:
        text = json_match.group(0)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            verified = bool(parsed.get("verified", False))
            confidence = float(parsed.get("confidence", 0.85 if verified else 0.2))
            desc = str(parsed.get("description", parsed.get("details", "")))
            state_change = bool(parsed.get("state_change_observed", verified))
            active_win = str(parsed.get("active_window", ""))
            vis_text = parsed.get("visible_text", [])
            if not isinstance(vis_text, list):
                vis_text = [str(vis_text)] if vis_text else []
            return {
                "verified": verified,
                "confidence": confidence,
                "description": desc or text,
                "state_change_observed": state_change,
                "active_window": active_win,
                "visible_text": vis_text,
            }
    except Exception:
        pass

    # Heuristic fallback if model returned natural language
    lower = text.lower()
    verified = any(w in lower for w in ["yes", "verified", "is visible", "successfully", "confirmed", "true", "present"]) and not any(w in lower for w in ["not visible", "not found", "failed", "cannot see", "no change", "false"])
    return {
        "verified": verified,
        "confidence": 0.6 if verified else 0.3,
        "description": text,
        "state_change_observed": verified,
        "active_window": "",
        "visible_text": [],
    }


def verify_visual_state_sync(
    image_base64: str,
    expected_change: str,
    prior_context: Optional[str] = None,
    model: str = DEFAULT_VISION_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    keep_alive: Any = "5m",
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """Synchronously verify whether an expected GUI state change occurred in a screenshot."""
    try:
        cleaned_b64 = clean_base64_image(image_base64)
    except ValueError as e:
        return {
            "success": False,
            "verified": False,
            "confidence": 0.0,
            "error": f"Image validation error: {e}",
            "reason": "malformed_image",
        }

    prompt = build_verification_prompt(expected_change, prior_context)
    messages = [
        {
            "role": "user",
            "content": prompt,
            "images": [cleaned_b64],
        }
    ]

    payload = {
        "model": model,
        "messages": messages,
        "format": "json",
        "stream": False,
        "keep_alive": keep_alive,
        "options": {
            "temperature": 0.0,
            "num_predict": 200,
        },
    }

    residency_started = time.perf_counter()
    was_resident = check_model_resident(model, ollama_url)
    residency_check_seconds = time.perf_counter() - residency_started
    t0 = time.perf_counter()

    base = normalize_ollama_base_url(ollama_url or DEFAULT_OLLAMA_URL)
    url = f"{base}/api/chat"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            content = body.get("message", {}).get("content", "")
            verification = parse_verification_output(content)
            latency = round(time.perf_counter() - t0, 3)
            load_seconds = float(body.get("load_duration", 0) or 0) / 1_000_000_000
            return {
                "success": True,
                "verified": verification["verified"],
                "confidence": verification["confidence"],
                "description": verification["description"],
                "state_change_observed": verification["state_change_observed"],
                "active_window": verification.get("active_window", ""),
                "visible_text": verification.get("visible_text", []),
                "latency_seconds": latency,
                "was_resident": was_resident,
                "keep_alive": keep_alive,
                "model": model,
                "raw_response": content,
            }
    except urllib.error.HTTPError as e:
        err_msg = f"Ollama HTTP {e.code}: {e.reason}"
        try:
            err_body = e.read().decode("utf-8")
            err_json = json.loads(err_body)
            err_msg = err_json.get("error", err_msg)
        except Exception:
            pass
        return {
            "success": False,
            "verified": False,
            "confidence": 0.0,
            "error": err_msg,
            "status_code": e.code,
            "reason": "ollama_error",
            "latency_seconds": round(time.time() - t0, 3),
            "was_resident": was_resident,
        }
    except (urllib.error.URLError, TimeoutError, ConnectionRefusedError, OSError) as e:
        return {
            "success": False,
            "verified": False,
            "confidence": 0.0,
            "error": f"Ollama vision service unavailable: {e}",
            "reason": "service_unavailable",
        }
    except Exception as e:
        logger.exception("Unexpected error in visual verification: %s", e)
        return {
            "success": False,
            "verified": False,
            "confidence": 0.0,
            "error": f"Visual verification failed: {e}",
            "reason": "unexpected_error",
        }


async def verify_visual_state(
    image_base64: str,
    expected_change: str,
    prior_context: Optional[str] = None,
    model: str = DEFAULT_VISION_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    keep_alive: Any = "5m",
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """Asynchronously verify whether an expected GUI state change occurred in a screenshot."""
    return await asyncio.to_thread(
        verify_visual_state_sync,
        image_base64=image_base64,
        expected_change=expected_change,
        prior_context=prior_context,
        model=model,
        ollama_url=ollama_url,
        keep_alive=keep_alive,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Combined Verification + Observation (single model call)
# ---------------------------------------------------------------------------

def build_combined_prompt(
    expected_change: str,
    prior_context: Optional[str] = None,
    observation_query: Optional[str] = None,
) -> str:
    """Build a prompt that combines action verification and screen observation."""
    prompt = (
        "You are analyzing a screenshot after a GUI action.\n\n"
        "TASK 1 - VERIFY:\n"
        f"Expected change: {expected_change}\n"
    )
    if prior_context and prior_context.strip():
        prompt += f"Action context: {prior_context.strip()}\n"
    prompt += "\nTASK 2 - OBSERVE the current screen state.\n"
    if observation_query and observation_query.strip():
        prompt += f"Focus: {observation_query.strip()}\n"
    prompt += (
        "\nReturn a single valid JSON object:\n"
        "{\n"
        '  "verified": true|false,\n'
        '  "confidence": 0.0 to 1.0,\n'
        '  "description": "Verification result",\n'
        '  "state_change_observed": true|false,\n'
        '  "active_window": "Window title",\n'
        '  "observations": ["Screen observations"],\n'
        '  "windows": [{"title": "...", "state": "active|inactive", "region": "..."}],\n'
        '  "coordinates": [{"element": "...", "box_2d": [ymin, xmin, ymax, xmax]}],\n'
        '  "readable_text": ["Visible text labels"]\n'
        "}\n"
        "Return only valid JSON."
    )
    return prompt


def parse_combined_output(raw_output: str) -> Dict[str, Any]:
    """Parse combined verification + observation JSON from model response."""
    empty: Dict[str, Any] = {
        "verification": {
            "verified": False, "confidence": 0.0,
            "description": "No visual output generated",
            "state_change_observed": False,
            "active_window": "", "visible_text": [],
        },
        "observation": {
            "observations": ["No visual output generated"],
            "windows": [], "coordinates": [], "readable_text": [],
        },
    }
    if not raw_output or not isinstance(raw_output, str):
        return empty

    text = raw_output.strip()
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.DOTALL).strip()
    if "```" in text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            text = match.group(1).strip()
    json_match = re.search(r"\{[\s\S]*\}", text)
    if json_match:
        text = json_match.group(0)

    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return empty
    except Exception:
        lower = text.lower()
        verified = (
            any(w in lower for w in ["yes", "verified", "is visible", "successfully", "confirmed", "true", "present"])
            and not any(w in lower for w in ["not visible", "not found", "failed", "cannot see", "no change", "false"])
        )
        return {
            "verification": {
                "verified": verified, "confidence": 0.6 if verified else 0.3,
                "description": text, "state_change_observed": verified,
                "active_window": "", "visible_text": [],
            },
            "observation": {
                "observations": [text], "windows": [],
                "coordinates": [], "readable_text": [],
            },
        }

    verified = bool(parsed.get("verified", False))
    confidence = float(parsed.get("confidence", 0.85 if verified else 0.2))
    desc = str(parsed.get("description", ""))
    state_change = bool(parsed.get("state_change_observed", verified))
    active_win = str(parsed.get("active_window", ""))
    vis_text = parsed.get("visible_text", parsed.get("readable_text", []))
    if not isinstance(vis_text, list):
        vis_text = [str(vis_text)] if vis_text else []

    observations = parsed.get("observations", [desc] if desc else [])
    if not isinstance(observations, list):
        observations = [str(observations)]
    windows = parsed.get("windows", [])
    if not isinstance(windows, list):
        windows = []
    coordinates = parsed.get("coordinates", [])
    if not isinstance(coordinates, list):
        coordinates = []
    readable_text = parsed.get("readable_text", vis_text)
    if not isinstance(readable_text, list):
        readable_text = [str(readable_text)] if readable_text else []

    return {
        "verification": {
            "verified": verified, "confidence": confidence,
            "description": desc or text, "state_change_observed": state_change,
            "active_window": active_win, "visible_text": vis_text,
        },
        "observation": {
            "observations": observations, "windows": windows,
            "coordinates": coordinates, "readable_text": readable_text,
        },
    }


def analyze_and_verify_sync(
    image_base64: str,
    expected_change: str,
    prior_context: Optional[str] = None,
    observation_query: Optional[str] = None,
    model: str = DEFAULT_VISION_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    keep_alive: Any = "0",
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """Combined screenshot analysis + verification in a single model call.

    Merges ``analyze_screenshot`` and ``verify_visual_state`` so the same
    screenshot is not sent to the vision model twice.  Returns a dict with
    ``verification`` and ``observation`` sub-dicts plus top-level metadata.

    ``keep_alive`` defaults to ``"0"`` so the vision model is evicted from
    VRAM immediately after inference, freeing memory for the planner.
    """
    try:
        cleaned_b64 = clean_base64_image(image_base64)
    except ValueError as e:
        return {
            "success": False,
            "error": f"Image validation error: {e}",
            "reason": "malformed_image",
            "verification": {
                "verified": False, "confidence": 0.0,
                "description": str(e), "state_change_observed": False,
                "active_window": "", "visible_text": [],
            },
            "observation": {
                "observations": [], "windows": [],
                "coordinates": [], "readable_text": [],
            },
        }

    prompt = build_combined_prompt(expected_change, prior_context, observation_query)
    messages = [{"role": "user", "content": prompt, "images": [cleaned_b64]}]

    payload = {
        "model": model,
        "messages": messages,
        "format": "json",
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"temperature": 0.0, "num_predict": 200},
    }

    residency_started = time.perf_counter()
    was_resident = check_model_resident(model, ollama_url)
    residency_check_seconds = time.perf_counter() - residency_started
    t0 = time.perf_counter()
    base = normalize_ollama_base_url(ollama_url or DEFAULT_OLLAMA_URL)
    url = f"{base}/api/chat"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            content = body.get("message", {}).get("content", "")
            combined = parse_combined_output(content)
            latency = round(time.perf_counter() - t0, 3)
            load_seconds = float(body.get("load_duration", 0) or 0) / 1_000_000_000
            return {
                "success": True,
                "model": model,
                "verification": combined["verification"],
                "observation": combined["observation"],
                "raw_response": content,
                "latency_seconds": latency,
                "was_resident": was_resident,
                "residency_check_seconds": round(residency_check_seconds, 3),
                "model_load_seconds": round(load_seconds, 3),
                "model_inference_seconds": _ollama_inference_seconds(body, latency),
                "prompt_eval_duration_ns": body.get("prompt_eval_duration", 0),
                "eval_duration_ns": body.get("eval_duration", 0),
                "keep_alive": keep_alive,
            }
    except urllib.error.HTTPError as e:
        err_msg = f"Ollama HTTP {e.code}: {e.reason}"
        try:
            err_body = e.read().decode("utf-8")
            err_json = json.loads(err_body)
            err_msg = err_json.get("error", err_msg)
        except Exception:
            pass
        return {
            "success": False, "error": err_msg,
            "status_code": e.code, "reason": "ollama_error",
            "latency_seconds": round(time.time() - t0, 3),
            "verification": {"verified": False, "confidence": 0.0, "description": err_msg, "state_change_observed": False, "active_window": "", "visible_text": []},
            "observation": {"observations": [], "windows": [], "coordinates": [], "readable_text": []},
        }
    except (urllib.error.URLError, TimeoutError, ConnectionRefusedError, OSError) as e:
        return {
            "success": False,
            "error": f"Ollama vision service unavailable: {e}",
            "reason": "service_unavailable",
            "verification": {"verified": False, "confidence": 0.0, "description": str(e), "state_change_observed": False, "active_window": "", "visible_text": []},
            "observation": {"observations": [], "windows": [], "coordinates": [], "readable_text": []},
        }
    except Exception as e:
        logger.exception("Unexpected error in combined vision analysis: %s", e)
        return {
            "success": False,
            "error": f"Combined vision analysis failed: {e}",
            "reason": "unexpected_error",
            "verification": {"verified": False, "confidence": 0.0, "description": str(e), "state_change_observed": False, "active_window": "", "visible_text": []},
            "observation": {"observations": [], "windows": [], "coordinates": [], "readable_text": []},
        }


async def analyze_and_verify(
    image_base64: str,
    expected_change: str,
    prior_context: Optional[str] = None,
    observation_query: Optional[str] = None,
    model: str = DEFAULT_VISION_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    keep_alive: Any = "0",
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """Async wrapper for combined screenshot analysis + verification."""
    return await asyncio.to_thread(
        analyze_and_verify_sync,
        image_base64=image_base64,
        expected_change=expected_change,
        prior_context=prior_context,
        observation_query=observation_query,
        model=model,
        ollama_url=ollama_url,
        keep_alive=keep_alive,
        timeout=timeout,
    )

