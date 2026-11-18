"""Shared tool-call contract types and recognized tool names."""

from collections import namedtuple

from src.tool_security import BUILTIN_EMAIL_TOOLS

ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])

# Tool types that trigger execution.
TOOL_TAGS = {
    "bash", "python", "web_search", "web_fetch", "read_file", "write_file", "edit_file",
    "apply_patch", "todowrite",
    "grep", "glob", "ls", "get_workspace", "manage_bg_jobs",
    "create_document", "update_document", "edit_document",
    "search_chats",
    "chat_with_model", "create_session", "list_sessions",
    "send_to_session",
    "pipeline",
    "manage_session", "manage_memory", "list_models",
    "ui_control", "generate_image", "ask_user", "update_plan",
    "manage_tasks", "api_call", "ask_teacher", "manage_skills",
    "suggest_document",
    "manage_endpoints", "manage_mcp", "manage_webhooks",
    "manage_tokens", "manage_documents", "manage_settings",
    "manage_notes", "manage_calendar",
    "resolve_contact", "manage_contact",
    # Email tool names come from BUILTIN_EMAIL_TOOLS so parsing and policy
    # checks use the same set of supported names.
    # Cookbook tools (LLM serving and downloads) must be recognized before
    # native function calls reach the dispatcher.
    "download_model", "serve_model",
    "list_served_models", "stop_served_model",
    "list_downloads", "cancel_download",
    "search_hf_models", "list_cached_models",
    "list_serve_presets", "serve_preset", "adopt_served_model",
    "list_cookbook_servers",
    # Other tools the agent reaches for that were missing from earlier lists.
    "edit_image", "trigger_research", "manage_research",
    "send_notification", "schedule_reminder",
    "list_directory", "create_directory",
    "open_application", "open_url", "take_screenshot", "screenshot",
    "click", "double_click", "type_text", "press_key", "scroll",
    "computer_use",
    # Generic loopback to UI-button endpoints without a named wrapper.
    "app_api",
} | BUILTIN_EMAIL_TOOLS
