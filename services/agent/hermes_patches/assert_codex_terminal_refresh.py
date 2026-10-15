"""Exercise managed Codex grant ownership with synthetic credentials and no I/O."""
from __future__ import annotations
import base64
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


def assert_codex_terminal_refresh(root: Path) -> None:
    sys.path.insert(0, str(root))
    from agent import credential_pool as cp

    def token(account):
        payload = {"exp": int(time.time()) + 7200, "sub": account,
                   "https://api.openai.com/auth": {"chatgpt_account_id": account}}
        return 'fixture.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=') + '.signature'

    manual = cp.PooledCredential(id='manual', label='synthetic', priority=0, provider='openai-codex', source='manual:device_code',
                                auth_type='oauth', access_token=token('one'), refresh_token='manual-original')
    healthy = replace(manual, id='singleton', source='device_code', refresh_token='singleton-independent')
    quota = replace(manual, id='quota', access_token=token('two'), last_status=cp.STATUS_EXHAUSTED,
                    last_error_code=429, last_error_reset_at=time.time() + 86400)
    saved = []
    cases = 0
    with patch.dict(os.environ, {'HLT_MANAGED_MODEL_ROUTE': '1'}), \
         patch.object(cp.CredentialPool, '_persist', lambda *a, **kw: saved.append(kw)), \
         patch.object(cp.CredentialPool, '_sync_device_code_entry_to_auth_store', lambda *a: None), \
         patch.object(cp.CredentialPool, '_codex_quota_restored_upstream', lambda *a: False), \
         patch.object(cp, '_load_auth_store', side_effect=AssertionError('manual grant must not read singleton')), \
         patch.object(cp, '_save_auth_store', side_effect=AssertionError('manual grant must not overwrite singleton')):
        for code in ('invalid_grant', 'invalid_refresh_token', 'refresh_token_reused'):
            pool = cp.CredentialPool('openai-codex', [manual, healthy, quota])
            error = RuntimeError('synthetic terminal rejection')
            error.code = code
            with patch.object(cp, 'read_credential_pool', return_value=[manual.to_dict()]), \
                 patch.object(cp.auth_mod, 'refresh_codex_oauth_pure', side_effect=error) as refresh, \
                 patch.object(cp.auth_mod, '_is_terminal_codex_oauth_refresh_error', return_value=True):
                assert pool._refresh_entry_impl(manual, force=False) is None
                assert pool._entries[0].last_status == cp.STATUS_DEAD
                assert pool._entries[1:] == [healthy, quota]
                assert pool._entries[0].last_error_reason == code
                assert refresh.call_count == 1
                assert pool.readiness_counts() == {'profile_count': 3, 'selectable_count': 1}
            cases += 1
        # Transient failure remains retryable; quota state and other grant stay intact.
        pool = cp.CredentialPool('openai-codex', [manual, healthy, quota])
        with patch.object(cp, 'read_credential_pool', return_value=[manual.to_dict()]), \
             patch.object(cp.auth_mod, 'refresh_codex_oauth_pure', side_effect=TimeoutError('synthetic timeout')), \
             patch.object(cp.auth_mod, '_is_terminal_codex_oauth_refresh_error', return_value=False):
            assert pool._refresh_entry_impl(manual, force=False) is None
            assert pool._entries[0].last_status == cp.STATUS_EXHAUSTED
            assert pool._entries[1:] == [healthy, quota]
        cases += 1
        # A peer's rotated pair is recovered only from this exact pool row.
        winner = replace(manual, access_token=token('one'), refresh_token='peer-winner')
        pool = cp.CredentialPool('openai-codex', [manual, healthy, quota])
        with patch.object(cp, 'read_credential_pool', return_value=[winner.to_dict(), quota.to_dict()]):
            assert pool._sync_entry_from_auth_store(manual) == winner
            assert pool._entries == [winner, healthy, quota]
        cases += 1
        with patch.object(cp, 'read_credential_pool', return_value=[quota.to_dict()]):
            assert pool._sync_entry_from_auth_store(manual) == manual
        cases += 1
        # A real successful refresh preserves account/grant ownership and clears status.
        pool = cp.CredentialPool('openai-codex', [manual, healthy, quota])
        with patch.object(cp, 'read_credential_pool', return_value=[manual.to_dict()]), \
             patch.object(cp.auth_mod, 'refresh_codex_oauth_pure', return_value={
                 'access_token': token('one'), 'refresh_token': 'new-manual', 'last_refresh': 'now'}) as refresh:
            result = pool._refresh_entry_impl(manual, force=False)
            assert result.refresh_token == 'new-manual' and result.last_status == cp.STATUS_OK
            refresh.assert_called_once_with(manual.access_token, 'manual-original')
            assert pool._entries[1:] == [healthy, quota]
        cases += 1
    print(f'Codex grant ownership, terminal/transient classification, quota and readiness: {cases} cases passed')


if __name__ == '__main__':
    assert_codex_terminal_refresh(Path(sys.argv[1]))
