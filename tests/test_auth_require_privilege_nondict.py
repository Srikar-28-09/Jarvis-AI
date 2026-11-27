import types

import pytest

from src import auth_helpers
from src.auth_helpers import require_privilege


class _Mgr:
    def __init__(self, privs):
        self._privs = privs

    def get_privileges(self, user):
        return self._privs


def _request(mgr):
    state = types.SimpleNamespace(auth_manager=mgr)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def test_require_privilege_rejects_non_dict_privileges(monkeypatch):
    # Corrupt privilege state must fail closed rather than returning a 500 or
    # accidentally granting access.
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Mgr(["do_x"]))
    with pytest.raises(Exception):
        require_privilege(req, "do_x")


def test_require_privilege_still_blocks_disallowed(monkeypatch):
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Mgr({"do_x": False}))
    with pytest.raises(Exception):
        require_privilege(req, "do_x")


def test_require_privilege_rejects_unknown_known_key(monkeypatch):
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Mgr({"can_use_documents": True}))
    with pytest.raises(Exception):
        require_privilege(req, "unknown_sensitive_capability")


def test_require_privilege_rejects_missing_known_key(monkeypatch):
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Mgr({}))
    with pytest.raises(Exception):
        require_privilege(req, "can_use_documents")
