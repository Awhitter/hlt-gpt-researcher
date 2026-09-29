"""Native continuation proof with synthetic providers; never invokes a model."""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


async def exercise(root: Path) -> None:
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.platforms.api_server_runs import _resume_due_provider_waits
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
    from agent.conversation_loop import _LoopState
    from agent.iteration_budget import IterationBudget
    from agent.tool_guardrails import ToolCallGuardrailController
    from hlt_provider_checkpoint import snapshot_provider_wait, restore_turn_context, _settled_tools

    transcript = []
    executions, tool_effects, turn_ids = [], [], []

    class Agent:
        def __init__(self, **kwargs):
            self.session_id = kwargs['session_id']
            self.callback = kwargs['tool_progress_callback']
            self.max_iterations = 4
            self.iteration_budget = IterationBudget(4)
            self._tool_guardrails = ToolCallGuardrailController()
            self._interrupt_requested = False
            self._memory_store = SimpleNamespace()
            self._todo_store = SimpleNamespace(has_items=lambda: True)
            self._user_turn_count = 1
            self.api_mode = 'anthropic_messages'
            self._compression_warning = None
            self._session_db_created = True
            self._session_db = SimpleNamespace(get_messages=lambda *a, **k: copy.deepcopy(transcript))
            self._end_session_on_close = True
            self.session_prompt_tokens = 20
            self.session_completion_tokens = 10
            self.session_total_tokens = 30
            self.closed = False
        def _ensure_db_session(self):
            self._session_db_created = True
        def _flush_messages_to_session_db(self, messages, history=None):
            for row in messages:
                if not row.get('_db_persisted'):
                    row['_db_persisted'] = True
                    transcript.append(copy.deepcopy(row))
            return True
        def close(self):
            self.closed = True
        def run_conversation(self, user_message, **kwargs):
            executions.append(self)
            assert self._hlt_provider_wait_enabled, 'authenticated native admission did not enable provider recovery'
            if getattr(self, '_hlt_resume_turn', None):
                ctx = restore_turn_context(self, self._hlt_resume_turn, None,
                    lambda: SimpleNamespace(_set_interrupt=lambda *a, **k: None))
                turn_ids.append(ctx.turn_id)
                assert ctx.messages[1]['tool_calls'][0]['id'] == 'tool-once'
                assert self.iteration_budget.used == 2
                assert self._tool_guardrails._turn_web_search_count == 1
                assert self.session_total_tokens == 30
                assert self._delivered_interim_texts == {'Researching sources.'}
                assert len([r for r in transcript if r['role'] == 'user']) == 1
                return {'completed': True, 'final_response': 'Search count: 975.'}
            messages = [
                {'role': 'user', 'content': user_message},
                {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'tool-once',
                    'function': {'name': 'web_search', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'tool-once', 'content': '{"search_count":975}'}]
            tool_effects.append('search')
            self.callback('tool.completed', 'web_search', result={'search_count': 975}, is_error=False)
            self._tool_guardrails.before_call('web_search', {})
            self.iteration_budget.consume()
            self.iteration_budget.consume()
            self._delivered_interim_texts = {'Researching sources.'}
            state = _LoopState(user_message=user_message, system_message=None, moa_config=None,
                original_user_message=user_message, conversation_history=[], effective_task_id=self.session_id,
                turn_id='original-turn', _should_review_memory=False, _plugin_user_context=None,
                _ext_prefetch_cache=None, messages=messages, active_system_prompt='Existing governed prompt',
                current_turn_user_idx=0, _preflight_compression_blocked=False, max_compression_attempts=3,
                api_call_count=2)
            turn_ids.append(state.turn_id)
            checkpoint = snapshot_provider_wait(self, state,
                {'failed': True, 'failure_reason': 'rate_limit', 'failure_resets_at': time.time() + 300})
            assert checkpoint is not None, 'settled native boundary did not produce a checkpoint'
            return checkpoint

    class Request:
        def __init__(self, body=None, run_id=None):
            self.body = body or {}
            self.headers = {'Authorization': 'Bearer synthetic-test-key', 'Idempotency-Key': 'initial-admission'}
            self.path = f'/v1/runs/{run_id}/stop' if run_id else '/v1/runs'
            self.method = 'POST'
            self.match_info = {'run_id': run_id}
        async def json(self):
            return self.body

    def adapter():
        value = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'synthetic-test-key'}))
        value._create_agent = lambda **kwargs: Agent(**kwargs)
        return value

    async def settled(value, run_id):
        for _ in range(200):
            if run_id not in value._active_run_tasks:
                return value._run_statuses[run_id]
            await asyncio.sleep(.01)
        raise AssertionError('native worker did not settle')

    first = adapter()
    accepted = await first._handle_runs(Request({'input': 'Read search count.', 'session_id': 'same-session',
        'max_iterations': 4, 'provider_recovery': True, 'execution_budget_seconds': 300}))
    assert accepted.status == 202, accepted.text
    body = json.loads(accepted.text)
    assert body['nativeStop'] is True
    run_id = body['run_id']
    waiting = await settled(first, run_id)
    assert waiting['status'] == 'waiting_for_provider', waiting
    assert waiting['recovery']['automaticResume'] is True
    assert executions[0]._end_session_on_close is False and executions[0].closed
    assert waiting['activeExecutionSeconds'] > 0
    scope = next(iter(first._run_owners.values()))
    saved = first._run_idempotency_store.provider_waits(scope)[0]
    assert not first._run_idempotency_store.claim_provider_wait(scope, run_id, saved['generation'],
        owner_pid=1, owner_started=1, now=time.time())
    # Restart the native adapter; make the already-recorded reset due without waiting.
    first._run_idempotency_store.close()
    restarted = adapter()
    due = dict(waiting, recovery=dict(waiting['recovery'], retryAt=1))
    restarted._run_idempotency_store.update_status(run_id, due)
    await asyncio.gather(_resume_due_provider_waits(restarted), _resume_due_provider_waits(restarted))
    complete = await settled(restarted, run_id)
    assert complete['status'] == 'completed', complete
    assert complete['output'] == 'Search count: 975.'
    assert complete['grounding']['status'] == 'passed'
    assert tool_effects == ['search'] and turn_ids == ['original-turn', 'original-turn']
    assert len(executions) == 2
    assert restarted._run_idempotency_store.provider_waits(scope) == []
    assert sum(row['role'] == 'user' for row in transcript) == 1
    # A completed call without its receipt, failed result or detached child may
    # not claim an automatic continuation.
    base = saved['checkpoint']['turn']['loop']['messages']
    assert not _settled_tools(base[:-1], 0)
    bad = copy.deepcopy(base); bad[-1]['content'] = '{"status":"unknown"}'
    assert not _settled_tools(bad, 0)
    from agent.tool_dispatch_helpers import _maybe_wrap_untrusted
    bad[-1]['tool_name'] = 'web_search'
    bad[-1]['content'] = _maybe_wrap_untrusted('web_search', json.dumps({'success': False, 'error': 'x' * 500}))
    assert not _settled_tools(bad, 0), 'wrapped native failure authorized replay'
    bad[-1]['content'] = _maybe_wrap_untrusted('web_search', 'unstructured result ' * 50)
    assert not _settled_tools(bad, 0), 'unparseable wrapped result authorized replay'
    from agent.tool_dispatch_helpers import make_tool_result_message
    from agent.tool_executor import _unfinished_tool_result
    unfinished = SimpleNamespace(name='write_file', emit_post=lambda *a, **k: None)
    text, _, disposition = _unfinished_tool_result(SimpleNamespace(), unfinished,
        timed_out=True, timeout_s=30.0)
    bad = copy.deepcopy(base)
    bad[1]['tool_calls'][0]['function']['name'] = 'write_file'
    bad[-1] = make_tool_result_message('write_file', text, 'tool-once', effect_disposition=disposition)
    assert disposition == 'unknown' and not _settled_tools(bad, 0), 'native timed-out write authorized replay'
    text, _, disposition = _unfinished_tool_result(SimpleNamespace(_interrupt_requested=False), unfinished,
        timed_out=False, timeout_s=None)
    bad[-1] = make_tool_result_message('write_file', text, 'tool-once', effect_disposition=disposition)
    assert disposition is None and not _settled_tools(bad, 0), 'native missing-worker receipt authorized replay'
    for unsupported in ('pending', 'in_progress', 'future_disposition', ''):
        bad[-1] = make_tool_result_message('write_file', '{}', 'tool-once', effect_disposition=unsupported)
        assert not _settled_tools(bad, 0), 'unsupported native disposition authorized replay'
    bad = copy.deepcopy(base); bad[1]['tool_calls'][0]['function']['name'] = 'delegate_task'
    assert not _settled_tools(bad, 0)
    changed = Agent(session_id='same-session', tool_progress_callback=lambda *a, **k: None)
    transcript.append({'role': 'user', 'content': 'Another writer changed the session.'})
    try:
        restore_turn_context(changed, saved['checkpoint']['turn'], None, lambda: None)
    except ValueError as exc:
        assert 'transcript changed' in str(exc)
    else:
        raise AssertionError('changed transcript resumed')
    with patch('hlt_provider_checkpoint._runtime_contract_digest', return_value='different-source'):
        try:
            restore_turn_context(changed, saved['checkpoint']['turn'], None, lambda: None)
        except ValueError as exc:
            assert 'runtime changed' in str(exc)
        else:
            raise AssertionError('different source resumed a parked checkpoint')
    changed.model = 'changed-model-policy'
    try:
        restore_turn_context(changed, saved['checkpoint']['turn'], None, lambda: None)
    except ValueError as exc:
        assert 'model or tool policy changed' in str(exc)
    else:
        raise AssertionError('different model policy resumed a parked checkpoint')
    # Exercise the actual SQLite owner: parked Stop persists across reopen;
    # competing generation claims and a claimed-worker crash cannot replay.
    store_path = Path(os.environ['HERMES_HOME']) / 'lease-test.db'
    store = RunIdempotencyStore(str(store_path))
    for name in ('stopped', 'claimed', 'rolledback', 'reconcile-failed', 'reconcile-interrupted'):
        store.reserve(scope, name, name, name, {'status': 'running', 'run_id': name}, owner_pid=11, owner_started=12)
        status = {'status': 'waiting_for_provider', 'run_id': name,
                  'recovery': {'state': 'waiting_for_provider', 'reason': 'rate_limit', 'retryAt': 1,
                               'automaticResume': True, 'continuationRequired': False}}
        assert store.park_provider_wait(scope, name, 0, saved['checkpoint'], status, owner_pid=11, owner_started=12)
    # A rejected continuation retains completed work and cumulative budgets for
    # private reconciliation, while its terminal state permanently blocks replay.
    for status in ('failed', 'interrupted'):
        name = 'reconcile-' + status
        store.update_status(name, {'run_id': name, 'status': status})
        row = store._conn.execute(
            'SELECT provider_wait_state,provider_wait_json FROM run_idempotency WHERE run_id=?',
            (name,),
        ).fetchone()
        assert row[0] == 'terminal'
        assert json.loads(row[1]) == saved['checkpoint']
        assert not store.claim_provider_wait(scope, name, 1, owner_pid=13, owner_started=14, now=time.time())
    # Simulate the old runtime, which knows status_json but not new checkpoint columns.
    store._conn.execute("UPDATE run_idempotency SET status_json=? WHERE run_id='rolledback'",
                        (json.dumps({'run_id': 'rolledback', 'status': 'interrupted'}),))
    store._conn.commit()
    assert not store.claim_provider_wait(scope, 'rolledback', 1, owner_pid=13, owner_started=14, now=time.time())
    assert store.stop_provider_wait(scope, 'stopped')['status'] == 'cancelled'
    store.close(); store = RunIdempotencyStore(str(store_path))
    assert not store.claim_provider_wait(scope, 'stopped', 1, owner_pid=13, owner_started=14, now=time.time())
    assert store.claim_provider_wait(scope, 'claimed', 1, owner_pid=13, owner_started=14, now=time.time())
    assert not store.claim_provider_wait(scope, 'claimed', 1, owner_pid=15, owner_started=16, now=time.time())
    store.close(); store = RunIdempotencyStore(str(store_path))
    assert store.provider_waits(scope) == []
    for status in ('failed', 'interrupted'):
        name = 'reconcile-' + status
        row = store._conn.execute(
            'SELECT provider_wait_json FROM run_idempotency WHERE run_id=?', (name,),
        ).fetchone()
        assert json.loads(row[0]) == saved['checkpoint']
        assert not store.claim_provider_wait(scope, name, 1, owner_pid=15, owner_started=16, now=time.time())
    store.stop_provider_wait(scope, 'claimed')
    assert store.provider_wait_stopped(scope, 'claimed')
    assert not store.park_provider_wait(scope, 'claimed', 1, saved['checkpoint'], due, owner_pid=13, owner_started=14)
    store.close(); restarted._run_idempotency_store.close()


def exercise_actual_conversation_loop(*, slack: bool = False, artifact: bool = False, invalid_owner: bool = False) -> None:
    """Real facade, loop, retry classifier/fallback, tool persistence and restore.

    Only provider transport and the harmless tool implementation are synthetic;
    no loop phase, context admission or checkpoint function is replaced.
    """
    from contextlib import ExitStack
    from unittest.mock import MagicMock
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent
    from hermes_state import SessionDB
    import agent.conversation_loop as native
    import hlt_provider_checkpoint as checkpoint_owner

    tool_name = 'mcp__katailyst2__artifact_save' if artifact else 'web_search'
    tool = {'type': 'function', 'function': {'name': tool_name, 'description': 'Synthetic research',
            'parameters': {'type': 'object', 'properties': {}}}}
    calls, effects = [], []
    recovered = False
    db = SessionDB(db_path=Path(os.environ['HERMES_HOME']) / ('actual-invalid-owner-loop.db' if invalid_owner else 'actual-artifact-loop.db' if artifact else 'actual-slack-loop.db' if slack else 'actual-loop.db'))
    stream = MagicMock()
    native_thread_ids, hook_thread_ids = [], []
    session_id = 'hook:k2:019a05e0-1bc9-7040-b76f-628f7b875af5' if artifact else 'actual-original-session'
    if invalid_owner:
        assert artifact
        session_id = 'hook:k2:019a05e0-1bc9-7040-b76f-628f7b875af6'
    tool_args = {'title': 'Verified evidence', 'bodyMd': 'Search count: 975.', 'idempotencyKey': 'native-proof'} if artifact else {}
    expected_args = {**tool_args, 'agentRunId': session_id.removeprefix('hook:k2:')} if artifact else {}
    native_adapter, native_launch = None, None
    if artifact:
        from gateway.config import PlatformConfig
        from gateway.platforms.api_server import APIServerAdapter
        from agent_run_ledger import AgentRunLedger
        native_adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': 'synthetic-test-key'}))
        k2_id = expected_args['agentRunId']
        wrapper = AgentRunLedger(Path(os.environ['HERMES_HOME']) / 'agent-runs.sqlite3')
        record, _ = wrapper.admit(k2_run_id=k2_id, session_key=session_id, org_id='synthetic-org',
            agent_ref='agent:cleo', fingerprint='synthetic-fingerprint')
        assert wrapper.claim_dispatch(record['wrapper_run_id'])
        native_id, scope = ('native-artifact-invalid' if invalid_owner else 'native-artifact-original'), 'synthetic-native-scope'
        native_adapter._run_owners[native_id] = scope
        native_adapter._run_idempotency_store.reserve(scope, 'foreign-native-admission' if invalid_owner else 'hlt-k2:' + record['wrapper_run_id'],
            'native-fingerprint', native_id, {'run_id': native_id, 'status': 'running'}, owner_pid=1, owner_started=1)
        native_launch = SimpleNamespace(owner=native_adapter, run_id=native_id, session_id=session_id,
            gateway_session_key=session_id, approval_session_key=native_id, request_profile=None,
            browser_control_principal=None, browser_control_transport_family=None,
            session_history_delivery=True, agent_kwargs={'room_dispatch': None}, turn_author=None,
            max_turns=8, provider_wait_enabled=True, active_seconds=0.0, provider_checkpoint=None,
            user_message='Save the verified evidence.', conversation_history=[], declared_selected=False)

    def run_turn(value, saved=None):
        if artifact:
            import threading
            from gateway.platforms import api_server
            from gateway.platforms.api_server_runs import _run_agent_sync
            from hlt_artifact_run_context import _RUN
            from concurrent.futures import ThreadPoolExecutor
            def worker():
                native_thread_ids.append(threading.get_ident())
                result, _, _ = _run_agent_sync(native_adapter, native_launch, value, lambda *a, **k: None, _api_server=api_server)
                assert _RUN.get() is None, 'native API worker leaked artifact identity after finally'
                return result
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(worker).result(timeout=30)
        if not slack:
            return value.run_conversation('Read and save the search count.', task_id='actual-original-task')
        from gateway.run_turn_runner import TurnRunner
        from gateway.turn_context import TurnContext
        from gateway.config import Platform
        from gateway.session import SessionSource
        ctx = TurnContext(source=SessionSource(platform=Platform.SLACK, chat_id='proof-channel', user_id='proof-human'),
            session_id=value.session_id, session_key='original-slack-thread', user_config={},
            message='Read and save the search count.', agent_holder=[value])
        ctx._hlt_managed_control = {'provider_owner': {'active_seconds': 0.0, 'tool_budget': {}}}
        if saved is not None:
            ctx._hlt_resume_turn = saved
        runner = MagicMock()
        runner._pre_agent_fallback_notice = None
        runner._resolve_session_agent_runtime.return_value = ('synthetic-primary', {'provider': 'openai'})
        runner._resolve_session_reasoning_config.return_value = {}
        native_runner = TurnRunner(runner, ctx)
        native_runner._combined_ephemeral_prompt = lambda: ''
        native_runner._setup_stream_consumer = lambda key: (stream, None, None, False)
        native_runner._resolve_turn_agent = lambda *a: (value, False)
        native_runner._wire_turn_agent_callbacks = lambda *a: None
        native_runner._load_turn_history = lambda *a: ([], None, set())
        native_runner._prepare_turn_message = lambda *a: (None, None)
        native_runner._native_image_run_message = lambda: ctx.message
        native_runner._sync_session_after_run = lambda *a: (False, value.session_id, 0)
        native_runner._append_auto_media_tags = lambda final, *a: final
        # Actual TurnRunner.run_sync, actual approval context, actual native
        # loop and retry ordering; only boot/display plumbing is synthetic.
        return native_runner.run_sync()

    class Exhausted(Exception):
        status_code = 429
        def __init__(self):
            super().__init__('Error code: 429 - rate limit exceeded')
            self.response = SimpleNamespace(headers={})
            self.body = {'error': {'message': 'rate limit exceeded'}}

    def response(model, *, tool_call=False):
        message = {'role': 'assistant', 'content': 'Search count: 975.' if not tool_call else None}
        if tool_call:
            message['tool_calls'] = [{'id': 'actual-tool-once', 'type': 'function',
                'function': {'name': tool_name, 'arguments': json.dumps(tool_args)}}]
        return ChatCompletion.model_validate({'id': 'synthetic-response', 'object': 'chat.completion',
            'created': 1, 'model': model, 'choices': [{'index': 0, 'message': message,
                'finish_reason': 'tool_calls' if tool_call else 'stop'}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}})

    def make_agent():
        value = AIAgent(model='synthetic-primary', provider='openai', api_key='synthetic-key',
            base_url='http://localhost:1/v1', platform='slack' if slack else 'api_server', max_iterations=8,
            session_id=session_id, session_db=db, quiet_mode=True,
            skip_context_files=True, skip_memory=True, save_trajectories=False,
            fallback_model=[{'provider': 'openrouter', 'model': 'synthetic-recovery',
                             'base_url': 'http://localhost:2/v1'}])
        value._disable_streaming = True
        value._api_max_retries = 2
        value._hlt_provider_wait_enabled = True
        value._cached_system_prompt = 'Use the research tool once, then report its saved evidence.'
        value._try_recover_primary_transport = lambda *a, **k: False
        def transport(kwargs):
            calls.append((value.provider, value.model, copy.deepcopy(kwargs.get('messages'))))
            if len(calls) == 1:
                return response(value.model, tool_call=True)
            if not recovered:
                raise Exhausted()
            return response(value.model)
        def invoke(name, args, *a, **kwargs):
            effects.append((name, copy.deepcopy(args)))
            return '{"search_count":975}'
        value._interruptible_api_call = transport
        value._invoke_tool = invoke
        return value

    fallback_client = MagicMock()
    fallback_client.base_url = 'http://localhost:2/v1'
    fallback_client.api_key = 'synthetic-recovery-key'
    fallback_client._custom_headers = None
    fallback_client.default_headers = None
    agents = []
    try:
        with ExitStack() as stack:
            stack.enter_context(patch('model_tools.get_tool_definitions', return_value=[tool]))
            stack.enter_context(patch('model_tools.check_toolset_requirements', return_value={}))
            # The native sequential registry path dispatches here, while its
            # concurrent path uses agent._invoke_tool. Keep both transports
            # synthetic; the native hooks, worker and persistence still run.
            stack.enter_context(patch('model_tools.handle_function_call', side_effect=lambda name, args, *a, **k: (effects.append((name, copy.deepcopy(args))) or '{"search_count":975}')))
            stack.enter_context(patch('agent.process_bootstrap.OpenAI', return_value=MagicMock()))
            stack.enter_context(patch('agent.auxiliary_client.resolve_provider_client', return_value=(fallback_client, 'synthetic-recovery')))
            stack.enter_context(patch('agent.model_metadata.get_model_context_length', return_value=200000))
            stack.enter_context(patch('hermes_cli.model_normalize.normalize_model_for_provider', side_effect=lambda model, provider: model))
            stack.enter_context(patch('agent.turn_api_error.interruptible_backoff_sleep', return_value=None))
            stack.enter_context(patch('agent.turn_context._maybe_title_session_at_turn_start'))
            if artifact:
                import importlib.util
                import threading
                import hlt_artifact_run_context
                # Docker keeps assertions in /tmp/hermes-patches and the actual
                # application in /app. Resolve the plugin beside its installed
                # native context owner, never beside this standalone proof.
                plugin_dir = Path(hlt_artifact_run_context.__file__).resolve().parent / 'hermes_plugins' / 'hlt_k2_context'
                spec = importlib.util.spec_from_file_location('hlt_artifact_proof_plugin', plugin_dir / '__init__.py', submodule_search_locations=[str(plugin_dir)])
                plugin = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = plugin
                spec.loader.exec_module(plugin)
                def lifecycle(name, **kwargs):
                    if name == 'pre_tool_call':
                        hook_thread_ids.append(threading.get_ident())
                        return [plugin._pre_tool_call(**kwargs)]
                    return []
                stack.enter_context(patch('hermes_cli.lifecycle.invoke_hook', side_effect=lifecycle))
            if slack:
                # The separate owner proof exercises the Slack policy ledger;
                # this fixture focuses on the actual conversation/tool boundary.
                stack.enter_context(patch('hlt_slack_provider_wait.restore_tool_budget'))
            # Network is forbidden even if an unforeseen optional probe appears.
            stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('proof attempted network')))
            stack.enter_context(patch('socket.getaddrinfo', side_effect=AssertionError('proof attempted DNS lookup')))
            actual_turn = stack.enter_context(patch.object(native, '_run_conversation_turn', wraps=native._run_conversation_turn))
            actual_retry = stack.enter_context(patch.object(native, '_run_api_retry_loop', wraps=native._run_api_retry_loop))
            snapshot = stack.enter_context(patch.object(checkpoint_owner, 'snapshot_provider_wait', wraps=checkpoint_owner.snapshot_provider_wait))
            failed_close = stack.enter_context(patch.object(native, '_close_durable_failed_turn', wraps=native._close_durable_failed_turn))
            first = make_agent(); agents.append(first)
            parked = run_turn(first)
            if invalid_owner:
                # A hook exception would fail open upstream. Exercise the actual
                # directive consumer on its daemon tool worker and prove that
                # it refuses transport, persists a blocked result, and prevents
                # a checkpoint from authorizing replay of that refused effect.
                assert effects == [], effects
                assert parked.get('failed') is True and not parked.get('provider_wait'), parked
                rows = [row for row in db.get_messages(first.session_id) if row['role'] == 'tool']
                assert len(rows) == 1 and rows[0].get('tool_call_id') == 'actual-tool-once', rows
                assert 'Artifact provenance could not be verified against the native K2 admission.' in rows[0]['content'], rows
                assert hook_thread_ids and set(hook_thread_ids).isdisjoint(native_thread_ids), 'native tool worker boundary was not exercised'
                from hlt_artifact_run_context import _RUN
                assert _RUN.get() is None
                return
            assert parked.get('provider_wait') is True, parked
            assert actual_turn.call_count == 1 and actual_retry.call_count >= 2
            assert snapshot.call_count == 1 and failed_close.call_count == 0
            assert {call[0] for call in calls} == {'openai', 'openrouter'}, calls
            assert effects == [(tool_name, expected_args)], (effects, parked['checkpoint']['loop']['messages'])
            if slack:
                stream.finish.assert_not_called()
            saved = parked['checkpoint']
            boundary = saved['loop']['current_turn_user_idx']
            assert saved['loop']['messages'][boundary]['role'] == 'user'
            assert any(row.get('tool_call_id') == 'actual-tool-once' for row in saved['loop']['messages'])
            assert sum(row['role'] == 'user' for row in db.get_messages(first.session_id)) == 1
            assert sum(row['role'] == 'tool' for row in db.get_messages(first.session_id)) == 1
            used = saved['iteration_budget']['used']
            total_tokens = saved['agent']['session_total_tokens']
            original_turn_id = first._current_turn_id
            first._end_session_on_close = False
            first.close()
            recovered = True
            resumed = make_agent(); agents.append(resumed)
            resumed._hlt_resume_turn = json.loads(json.dumps(saved))
            complete = run_turn(resumed, saved)
            assert complete.get('completed') is True, complete
            assert complete['final_response'] == 'Search count: 975.'
            assert actual_turn.call_count == 2
            assert resumed._current_turn_id == original_turn_id
            assert resumed._current_task_id == (session_id if slack or artifact else 'actual-original-task')
            if slack:
                stream.finish.assert_called_once_with('Search count: 975.')
            assert resumed.iteration_budget.used >= used
            assert resumed.session_total_tokens >= total_tokens
            assert effects == [(tool_name, expected_args)]
            assert sum(row['role'] == 'user' for row in db.get_messages(resumed.session_id)) == 1
            assert sum(row['role'] == 'tool' for row in db.get_messages(resumed.session_id)) == 1
            assert any(row.get('tool_call_id') == 'actual-tool-once' for row in calls[-1][2])
            if artifact:
                assert hook_thread_ids and set(hook_thread_ids).isdisjoint(native_thread_ids), 'native tool worker boundary was not exercised'
    finally:
        for agent in agents:
            agent.close()
        db.close()
        if native_adapter is not None:
            native_adapter._run_idempotency_store.close()


def main():
    root = Path(sys.argv[1])
    # Ensure the tested helper owns the actual native early-exit, before the
    # failed-turn closer; resume bypasses initial user admission and hooks.
    loop_source = (root / 'agent/conversation_loop.py').read_text()
    ast.parse(loop_source)
    assert 'snapshot_provider_wait(agent, s, early_result)' in loop_source
    assert 'not result.get("provider_wait")' in loop_source
    context = (root / 'agent/turn_context.py').read_text()
    assert context.index('return restore_turn_context(') < context.index('effective_task_id, turn_id = _bind_turn_identity(', context.index('def build_turn_context('))
    with tempfile.TemporaryDirectory(prefix='hlt-provider-wait-proof-') as temporary:
        with patch.dict(os.environ, {'HERMES_HOME': temporary, 'HLT_MANAGED_MODEL_ROUTE': '1'}):
            asyncio.run(exercise(root))
            exercise_actual_conversation_loop()
            exercise_actual_conversation_loop(slack=True)
            exercise_actual_conversation_loop(artifact=True)
            exercise_actual_conversation_loop(artifact=True, invalid_owner=True)


if __name__ == '__main__':
    main()
