from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def secret_storage(tmp_path, monkeypatch):
    import src.secret_storage as secret_storage_module

    monkeypatch.setattr(
        secret_storage_module, "_KEY_PATH", tmp_path / ".app_key"
    )
    monkeypatch.setattr(secret_storage_module, "_fernet", None)
    return secret_storage_module


class _Query:
    def filter(self, *_args, **_kwargs):
        return self

    def count(self):
        return 0

    def update(self, *_args, **_kwargs):
        return 0


class _Database:
    def __init__(self, row=None):
        self.row = row
        self.added = []

    def query(self, _model):
        return _Query()

    def add(self, row):
        self.added.append(row)
        self.row = row

    def get(self, _model, _key):
        return self.row

    def commit(self):
        pass

    def close(self):
        pass


def _install_database(monkeypatch, database, model):
    import core.database

    monkeypatch.setattr(core.database, "SessionLocal", lambda: database)
    monkeypatch.setattr(core.database, "EmailAccount", model)


def _route_endpoint(router, path, method):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


@pytest.mark.asyncio
async def test_create_email_account_stores_literal_enc_password_and_preserves_ciphertext(
    monkeypatch, secret_storage
):
    import routes.email_routes as email_routes

    class EmailAccount:
        id = ""
        is_default = False

        def __init__(self, **values):
            self.__dict__.update(values)

    literal_password = "enc:literal-user-password"
    encrypted_compat_value = secret_storage.encrypt("existing credential")
    database = _Database()
    _install_database(monkeypatch, database, EmailAccount)
    monkeypatch.setattr(
        email_routes, "_lock_email_account_owner_mutation", lambda *_args: None
    )
    monkeypatch.setattr(
        email_routes, "_email_account_owner_scope", lambda query, _owner: query
    )

    endpoint = _route_endpoint(
        email_routes.setup_email_routes(),
        "/api/email/accounts",
        "POST",
    )
    result = await endpoint(
        {
            "name": "Test",
            "imap_password": literal_password,
            "smtp_password": encrypted_compat_value,
        },
        owner="alice",
    )

    assert result["ok"] is True
    row = database.row
    assert secret_storage.decrypt(row.imap_password) == literal_password
    assert row.imap_password.startswith("enc:v2:")
    assert row.smtp_password == encrypted_compat_value
    assert secret_storage.decrypt(row.smtp_password) == "existing credential"


@pytest.mark.asyncio
async def test_update_email_account_stores_literal_enc_password(
    monkeypatch, secret_storage
):
    import routes.email_routes as email_routes

    row = SimpleNamespace(
        id="account-1",
        name="Existing",
        imap_password=secret_storage.encrypt("old imap password"),
        smtp_password=secret_storage.encrypt("keep smtp password"),
        smtp_port=465,
    )
    database = _Database(row)

    class EmailAccount:
        id = ""
        is_default = False
        pass

    _install_database(monkeypatch, database, EmailAccount)
    monkeypatch.setattr(email_routes, "_assert_owns_account", lambda *_args: None)

    endpoint = _route_endpoint(
        email_routes.setup_email_routes(),
        "/api/email/accounts/{account_id}",
        "PUT",
    )
    result = await endpoint(
        "account-1",
        {"imap_password": "enc:updated-literal"},
        owner="alice",
    )

    assert result["ok"] is True
    assert secret_storage.decrypt(row.imap_password) == "enc:updated-literal"
    assert secret_storage.decrypt(row.smtp_password) == "keep smtp password"


def test_malformed_legacy_ciphertext_still_fails_closed(secret_storage):
    malformed = "enc:not-a-valid-token"

    with pytest.raises(secret_storage.AmbiguousLegacySecretError):
        secret_storage.decrypt(malformed)
    with pytest.raises(secret_storage.AmbiguousLegacySecretError):
        secret_storage.migrate_encrypted_value(malformed)


def test_email_password_helper_preserves_authenticated_legacy_ciphertext(
    monkeypatch, secret_storage
):
    import routes.email_routes as email_routes

    fernet = Fernet(Fernet.generate_key())
    legacy_ciphertext = "enc:" + fernet.encrypt(b"existing password").decode("ascii")
    monkeypatch.setattr(secret_storage, "_fernet", fernet)

    assert email_routes._encrypt_email_password(legacy_ciphertext) == legacy_ciphertext
