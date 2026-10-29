"""Regression coverage for concise deterministic assistant turns."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from src.simple_intents import (
    answer_simple_request,
    answer_length_policy,
    is_low_effort_conversation,
    reply_token_limit,
)
from src.user_time import clear_user_time_context, set_user_tz_name


def test_time_answer_uses_request_local_timezone_and_live_datetime():
    set_user_tz_name("Asia/Kolkata")
    try:
        now = datetime(2026, 10, 14, 6, 45, tzinfo=timezone.utc)
        assert answer_simple_request("What time is it?", now=now) == "It’s 12:15 PM, Sir."
        assert answer_simple_request("What's today's date?", now=now) == "It’s Wednesday, October 14, Sir."
        for phrase in (
            "what time is it right now???",
            "What time is it right now?",
            "what time is it now?",
            "Tell me the time right now!!!",
            "current time",
        ):
            assert answer_simple_request(phrase, now=now) == "It’s 12:15 PM, Sir."
    finally:
        clear_user_time_context()


def test_greetings_and_safe_arithmetic_are_short_and_deterministic():
    from src.action_intents import classify_tool_intent

    assert answer_simple_request("Hi") == "Hi, I’m here."
    assert answer_simple_request("Hi Jarvis!") == "Hi, I’m here."
    assert classify_tool_intent("Hi Jarvis!").needs_tools is False
    assert answer_simple_request("Thanks, Jarvis!") == "You’re welcome."
    assert answer_simple_request("What is 12 / 4?") == "3"
    assert answer_simple_request("calculate (2 + 3) * 4") == "20"
    assert answer_simple_request("What is 2 ^ 5?") == "32"


def test_ambiguous_or_action_sensitive_requests_do_not_use_the_fast_path():
    assert answer_simple_request("What time is it in Tokyo?") is None
    assert answer_simple_request("Open Chrome") is None
    assert answer_simple_request("What time will Chrome finish downloading?") is None
    assert answer_simple_request("__import__('os').system('id')") is None
    assert answer_simple_request("What is 1 / 0?") is None


def test_simple_turns_disable_extended_reasoning_but_complex_and_actions_do_not():
    assert is_low_effort_conversation("What does CPU stand for?")
    assert is_low_effort_conversation("Hi")
    assert not is_low_effort_conversation("Explain why time zones change with daylight saving time.")
    assert not is_low_effort_conversation("Compare the tradeoffs step by step.")
    assert not is_low_effort_conversation("Open Chrome")
    assert reply_token_limit(4096, "What does CPU stand for?") == 192
    assert reply_token_limit(4096, "Explain how CPUs execute instructions.") == 4096
    assert reply_token_limit(1024, "What does CPU stand for?", preset_id="custom") == 1024


def test_short_bounded_educational_prompts_use_simple_answer_settings():
    for message in (
        "What is an LLM? Explain in two simple sentences.",
        "What is NLP? Explain simply.",
        "Define regression in one sentence.",
    ):
        assert is_low_effort_conversation(message), message
        assert reply_token_limit(None, message) == 192


def test_define_saas_in_one_sentence_keeps_the_existing_short_answer_budget():
    message = "Define SaaS in one sentence."
    assert answer_length_policy(message) is None
    assert is_low_effort_conversation(message)
    assert reply_token_limit(4096, message) == 192


def test_bounded_educational_exception_keeps_complex_and_action_requests_out():
    for message in (
        "Explain why this algorithm works in one sentence, then prove it.",
        "Compare these theories step by step, in two sentences.",
        "Write Python code to define a regression model in one sentence.",
        "Search the web for the latest regression results in one sentence.",
        "Open the spreadsheet and define regression in one sentence.",
    ):
        assert not is_low_effort_conversation(message), message


def test_complex_answers_keep_reasoning_and_explicit_detail_instruction():
    assert not is_low_effort_conversation("Explain in detail how daylight saving time works.")
    assert reply_token_limit(4096, "Explain in detail how daylight saving time works.") == 4096

    root = Path(__file__).resolve().parents[1]
    chat_style = (root / "src/chat_processor.py").read_text(encoding="utf-8")
    agent_rules = (root / "src/agent_loop.py").read_text(encoding="utf-8")
    assert "user asks for detail or the task needs it." in chat_style
    assert "Expand when asked or when the task needs detail." in agent_rules


def test_explicit_academic_answer_length_overrides_the_short_reply_cap():
    cases = [
        ("Give me an 8-mark answer about the difference between an ocean and a sea.", 1024),
        ("Explain the difference between an ocean and a sea as an 8-mark answer, briefly.", 1024),
        ("Write a 10-mark answer about cloud computing architecture.", 1280),
    ]
    for message, expected_budget in cases:
        policy = answer_length_policy(message)
        assert policy is not None and policy.kind == "marks"
        assert reply_token_limit(192, message) == expected_budget
        assert is_low_effort_conversation(message)  # mark format alone does not enable extra reasoning
        assert "mark exam response" in policy.instruction
        assert "exact requested topic" in policy.instruction


def test_explicit_short_and_detailed_formats_keep_distinct_budgets():
    for message in (
        "Explain RAG in two simple sentences.",
        "Define an LLM in one sentence.",
    ):
        assert answer_length_policy(message) is None
        assert is_low_effort_conversation(message)
        assert reply_token_limit(4096, message) == 192

    assert is_low_effort_conversation("Explain briefly.")
    assert reply_token_limit(4096, "Explain briefly.") == 192

    detailed = answer_length_policy("Explain this in detail.")
    assert detailed is not None and detailed.kind == "detailed"
    assert reply_token_limit(192, "Explain this in detail.") == 1024
    assert not is_low_effort_conversation("Explain this in detail.")


def test_complex_coding_and_search_intents_are_not_mark_answer_fast_path_requests():
    assert not is_low_effort_conversation("Analyze the tradeoffs of cloud architectures step by step.")
    assert not is_low_effort_conversation(
        "Analyze whether SaaS is suitable for a regulated company as an 8-mark answer."
    )
    assert not is_low_effort_conversation("Write Python code to compare two ocean measurements.")
    assert not is_low_effort_conversation("Search the web for current cloud computing architecture trends.")


def test_answer_length_instruction_is_added_to_context_prompt():
    from routes.chat_helpers import add_answer_length_instruction

    preface = [{"role": "system", "content": "existing style"}]
    policy = add_answer_length_instruction(
        preface,
        "Explain the difference between an ocean and a sea as an 8-mark answer, briefly.",
    )
    assert policy is not None
    assert preface[-1]["role"] == "system"
    assert "## Requested answer format" in preface[-1]["content"]
    assert "exact requested topic" in preface[-1]["content"]


def test_mark_answer_prompt_requires_all_requested_sections_in_user_order():
    from routes.chat_helpers import add_answer_length_instruction

    message = (
        "Just give me an 8 marks answer for Software as a Service. I need the definition, "
        "architecture, components, working and example, advantages and disadvantages and "
        "conclusion. I need each of the sections to be briefly explained."
    )
    preface = [{"role": "system", "content": "Default to short, direct answers."}]
    policy = add_answer_length_instruction(preface, message)

    assert policy is not None and policy.kind == "marks"
    assert policy.min_tokens == 1024
    assert is_low_effort_conversation(message)
    instruction = preface[-1]["content"]
    headings = (
        "definition", "architecture", "components", "working", "example",
        "advantages", "disadvantages", "conclusion",
    )
    assert "Use these requested headings in the same order, and include every one" in instruction
    requested_heading_list = instruction.split("include every one: ", 1)[1].split(".", 1)[0]
    positions = [requested_heading_list.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "do not replace the topic with a broader parent subject" in instruction.lower()
    assert "keep each requested section focused and concise" in instruction.lower()


def test_table_and_real_world_example_requests_get_only_the_requested_format_instruction():
    from routes.chat_helpers import add_answer_length_instruction

    table_preface = []
    policy = add_answer_length_instruction(
        table_preface,
        "Explain the difference between SaaS, PaaS and IaaS in a table.",
    )
    assert policy is None
    assert len(table_preface) == 1
    assert "comparison in a table" in table_preface[0]["content"]

    example_preface = []
    add_answer_length_instruction(
        example_preface,
        "Explain SaaS with a real-world example.",
    )
    assert len(example_preface) == 1
    assert "demonstrates the concept" in example_preface[0]["content"]
    assert "do not only name a company" in example_preface[0]["content"]


def test_expanding_an_earlier_answer_is_a_detail_request_without_forcing_exam_format():
    policy = answer_length_policy("Expand the previous SaaS answer with more detail.")
    assert policy is not None and policy.kind == "detailed"
    assert "add meaningful detail instead of repeating" in policy.instruction
    assert "mark exam response" not in policy.instruction


def test_reasoning_markup_is_kept_out_of_the_user_facing_answer():
    from routes.chat_helpers import clean_thinking_for_save

    answer, metadata = clean_thinking_for_save(
        "<think>Let me think through the answer.</think>It's 4:15 PM."
    )
    assert answer == "It's 4:15 PM."
    assert metadata["thinking"] == "Let me think through the answer."

    voice_source = (Path(__file__).resolve().parents[1] / "static/js/chat.js").read_text(encoding="utf-8")
    assert "extractThinkingBlocks(accumulated || '').content.trim()" in voice_source
    assert "enqueue(voiceAnswer" in voice_source
    style = (Path(__file__).resolve().parents[1] / "static/style.css").read_text(encoding="utf-8")
    html = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
    assert ".thinking-section,\n.agent-thread { display: none !important; }" in style
    assert 'data-ui-key="show-thinking"' not in html


def test_fast_path_runs_before_context_build_and_keeps_request_id_deduplication():
    source = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text(encoding="utf-8")
    fast_path = source.index("if simple_fast_path:")
    context_build = source.index("ctx = await build_chat_context(", fast_path)
    assert fast_path < context_build
    assert "and not tool_approval_continuation" in source[source.rfind("simple_fast_path = bool(", 0, fast_path):fast_path]
    assert "agent_runs.start(\n                    session, _simple_response_stream(), request_id=client_request_id" in source
    assert "and not (_tool_intent and _tool_intent.needs_tools)" in source[source.rfind("simple_fast_path = bool(", 0, fast_path):fast_path]
    assert 'logger.info("agent→chat fast routing: low-effort conversational turn")' in source
    assert "and not _explicit_browser_intent" in source
    assert "and not tool_approval_continuation" in source[source.index('chat_mode == "agent"', source.index("agent→chat fast routing") - 1000):source.index('logger.info("agent→chat fast routing')]


def test_sticky_web_toggle_does_not_force_planner_for_deterministic_simple_turns():
    source = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text(encoding="utf-8")
    simple = source.index("simple_answer = answer_simple_request(message)")
    override = source.index(
        'if simple_answer is not None:\n            _search_enabled = False\n            use_rag = "false"',
        simple,
    )
    fast_path = source.index("if simple_fast_path:", override)
    assert simple < override < fast_path
    assert "not _search_enabled" in source[source.index("simple_fast_path = bool(", override):fast_path]


def test_time_variations_select_deterministic_path_without_llm_or_planner(monkeypatch):
    from src import simple_intents

    llm = Mock(side_effect=AssertionError("simple time question reached an LLM"))
    planner = Mock(side_effect=AssertionError("simple time question reached a planner"))
    monkeypatch.setattr("src.llm_core.llm_call_async", llm)
    monkeypatch.setattr("src.llm_core.stream_llm", llm)
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", planner)

    assert simple_intents.answer_simple_request("what time is it right now???")
    assert simple_intents.answer_simple_request("What time is it now?")
    llm.assert_not_called()
    planner.assert_not_called()

    source = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text(encoding="utf-8")
    selected = source.index("if simple_fast_path:")
    assert source.index("agent_runs.start(\n                    session, _simple_response_stream()", selected) < source.index("ctx = await build_chat_context(", selected)
    assert "fast_path_selected=true" in source
    assert "simple_intent_matched=%s route=chat_stream" in source


def test_unified_jarvis_mode_routes_desktop_actions_to_existing_computer_tool():
    from src.action_intents import classify_tool_intent

    intent = classify_tool_intent("Open Firefox")
    assert intent.needs_tools
    assert intent.category == "computer"

    root = Path(__file__).resolve().parents[1]
    html = (root / "static/index.html").read_text(encoding="utf-8")
    frontend = (root / "static/js/chat.js").read_text(encoding="utf-8")
    app = (root / "static/app.js").read_text(encoding="utf-8")
    routes = (root / "routes/chat_routes.py").read_text(encoding="utf-8")
    tools = (root / "src/agent_tools/__init__.py").read_text(encoding="utf-8")
    assert 'class="jarvis-mode-badge"' in html
    assert 'id="mode-agent-btn"' not in html
    assert "fd.append('mode', 'jarvis')" in frontend
    assert "st.mode = 'jarvis'" in app
    assert 'requested_mode in {"", "jarvis", "unified"}' in routes
    assert '"computer_use": ComputerUseTool().execute' in tools


def test_unified_jarvis_routes_search_research_and_detailed_chat_without_mode_switches():
    from src.action_intents import classify_tool_intent

    assert classify_tool_intent("Search online for the latest Mars launch news").category == "web"
    assert classify_tool_intent("Research the history of the Hubble telescope").category == "research"
    detailed = "Explain neural networks in detail."
    assert answer_simple_request(detailed) is None
    assert not is_low_effort_conversation(detailed)

