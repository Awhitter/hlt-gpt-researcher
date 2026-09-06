"""Exercise the pinned Codex refresh failure path without providers or secrets."""

from __future__ import annotations

import ast
import logging
import sys
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


@dataclass
class Credential:
    id: str
    source: str = "manual:device_code"
    access_token: str = "test-access"
    refresh_token: str = "test-refresh"
    auth_type: str = "oauth"
    last_status: str = "ok"
    last_status_at: float | None = None
    last_error_code: int | None = None
    last_error_reason: str | None = None
    last_error_message: str | None = None
    last_error_reset_at: float | None = None
    last_refresh: str | None = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, provider, payload):
        return cls(**payload)


def assert_codex_terminal_refresh(hermes_root: Path) -> None:
    path = hermes_root / "agent" / "credential_pool.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    pool_class = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == "CredentialPool")
    methods = {
        "_refresh_entry_impl", "_mark_exhausted", "_is_terminal_auth_failure",
        "_available_entries",
        "_sync_codex_entry_from_auth_store", "_refresh_entry",
    }
    nodes = [n for n in pool_class.body if isinstance(n, ast.FunctionDef)
             and n.name in methods]
    assert len(nodes) == len(methods)
    terminal_reasons = next(n for n in tree.body if isinstance(n, ast.Assign)
                            and any(isinstance(t, ast.Name)
                                    and t.id == "_TERMINAL_AUTH_REASONS"
                                    for t in n.targets))
    namespace = {
        "replace": replace, "time": time, "datetime": datetime,
        "timezone": timezone, "logger": logging.getLogger(__name__),
        "STATUS_OK": "ok", "STATUS_DEAD": "dead",
        "STATUS_EXHAUSTED": "exhausted", "AUTH_TYPE_OAUTH": "oauth",
        "AUTH_TYPE_API_KEY": "api_key", "DEAD_MANUAL_PRUNE_TTL_SECONDS": 86400,
        "CREDENTIAL_PERSIST_FAILED_REASON": "credential_persist_failed",
        "_normalize_error_context": lambda value: dict(value or {}),
        "_is_manual_source": lambda source: source.startswith("manual:"),
        "_exhausted_until": lambda entry, **kwargs: entry.last_error_reset_at,
        "_auth_store_lock": lambda **kwargs: nullcontext(),
        "PooledCredential": Credential,
    }
    # Execute the real patched methods, stubbing only persistence/provider IO
    # and unrelated helpers. No Hermes imports, credentials, models, or network.
    exec(compile(ast.Module(body=[terminal_reasons, *nodes], type_ignores=[]),
                 str(path), "exec"), namespace)

    class Pool:
        provider = "openai-codex"

        def __init__(self, entries, *, code="refresh_token_reused", terminal=True,
                     rotated=False, succeeds=False):
            self._entries = entries
            self._current_id = entries[0].id
            self._lock = nullcontext()
            self.persisted = []
            self.store_reads = 0
            self.syncs = 0
            self.rotated = rotated
            self.calls = 0
            self.singleton = {"tokens": {"access_token": "other-access",
                                          "refresh_token": "other-refresh"}}

            def refresh(*args):
                self.calls += 1
                if succeeds:
                    return {"access_token": "new-access",
                            "refresh_token": "new-refresh", "last_refresh": "now"}
                error = RuntimeError("synthetic provider failure")
                error.code = code
                raise error

            def load_store():
                self.store_reads += 1
                return {}

            namespace.update({
                "auth_mod": SimpleNamespace(
                    refresh_codex_oauth_pure=refresh,
                    _is_terminal_codex_oauth_refresh_error=lambda exc: terminal),
                "_load_auth_store": load_store,
                "_load_provider_state": lambda *args: self.singleton,
                "_save_provider_state": lambda *args: None,
                "_save_auth_store": lambda *args: None,
            })

        def _sync_codex_entry_from_auth_store(self, entry):
            self.syncs += 1
            if self.rotated and self.syncs > 1:
                return replace(entry, access_token="winner-access",
                               refresh_token="winner-refresh")
            return entry

        def _replace_entry(self, old, new):
            self._entries = [new if e.id == old.id else e for e in self._entries]

        def _persist(self, **kwargs):
            self.persisted.append(kwargs)

        def _sync_device_code_entry_to_auth_store(self, entry):
            pass

        def _codex_quota_restored_upstream(self, entry):
            return False

        def _single_use_refresh_lock_timeout(self):
            return 5

        def _entry_needs_refresh(self, entry):
            return entry.access_token == "test-access"

    for name in methods:
        if name != "_sync_codex_entry_from_auth_store":
            setattr(Pool, name, namespace[name])

    quota = Credential("quota", last_status="exhausted", last_error_code=429,
                       last_error_reset_at=time.time() + 86400)
    healthy = Credential("healthy", source="device_code")
    cases = 0
    for code in ("refresh_token_reused", "invalid_grant", "other_terminal_code"):
        broken = Credential("broken")
        pool = Pool([broken, quota, healthy], code=code)
        assert pool._refresh_entry_impl(broken, force=False) is None
        failed = pool._entries[0]
        assert failed.last_status == "dead", "terminal manual grant must stop retrying"
        assert failed.last_error_code == 401
        assert failed.last_error_reason in namespace["_TERMINAL_AUTH_REASONS"]
        assert pool._entries[1:] == [quota, healthy], "leave other accounts untouched"
        assert pool.store_reads == 0, "manual grant must not clear singleton auth"
        assert len(pool.persisted) == 1
        available, pending = pool._available_entries()
        assert [e.id for e in available] == ["healthy"] and not pending
        assert pool.calls == 1, "enumeration must not retry the dead grant"
        cases += 1

    broken = Credential("transient")
    pool = Pool([broken, quota], code="timeout", terminal=False)
    assert pool._refresh_entry_impl(broken, force=False) is None
    assert pool._entries[0].last_status == "exhausted", "transient errors can recover"
    assert pool._entries[1] == quota
    cases += 1

    broken = Credential("race")
    pool = Pool([broken, quota], rotated=True)
    result = pool._refresh_entry_impl(broken, force=False)
    assert result.last_status == "ok" and result.refresh_token == "winner-refresh"
    assert pool._entries[1] == quota and pool.calls == 1
    cases += 1

    singleton = Credential("singleton", source="device_code")
    pool = Pool([singleton, quota])
    assert pool._refresh_entry_impl(singleton, force=False) is None
    assert pool._entries == [quota], "retain existing singleton recovery behavior"
    assert pool.persisted == [{"removed_ids": ["singleton"]}]
    assert pool.singleton["tokens"]["refresh_token"] == "other-refresh"
    cases += 1

    broken = Credential("refreshable")
    pool = Pool([broken, quota], succeeds=True)
    result = pool._refresh_entry_impl(broken, force=False)
    assert result.last_status == "ok" and result.refresh_token == "new-refresh"
    assert pool._entries[1] == quota
    cases += 1

    # Now exercise the real synchronization path too. A manual grant's token
    # authority is its exact persisted pool row, never the login singleton.
    Pool._sync_codex_entry_from_auth_store = namespace[
        "_sync_codex_entry_from_auth_store"
    ]
    manual = Credential("manual")
    pool = Pool([manual, quota])
    namespace["read_credential_pool"] = lambda provider: [asdict(manual), asdict(quota)]
    assert pool._sync_codex_entry_from_auth_store(manual) == manual
    assert pool._entries == [manual, quota] and pool.store_reads == 0
    cases += 1

    # A second process has persisted the winner's rotated pair while this
    # instance waited for the existing cross-process refresh lock.
    winner = replace(manual, access_token="winner-access", refresh_token="winner-refresh")
    namespace["read_credential_pool"] = lambda provider: [asdict(winner), asdict(quota)]
    result = pool._refresh_entry(manual, force=False)
    assert result == winner and pool._entries == [winner, quota]
    assert pool.calls == 0 and pool.store_reads == 0 and not pool.persisted
    cases += 1

    pool = Pool([manual, quota])
    namespace["read_credential_pool"] = lambda provider: [asdict(quota)]
    assert pool._sync_codex_entry_from_auth_store(manual) == manual
    assert pool.store_reads == 0, "missing own row must not borrow another identity"
    cases += 1

    singleton = Credential("singleton", source="device_code")
    pool = Pool([singleton, quota])
    result = pool._sync_codex_entry_from_auth_store(singleton)
    assert result.access_token == "other-access" and pool.store_reads == 1
    assert pool._entries[1] == quota
    cases += 1
    print(f"Codex terminal refresh: {cases} focused cases passed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: assert_codex_terminal_refresh.py HERMES_ROOT")
    assert_codex_terminal_refresh(Path(sys.argv[1]))
