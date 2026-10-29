import pytest
from types import SimpleNamespace
from src.memory import MemoryManager
from src.chat_processor import ChatProcessor
from services.memory.memory_extractor import (
    has_explicit_memory_intent,
    extract_explicit_memory_candidates,
    extract_explicit_memory_and_store,
)
from routes.chat_helpers import run_post_response_tasks


class FakeVectorStore:
    def __init__(self):
        self.healthy = True
        self.added = {}

    def add(self, memory_id: str, text: str):
        self.added[memory_id] = text

    def find_similar(self, text: str, threshold: float = 0.72):
        for mid, t in self.added.items():
            if t == text:
                return mid
        return None

    def search(self, query: str, k: int = 5):
        results = []
        for mid, text in self.added.items():
            results.append({"memory_id": mid, "score": 0.85})
        return results


def test_explicit_memory_intent_detection():
    # Positives
    assert has_explicit_memory_intent("Remember that I prefer to write Python using async/await.")
    assert has_explicit_memory_intent("Memorize that my office is on 3rd floor.")
    assert has_explicit_memory_intent("Note that I work remotely.")
    assert has_explicit_memory_intent("Save this: my car is a Honda.")
    assert has_explicit_memory_intent("Don't forget that I prefer pytest.")
    assert has_explicit_memory_intent("Jarvis, remember that I prefer dark mode.")
    assert has_explicit_memory_intent("Please remember that I hate mushrooms.")

    # Negatives (conversational statements / questions)
    assert not has_explicit_memory_intent("I am working on a Python project.")
    assert not has_explicit_memory_intent("I remember when Python 2 was deprecated.")
    assert not has_explicit_memory_intent("Do you remember my favorite color?")
    assert not has_explicit_memory_intent("How do I prefer to write Python?")
    assert not has_explicit_memory_intent("Python is a great programming language.")


def test_explicit_memory_immediately_creates_preference_memory(tmp_path):
    # Requirement 9A: "Remember that I prefer to write Python using async/await." immediately creates a preference memory
    mm = MemoryManager(str(tmp_path))
    fake_vector = FakeVectorStore()
    sess = SimpleNamespace(id="sess-1", owner="admin", history=[{"role": "user", "content": "test"}])

    msg = "Remember that I prefer to write Python using async/await."
    assert has_explicit_memory_intent(msg)

    created = extract_explicit_memory_and_store(sess, msg, mm, fake_vector, owner="admin")
    assert len(created) == 1
    entry = created[0]
    assert entry["category"] == "preference"
    assert "async/await" in entry["text"]
    assert "User prefers" in entry["text"]
    assert entry["owner"] == "admin"

    # Verify JSON persistence
    loaded = mm.load(owner="admin")
    assert any(m["id"] == entry["id"] for m in loaded)

    # Verify vector index
    assert entry["id"] in fake_vector.added


def test_ordinary_message_does_not_create_explicit_memory(tmp_path):
    # Requirement 9B: "I am working on a Python project." does NOT become an explicit memory
    mm = MemoryManager(str(tmp_path))
    fake_vector = FakeVectorStore()
    sess = SimpleNamespace(
        id="sess-2",
        name="Test Session",
        endpoint_url="",
        model="",
        headers={},
        owner="admin",
        history=[
            {"role": "user", "content": "I am working on a Python project."},
            {"role": "assistant", "content": "That sounds interesting!"},
        ],
    )

    msg = "I am working on a Python project."
    assert not has_explicit_memory_intent(msg)

    # Call run_post_response_tasks on 1-turn session
    run_post_response_tasks(
        sess,
        session_manager=None,
        session_id="sess-2",
        message=msg,
        full_response="That sounds interesting!",
        last_metrics=None,
        uprefs={"auto_memory": True},
        memory_manager=mm,
        memory_vector=fake_vector,
        webhook_manager=None,
        owner="admin",
    )

    # Memory list should remain completely empty
    loaded = mm.load(owner="admin")
    assert len(loaded) == 0
    assert len(fake_vector.added) == 0


def test_new_chat_retrieval_of_preference(tmp_path):
    # Requirement 9C: New-chat retrieval of "How do I prefer to write Python?" retrieves async/await preference
    mm = MemoryManager(str(tmp_path))
    fake_vector = FakeVectorStore()

    # Store preference memory
    entry = mm.add_entry(
        "User prefers to write Python using async/await.",
        source="user_explicit",
        category="preference",
        owner="admin",
    )
    mm.save([entry])
    fake_vector.add(entry["id"], entry["text"])

    processor = ChatProcessor(memory_manager=mm, personal_docs_manager=None, memory_vector=fake_vector)
    preface, rag, web = processor.build_context_preface(
        message="How do I prefer to write Python?",
        session=SimpleNamespace(id="sess-new", history=[]),
        use_memory=True,
        owner="admin",
    )

    # Verify retrieval
    used = getattr(processor, "_last_used_memories", [])
    assert len(used) >= 1
    assert any("async/await" in m["text"] for m in used)

    # Verify prompt injection in preface
    found_context = False
    for p in preface:
        content = p.get("content", "")
        if "async/await" in str(content):
            found_context = True
            break
    assert found_context, "Async/await preference was not injected into prompt preface"
