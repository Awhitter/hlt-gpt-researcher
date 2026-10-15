"""Provenance is an authenticated native run binding, outside model arguments."""
from contextlib import contextmanager
import copy
import sqlite3
import threading
from types import SimpleNamespace

import pytest
import hlt_artifact_run_context as context

K2 = '019a05e0-1bc9-7040-b76f-628f7b875af5'
OTHER = '019a05e0-1bc9-7040-b76f-628f7b875af6'


@pytest.fixture
def native(tmp_path, monkeypatch):
    path = tmp_path / 'wrapper.db'
    db = sqlite3.connect(path)
    db.execute('''CREATE TABLE agent_run_admissions(wrapper_run_id,k2_run_id,session_key,
        agent_ref,org_id,admission_status,provider_run_id)''')
    db.execute('INSERT INTO agent_run_admissions VALUES(?,?,?,?,?,?,?)',
        ('run_' + K2.replace('-', ''), K2, 'hook:k2:' + K2, 'agent:cleo', 'org-one', 'dispatching', None))
    db.commit()
    provider = sqlite3.connect(':memory:')
    provider.execute('CREATE TABLE run_idempotency(scope,run_id,idempotency_key)')
    provider.execute('INSERT INTO run_idempotency VALUES(?,?,?)',
        ('scope-one', 'native-one', 'hlt-k2:run_' + K2.replace('-', '')))
    provider.commit()
    monkeypatch.setenv('HLT_AGENT_RUN_LEDGER_PATH', str(path))
    monkeypatch.setenv('HLT_AGENT_REF', 'agent:cleo')
    run = SimpleNamespace(gateway_session_key='hook:k2:' + K2, session_id='hook:k2:' + K2,
        run_id='native-one', owner=SimpleNamespace(_run_owners={'native-one': 'scope-one'},
            _run_idempotency_store=SimpleNamespace(_lock=threading.Lock(), _conn=provider)))
    yield run, db, provider
    db.close()
    provider.close()


@contextmanager
def bound(run):
    token = context.bind_native_run(run)
    try:
        yield
    finally:
        context.reset_native_run(token)


@pytest.mark.parametrize('name,args', [
    ('mcp__katailyst2__artifact_save', {'bodyMd': 'Evidence'}),
    ('mcp_katailyst2_artifact_save', {'bodyMd': 'Evidence'}),
    ('mcp__katailyst2__tool_execute', {'verb': 'artifact.save', 'args': {'bodyMd': 'Evidence'}}),
    ('mcp__katailyst2__tool_execute', {'toolRef': 'artifact.save', 'args': {'bodyMd': 'Evidence'}}),
])
def test_artifact_owner_is_injected_without_mutating_model_input(native, name, args):
    original = copy.deepcopy(args)
    with bound(native[0]):
        directive = context.artifact_directive(name, args)
    assert args == original
    rewritten = directive['args'].get('args', directive['args'])
    assert directive['action'] == 'modify' and rewritten['agentRunId'] == K2
    assert rewritten['bodyMd'] == 'Evidence'
    assert context.artifact_directive(name, args) is None


@pytest.mark.parametrize('foreign', [OTHER, None, K2.upper()])
def test_conflicting_model_run_id_is_blocked(native, foreign):
    with bound(native[0]):
        result = context.artifact_directive('mcp__katailyst2__tool_execute',
            {'verb': 'artifact.save', 'args': {'agentRunId': foreign}})
    assert result['action'] == 'block'


@pytest.mark.parametrize('column,value', [('agent_ref', 'agent:julius'), ('provider_run_id', 'foreign-native'),
    ('admission_status', 'terminal'), ('session_key', 'hook:k2:' + OTHER)])
def test_foreign_or_terminal_admission_is_not_artifact_authority(native, column, value):
    native[1].execute(f'UPDATE agent_run_admissions SET {column}=?', (value,))
    native[1].commit()
    with bound(native[0]):
        assert context.artifact_directive('mcp__katailyst2__artifact_save', {})['action'] == 'block'


def test_wrong_native_idempotency_key_is_rejected(native):
    native[2].execute("UPDATE run_idempotency SET idempotency_key='another-admission'")
    with bound(native[0]):
        assert context.artifact_directive('mcp__katailyst2__artifact_save', {})['action'] == 'block'


def test_slack_ordinary_api_and_unrelated_tools_get_no_inferred_id(native):
    with bound(native[0]):
        assert context.artifact_directive('mcp__vault__artifact_save', {}) is None
        assert context.artifact_directive('mcp__katailyst2__artifact_get', {}) is None
    for session in ('slack:workspace:thread', 'ordinary-api-session'):
        native[0].gateway_session_key = native[0].session_id = session
        with bound(native[0]):
            assert context.artifact_directive('mcp__katailyst2__artifact_save', {}) is None
