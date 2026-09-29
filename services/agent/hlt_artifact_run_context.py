"""Artifact provenance from the native API owner, never from model prose."""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3

_RUN = ContextVar('hlt_authenticated_artifact_run', default=None)
_SESSION = re.compile(r'hook:k2:([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\Z')


def bind_native_run(run):
    session = str(run.gateway_session_key or run.session_id or '')
    if not session.startswith('hook:k2:'):
        return _RUN.set(None)
    try:
        match = _SESSION.fullmatch(session)
        if not match:
            raise ValueError('Native K2 session identity is invalid.')
        k2_id = match[1]
        wrapper_id = 'run_' + k2_id.replace('-', '')
        from hermes_constants import get_hermes_home
        path = Path(os.getenv('HLT_AGENT_RUN_LEDGER_PATH') or get_hermes_home() / 'agent-runs.sqlite3').expanduser()
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute('SELECT * FROM agent_run_admissions WHERE wrapper_run_id=?', (wrapper_id,)).fetchone()
        agent_ref = os.getenv('HLT_AGENT_REF', '').strip() or 'agent:' + (os.getenv('AGENT_ID', 'cleo').strip().lower() or 'cleo')
        if (row is None or row['k2_run_id'] != k2_id or row['session_key'] != session
                or row['agent_ref'] != agent_ref or not row['org_id']
                or row['admission_status'] not in {'dispatching', 'provider_bound'}
                or row['provider_run_id'] not in {None, run.run_id}):
            raise ValueError('Native artifact owner does not match the authenticated K2 admission.')
        # The wrapper can still be recording the just-returned provider ID.
        # Only the exact native idempotency reservation proves that race safe.
        store = run.owner._run_idempotency_store
        scope = run.owner._run_owners.get(run.run_id)
        with store._lock:
            native = store._conn.execute('SELECT idempotency_key FROM run_idempotency WHERE scope=? AND run_id=?',
                                         (scope, run.run_id)).fetchone()
        if native is None or native[0] != 'hlt-k2:' + wrapper_id:
            raise ValueError('Native artifact owner lacks the original idempotent admission.')
        return _RUN.set({'agentRunId': k2_id})
    except Exception:
        # A plugin-hook exception normally fails open upstream. Return an
        # explicit block directive at the artifact boundary instead.
        return _RUN.set({'error': 'Artifact provenance could not be verified against the native K2 admission.'})


def reset_native_run(token):
    _RUN.reset(token)


def artifact_directive(tool_name, args):
    name = str(tool_name or '')
    for prefix in ('mcp__katailyst2__', 'mcp_katailyst2_'):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    else:
        return None
    is_save = name in {'artifact.save', 'artifact_save'}
    nested = name in {'tool.execute', 'tool_execute'} and isinstance(args, dict) and (
        args.get('verb') in ('artifact.save', 'artifact_save') or args.get('toolRef') in ('artifact.save', 'artifact_save'))
    if not is_save and not nested:
        return None
    context = _RUN.get()
    if context is None:
        return None
    if context.get('error'):
        return {'action': 'block', 'message': context['error']}
    value = args.get('args') if nested else args
    if not isinstance(value, dict):
        return {'action': 'block', 'message': 'artifact.save requires an argument object.'}
    run_id = context['agentRunId']
    if 'agentRunId' in value and value['agentRunId'] != run_id:
        return {'action': 'block', 'message': 'artifact.save agentRunId conflicts with the native K2 run.'}
    bound = {**value, 'agentRunId': run_id}
    return {'action': 'modify', 'args': {**args, 'args': bound} if nested else bound}
