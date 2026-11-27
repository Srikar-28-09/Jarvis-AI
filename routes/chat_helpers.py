"""Shared helpers for chat routes — context building, post-response tasks, auth resolution."""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from core.models import ChatMessage
from core.database import SessionLocal
from core.database import Session as DBSession, ModelEndpoint
from src.llm_core import normalize_model_id
from src.endpoint_resolver import normalize_base
from src.context_compactor import maybe_compact, trim_for_context
from src.model_context import estimate_tokens, get_context_length
from src.auth_helpers import effective_user
from src.prompt_security import untrusted_context_message
from src.attachment_refs import attachment_ref
from src.text_helpers import strip_think
from src.simple_intents import follow_up_context_instruction
from routes.prefs_routes import _load_for_user as load_prefs_for_user

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_CASUAL_OPENING_RE = re.compile(
    r"^\s*(?:h+i+|hey+|hello+|yo+|sup+|what'?s up|wass?up|hiya|howdy|"
    r"lol|lmao|haha+|hehe+|thanks?|thank you|ty|idk|dunno|meh|bruh|bro)\b(?P<tail>.*)$",
    re.IGNORECASE,
)
_CASUAL_BLOCKLIST_RE = re.compile(
    r"\b(?:cookbook|serve|serving|launch|start|vllm|sglang|llama\.?cpp|ollama|"
    r"download|model|email|document|doc|note|calendar|task|search|web|research|"
    r"file|folder|repo|git|settings?|endpoint|api|token|mcp)\b",
    re.IGNORECASE,
)


def _is_casual_low_signal(text: str) -> bool:
    """Short greetings/slang should not pull memory, skills, RAG, or docs."""
    s = str(text or "").strip()
    m = _CASUAL_OPENING_RE.match(s)
    if not m:
        return False
    tail = m.group("tail") or ""
    if _CASUAL_BLOCKLIST_RE.search(tail):
        return False
    tail_words = re.findall(r"[A-Za-z0-9_'-]+", tail)
    return len(tail_words) <= 2


def _has_completed_exchange(messages: list[dict]) -> bool:
    last_user_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
        ),
        None,
    )
    if last_user_index is None:
        return False
    for message in messages[last_user_index + 1:]:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, list) and any(
            isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].strip()
            for block in content
        ):
            return True
    return False


# Strong references to in-flight fire-and-forget tasks scheduled from this
# module. asyncio only keeps weak references to tasks created via
# create_task, so without this the GC can collect a task mid-execution and
# the background work (extraction, auto-naming) silently never runs.
# Mirrors WebhookManager._spawn_tracked from src/webhook_manager.py.
_BG_TASKS: set[asyncio.Task] = set()
_INCOGNITO_CONTEXTS: dict[str, dict[str, Any]] = {}
_INCOGNITO_CONTEXT_TTL_SECONDS = 6 * 60 * 60
_INCOGNITO_CONTEXT_MAX_MESSAGES = 80


def _spawn_bg(coro) -> asyncio.Task:
    """Schedule tracked background work without inheriting a chat trace ID."""
    from src.request_timing import reset_request_context, set_request_context

    async def _run_untraced():
        # asyncio tasks inherit the creator's ContextVars. Naming and memory
        # work can be scheduled during a chat request, but must not be counted
        # as one of its foreground LLM calls or logged under its request ID.
        tokens = set_request_context("-")
        try:
            return await coro
        finally:
            reset_request_context(tokens)

    task = asyncio.create_task(_run_untraced())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


def _prune_incognito_contexts(now: float | None = None):
    now = now or time.time()
    stale = [
        sid for sid, bundle in _INCOGNITO_CONTEXTS.items()
        if now - float(bundle.get("updated_at") or 0) > _INCOGNITO_CONTEXT_TTL_SECONDS
    ]
    for sid in stale:
        _INCOGNITO_CONTEXTS.pop(sid, None)


def _incognito_messages(session_id: str) -> list[dict[str, Any]]:
    _prune_incognito_contexts()
    bundle = _INCOGNITO_CONTEXTS.get(str(session_id or ""))
    if not bundle:
        return []
    return [dict(m) for m in bundle.get("messages", []) if isinstance(m, dict)]


def add_answer_length_instruction(preface: list[dict[str, Any]], message: str):
    """Add a deterministic, turn-scoped format instruction when requested."""
    from src.simple_intents import answer_length_policy, requested_answer_format_instruction

    policy = answer_length_policy(message)
    if policy:
        preface.append({
            "role": "system",
            "content": f"## Requested answer format\n{policy.instruction}",
        })
    format_instruction = requested_answer_format_instruction(message)
    if format_instruction:
        preface.append({
            "role": "system",
            "content": f"## Requested presentation\n{format_instruction}",
        })
    return policy


def _append_incognito_message(session_id: str, role: str, content: Any, metadata: dict | None = None):
    sid = str(session_id or "").strip()
    if not sid:
        return
    _prune_incognito_contexts()
    bundle = _INCOGNITO_CONTEXTS.setdefault(sid, {"messages": [], "updated_at": time.time()})
    msg: dict[str, Any] = {"role": role, "content": content}
    if metadata:
        msg["metadata"] = dict(metadata)
    messages = bundle.setdefault("messages", [])
    messages.append(msg)
    if len(messages) > _INCOGNITO_CONTEXT_MAX_MESSAGES:
        del messages[:-_INCOGNITO_CONTEXT_MAX_MESSAGES]
    bundle["updated_at"] = time.time()


# ── Data containers ────────────────────────────────────────────────────── #

@dataclass
class PresetInfo:
    """Extracted preset parameters."""
    temperature: Optional[float]
    max_tokens: Optional[int]
    system_prompt: Optional[str]
    character_name: Optional[str]


@dataclass
class PreprocessedMessage:
    """Result of chat_handler.preprocess_message."""
    enhanced_message: str
    user_content: Any  # str or list (multimodal)
    text_for_context: str
    youtube_transcripts: list
    attachment_meta: list


@dataclass
class ChatContext:
    """Everything needed to call the LLM after context-building."""
    preface: list
    rag_sources: list
    web_sources: list
    used_memories: list
    messages: list
    context_length: int
    was_compacted: bool
    user: Optional[str]
    uprefs: dict
    preset: PresetInfo
    preprocessed: PreprocessedMessage
    context_trimmed: bool = False
    context_messages_before_trim: int = 0
    context_messages_after_trim: int = 0
    context_tokens_before_trim: int = 0
    context_tokens_after_trim: int = 0
    # Documents auto-created server-side during preprocess (e.g. when an
    # attached fillable PDF gets rendered into a markdown editor doc).
    # The chat route emits a doc_update SSE event for each before streaming
    # begins, so the editor pane switches to the new doc immediately.
    auto_opened_docs: list = field(default_factory=list)
    # Uploads attached to this user turn, resolved and owner-checked for the
    # agent's private context. This is not emitted to the browser.
    uploaded_files: list = field(default_factory=list)
    # Route-neutral prompt before any model-window compaction/trimming. This is
    # retained only when explicit foreground fallbacks are enabled so each
    # concrete candidate can apply its own context budget independently.
    route_messages: list = field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────── #

def _allowed_models_from_privileges(privs: dict) -> Optional[frozenset[str]]:
    if privs.get("block_all_models"):
        return frozenset()
    allowed_raw = privs.get("allowed_models")
    allowed = allowed_raw if isinstance(allowed_raw, list) else []
    restricted = bool(privs.get("allowed_models_restricted")) or bool(allowed)
    return frozenset(model for model in allowed if isinstance(model, str)) if restricted else None


def _allowed_models_for_request(request) -> Optional[frozenset[str]]:
    """Return the caller's model allowlist, or ``None`` when unrestricted."""

    try:
        user = effective_user(request)
    except Exception:
        user = None
    if not user:
        return None
    auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
    if not auth_manager:
        return None
    privs = auth_manager.get_privileges(user) or {}
    return _allowed_models_from_privileges(privs)

def _enforce_chat_privileges(request, sess) -> None:
    """Apply the per-user privilege gates (allowed_models + max_messages_per_day)
    that both /api/chat and /api/chat_stream must enforce BEFORE any LLM work.

    Raises HTTPException(403) if the session's model is not in the user's
    allowlist, or HTTPException(429) if the user has hit their daily message
    cap. No-op for unauthenticated callers or when auth_manager is absent
    (single-user mode). Admins receive ADMIN_PRIVILEGES from get_privileges,
    which means unrestricted allowed_models / zero cap -> no-op for them.
    """
    try:
        user = effective_user(request)
    except Exception:
        user = None
    if not user:
        return
    auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
    if not auth_manager:
        return

    privs = auth_manager.get_privileges(user) or {}

    # Explicit "block everything" sentinel takes precedence over the
    # allowlist — it's the only way to distinguish "user clicked [None]"
    # (block all) from "user clicked [All]" (no restriction), since both
    # otherwise produce an empty `allowed_models` list.
    if privs.get("block_all_models"):
        raise HTTPException(403, f"Your account is not allowed to use model '{sess.model}'.")

    allowed_models = _allowed_models_from_privileges(privs)
    if allowed_models is not None and sess.model and sess.model not in allowed_models:
        raise HTTPException(403, f"Your account is not allowed to use model '{sess.model}'.")

    cap = int(privs.get("max_messages_per_day") or 0)
    if cap <= 0:
        return

    from datetime import datetime as _dt, timedelta as _td
    from core.database import Session as _DbSess, ChatMessage as _Cm
    db = SessionLocal()
    try:
        count = (
            db.query(_Cm)
            .join(_DbSess, _Cm.session_id == _DbSess.id)
            .filter(_DbSess.owner == user,
                    _Cm.role == "user",
                    _Cm.timestamp >= _dt.utcnow() - _td(days=1))
            .count()
        )
    finally:
        db.close()
    if count >= cap:
        raise HTTPException(429, f"Daily message limit reached ({cap}). Try again in 24 hours.")


def needs_auto_name(name: str) -> bool:
    """Check if a session still has its default/placeholder name."""
    if not name:
        return True
    if name.startswith("Chat:") or name == "Chat":
        return True
    # Default frontend name: "modelname HH:MM:SS AM/PM"
    if re.match(r"^.+ \d{1,2}:\d{2}:\d{2}(\s*(AM|PM))?$", name, re.IGNORECASE):
        return True
    return False


async def auto_name_session(session_manager, sess):
    """Generate a short title for a session from its first user message."""
    try:
        from src.llm_core import llm_call_async
        from src.task_endpoint import resolve_task_endpoint

        # Find first user message
        first_msg = ""
        for msg in sess.history:
            if msg.role == "user":
                content = msg.content
                if isinstance(content, list):
                    content = next(
                        (i.get("text", "") for i in content if isinstance(i, dict) and i.get("type") == "text"),
                        "",
                    )
                first_msg = str(content)[:500]
                break

        if not first_msg:
            return

        owner = getattr(sess, "owner", None)
        t_url, t_model, t_headers = resolve_task_endpoint(
            sess.endpoint_url, sess.model, sess.headers, owner=owner
        )
        if not t_model:
            logger.debug("[auto-name] No model provided, skipping")
            return

        # max_tokens big enough that reasoning models (Minimax M2,
        # DeepSeek R1, QwQ, etc.) have headroom for <think>…</think>
        # plus the actual title — 200 used to clip them mid-reasoning
        # so strip_think left an empty string and no rename happened.
        # Timeout matches: 60s gives slow local reasoners room to finish.
        title = await llm_call_async(
            t_url,
            t_model,
            [
                {"role": "system", "content": "Generate a short title (3-6 words, no quotes) for a conversation that starts with this message. Reply with ONLY the title, nothing else. Do NOT include any thinking, reasoning, or explanation — just the title."},
                {"role": "user", "content": first_msg},
            ],
            temperature=0.3,
            max_tokens=4096,
            headers=t_headers,
            timeout=60,
            workload="background",
        )

        title = title.strip().strip('"\'').strip()
        # Strip <think>/<thinking> blocks (closed, dangling, or stray tags)
        # via the central helper.
        from src.text_helpers import strip_think
        title = strip_think(title, prose=False, prompt_echo=False)
        if title and len(title) < 80:
            session_manager.update_session_name(sess.id, title)
            logger.info(f"Auto-named session {sess.id}: {title}")

    except Exception as e:
        import traceback
        logger.error(f"Auto-name failed for {sess.id}: {e}\n{traceback.format_exc()}")


def extract_preset(chat_handler, preset_id) -> PresetInfo:
    """Extract preset parameters via chat_handler."""
    temperature, max_tokens, system_prompt, char_name = (
        chat_handler.validate_and_extract_preset(preset_id)
    )
    return PresetInfo(
        temperature=temperature,
        max_tokens=max_tokens,
        system_prompt=system_prompt,
        character_name=char_name,
    )


async def preprocess(
    chat_handler, message, att_ids, sess,
    auto_opened_docs: Optional[list] = None,
    allow_tool_preprocessing: bool = True,
) -> PreprocessedMessage:
    """Run chat_handler.preprocess_message and wrap the result."""
    enhanced, user_content, text_ctx, yt_transcripts, att_meta = (
        await chat_handler.preprocess_message(
            message,
            att_ids,
            sess,
            auto_opened_docs=auto_opened_docs,
            allow_tool_preprocessing=allow_tool_preprocessing,
        )
    )
    return PreprocessedMessage(
        enhanced_message=enhanced,
        user_content=user_content,
        text_for_context=text_ctx,
        youtube_transcripts=yt_transcripts,
        attachment_meta=att_meta,
    )


def build_uploaded_file_manifest(att_ids: list, upload_handler, owner: Optional[str]) -> list[dict]:
    """Resolve current-turn upload IDs into a small tool-facing manifest.

    The chat UI already sends attachment ids, and preprocessing inlines as much
    text as fits. Agent mode still needs a discoverable bridge for files whose
    content was truncated/omitted or when the model chooses file tools. Only
    owner-authorized uploads are included, and paths must remain inside the
    configured upload directory.
    """
    if not att_ids or not upload_handler or not hasattr(upload_handler, "resolve_upload"):
        return []

    def _read_file_can_open(path: str) -> bool:
        try:
            from src.tool_execution import _resolve_tool_path

            return _resolve_tool_path(path) == os.path.realpath(path)
        except Exception:
            return False

    manifest: list[dict] = []
    for att_id in att_ids:
        try:
            info = upload_handler.resolve_upload(str(att_id), owner=owner)
        except Exception:
            logger.debug("Failed to resolve upload %r for agent manifest", att_id, exc_info=True)
            continue
        if not isinstance(info, dict):
            continue

        path = info.get("path")
        if path:
            try:
                inside = True
                if hasattr(upload_handler, "_inside_upload_dir"):
                    inside = bool(upload_handler._inside_upload_dir(path))
                elif hasattr(upload_handler, "inside_base_dir"):
                    inside = bool(upload_handler.inside_base_dir(path))
                if not inside or not os.path.exists(path) or not _read_file_can_open(path):
                    path = None
            except Exception:
                path = None

        ref = attachment_ref({**info, "id": info.get("id") or str(att_id)})
        ref.update({
            "id": ref["attachment_id"],
            "uri": f"odysseus://attachment/{ref['attachment_id']}",
            "read_policy": "owner_checked_upload",
            # Transitional compatibility: existing built-in tools can still use
            # this path, but only after owner, upload-root, and tool-root checks.
            "path": path,
        })
        manifest.append(ref)
    return manifest


def add_user_message(sess, chat_handler, preprocessed: PreprocessedMessage, incognito: bool = False):
    """Add user message to session history and update session name.
    Incognito messages must not mutate persistent session history, even in
    memory, because a later normal turn can persist the same session object."""
    if incognito:
        return
    user_meta = {"attachments": preprocessed.attachment_meta} if preprocessed.attachment_meta else None
    sess.add_message(ChatMessage("user", preprocessed.user_content, metadata=user_meta))
    chat_handler.update_session_name_if_needed(sess, preprocessed.text_for_context)


def fire_message_event(request, webhook_manager, session_id: str, sess, message: str, compare_mode: bool = False):
    """Fire webhook and event_bus events for a new user message."""
    if webhook_manager and not compare_mode:
        webhook_manager.fire_and_forget("chat.message", {
            "session_id": session_id, "model": sess.model, "message": message[:2000],
        })
    from src.event_bus import fire_event
    user = effective_user(request)
    fire_event("message_sent", user)


def _session_url_matches_endpoint(session_url: str, endpoint_base: str) -> bool:
    if not session_url or not endpoint_base:
        return False
    try:
        from src.endpoint_resolver import build_chat_url, normalize_base

        sess_url = session_url.rstrip("/")
        base = normalize_base(endpoint_base).rstrip("/")
        return sess_url in {
            base,
            base + "/chat/completions",
            build_chat_url(base).rstrip("/"),
        }
    except Exception:
        return False


def _has_auth_keys(headers) -> bool:
    """True if a headers dict carries an Authorization/x-api-key entry."""
    return isinstance(headers, dict) and any(
        k.lower() in ('authorization', 'x-api-key') for k in headers
    )


def resolve_session_auth(sess, session_id: str, owner: Optional[str] = None):
    """Ensure session has auth headers — resolve from endpoint DB if missing."""
    try:
        from src.chatgpt_subscription import is_chatgpt_subscription_base
        is_chatgpt_subscription = is_chatgpt_subscription_base(getattr(sess, "endpoint_url", "") or "")
    except Exception:
        is_chatgpt_subscription = False
    has_auth = _has_auth_keys(sess.headers)
    if has_auth and not is_chatgpt_subscription:
        return

    try:
        from src.endpoint_resolver import build_headers, resolve_endpoint_runtime
        db = SessionLocal()
        try:
            target_url = getattr(sess, "endpoint_url", "") or ""
            if not target_url:
                return
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
            if owner:
                # Missing headers usually means "recover from the saved endpoint".
                # Scope that lookup to the session owner, otherwise two users
                # with similar endpoint URLs can borrow each other's API key.
                from src.auth_helpers import owner_filter
                q = owner_filter(q, ModelEndpoint, owner)
            for ep in q.all():
                if not _session_url_matches_endpoint(target_url, ep.base_url or ""):
                    continue
                try:
                    base, api_key = resolve_endpoint_runtime(ep, owner=owner)
                except Exception as e:
                    logger.warning("Failed to resolve provider auth for session %s: %s", session_id, e)
                    return
                if not api_key:
                    # No usable key (e.g. ChatGPT Subscription needs re-auth).
                    return
                sess.headers = build_headers(api_key, base)
                if is_chatgpt_subscription:
                    # The bearer is short-lived and re-resolved per request, so it
                    # stays request-local and is never written to the plaintext
                    # sessions.headers column. Proactively strip any bearer an
                    # older code path may have persisted so it does not linger.
                    stale_q = db.query(DBSession).filter(DBSession.id == session_id)
                    if owner:
                        stale_q = stale_q.filter(DBSession.owner == owner)
                    stored = stale_q.first()
                    if stored is not None and _has_auth_keys(stored.headers):
                        stale_q.update({"headers": {}})
                        db.commit()
                        logger.info(f"Cleared persisted ChatGPT Subscription bearer from session {session_id}")
                    logger.debug(f"Resolved request-local ChatGPT Subscription auth for session {session_id}")
                    return
                update_q = db.query(DBSession).filter(DBSession.id == session_id)
                if owner:
                    update_q = update_q.filter(DBSession.owner == owner)
                update_q.update({"headers": sess.headers})
                db.commit()
                logger.info(f"Resolved and persisted auth headers for session {session_id} from endpoint {ep.name}")
                return
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Failed to resolve session headers: {e}")


def _match_cached_model_id(requested: str, models) -> Optional[str]:
    if not requested or not models:
        return None
    model_ids = [str(m) for m in models if m]
    if requested in model_ids:
        return requested

    req_base = os.path.basename(requested.rstrip("/"))
    for model_id in model_ids:
        if os.path.basename(model_id.rstrip("/")) == req_base:
            return model_id
    return None


def _normalize_model_id_from_cache(sess) -> Optional[str]:
    """Use stored endpoint model IDs before falling back to a live /models probe."""
    endpoint_url = getattr(sess, "endpoint_url", "") or ""
    requested = getattr(sess, "model", "") or ""
    if not endpoint_url or not requested:
        return None

    try:
        session_base = normalize_base(endpoint_url)
    except Exception:
        session_base = endpoint_url.rstrip("/")
    if not session_base:
        return None

    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        owner = getattr(sess, "owner", None)
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        endpoints = q.all()
        for ep in endpoints:
            try:
                if normalize_base(getattr(ep, "base_url", "") or "") != session_base:
                    continue
            except Exception:
                continue

            raw_models = getattr(ep, "cached_models", None)
            if not raw_models:
                continue
            try:
                models = json.loads(raw_models) if isinstance(raw_models, str) else raw_models
            except Exception:
                continue

            matched = _match_cached_model_id(requested, models)
            if matched:
                return matched
    except Exception as e:
        logger.debug("Cached model normalization skipped: %s", e)
    finally:
        db.close()

    return None


def _session_is_research_spinoff(sess) -> bool:
    """True if this session was created via research "Discuss" spin-off.

    Detected by the primer system message the spin-off endpoint seeds into
    history (metadata ``research_spinoff_from``). Such sessions are grounded
    on the seeded report, so global memory + personal-doc RAG injection is
    suppressed for them (the report is the sole knowledge base). Handles both
    ChatMessage objects and plain dicts.
    """
    for m in getattr(sess, "history", []) or []:
        role = getattr(m, "role", None)
        if role is None and isinstance(m, dict):
            role = m.get("role")
        if role != "system":
            continue
        md = getattr(m, "metadata", None)
        if md is None and isinstance(m, dict):
            md = m.get("metadata")
        if (md or {}).get("research_spinoff_from"):
            return True
    return False


async def build_chat_context(
    sess,
    request,
    chat_handler,
    chat_processor,
    message: str,
    session_id: str,
    preset_id=None,
    att_ids: list = None,
    use_web=None,
    use_rag=None,
    use_research=None,
    time_filter=None,
    incognito: bool = False,
    no_memory: bool = False,
    search_context: str = None,
    compare_mode: bool = False,
    webhook_manager=None,
    use_enhanced_message: bool = False,
    agent_mode: bool = False,
    allow_tool_preprocessing: bool = True,
    defer_context_shaping: bool = False,
    continuation_context_message: str | None = None,
    persist_user_message: bool = True,
) -> ChatContext:
    """Build the full context (preface + messages) for an LLM call.

    This is the shared logic between /chat and /chat_stream — preset extraction,
    message preprocessing, memory/RAG/web injection, compaction, normalization.
    """
    # Preset
    preset = extract_preset(chat_handler, preset_id)

    # Preprocess message (CoT, YouTube, VL images, build content). The
    # auto_opened_docs collector captures any docs created server-side
    # (e.g. fillable PDF → markdown editor doc) so the chat route can
    # announce them to the frontend before streaming.
    auto_opened_docs: list = []
    preprocessed = await preprocess(
        chat_handler, message, att_ids or [], sess,
        auto_opened_docs=auto_opened_docs,
        allow_tool_preprocessing=allow_tool_preprocessing,
    )

    # Add user message to history. Nobody/incognito uses a request-local
    # transcript store instead of session history so stale saved chats cannot
    # bleed into context and the turn is not persisted.
    if persist_user_message and incognito:
        user_meta = {"attachments": preprocessed.attachment_meta} if preprocessed.attachment_meta else None
        _append_incognito_message(session_id, "user", preprocessed.user_content, user_meta)
    elif persist_user_message:
        add_user_message(sess, chat_handler, preprocessed, incognito=False)

    # Fire events
    if persist_user_message and not incognito:
        fire_message_event(request, webhook_manager, session_id, sess, message, compare_mode)

    # Resolve owner-scoped prefs/context. Browser requests keep the cookie user;
    # bearer-token chat requests use the token owner instead of the "api" sentinel.
    user = effective_user(request)
    uprefs = load_prefs_for_user(user)
    uploaded_files = build_uploaded_file_manifest(
        att_ids or [],
        getattr(chat_handler, "upload_handler", None),
        getattr(sess, "owner", None),
    )
    context_message = (
        str(continuation_context_message).strip()
        if continuation_context_message
        else message
    )
    casual_low_signal = _is_casual_low_signal(context_message)

    # Memory enabled?
    mem_enabled = not incognito and not no_memory and uprefs.get("memory_enabled", True)
    # Skills injection respects its own enable toggle (mirrors memory_enabled).
    # When off, the "Available skills" index is not added to the prompt.
    skills_enabled = not incognito and uprefs.get("skills_enabled", True)
    if not allow_tool_preprocessing:
        mem_enabled = False
        skills_enabled = False
    if casual_low_signal:
        mem_enabled = False
        skills_enabled = False
    logger.debug(
        "Memory enabled=%s for user=%s (incognito=%s, no_memory=%s, pref=%s)",
        mem_enabled, user, incognito, no_memory, uprefs.get("memory_enabled", "NOT_SET"),
    )

    # Research-spinoff ("Discuss") sessions are grounded on the seeded report:
    # the primer system message IS the knowledge base. Injecting global memory
    # or personal-doc RAG on every turn pulls in keyword-matched but off-topic
    # facts ("wrong data") and competes with the report, so suppress both here.
    is_research_spinoff = _session_is_research_spinoff(sess)
    if is_research_spinoff:
        mem_enabled = False

    # Use RAG?
    use_rag_val = (str(use_rag).lower() != "false") if use_rag is not None else True
    if incognito or not allow_tool_preprocessing or is_research_spinoff or casual_low_signal:
        use_rag_val = False

    # If pre-fetched search context was provided (compare mode), skip live web search
    skip_web = bool(search_context) or not allow_tool_preprocessing or casual_low_signal

    # Build context preface
    # The stream path uses enhanced_message (with CoT/preprocessing applied),
    # the sync path uses text_for_context.
    _ctx_msg = (
        context_message
        if continuation_context_message
        else (
            preprocessed.enhanced_message
            if use_enhanced_message
            else preprocessed.text_for_context
        )
    )
    # Form fields arrive as strings. In particular, the literal "false" is
    # truthy in Python, so passing it through here used to trigger a web search
    # (and its query-extraction LLM call) after the route had disabled search.
    use_web_val = str(use_web).lower() == "true" if use_web is not None else False
    _preface_kwargs = dict(
        message=_ctx_msg,
        session=sess,
        use_web=use_web_val and not skip_web,
        use_memory=mem_enabled,
        time_filter=time_filter,
        preset_system_prompt=preset.system_prompt,
        owner=user,
        character_name=preset.character_name,
        agent_mode=agent_mode,
        incognito=incognito,
        use_skills=skills_enabled,
    )
    if use_rag is not None or is_research_spinoff or casual_low_signal:
        _preface_kwargs["use_rag"] = use_rag_val
    preface, rag_sources, web_sources = chat_processor.build_context_preface(**_preface_kwargs)

    # Capture used memories immediately
    used_memories = getattr(chat_processor, '_last_used_memories', [])

    # Inject pre-fetched search context (compare mode)
    if search_context and allow_tool_preprocessing and not casual_low_signal:
        preface.append(untrusted_context_message("prefetched search context", search_context))

    # YouTube transcripts
    for transcript in preprocessed.youtube_transcripts:
        preface.append(untrusted_context_message("youtube transcript", transcript))

    add_answer_length_instruction(preface, message)

    # Normalize model ID. Prefer cached endpoint models so group chat does not
    # re-hit slow local /models endpoints on every participant turn.
    norm = _normalize_model_id_from_cache(sess) or normalize_model_id(
        sess.endpoint_url,
        sess.model,
        owner=getattr(sess, "owner", None),
    )
    if norm:
        sess.model = norm

    # Build messages. In Nobody/incognito mode, never read saved session
    # history: the session id may be a temporary wrapper or, in buggy clients, a
    # stale normal session id. Only the ephemeral incognito transcript is safe.
    conversation_messages = (
        _incognito_messages(session_id)
        if incognito
        else sess.get_context_messages()
    )
    if persist_user_message and not continuation_context_message:
        prior_messages = list(conversation_messages)
        if prior_messages and prior_messages[-1].get("role") == "user":
            prior_messages.pop()
        follow_up_instruction = follow_up_context_instruction(
            message,
            has_prior_exchange=_has_completed_exchange(prior_messages),
        )
        if follow_up_instruction:
            preface.append({
                "role": "system",
                "content": follow_up_instruction,
                "_protected": True,
            })
    messages = preface + conversation_messages

    # Current date/time — injected as a standalone *user*-role context message
    # placed immediately before the latest user turn, NOT folded into the
    # system prompt. Its text changes every minute, and local OpenAI-compatible
    # backends (llama.cpp / LM Studio) key their KV-cache prefix off the
    # system message byte-for-byte; mixing ever-changing timestamp text into
    # it would invalidate the cached prefix on every request (issue #2927).
    # Placing it at the tail also keeps it out of the stable
    # preface+history prefix, so that prefix stays byte-identical turn over
    # turn (modulo the genuinely new history entries) and the cache survives.
    if not agent_mode:
        try:
            from src.user_time import current_datetime_context_message
            _dt_msg = current_datetime_context_message()
            if messages and messages[-1].get("role") == "user":
                messages.insert(len(messages) - 1, _dt_msg)
            else:
                messages.append(_dt_msg)
        except Exception:
            logger.debug("Failed to add current date/time context", exc_info=True)

    route_messages = list(messages)
    # Explicit fallback routing must shape from the same route-neutral prompt
    # for every candidate. Running selected-model compaction here would mutate
    # session history before we know which route can answer and would make a
    # later larger-context candidate unable to recover discarded history.
    if defer_context_shaping:
        context_length = get_context_length(sess.endpoint_url, sess.model)
        was_compacted = False
    else:
        messages, context_length, was_compacted = await maybe_compact(
            sess, sess.endpoint_url, sess.model, messages, sess.headers, owner=user,
        )
    _before_trim_messages = len(messages)
    _before_trim_tokens = estimate_tokens(messages)
    if not defer_context_shaping:
        messages = trim_for_context(messages, context_length)
    _after_trim_messages = len(messages)
    _after_trim_tokens = estimate_tokens(messages)
    _context_trimmed = _after_trim_messages < _before_trim_messages or _after_trim_tokens < _before_trim_tokens

    return ChatContext(
        preface=preface,
        rag_sources=rag_sources,
        web_sources=web_sources,
        used_memories=used_memories,
        messages=messages,
        context_length=context_length,
        was_compacted=was_compacted,
        user=user,
        uprefs=uprefs,
        preset=preset,
        preprocessed=preprocessed,
        context_trimmed=_context_trimmed,
        context_messages_before_trim=_before_trim_messages,
        context_messages_after_trim=_after_trim_messages,
        context_tokens_before_trim=_before_trim_tokens,
        context_tokens_after_trim=_after_trim_tokens,
        auto_opened_docs=auto_opened_docs,
        uploaded_files=uploaded_files,
        route_messages=route_messages,
    )


def accumulate_token_usage(session_id: str, metrics: dict):
    """Add input/output token counts to the session's running totals."""
    in_t = metrics.get("input_tokens", 0)
    out_t = metrics.get("output_tokens", 0)
    if not (in_t or out_t):
        return
    db = SessionLocal()
    try:
        db_s = db.query(DBSession).filter(DBSession.id == session_id).first()
        if db_s:
            db_s.total_input_tokens = (db_s.total_input_tokens or 0) + in_t
            db_s.total_output_tokens = (db_s.total_output_tokens or 0) + out_t
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def public_response_delta(data: dict) -> str:
    """Return only answer-channel text from a structured model stream event."""
    if not isinstance(data, dict) or data.get("thinking") is True:
        return ""
    delta = data.get("delta")
    return delta if isinstance(delta, str) else ""


def clean_thinking_for_save(content: str, metadata: dict | None = None) -> tuple[str, dict]:
    """Remove private model reasoning before saving a user-facing response."""
    md = dict(metadata) if metadata else {}
    md.pop("thinking", None)
    md.pop("thinking_time", None)
    return strip_think(content, prose=False, prompt_echo=False), md


def require_user_facing_answer(response: Any) -> str:
    """Reject an empty final answer instead of reporting a blank success."""
    if not isinstance(response, str) or not response.strip():
        raise HTTPException(
            status_code=502,
            detail="The model returned no user-facing answer. Please try again.",
        )
    return response


def save_assistant_response(
    sess,
    session_manager,
    session_id: str,
    full_response: str,
    last_metrics: dict | None,
    *,
    character_name: str = None,
    web_sources: list = None,
    rag_sources: list = None,
    research_sources: list = None,
    used_memories: list = None,
    do_research: bool = False,
    tool_events: list = None,
    incognito: bool = False,
):
    """Add assistant response to session history.

    Incognito responses are intentionally not added to the session object. The
    session may later be saved by a normal turn, so "in-memory only" is not
    private enough.
    """
    md = dict(last_metrics) if last_metrics else {}
    def _model_value(value) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.strip()

    requested_model = _model_value(md.get("requested_model") or md.get("selected_model") or getattr(sess, "model", ""))
    actual_model = _model_value(md.get("model") or md.get("actual_model") or requested_model)
    if requested_model:
        md["requested_model"] = requested_model
    if actual_model:
        md["model"] = actual_model
    if character_name:
        md["character_name"] = character_name
    if web_sources:
        md["web_sources"] = web_sources
    if rag_sources:
        md["rag_sources"] = rag_sources
    if research_sources:
        md["research_sources"] = research_sources
    if used_memories:
        md["memories_used"] = used_memories
    if do_research and not research_sources:
        md["research_clarification"] = True
    if tool_events:
        md["tool_events"] = tool_events

    md.pop("thinking", None)
    md.pop("thinking_time", None)
    if isinstance(md.get("round_texts"), list):
        md["round_texts"] = [
            strip_think(text, prose=False, prompt_echo=False)
            if isinstance(text, str)
            else text
            for text in md["round_texts"]
        ]
    _content = strip_think(full_response, prose=False, prompt_echo=False)
    if incognito:
        _append_incognito_message(session_id, "assistant", _content, md)
        return None
    sess.add_message(ChatMessage("assistant", _content, metadata=md))

    from core.database import update_session_last_accessed
    update_session_last_accessed(session_id)
    session_manager.save_sessions()

    # Return the persisted message's DB id so the stream can wire it onto the
    # freshly-rendered bubble — lets the user edit/delete a just-streamed reply
    # without reloading.
    try:
        _last = sess.history[-1]
        _meta = getattr(_last, "metadata", None)
        if isinstance(_meta, dict):
            return _meta.get("_db_id")
    except (IndexError, AttributeError):
        pass
    return None


def _is_session_stream_active(session_id: str) -> bool:
    """Best-effort check for "is a chat completion currently streaming for
    this session?" — used to keep background extraction from overlapping a
    main completion and competing for the local backend's processing slots
    (issue #2927). Lazily imports the route module's live registry to avoid
    a circular import (chat_routes imports this module at load time)."""
    try:
        from routes import chat_routes as _cr
        return session_id in getattr(_cr, "_active_streams", {})
    except Exception:
        return False


async def _run_extraction_jobs_sequentially(session_id: str, jobs: list, max_wait_s: float = 120.0):
    """Run queued background-extraction coroutines one at a time, only once
    no chat completion is actively streaming for this session.

    As diagnosed in issue #2927, firing memory/skill extraction concurrently
    with the main chat completion (or with each other) makes them compete for
    the local backend's limited processing slots, evicting the main
    conversation's cached KV-cache checkpoint and forcing a full prompt
    re-evaluation on the next turn. Waiting for the stream to go idle and then
    running the jobs strictly in sequence keeps at most one "side" request in
    flight against the backend at any time, and never alongside the user's
    own conversation.
    """
    # Wait for the triggering turn's own stream to finish winding down (it
    # almost always already has by the time this task gets scheduled — this
    # is a small safety margin, not the primary mechanism).
    waited = 0.0
    poll = 0.25
    while _is_session_stream_active(session_id) and waited < max_wait_s:
        await asyncio.sleep(poll)
        waited += poll

    for name, job in jobs:
        # Re-check before each job: a fast follow-up message from the user
        # may have started a new stream for this session while we waited.
        waited = 0.0
        while _is_session_stream_active(session_id) and waited < max_wait_s:
            await asyncio.sleep(poll)
            waited += poll
        try:
            await job
        except Exception:
            logger.warning("[bg-extract] %s extraction job failed for session %s", name, session_id, exc_info=True)


def run_post_response_tasks(
    sess,
    session_manager,
    session_id: str,
    message: str,
    full_response: str,
    last_metrics: dict | None,
    uprefs: dict,
    memory_manager,
    memory_vector,
    webhook_manager,
    *,
    incognito: bool = False,
    compare_mode: bool = False,
    character_name: str = None,
    agent_rounds: int = 0,
    agent_tool_calls: int = 0,
    skills_manager=None,
    owner: str = None,
    extract_skills: bool = True,
    allow_background_extraction: bool = True,
):
    """Fire background tasks after a completed response: memory extraction, webhooks, auto-name, skill extraction.

    Memory/skill extraction are queued to run *sequentially*, after the main
    completion stream for this session has fully wound down — never
    concurrently with it or with each other. As diagnosed in issue #2927,
    firing these "side" LLM calls in parallel with the main chat completion
    makes them compete for the local backend's limited processing slots
    (llama.cpp defaults to 4), evicting the main conversation's cached
    checkpoint and forcing a full prompt re-evaluation on the next turn. By
    the time this function runs the main response is already saved, but the
    extraction calls themselves are still async — queuing them through
    ``_queue_background_extraction`` keeps them from overlapping the *next*
    turn's request too.
    """
    _extraction_jobs: list = []

    # Explicit memory extraction — immediately persist explicit memory commands
    # ("Remember that...", "Note that...", etc.) without waiting for the 4th turn.
    if allow_background_extraction and not incognito and not compare_mode and uprefs.get("auto_memory", True):
        from services.memory import memory_extractor as _memory_extractor
        has_explicit_memory_intent = getattr(
            _memory_extractor, "has_explicit_memory_intent", None
        )
        extract_explicit_memory_and_store = getattr(
            _memory_extractor, "extract_explicit_memory_and_store", None
        )
        if (
            callable(has_explicit_memory_intent)
            and callable(extract_explicit_memory_and_store)
            and has_explicit_memory_intent(message)
        ):
            try:
                extract_explicit_memory_and_store(
                    sess, message, memory_manager, memory_vector, owner=owner,
                )
            except Exception:
                logger.warning("[explicit-memory] extraction failed for session %s", session_id, exc_info=True)

    # Memory extraction — only every 4th message pair to avoid excess LLM calls
    _msg_count = len(sess.history) if hasattr(sess, 'history') else 0
    _should_extract = (_msg_count >= 4) and (_msg_count % 4 == 0)
    if allow_background_extraction and not incognito and not compare_mode and _should_extract and uprefs.get("auto_memory", True):
        from services.memory.memory_extractor import extract_and_store
        from src.task_endpoint import resolve_task_endpoint
        t_url, t_model, t_headers = resolve_task_endpoint(
            sess.endpoint_url, sess.model, sess.headers, owner=owner,
        )
        _extraction_jobs.append(("memory", extract_and_store(
            sess, memory_manager, memory_vector,
            t_url, t_model, t_headers,
        )))

    # Skill extraction from complex agent runs. Only when the user actually
    # chose agent mode — not a chat we auto-escalated for a notes/calendar
    # intent, and never in incognito/compare.
    auto_skills_enabled = bool(uprefs.get("auto_skills", True))
    # Quiet by default — full gate/dispatch/start trace runs at DEBUG so
    # users can re-enable diagnostics with LOG_LEVEL=DEBUG when something
    # silently breaks. INFO-level only shows the outcome inside
    # maybe_extract_skill (Auto-extracted / dropped / failed).
    logger.debug(
        "[skill-extract] gate: extract_skills=%s auto_skills=%s incognito=%s "
        "compare=%s rounds=%d tools=%d skills_manager=%s",
        extract_skills, auto_skills_enabled, incognito, compare_mode,
        agent_rounds, agent_tool_calls, "set" if skills_manager else "MISSING",
    )
    if (
        extract_skills
        and allow_background_extraction
        and auto_skills_enabled
        and not incognito
        and not compare_mode
        and (agent_rounds >= 2 or agent_tool_calls >= 2)
    ):
        if skills_manager is None:
            logger.warning(
                "[skill-extract] gate PASSED but skills_manager is None — "
                "extraction skipped. (Bug: caller didn't pass skills_manager.)"
            )
        else:
            from services.memory.skill_extractor import maybe_extract_skill
            from src.task_endpoint import resolve_task_endpoint
            s_url, s_model, s_headers = resolve_task_endpoint(
                sess.endpoint_url, sess.model, sess.headers, owner=owner,
            )
            logger.debug("[skill-extract] dispatching extractor (model=%s)", s_model)
            _extraction_jobs.append(("skill", maybe_extract_skill(
                sess, skills_manager,
                s_url, s_model, s_headers,
                agent_rounds, agent_tool_calls,
                owner=owner,
            )))

    if _extraction_jobs:
        _spawn_bg(_run_extraction_jobs_sequentially(session_id, _extraction_jobs))

    # Token accumulation
    if last_metrics:
        accumulate_token_usage(session_id, last_metrics)

    # Webhook
    if webhook_manager and not compare_mode:
        webhook_manager.fire_and_forget("chat.completed", {
            "session_id": session_id, "model": sess.model,
            "user_message": message, "response": full_response[:2000],
        })

    # Auto-name
    if needs_auto_name(getattr(sess, "name", "")):
        _spawn_bg(auto_name_session(session_manager, sess))
