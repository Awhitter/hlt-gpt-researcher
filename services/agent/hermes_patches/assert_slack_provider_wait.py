"""Synthetic native Slack ownership proof; no provider or Slack network calls."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


async def exercise():
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    from gateway.stream_consumer import StreamConsumerConfig
    from gateway.run_busy import GatewayBusySessionMixin
    from hlt_slack_provider_wait import SlackProviderOwner, _CONSUMER_FIELDS, verify_coordination

    source = SessionSource(platform=Platform.SLACK, chat_id='channel', scope_id='workspace',
        thread_id='original-thread', user_id='human', profile='default')
    event = MessageEvent(text='Original request', message_type=MessageType.TEXT,
        source=source, message_id='human-request')
    state = SimpleNamespace(turn=SimpleNamespace(lease=MagicMock(), event=event))
    adapter = SimpleNamespace(_active_streams={}, _uncertain_stream_starts={}, _bot_message_ts=set(),
        _active_sessions={}, _session_tasks={}, _release_session_guard=MagicMock(),
        set_native_agent_session_status=AsyncMock(), _start_session_processing=MagicMock(return_value=True))
    runner = SimpleNamespace(
        _peek_session_state=lambda key: state, _session_state=lambda key: state,
        _delivery_adapter_for=lambda source: adapter, _is_user_authorized_for_source=lambda source: True,
        _is_session_run_current=lambda key, generation: True,
        _claim_active_session_slot=MagicMock(return_value=(MagicMock(), None)),
        _clear_durable_active_turn=AsyncMock(), _mark_durable_active_turn=AsyncMock(),
        async_session_store=SimpleNamespace(clear_resume_pending=AsyncMock()),
        _enqueue_fifo=MagicMock(), _is_session_running=lambda key: False,
        _release_running_agent_state=MagicMock(), _release_turn_lease=MagicMock(),
        _begin_session_run_generation=lambda key: 22,
        _session_sources={}, session_store=SimpleNamespace(_entries={
            'thread-key': SimpleNamespace(session_id='session-original')}))
    service = SlackProviderOwner(runner)
    runner._hlt_slack_provider_owner = service
    service.pid, service.started = 11, 12
    launch = {'run_id': 'original', 'schema': 'hlt_slack_provider_wait.v1',
        'session_key': 'thread-key', 'session_id': 'session-original',
        'source': source.to_dict(), 'event': {'text': event.text, 'message_id': event.message_id},
        'message_type': 'text', 'prepared': {}, 'generation': 7,
        'checkpoint_generation': 0, 'active_seconds': 23.5}
    service.store.reserve(service.scope, 'original', 'original', 'original',
        {'run_id': 'original', 'status': 'running'}, owner_pid=11, owner_started=12)
    service.live['thread-key'] = launch
    control = {'provider_owner': launch, 'stop_requested': False}
    ctx = SimpleNamespace(source=source, session_key='thread-key', run_generation=7,
        event_message_id='original-thread', stream_consumer_holder=[None])
    result = {'provider_wait': True, 'checkpoint': {'loop': {'turn_id': 'same-native-turn'},
        'iteration_budget': {'used': 3, 'max_total': 8}},
        'recovery': {'state': 'waiting_for_provider', 'reason': 'billing', 'retryAt': 1,
                     'automaticResume': True, 'continuationRequired': False}}
    with patch('hlt_slack_provider_wait.capture_tool_budget', return_value={'rounds': ['completed-round']}), \
         patch('hlt_slack_provider_wait.verify_coordination'):
        assert await service.park_and_wait(ctx, result, control)
    assert ctx._hlt_resume_turn == result['checkpoint']
    assert ctx._hlt_active_seconds == 23.5
    assert launch['checkpoint_generation'] == 1
    assert service.store.provider_waits(service.scope) == []
    assert runner._clear_durable_active_turn.await_count == 1
    assert runner._mark_durable_active_turn.await_count == 1
    assert runner._claim_active_session_slot.call_args.kwargs == {'continuation': True}
    assert service.waiting_count() == 0
    assert not service.store.claim_provider_wait(service.scope, 'original', 1,
        owner_pid=13, owner_started=14, now=99999)

    # Park a second exhaustion on the same run, then Stop survives reopen and
    # prevents a model lease. No fresh event/second admission is generated.
    due = {**result['recovery'], 'retryAt': 9999999999999}
    service.wait_and_claim = AsyncMock(return_value=False)
    with patch('hlt_slack_provider_wait.capture_tool_budget', return_value={'rounds': ['completed-round']}):
        await service.park_and_wait(ctx, {**result, 'recovery': due}, control)
    assert service.waiting_count() == 1
    followup = MessageEvent(text='Human follow-up', message_type=MessageType.TEXT,
        source=source, message_id='followup-once')
    assert service.queue_followup(followup, 'thread-key')
    assert service.queue_followup(followup, 'thread-key')
    bot = MessageEvent(text='Bot cannot recruit', message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.SLACK, chat_id='channel', is_bot=True), message_id='bot')
    assert not service.queue_followup(bot, 'thread-key')
    assert service.stop('thread-key')
    assert service.store.provider_wait_stopped(service.scope, 'original')
    service.finish('thread-key', 'completed')
    assert service.store.status_for_run(service.scope, 'original')['status']['status'] == 'cancelled'
    await service.drain_followups('thread-key', adapter)
    await service.drain_followups('thread-key', adapter)
    assert runner._enqueue_fifo.call_count == 1
    assert runner._enqueue_fifo.call_args.args[1].message_id == 'followup-once'
    assert service.store._conn.execute('SELECT state FROM slack_wait_followups').fetchone()[0] == 'claimed'
    service.acknowledge_followup(runner._enqueue_fifo.call_args.args[1], 'thread-key', 'native-followup-run')
    assert service.store._conn.execute('SELECT state,admitted_run_id FROM slack_wait_followups').fetchone() == ('admitted', 'native-followup-run')
    service.store.close()

    # A retained session guard must not bypass the actual native capacity lease.
    # Waiting does not consume capacity, but the resumed execution does.
    class CapacityRunner(GatewayBusySessionMixin):
        config = {}
        _hlt_slack_provider_owner = SimpleNamespace(waiting_count=lambda: 1)
        def _get_max_concurrent_sessions(self):
            return 1
        def _running_agent_count(self):
            return 1
        def _is_session_running(self, key):
            return key == 'parked'
    capacity = CapacityRunner()
    lease = MagicMock()
    with patch('hermes_cli.active_sessions.try_acquire_active_session', return_value=(lease, None)) as acquire:
        assert capacity._claim_active_session_slot('parked', source) == (None, None)
        acquire.assert_not_called()
        assert capacity._claim_active_session_slot('parked', source, continuation=True) == (lease, None)
        assert acquire.call_count == 1
        capacity._running_agent_count = lambda: 2
        assert capacity._claim_active_session_slot('parked', source, continuation=True)[1]
        assert acquire.call_count == 1
    capacity._running_agent_count = lambda: 1
    with patch('hermes_cli.active_sessions.try_acquire_active_session', side_effect=OSError('registry unavailable')):
        assert capacity._claim_active_session_slot('parked', source, continuation=True)[1]

    saved_decision = {'coordinationId': 'original-coordination', 'revision': 1,
                      'leadAgentRef': 'agent:cleo', 'mayRespond': True}
    plugin = SimpleNamespace(claim_coordination=MagicMock(return_value={**saved_decision, 'revision': 2}))
    with patch('hlt_slack_provider_wait._plugin', return_value=plugin):
        try:
            verify_coordination({'coordination': saved_decision, 'caller_agent_ref': 'agent:cleo'})
        except ValueError:
            pass
        else:
            raise AssertionError('changed lead decision was automatically resumed')
    assert plugin.claim_coordination.call_args.kwargs['operation'] == 'get'

    # Restoring the same acknowledged native stream cannot post another one.
    service = SlackProviderOwner(runner)
    consumer = SimpleNamespace(cfg=StreamConsumerConfig(), **{name: None for name in _CONSUMER_FIELDS})
    restored = {**launch, 'generation': 22, 'delivery': {'mode': 'native',
        'key': ['workspace', 'channel', 'original-thread', 'same-consumer'],
        'config': dataclasses.asdict(consumer.cfg),
        'stream': {'ts': 'slack-message-once', 'sent': 'Research saved.',
                   'session_key': 'thread-key', 'run_generation': 7},
        'consumer': {'_turn_id': 'same-consumer', '_transport_primed': True}}}
    service.restore_delivery(adapter, restored, consumer)
    restored_stream = adapter._active_streams[tuple(restored['delivery']['key'])]
    assert restored_stream['ts'] == 'slack-message-once'
    assert restored_stream['run_generation'] == 22
    assert consumer._turn_id == 'same-consumer' and consumer._transport_primed
    assert 'slack-message-once' in adapter._bot_message_ts
    uncertain = copy.deepcopy(restored)
    uncertain['delivery']['key'][-1] = 'uncertain'
    uncertain['delivery']['stream']['append_uncertain'] = True
    try:
        service.restore_delivery(adapter, uncertain, consumer)
    except ValueError:
        pass
    else:
        raise AssertionError('uncertain Slack delivery was automatically replayed')
    changed_policy = copy.deepcopy(restored)
    changed_policy['delivery']['key'][-1] = 'changed-policy'
    changed_policy['delivery']['config']['transport'] = 'off'
    try:
        service.restore_delivery(adapter, changed_policy, consumer)
    except ValueError:
        pass
    else:
        raise AssertionError('changed Slack delivery policy was automatically resumed')
    service.store.close()

    # Restart adopts only a parked dead owner. It restores the same inbound ID
    # directly into the native adapter owner, with no new admission/lead claim.
    service = SlackProviderOwner(runner)
    service.pid, service.started = 21, 22
    saved = {**launch, 'run_id': 'restart-once', 'stopped': False,
             'checkpoint_generation': 0, 'delivery': {'mode': 'unstarted'}}
    service.store.reserve(service.scope, saved['run_id'], saved['run_id'], saved['run_id'],
        {'run_id': saved['run_id'], 'status': 'running'}, owner_pid=11, owner_started=12)
    assert service.store.park_provider_wait(service.scope, saved['run_id'], 0, saved,
        {'run_id': saved['run_id'], 'status': 'waiting_for_provider', 'recovery': due},
        owner_pid=11, owner_started=12)
    with patch('gateway.delivery_ledger._owner_alive', return_value=True):
        assert service.schedule() == 0
    with patch('gateway.delivery_ledger._owner_alive', return_value=False):
        assert service.schedule() == 1
        assert service.schedule() == 0
    restored_event = adapter._start_session_processing.call_args.args[0]
    assert restored_event.message_id == 'human-request'
    assert restored_event._hermes_pre_gateway_dispatch_done
    assert restored_event._hlt_provider_resume['run_id'] == 'restart-once'
    assert service.waiting_count() == 1
    assert service.stop('thread-key')
    stopped_control = {'provider_owner': restored_event._hlt_provider_resume}
    claim_count = runner._claim_active_session_slot.call_count
    assert not await SlackProviderOwner.wait_and_claim(service, ctx, stopped_control)
    assert runner._claim_active_session_slot.call_count == claim_count
    service.store.close()

    # An adapter spawn failure must release only the guard it just installed.
    service = SlackProviderOwner(runner)
    service.pid, service.started = 41, 42
    failed_spawn = {**saved, 'run_id': 'spawn-failed', 'stopped': False}
    service.store.reserve(service.scope, 'spawn-failed', 'spawn-failed', 'spawn-failed',
        {'run_id': 'spawn-failed', 'status': 'running'}, owner_pid=11, owner_started=12)
    assert service.store.park_provider_wait(service.scope, 'spawn-failed', 0, failed_spawn,
        {'run_id': 'spawn-failed', 'status': 'waiting_for_provider', 'recovery': due},
        owner_pid=11, owner_started=12)
    def failed_start(event, key, *, interrupt_event):
        adapter._active_sessions[key] = interrupt_event
        raise RuntimeError('native task spawn failed')
    def release_guard(key, *, guard):
        if adapter._active_sessions.get(key) is guard:
            adapter._active_sessions.pop(key)
    adapter._release_session_guard = MagicMock(side_effect=release_guard)
    with patch.object(adapter, '_start_session_processing', side_effect=failed_start), \
         patch('gateway.delivery_ledger._owner_alive', return_value=False):
        assert service.schedule() == 0
    assert 'thread-key' not in adapter._active_sessions
    assert 'thread-key' not in runner._session_sources
    runner._release_running_agent_state.assert_called_with('thread-key', run_generation=22)
    assert service.store.status_for_run(service.scope, 'spawn-failed')['status']['status'] == 'failed'
    assert service.store._conn.execute("SELECT provider_wait_json FROM run_idempotency WHERE run_id='spawn-failed'").fetchone()[0]
    service.store.close()

    # A crash after the parent settles but before FIFO dispatch is recoverable.
    # A crash AFTER process-local dispatch is explicitly ambiguous, not lost or replayed.
    service = SlackProviderOwner(runner)
    service.pid, service.started = 51, 52
    parent = {**launch, 'run_id': 'followup-parent', 'stopped': False}
    service.store.reserve(service.scope, 'followup-parent', 'followup-parent', 'followup-parent',
        {'run_id': 'followup-parent', 'status': 'waiting_for_provider'}, owner_pid=51, owner_started=52)
    service.live['thread-key'] = parent
    restart_followup = MessageEvent(text='Preserved after restart', message_type=MessageType.TEXT,
        source=source, message_id='restart-followup')
    assert service.queue_followup(restart_followup, 'thread-key')
    service.finish('thread-key', 'completed')
    service.store.close()
    service = SlackProviderOwner(runner)
    service.pid, service.started = 61, 62
    assert service.schedule() == 1
    assert service.schedule() == 0
    assert adapter._start_session_processing.call_args.args[0].message_id == 'restart-followup'
    service.store.close()
    with patch('gateway.delivery_ledger._owner_alive', return_value=False):
        service = SlackProviderOwner(runner)
    assert service.store._conn.execute("SELECT state FROM slack_wait_followups WHERE message_id='restart-followup'").fetchone()[0] == 'reconciliation_required'
    assert service.schedule() == 0
    service.store.close()

    # Failed serialization or DB parking preserves the original native evidence
    # and an addressable, unsealed stream; neither path finalizes or retries it.
    for failure in ('serialization', 'database', 'database_all_writes'):
        service = SlackProviderOwner(runner)
        service.pid, service.started = 31, 32
        failed_launch = {**launch, 'run_id': failure, 'stopped': False, 'checkpoint_generation': 0}
        service.store.reserve(service.scope, failure, failure, failure,
            {'run_id': failure, 'status': 'running'}, owner_pid=31, owner_started=32)
        service.live['thread-key'] = failed_launch
        failed_control = {'provider_owner': failed_launch}
        failed_consumer = SimpleNamespace(cfg=StreamConsumerConfig(),
            **{name: None for name in _CONSUMER_FIELDS})
        failed_consumer.flush_pending_sync = lambda timeout: True
        failed_consumer._metadata_for_send = lambda: {}
        failed_ctx = SimpleNamespace(**{**vars(ctx), 'stream_consumer_holder': [failed_consumer]})
        failed_key = ('workspace', 'channel', 'original-thread', failure)
        failed_stream = {'ts': failure + '-message', 'sent': 'Saved progress.',
            'session_key': 'thread-key', 'run_generation': 7, 'lock': asyncio.Lock()}
        if failure == 'serialization':
            failed_stream['unserializable'] = object()
        adapter._active_streams[failed_key] = failed_stream
        adapter._native_stream_key = lambda *a, **k: failed_key
        adapter._set_thread_status = AsyncMock()
        service.wait_and_claim = AsyncMock()
        with patch('hlt_slack_provider_wait.capture_tool_budget', return_value={'rounds': []}):
            if failure == 'database_all_writes':
                connection = service.store._conn
                class FailedWrites:
                    def execute(self, statement, *args):
                        if statement.lstrip().upper().startswith('UPDATE'):
                            raise OSError('SQLite writes unavailable')
                        return connection.execute(statement, *args)
                    def commit(self):
                        return connection.commit()
                with patch.object(service.store, 'park_provider_wait', side_effect=OSError('SQLite unavailable')), \
                     patch.object(service.store, '_conn', FailedWrites()):
                    assert not await service.park_and_wait(failed_ctx, result, failed_control)
            elif failure == 'database':
                with patch.object(service.store, 'park_provider_wait', side_effect=OSError('disk write refused')):
                    assert not await service.park_and_wait(failed_ctx, result, failed_control)
            else:
                assert not await service.park_and_wait(failed_ctx, result, failed_control)
        service.wait_and_claim.assert_not_awaited()
        assert failed_consumer._hlt_provider_parked
        assert not failed_stream.get('sealed')
        if failure == 'database_all_writes':
            assert service.store._conn.execute('SELECT provider_wait_json FROM run_idempotency WHERE run_id=?', (failure,)).fetchone()[0] is None
            assert service.owns_checkpoint('thread-key')
            service.store.close()
            service = SlackProviderOwner(runner)
            # The original generic active-turn marker can still exist. The
            # journal itself must prevent synthesizing a replacement request.
            assert service.owns_checkpoint('thread-key')
        assert service.store.status_for_run(service.scope, failure)['status']['status'] == 'failed'
        assert failure in service.retained and 'thread-key' not in service.live
        assert list(service.reconciliation_dir.glob('*.json'))
        assert 'reconciliation' in service.waiting_notice('channel', 'workspace', 'original-thread')
        assert service.confirm_retained_stop([(failed_key, failed_stream)])
        assert service.store.status_for_run(service.scope, failure)['status']['status'] == 'cancelled'
        service.store.close()
    (service.reconciliation_dir / 'corrupt.json').write_text('{incomplete')
    service = SlackProviderOwner(runner)
    assert service.owns_checkpoint('any-generic-recovery-session')
    service.store.close()


def main():
    root = Path(sys.argv[1])
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory(prefix='hlt-slack-provider-proof-') as temporary:
        with patch.dict(os.environ, {'HERMES_HOME': temporary, 'HLT_MANAGED_MODEL_ROUTE': '1'}), \
             patch('socket.socket.connect', side_effect=AssertionError('proof attempted network')):
            asyncio.run(exercise())
    print('Native Slack provider ownership assertions passed')


if __name__ == '__main__':
    main()
