import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from src.api_key_manager import APIKeyManager, WebhookSecretStorageError
from tests.helpers.import_state import clear_module, preserve_import_state


with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///:memory:"}), \
        preserve_import_state("src.database", "core.database"):
    clear_module("src.database")
    _core_database = sys.modules.get("core.database")
    if _core_database is not None and not getattr(_core_database, "__file__", None):
        del sys.modules["core.database"]
    from src import webhook_manager as _webhook_manager_module
    from src.webhook_manager import WebhookManager


class _Column:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return (self.name, value)


class _Webhook:
    id = _Column("id")
    is_active = _Column("is_active")

    def __init__(self, **values):
        self.__dict__.update(values)


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_conditions):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _Db:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.added = []
        self.closed = False

    def add(self, row):
        self.added.append(row)
        self.rows.append(row)

    def query(self, _model):
        return _Query(self.rows)

    def commit(self):
        pass

    def close(self):
        self.closed = True


def _load_webhook_routes(monkeypatch, db):
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = []
    core_db = types.ModuleType("core.database")
    core_db.SessionLocal = lambda: db
    core_db.Webhook = _Webhook
    core_db.ModelEndpoint = object
    core_middleware = types.ModuleType("core.middleware")
    core_middleware.require_admin = lambda request: None
    auth_helpers = types.ModuleType("src.auth_helpers")
    auth_helpers.owner_filter = lambda query, *_args: query
    url_security = types.ModuleType("src.url_security")
    url_security.validate_public_http_url = lambda url: url
    manager_module = types.ModuleType("src.webhook_manager")
    manager_module.WebhookManager = WebhookManager
    manager_module.validate_webhook_url = lambda url: url
    manager_module.validate_events = lambda events: events

    monkeypatch.setitem(sys.modules, "core", core_pkg)
    monkeypatch.setitem(sys.modules, "core.database", core_db)
    monkeypatch.setitem(sys.modules, "core.middleware", core_middleware)
    monkeypatch.setitem(sys.modules, "src.auth_helpers", auth_helpers)
    monkeypatch.setitem(sys.modules, "src.url_security", url_security)
    monkeypatch.setitem(sys.modules, "src.webhook_manager", manager_module)

    module_name = "routes.webhook_secret_storage_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[1] / "routes" / "webhook" / "webhook_routes.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _route_endpoint(router, path, method):
    return next(
        route.endpoint
        for route in router.routes
        if route.path == path and method in route.methods
    )


def test_webhook_secret_encryption_and_retrieval_are_versioned(tmp_path):
    manager = APIKeyManager(str(tmp_path))
    manager.mark_installation_state("first_run_pending")
    manager.initialize_key(first_time=True)

    stored = manager.encrypt_webhook_secret("signing-secret")

    assert stored.startswith("webhook-secret:v1:")
    assert "signing-secret" not in stored
    assert manager.decrypt_webhook_secret(stored) == "signing-secret"


def test_webhook_encryption_failure_does_not_return_plaintext(tmp_path, monkeypatch):
    manager = APIKeyManager(str(tmp_path))

    def fail_encryption(_secret):
        raise OSError("key storage unavailable")

    monkeypatch.setattr(manager, "encrypt_api_key", fail_encryption)
    with pytest.raises(WebhookSecretStorageError) as exc:
        manager.encrypt_webhook_secret("must-not-be-stored")

    assert "must-not-be-stored" not in str(exc.value)
    assert not (tmp_path / ".key").exists()


def test_authenticated_legacy_ciphertext_remains_readable(tmp_path):
    manager = APIKeyManager(str(tmp_path))
    manager.mark_installation_state("first_run_pending")
    manager.initialize_key(first_time=True)
    legacy_ciphertext = manager.encrypt_api_key("legacy-signing-secret")

    assert not legacy_ciphertext.startswith("webhook-secret:v1:")
    assert manager.decrypt_webhook_secret(legacy_ciphertext) == "legacy-signing-secret"


def test_legacy_plaintext_is_rejected_without_fallback(tmp_path):
    manager = APIKeyManager(str(tmp_path))
    manager.mark_installation_state("first_run_pending")
    manager.initialize_key(first_time=True)

    with pytest.raises(WebhookSecretStorageError, match="could not be authenticated"):
        manager.decrypt_webhook_secret("legacy-plaintext-secret")


def test_wrong_key_fails_decryption(tmp_path):
    writer = APIKeyManager(str(tmp_path / "writer"))
    writer.mark_installation_state("first_run_pending")
    writer.initialize_key(first_time=True)
    stored = writer.encrypt_webhook_secret("signing-secret")
    reader = APIKeyManager(str(tmp_path / "reader"))
    reader.mark_installation_state("first_run_pending")
    reader.initialize_key(first_time=True)

    with pytest.raises(WebhookSecretStorageError, match="could not be authenticated"):
        reader.decrypt_webhook_secret(stored)


def test_missing_key_fails_without_creating_a_replacement(tmp_path):
    manager = APIKeyManager(str(tmp_path))

    with pytest.raises(WebhookSecretStorageError, match="key is missing"):
        manager.decrypt_webhook_secret("gAAAA-legacy-ciphertext")

    assert not (tmp_path / ".key").exists()


def test_replacing_webhook_secret_keeps_old_and_new_values_separate(tmp_path):
    manager = APIKeyManager(str(tmp_path))
    manager.mark_installation_state("first_run_pending")
    manager.initialize_key(first_time=True)
    old_value = manager.encrypt_webhook_secret("old-secret")
    replacement = manager.encrypt_webhook_secret("replacement-secret")
    webhook = _Webhook(secret=old_value)
    webhook.secret = replacement

    assert old_value != replacement
    assert manager.decrypt_webhook_secret(old_value) == "old-secret"
    assert WebhookManager(api_key_manager=manager)._decrypt_secret(webhook.secret) == (
        "replacement-secret"
    )


def test_webhook_manager_does_not_return_unauthenticated_values(tmp_path):
    manager = WebhookManager(api_key_manager=APIKeyManager(str(tmp_path)))

    with pytest.raises(WebhookSecretStorageError):
        manager._decrypt_secret("legacy-plaintext-secret")
    with pytest.raises(WebhookSecretStorageError):
        WebhookManager()._decrypt_secret("legacy-plaintext-secret")


@pytest.mark.asyncio
async def test_webhook_manager_skips_legacy_plaintext_without_logging_it(
    tmp_path, monkeypatch, caplog
):
    db = _Db([_Webhook(
        id="hook-1",
        url="https://example.com/hook",
        secret="legacy-plaintext-secret",
        events="chat.completed",
        is_active=True,
    )])
    monkeypatch.setattr(_webhook_manager_module, "SessionLocal", lambda: db)
    manager = WebhookManager(api_key_manager=APIKeyManager(str(tmp_path)))
    deliveries = []
    monkeypatch.setattr(manager, "_spawn_tracked", lambda coro: deliveries.append(coro))

    await manager.fire("chat.completed", {"message": "test"})

    assert deliveries == []
    assert "delivery skipped" in caplog.text
    assert "legacy-plaintext-secret" not in caplog.text


def test_webhook_create_encrypts_secret_and_test_retrieves_it(tmp_path, monkeypatch):
    db = _Db()
    routes = _load_webhook_routes(monkeypatch, db)
    authorized_requests = []
    monkeypatch.setattr(
        routes, "_require_admin", lambda request: authorized_requests.append(request)
    )
    api_key_manager = APIKeyManager(str(tmp_path))
    api_key_manager.mark_installation_state("first_run_pending")
    api_key_manager.initialize_key(first_time=True)
    webhook_manager = WebhookManager(api_key_manager=api_key_manager)
    delivered = []

    async def capture_delivery(webhook_id, url, secret, event, payload):
        delivered.append((webhook_id, url, secret, event, payload))

    monkeypatch.setattr(webhook_manager, "_deliver", capture_delivery)
    router = routes.setup_webhook_routes(
        webhook_manager, auth_manager=None, api_key_manager=api_key_manager
    )
    create = _route_endpoint(router, "/api/webhooks", "POST")
    test = _route_endpoint(router, "/api/webhooks/{webhook_id}/test", "POST")
    request = object()

    result = create(
        request,
        name="test hook",
        url="https://example.com/hook",
        secret="signing-secret",
        events="chat.completed",
    )
    stored_hook = db.rows[0]
    test_result = asyncio.run(test(request, result["id"]))

    assert result["id"] == stored_hook.id
    assert stored_hook.secret.startswith("webhook-secret:v1:")
    assert "signing-secret" not in stored_hook.secret
    assert test_result == {"status": "sent"}
    assert delivered[0][2] == "signing-secret"
    assert len(authorized_requests) == 2


def test_webhook_create_missing_key_does_not_initialize(tmp_path, monkeypatch):
    db = _Db()
    routes = _load_webhook_routes(monkeypatch, db)
    api_key_manager = APIKeyManager(str(tmp_path))
    tmp_path.joinpath("api_keys.json").write_text("{}", encoding="utf-8")
    router = routes.setup_webhook_routes(
        WebhookManager(api_key_manager=api_key_manager),
        auth_manager=None,
        api_key_manager=api_key_manager,
    )
    create = _route_endpoint(router, "/api/webhooks", "POST")

    with pytest.raises(HTTPException) as exc:
        create(
            object(),
            name="test hook",
            url="https://example.com/hook",
            secret="must-not-be-stored",
            events="chat.completed",
        )

    assert exc.value.status_code == 503
    assert db.added == []
    assert not tmp_path.joinpath(".key").exists()


def test_webhook_create_fails_closed_when_encryption_is_unavailable(monkeypatch):
    db = _Db()
    routes = _load_webhook_routes(monkeypatch, db)
    manager = types.SimpleNamespace()
    router = routes.setup_webhook_routes(
        WebhookManager(), auth_manager=None, api_key_manager=manager
    )
    create = _route_endpoint(router, "/api/webhooks", "POST")

    with pytest.raises(HTTPException) as exc:
        create(
            object(),
            name="test hook",
            url="https://example.com/hook",
            secret="must-not-be-stored",
            events="chat.completed",
        )

    assert exc.value.status_code == 503
    assert "must-not-be-stored" not in str(exc.value.detail)
    assert db.added == []


def test_webhook_creation_still_requires_admin_before_encrypting(tmp_path, monkeypatch):
    db = _Db()
    routes = _load_webhook_routes(monkeypatch, db)
    api_key_manager = APIKeyManager(str(tmp_path))

    def reject_non_admin(_request):
        raise HTTPException(403, "Admin access required")

    monkeypatch.setattr(routes, "_require_admin", reject_non_admin)
    router = routes.setup_webhook_routes(
        WebhookManager(api_key_manager=api_key_manager),
        auth_manager=None,
        api_key_manager=api_key_manager,
    )
    create = _route_endpoint(router, "/api/webhooks", "POST")

    with pytest.raises(HTTPException) as exc:
        create(
            object(),
            name="test hook",
            url="https://example.com/hook",
            secret="must-not-be-encrypted",
            events="chat.completed",
        )

    assert exc.value.status_code == 403
    assert db.added == []
    assert not (tmp_path / ".key").exists()


@pytest.mark.asyncio
async def test_webhook_test_route_reports_legacy_plaintext_as_unavailable(
    tmp_path, monkeypatch
):
    db = _Db([_Webhook(
        id="hook-1",
        url="https://example.com/hook",
        secret="legacy-plaintext-secret",
        events="chat.completed",
        is_active=True,
    )])
    routes = _load_webhook_routes(monkeypatch, db)
    monkeypatch.setattr(routes, "_require_admin", lambda _request: None)
    api_key_manager = APIKeyManager(str(tmp_path))
    api_key_manager.mark_installation_state("first_run_pending")
    api_key_manager.initialize_key(first_time=True)
    webhook_manager = WebhookManager(api_key_manager=api_key_manager)
    router = routes.setup_webhook_routes(
        webhook_manager, auth_manager=None, api_key_manager=api_key_manager
    )
    test = _route_endpoint(router, "/api/webhooks/{webhook_id}/test", "POST")

    with pytest.raises(HTTPException) as exc:
        await test(object(), "hook-1")

    assert exc.value.status_code == 503
    assert "could not be authenticated" in exc.value.detail
