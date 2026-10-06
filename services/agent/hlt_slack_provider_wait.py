"""Native Slack continuation ownership. Never submits another inbound request.

The adapter keeps delivery custody. This owner parks a settled native turn,
releases execution capacity, and claims that same checkpoint before resuming.
Uncertain effects/delivery remain private reconciliation work, never replay.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import time
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from hlt_provider_checkpoint import MAX_CHECKPOINT_BYTES

logger = logging.getLogger(__name__)

_EVENT_FIELDS = (
    'text', 'message_id', 'channel_prompt', 'channel_context', 'metadata',
    'reply_to_message_id', 'reply_to_text', 'reply_to_author_id',
    'reply_to_author_name', 'reply_to_is_own_message', 'media_urls', 'media_types',
    'media_text_inlined', 'ledger_message_id',
)
_CONSUMER_FIELDS = (
    '_turn_id', '_message_id', '_accumulated', '_stream_ledger', '_last_sent_text',
    '_transport_primed', '_use_native_streaming', '_native_stream_opened',
    '_native_last_pushed_len', '_already_sent', '_delivered_commentary_texts',
    '_delivered_segment_texts', '_fallback_prefix', '_fallback_final_send',
    '_draft_id', '_use_draft_streaming', '_draft_failures',
    '_in_think_block', '_think_buffer', '_before_finalize_notified',
    '_tool_progress_lines', '_tool_progress_active', '_egress_declined',
    '_boundary_placeholder', '_boundary_reason', '_boundary_reopen',
    '_awaiting_reopen_after_boundary', '_reopen_seeded_eagerly',
    '_current_edit_interval', '_flood_strikes', 'stream_deltas_enabled',
)


def enabled(source):
    return (os.getenv('HLT_MANAGED_MODEL_ROUTE', '').lower() in {'1', 'true', 'yes', 'on'}
            and getattr(source.platform, 'value', '') == 'slack')


def owner(runner):
    current = getattr(runner, '_hlt_slack_provider_owner', None)
    if current is None:
        current = runner._hlt_slack_provider_owner = SlackProviderOwner(runner)
    return current


def _json(value):
    encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    if len(encoded.encode()) > MAX_CHECKPOINT_BYTES:
        raise ValueError('Slack continuation exceeds its private storage bound')
    return json.loads(encoded)


def _plugin():
    modules = [module for module in list(sys.modules.values())
               if getattr(module, 'RECEIPT_SCHEMA', None) == 'slack_agent_lead_decision.v1'
               and hasattr(module, '_TOOL_BUDGETS') and hasattr(module, '_lead_ledger')]
    if len(modules) != 1:
        raise ValueError('Slack policy owner is unavailable')
    return modules[0]


def verify_coordination(launch):
    plugin = _plugin()
    saved = launch['coordination']
    current = plugin.claim_coordination(os.getenv('KATAILYST2_MCP_URL', '').strip(),
        os.getenv('KATAILYST2_MCP_TOKEN', '').strip(),
        {'coordinationId': saved['coordinationId'], 'callerAgentRef': launch['caller_agent_ref']},
        operation='get')
    if any(current.get(k) != saved.get(k) for k in saved) or not current.get('mayRespond'):
        raise ValueError('Slack lead decision changed; reconciliation required')


def capture_tool_budget(turn_id):
    plugin = _plugin()
    with plugin._TOOL_BUDGET_LOCK:
        state = plugin._TOOL_BUDGETS.get(turn_id)
        if state is None:
            raise ValueError('Slack tool budget is unavailable')
        return {'rounds': sorted(state['rounds']), 'blocked_rounds': sorted(state['blocked_rounds']),
                'source_digest': hashlib.sha256(Path(plugin.__file__).read_bytes()).hexdigest(),
                'limit': plugin.SLACK_TOOL_ROUND_LIMIT}


def restore_tool_budget(turn_id, saved):
    plugin = _plugin()
    if (saved['source_digest'] != hashlib.sha256(Path(plugin.__file__).read_bytes()).hexdigest()
            or saved['limit'] != plugin.SLACK_TOOL_ROUND_LIMIT):
        raise ValueError('Slack tool policy changed; reconciliation required')
    with plugin._TOOL_BUDGET_LOCK:
        plugin._TOOL_BUDGETS[turn_id] = {'started_at': time.monotonic(),
            'rounds': set(saved['rounds']), 'blocked_rounds': set(saved['blocked_rounds'])}


class SlackProviderOwner:
    def __init__(self, runner):
        from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore
        from hermes_constants import get_hermes_home
        from gateway.delivery_ledger import _owner_stamp
        self.runner = runner
        self.store = RunIdempotencyStore(str(get_hermes_home() / 'slack-provider-wait.db'))
        self.pid, self.started = _owner_stamp()
        self.scope = 'hlt-native-slack-v1'
        self.live = {}
        self.retained = {}
        self.invalid_reconciliation_journal = False
        self.reconciliation_dir = get_hermes_home() / 'slack-provider-reconciliation'
        with self.store._lock:
            self.store._conn.execute('''CREATE TABLE IF NOT EXISTS slack_wait_followups(
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_key TEXT NOT NULL,
                message_id TEXT NOT NULL, event_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', UNIQUE(session_key,message_id))''')
            columns = {row[1] for row in self.store._conn.execute('PRAGMA table_info(slack_wait_followups)')}
            for name, declaration in {'parent_run_id': 'TEXT', 'owner_pid': 'INTEGER',
                                      'owner_started': 'INTEGER', 'admitted_run_id': 'TEXT'}.items():
                if name not in columns:
                    self.store._conn.execute(f'ALTER TABLE slack_wait_followups ADD COLUMN {name} {declaration}')
            self.store._conn.commit()
        from gateway.delivery_ledger import _owner_alive
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT run_id,owner_pid,owner_started,status_json FROM run_idempotency WHERE scope=? AND provider_wait_state='claimed'",
                (self.scope,)).fetchall()
        for run_id, pid, started, encoded in rows:
            if not _owner_alive(pid, started):
                status = json.loads(encoded)
                status.update(status='interrupted', error='Native continuation owner exited; reconciliation required.')
                self.store.update_status(run_id, status)
        with self.store._lock:
            for identifier, pid, started in self.store._conn.execute(
                    "SELECT id,owner_pid,owner_started FROM slack_wait_followups WHERE state='claimed'").fetchall():
                if not _owner_alive(pid or 0, started or 0):
                    self.store._conn.execute("UPDATE slack_wait_followups SET state='reconciliation_required' WHERE id=?", (identifier,))
            self.store._conn.commit()
            rows = self.store._conn.execute(
                "SELECT provider_wait_json,status_json FROM run_idempotency WHERE scope=? AND provider_wait_state='terminal' AND provider_wait_json IS NOT NULL",
                (self.scope,)).fetchall()
        for checkpoint, status in rows:
            try:
                if json.loads(status).get('status') in {'failed', 'interrupted'}:
                    saved = json.loads(checkpoint)
                    self.retained[saved['run_id']] = saved
            except (ValueError, KeyError, TypeError):
                logger.error('Invalid private Slack reconciliation record')
        for path in self.reconciliation_dir.glob('*.json'):
            try:
                saved = json.loads(path.read_text())
                record = self.store.status_for_run(self.scope, saved['run_id'])
                if record is None or record['status'].get('status') not in {'completed', 'cancelled'}:
                    self.retained[saved['run_id']] = saved
                    if record and record['status'].get('status') not in {'failed', 'interrupted'}:
                        self.store.update_status(saved['run_id'], {'run_id': saved['run_id'],
                            'status': 'failed', 'error': 'Private Slack recovery requires reconciliation.'})
            except (ValueError, KeyError, TypeError, OSError):
                self.invalid_reconciliation_journal = True
                logger.error('Invalid private Slack reconciliation journal')

    def begin(self, event, prepared, session_key, generation, control):
        if not enabled(event.source) or control is None or event.internal or not event.message_id or not self.started:
            return None
        # A restored event already owns a private continuation. It cannot reserve anew.
        resume = getattr(event, '_hlt_provider_resume', None)
        if resume is not None:
            control['provider_owner'] = resume
            self.live[session_key] = resume
            return resume
        source = event.source.to_dict()
        plugin = _plugin()
        with closing(plugin._lead_ledger()._connect()) as connection:
            row = connection.execute('SELECT receipt_json FROM slack_agent_lead_tombstones WHERE workspace_id=? AND channel_id=? AND message_ts=?',
                (source.get('scope_id'), source['chat_id'], str(event.message_id))).fetchone()
        receipt = json.loads(row[0]) if row else {}
        coordination = receipt.get('coordination')
        if not coordination or not coordination.get('mayRespond'):
            raise ValueError('Slack turn has no durable lead decision')
        identity = [source, str(event.message_id), prepared.persistence_session_id]
        run_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        launch = _json({
            'schema': 'hlt_slack_provider_wait.v1', 'run_id': run_id,
            'source': source, 'event': {key: getattr(event, key, None) for key in _EVENT_FIELDS},
            'event_timestamp': event.timestamp.isoformat() if event.timestamp else None,
            'message_type': event.message_type.value,
            'prepared': dataclasses.asdict(prepared), 'session_key': session_key,
            'session_id': prepared.persistence_session_id, 'generation': generation,
            'active_seconds': 0.0, 'checkpoint_generation': 0,
            'coordination': coordination, 'caller_agent_ref': receipt['localAgentRef'],
        })
        outcome, _ = self.store.reserve(
            self.scope, run_id, run_id, run_id,
            {'run_id': run_id, 'status': 'running', 'session_key': session_key},
            owner_pid=self.pid, owner_started=self.started or 0,
        )
        if outcome != 'created':
            raise ValueError('Slack request already has a native owner; reconciliation required')
        self.live[session_key] = launch
        control['provider_owner'] = launch
        self.acknowledge_followup(event, session_key, run_id)
        return launch

    def acknowledge_followup(self, event, session_key, run_id):
        identifier = (event.metadata or {}).get('_hlt_durable_followup_id')
        if identifier is None:
            return
        with self.store._lock:
            self.store._conn.execute("""UPDATE slack_wait_followups SET state='admitted',admitted_run_id=?
                WHERE id=? AND session_key=? AND message_id=? AND state='claimed'
                AND owner_pid=? AND owner_started=?""",
                (run_id, identifier, session_key, str(event.message_id), self.pid, self.started))
            self.store._conn.commit()

    def waiting_count(self):
        return sum(bool(launch.get('waiting')) for launch in self.live.values())

    def stop(self, session_key):
        launch = self.live.get(session_key)
        if launch is None:
            matches = [row for row in self.store.provider_waits(self.scope)
                       if row['checkpoint'].get('session_key') == session_key]
            if len(matches) == 1:
                launch = matches[0]['checkpoint']
            else:
                retained = [saved for saved in self.retained.values() if saved.get('session_key') == session_key]
                if len(retained) != 1:
                    return False
                launch = retained[0]
        self.store.stop_provider_wait(self.scope, launch['run_id'])
        # Also fence a native worker stopped before its first provider wait.
        with self.store._lock:
            self.store._conn.execute('UPDATE run_idempotency SET provider_wait_stop=1 WHERE scope=? AND run_id=?',
                (self.scope, launch['run_id']))
            self.store._conn.commit()
        launch['stopped'] = True
        # Reconciliation already proved the worker exited. Keep its delivery
        # locator until Slack confirms Stop, while preventing any retry now.
        if launch['run_id'] in self.retained:
            self.store.update_status(launch['run_id'], {'run_id': launch['run_id'],
                'status': 'interrupted', 'cancellationRequested': True,
                'error': 'Stopped; delivery reconciliation remains available.'})
        return True

    def finish(self, session_key, status):
        launch = self.live.pop(session_key, None)
        if launch is not None:
            launch['waiting'] = False
            if launch.get('stopped'):
                status = 'cancelled'
            elif status == 'completed':
                status = launch.get('native_status', status)
            if status in {'failed', 'interrupted'} and launch.get('turn'):
                self.retained[launch['run_id']] = launch
                from gateway.session import SessionSource
                adapter = self.runner._delivery_adapter_for(SessionSource.from_dict(launch['source']))
                key = tuple(launch.get('delivery', {}).get('key', ()))
                if adapter is not None:
                    stream = adapter._active_streams.get(key) or adapter._uncertain_stream_starts.get(key)
                    if stream is not None:
                        stream['_hlt_provider_parked'] = True
                        stream['_hlt_reconciliation_run_id'] = launch['run_id']
            self.store.update_status(launch['run_id'], {
                'run_id': launch['run_id'], 'session_key': session_key,
                'status': status, 'updated_at': time.time(),
            })

    def finish_delivery(self, session_key, adapter, processing_ok):
        launch = self.live.get(session_key)
        delivery = launch.get('delivery') if launch else None
        if delivery and delivery.get('mode') == 'native':
            key = tuple(delivery['key'])
            # Slack deliberately suppresses duplicate sends after ambiguous ACKs;
            # that suppression is not a confirmed delivery receipt.
            processing_ok = processing_ok and key in adapter._finalized_streams
        self.finish(session_key, 'completed' if processing_ok else 'failed')

    def owns_checkpoint(self, session_key):
        # Generic restart recovery must never turn this native checkpoint into
        # a synthesized user message, including failed/claimed reconciliation.
        if self.invalid_reconciliation_journal or any(
                saved.get('session_key') == session_key for saved in self.retained.values()):
            return True
        with self.store._lock:
            rows = self.store._conn.execute(
                'SELECT provider_wait_json FROM run_idempotency WHERE scope=? AND provider_wait_json IS NOT NULL',
                (self.scope,)).fetchall()
        for row in rows:
            try:
                if json.loads(row[0]).get('session_key') == session_key:
                    return True
            except (ValueError, TypeError, AttributeError):
                # Corrupt private custody is not permission to synthesize work.
                logger.error('Corrupt Slack checkpoint; generic recovery held for reconciliation')
                return True
        return False

    def waiting_notice(self, chat_id, team_id, thread_id):
        for launch in [*self.live.values(), *self.retained.values()]:
            source = launch['source']
            if (str(source.get('chat_id')) != str(chat_id)
                    or str(source.get('scope_id') or source.get('guild_id') or '') != str(team_id)
                    or str(source.get('thread_id') or '') != str(thread_id)):
                continue
            record = self.store.status_for_run(self.scope, launch['run_id'])
            if record and record['status'].get('status') == 'waiting_for_provider':
                return 'Waiting for model capacity. Your work is saved, and I will continue automatically.'
            if launch['run_id'] in self.retained:
                return 'Work is saved, but recovery needs reconciliation before it can continue.'
            # A newer running request owns the visible status for this thread.
            return None
        return None

    def _journal_reconciliation(self, checkpoint):
        """A failed SQLite park must not discard the settled native checkpoint."""
        self.reconciliation_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = hashlib.sha256(checkpoint['run_id'].encode()).hexdigest() + '.json'
        with tempfile.NamedTemporaryFile(mode='w', dir=self.reconciliation_dir, delete=False) as handle:
            json.dump(_json(checkpoint), handle, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, self.reconciliation_dir / name)

    def _retain_delivery_locator(self, ctx, launch):
        consumer = ctx.stream_consumer_holder[0]
        adapter = self.runner._delivery_adapter_for(ctx.source)
        if consumer is None or adapter is None:
            return {'mode': 'uncertain'}
        consumer._hlt_provider_parked = True
        key = adapter._native_stream_key(ctx.source.chat_id, reply_to=ctx.event_message_id,
                                         metadata=consumer._metadata_for_send())
        stream = adapter._active_streams.get(key) or adapter._uncertain_stream_starts.get(key)
        if key is None or stream is None:
            return {'mode': 'uncertain'}
        stream['_hlt_provider_parked'] = True
        stream['_hlt_reconciliation_run_id'] = launch['run_id']
        return {'mode': 'reconciliation', 'key': list(key), 'stream': {
            name: stream.get(name) for name in ('ts', 'sent', 'session_key', 'run_generation',
                'append_uncertain', 'finalization_uncertain', 'sealed', 'stopped', 'profile')}}

    async def reconcile(self, ctx, result, control):
        launch = control['provider_owner']
        try:
            delivery = self._retain_delivery_locator(ctx, launch)
        except Exception:
            delivery = {'mode': 'uncertain'}
        launch.update(turn=result['checkpoint'], waiting=False, delivery=delivery)
        self.retained[launch['run_id']] = launch
        # This journal is never an execution source, even if native SQLite is
        # unavailable. Stop and operators can address its original delivery ID.
        self._journal_reconciliation(launch)
        try:
            with self.store._lock:
                self.store._conn.execute("""UPDATE run_idempotency SET provider_wait_json=?,provider_wait_state='terminal'
                    WHERE scope=? AND run_id=? AND owner_pid=? AND owner_started=? AND provider_wait_stop=0""",
                    (json.dumps(_json(launch)), self.scope, launch['run_id'], self.pid, self.started))
                self.store._conn.commit()
            self.finish(ctx.session_key, 'failed')
        except Exception:
            self.live.pop(ctx.session_key, None)
            logger.exception('Slack SQLite unavailable; private reconciliation journal retained')
        control['provider_waiting'] = False
        control['provider_reconciliation'] = True
        adapter = self.runner._delivery_adapter_for(ctx.source)
        if hasattr(adapter, '_set_thread_status'):
            try:
                await adapter._set_thread_status(ctx.source.chat_id, ctx.source.scope_id or '',
                    ctx.source.thread_id or ctx.event_message_id,
                    'Work is saved; recovery needs reconciliation before it can continue.',
                    'reconciliation status unavailable')
            except Exception:
                logger.warning('Could not present Slack reconciliation status')

    def confirm_retained_stop(self, candidates):
        """Slack already stopped these exact streams; never target a newer run."""
        if not candidates:
            return False
        launches = []
        for key, stream in candidates:
            run_id = stream.get('_hlt_reconciliation_run_id')
            launch = self.retained.get(run_id)
            if launch is None or tuple(launch.get('delivery', {}).get('key', ())) != key:
                return False
            launches.append(launch)
        for launch in launches:
            with self.store._lock:
                self.store._conn.execute('UPDATE run_idempotency SET provider_wait_stop=1 WHERE scope=? AND run_id=?',
                    (self.scope, launch['run_id']))
                self.store._conn.commit()
            self.store.update_status(launch['run_id'], {'run_id': launch['run_id'], 'status': 'cancelled'})
            self.retained.pop(launch['run_id'], None)
        return True

    def queue_followup(self, event, session_key):
        launch = self.live.get(session_key)
        if (launch is None or event.internal or event.get_command() or not event.message_id
                or getattr(event.source, 'is_bot', False)):
            return False
        record = self.store.status_for_run(self.scope, launch['run_id'])
        if record is None or record['status'].get('status') != 'waiting_for_provider':
            return False
        value = {'source': event.source.to_dict(), 'message_type': event.message_type.value,
                 'event': {k: getattr(event, k, None) for k in _EVENT_FIELDS},
                 'timestamp': event.timestamp.isoformat() if event.timestamp else None}
        with self.store._immediate_txn():
            self.store._conn.execute('INSERT OR IGNORE INTO slack_wait_followups(session_key,message_id,event_json,parent_run_id) VALUES(?,?,?,?)',
                (session_key, str(event.message_id), json.dumps(_json(value)), launch['run_id']))
            self.store._conn.commit()
        return True

    def _claim_followup(self, session_key):
        # Only definitely unadmitted work whose parent has settled may advance.
        # A claimed row remains durable until begin() acknowledges native admission.
        with self.store._immediate_txn():
            blocked = self.store._conn.execute("SELECT 1 FROM slack_wait_followups WHERE session_key=? AND state IN ('claimed','reconciliation_required') LIMIT 1",
                (session_key,)).fetchone()
            row = None if blocked else self.store._conn.execute("""SELECT f.id,f.event_json,r.status_json
                FROM slack_wait_followups f LEFT JOIN run_idempotency r ON r.scope=? AND r.run_id=f.parent_run_id
                WHERE f.session_key=? AND f.state='queued' ORDER BY f.id LIMIT 1""",
                (self.scope, session_key)).fetchone()
            if row is None or not row[2] or json.loads(row[2]).get('status') not in {'completed', 'cancelled'}:
                self.store._conn.commit()
                return None
            self.store._conn.execute("UPDATE slack_wait_followups SET state='claimed',owner_pid=?,owner_started=? WHERE id=? AND state='queued'",
                (self.pid, self.started, row[0]))
            self.store._conn.commit()
        from gateway.platforms.base import MessageEvent, MessageType
        from gateway.session import SessionSource
        value = json.loads(row[1])
        fields = {k: v for k, v in value['event'].items() if k != 'ledger_message_id'}
        fields['metadata'] = {**(fields.get('metadata') or {}), '_hlt_durable_followup_id': row[0]}
        event = MessageEvent(source=SessionSource.from_dict(value['source']),
            message_type=MessageType(value['message_type']), **fields)
        event._hermes_pre_gateway_dispatch_done = True
        if value.get('timestamp'):
            event.timestamp = datetime.fromisoformat(value['timestamp'])
        return event

    async def drain_followups(self, session_key, adapter):
        event = self._claim_followup(session_key)
        if event is not None:
            self.runner._enqueue_fifo(session_key, event, adapter)
            # Never call process-local enqueue an admission acknowledgement.
            # A crash now is retained as reconciliation_required at next boot.

    def schedule_followups(self, platform=None):
        count = 0
        with self.store._lock:
            keys = [row[0] for row in self.store._conn.execute(
                "SELECT DISTINCT session_key FROM slack_wait_followups WHERE state='queued'").fetchall()]
        for key in keys:
            if self.runner._is_session_running(key):
                continue
            with self.store._lock:
                row = self.store._conn.execute("SELECT event_json FROM slack_wait_followups WHERE session_key=? AND state='queued' ORDER BY id LIMIT 1", (key,)).fetchone()
            from gateway.session import SessionSource
            source = SessionSource.from_dict(json.loads(row[0])['source'])
            adapter = self.runner._delivery_adapter_for(source)
            if (adapter is None or (platform is not None and source.platform != platform)
                    or key in getattr(adapter, '_active_sessions', {})
                    or not self.runner._is_user_authorized_for_source(source)):
                continue
            event = self._claim_followup(key)
            if event is not None:
                try:
                    if adapter._start_session_processing(event, key) is not True:
                        raise ValueError('Native follow-up dispatch was not confirmed')
                    count += 1
                except Exception:
                    # Dispatch may have installed a task before failing. Do not
                    # replay its claim; startup identifies this durable ambiguity.
                    logger.exception('Native follow-up dispatch requires reconciliation')
        return count

    async def delivery_checkpoint(self, ctx):
        consumer = ctx.stream_consumer_holder[0]
        if consumer is None:
            return {'mode': 'unstarted'}
        adapter = self.runner._delivery_adapter_for(ctx.source)
        if any(getattr(consumer, name, False) for name in (
                '_delivery_ambiguous', '_final_response_sent', '_turn_split_delivery',
                '_fallback_final_send', '_egress_declined')):
            raise ValueError('Slack consumer custody requires reconciliation')
        # Finish draining already-authored frames before taking custody. Never
        # cancel an in-flight transport call to manufacture a settled checkpoint.
        if not await asyncio.to_thread(consumer.flush_pending_sync, 2.0):
            raise ValueError('Slack stream has unsettled frames')
        key = adapter._native_stream_key(ctx.source.chat_id,
            reply_to=ctx.event_message_id, metadata=consumer._metadata_for_send())
        if key in adapter._uncertain_stream_starts:
            raise ValueError('Slack stream start acknowledgement is unknown')
        stream = adapter._active_streams.get(key)
        if stream is None:
            # Edit-based previews and split final sends have different owners.
            # Their state must be reconciled before any automatic replay.
            raise ValueError('Slack stream custody requires reconciliation')
        async with stream['lock']:
            if any(stream.get(k) for k in ('append_uncertain', 'finalization_uncertain', 'sealed', 'stopped')):
                raise ValueError('Slack stream delivery is uncertain or terminal')
            stream['_hlt_provider_parked'] = True
            consumer._hlt_provider_parked = True
            return _json({
                'mode': 'native', 'key': list(key), 'config': dataclasses.asdict(consumer.cfg),
                'stream': {k: v for k, v in stream.items() if k != 'lock'},
                'consumer': {k: getattr(consumer, k) for k in _CONSUMER_FIELDS},
            })

    async def park_and_wait(self, ctx, result, control):
        launch = control['provider_owner']
        try:
            delivery = await self.delivery_checkpoint(ctx)
            checkpoint = _json({**launch, 'turn': result['checkpoint'], 'delivery': delivery,
                'tool_budget': capture_tool_budget(result['checkpoint']['loop']['turn_id'])})
            checkpoint.pop('claimed', None)
            status = {'run_id': launch['run_id'], 'status': 'waiting_for_provider',
                      'session_key': ctx.session_key, 'recovery': result['recovery']}
            parked = self.store.park_provider_wait(
                self.scope, launch['run_id'], launch['checkpoint_generation'], checkpoint, status,
                owner_pid=self.pid, owner_started=self.started or 0,
            )
            if not parked:
                raise ValueError('Slack continuation lease was superseded')
            launch.update(checkpoint)
            launch['checkpoint_generation'] += 1
            event = self.runner._session_state(ctx.session_key).turn.event
            await self.runner._clear_durable_active_turn(event)
            await self.runner.async_session_store.clear_resume_pending(ctx.session_key)
            # Retain the session sentinel/adapter guard so human follow-ups queue;
            # release only global execution capacity while no worker is alive.
            state = self.runner._peek_session_state(ctx.session_key)
            if state and state.turn.lease is not None:
                state.turn.lease.release()
                state.turn.lease = None
            control['provider_waiting'] = True
            launch['waiting'] = True
            adapter = self.runner._delivery_adapter_for(ctx.source)
            if hasattr(adapter, '_set_thread_status'):
                await adapter._set_thread_status(ctx.source.chat_id, ctx.source.scope_id or '',
                    ctx.source.thread_id or ctx.event_message_id, self.waiting_notice(
                        ctx.source.chat_id, ctx.source.scope_id or '', ctx.source.thread_id or ctx.event_message_id) or
                    'Waiting for model capacity; your work is saved.', 'provider wait unavailable')
            return await self.wait_and_claim(ctx, control)
        except asyncio.CancelledError:
            # Shutdown preserves the parked checkpoint. Explicit Stop writes its
            # tombstone first; neither path submits a replacement request.
            raise
        except Exception:
            # The native worker has already exited. Keep its exact transcript
            # and delivery locator, but never turn failed parking into a send.
            try:
                await self.reconcile(ctx, result, control)
            except Exception:
                logger.exception('Slack reconciliation persistence failed; original custody retained in memory')
                control['provider_waiting'] = False
                control['provider_reconciliation'] = True
            return False

    async def wait_and_claim(self, ctx, control):
        launch = control['provider_owner']
        while True:
            if (launch.get('stopped') or control.get('stop_requested')
                    or self.store.provider_wait_stopped(self.scope, launch['run_id'])
                    or not self.runner._is_session_run_current(ctx.session_key, ctx.run_generation)):
                return False
            current = self.store.status_for_run(self.scope, launch['run_id'])
            status = current['status'] if current else {}
            if status.get('status') != 'waiting_for_provider':
                return False
            due = status['recovery']['retryAt'] / 1000
            if time.time() < due:
                await asyncio.sleep(min(1.0, due - time.time()))
                continue
            if not self.runner._is_user_authorized_for_source(ctx.source):
                raise ValueError('Slack owner authorization changed')
            lease, limit = self.runner._claim_active_session_slot(ctx.session_key, ctx.source, continuation=True)
            if limit is not None or lease is None:
                await asyncio.sleep(1.0)
                continue
            try:
                await asyncio.to_thread(verify_coordination, launch)
            except BaseException:
                lease.release()
                raise
            # Stop may arrive during the authority read; its durable CAS fence
            # wins before any new native worker receives this checkpoint.
            claimed = self.store.claim_provider_wait(self.scope, launch['run_id'],
                launch['checkpoint_generation'], owner_pid=self.pid,
                owner_started=self.started or 0, now=time.time())
            if not claimed:
                if lease is not None:
                    lease.release()
                return False
            self.runner._session_state(ctx.session_key).turn.lease = lease
            control['provider_waiting'] = False
            launch['waiting'] = False
            launch['resumed'] = True
            consumer = ctx.stream_consumer_holder[0]
            if consumer is not None:
                consumer._hlt_provider_parked = False
                adapter = self.runner._delivery_adapter_for(ctx.source)
                for stream in adapter._active_streams.values():
                    if stream.get('session_key') == ctx.session_key:
                        stream.pop('_hlt_provider_parked', None)
            ctx._hlt_resume_turn = launch['turn']
            ctx._hlt_active_seconds = launch['active_seconds']
            event = self.runner._session_state(ctx.session_key).turn.event
            await self.runner._mark_durable_active_turn(event, ctx.session_key)
            return True

    def restore_delivery(self, adapter, launch, consumer):
        delivery = launch['delivery']
        if delivery['mode'] == 'unstarted':
            return
        if delivery.get('config') != dataclasses.asdict(consumer.cfg):
            raise ValueError('Slack delivery policy changed; reconciliation required')
        key = tuple(delivery['key'])
        if key in adapter._active_streams or key in adapter._uncertain_stream_starts:
            raise ValueError('Slack stream already has a live owner')
        stream = delivery['stream']
        if any(stream.get(k) for k in ('append_uncertain', 'finalization_uncertain', 'sealed', 'stopped')):
            raise ValueError('Slack delivery requires reconciliation')
        adapter._active_streams[key] = {**stream, 'lock': asyncio.Lock(),
            'run_generation': launch['generation'], '_hlt_provider_parked': True}
        for name, value in delivery['consumer'].items():
            if name in _CONSUMER_FIELDS:
                setattr(consumer, name, value)
        adapter._bot_message_ts.add(str(stream['ts']))
        consumer._hlt_provider_parked = True

    def restore_reconciliation_custody(self, platform=None):
        from gateway.session import SessionSource
        for launch in self.retained.values():
            try:
                source = SessionSource.from_dict(launch['source'])
                if platform is not None and source.platform != platform:
                    continue
                adapter = self.runner._delivery_adapter_for(source)
                delivery = launch.get('delivery') or {}
                if adapter is None or not delivery.get('key') or not delivery.get('stream'):
                    continue
                key = tuple(delivery['key'])
                if key in adapter._active_streams or key in adapter._uncertain_stream_starts:
                    continue
                stream = {**delivery['stream'], 'lock': asyncio.Lock(), '_hlt_provider_parked': True,
                          '_hlt_reconciliation_run_id': launch['run_id']}
                target = adapter._active_streams if stream.get('ts') else adapter._uncertain_stream_starts
                target[key] = stream
                if stream.get('ts'):
                    adapter._bot_message_ts.add(str(stream['ts']))
            except Exception:
                logger.exception('Slack reconciliation delivery locator is unavailable')

    def schedule(self, platform=None):
        """Reserve native session guards before normal startup intake is released."""
        from gateway.platforms.base import MessageEvent, MessageType
        from gateway.session import SessionSource
        from gateway.run import _AGENT_PENDING_SENTINEL
        count = 0
        self.restore_reconciliation_custody(platform)
        if not self.started:
            return count
        for saved in self.store.provider_waits(self.scope):
            adopted = False
            generation, adapter, guard = None, None, None
            try:
                launch = saved['checkpoint']
                key = launch['session_key']
                if key in self.live or self.runner._is_session_running(key):
                    continue
                source = SessionSource.from_dict(launch['source'])
                if platform is not None and source.platform != platform:
                    continue
                adapter = self.runner._delivery_adapter_for(source)
                if adapter is None:
                    continue
                if not self.store.adopt_provider_wait(self.scope, launch['run_id'], saved['generation'],
                        owner_pid=self.pid, owner_started=self.started or 0):
                    continue
                adopted = True
                if not enabled(source) or not self.runner._is_user_authorized_for_source(source):
                    raise ValueError('Slack continuation authorization changed')
                entry = self.runner.session_store._entries.get(key)
                if entry is None or entry.session_id != launch['session_id']:
                    raise ValueError('Slack continuation session changed')
                # New process-local fencing token, same durable native turn/task IDs.
                generation = self.runner._begin_session_run_generation(key)
                launch['generation'] = generation
                launch['waiting'] = True
                launch['checkpoint_generation'] = saved['generation']
                event = MessageEvent(source=source, message_type=MessageType(launch['message_type']),
                    **{k: v for k, v in launch['event'].items() if k != 'ledger_message_id'})
                event.ledger_message_id = launch['event'].get('ledger_message_id')
                if launch.get('event_timestamp'):
                    event.timestamp = datetime.fromisoformat(launch['event_timestamp'])
                event._hlt_provider_resume = launch
                event._hermes_pre_gateway_dispatch_done = True
                self.live[key] = launch
                state = self.runner._session_state(key)
                state.turn.agent, state.turn.event = _AGENT_PENDING_SENTINEL, event
                state.turn.started_ts = time.time()
                self.runner._session_sources[key] = source
                guard = asyncio.Event()
                if adapter._start_session_processing(event, key, interrupt_event=guard) is not True:
                    raise ValueError('Native continuation dispatch was not confirmed')
                count += 1
            except Exception:
                logger.exception('Slack continuation restart requires reconciliation')
                # Preserve the private checkpoint, while closing automatic claims.
                if adopted:
                    self.live.pop(key, None)
                    self.retained[saved['run_id']] = launch
                    self.store.update_status(saved['run_id'], {'run_id': saved['run_id'],
                        'status': 'failed', 'error': 'Slack continuation requires reconciliation.'})
                    if generation is not None:
                        # This synchronous dispatch has not yielded to its task.
                        # Clean only our exact guard and generation, never a successor.
                        if guard is not None and adapter._active_sessions.get(key) is guard:
                            task = adapter._session_tasks.get(key)
                            if task is not None:
                                task.cancel()
                                adapter._session_tasks.pop(key, None)
                            adapter._release_session_guard(key, guard=guard)
                        self.runner._release_running_agent_state(key, run_generation=generation)
                        self.runner._release_turn_lease(key, generation)
                        if self.runner._session_sources.get(key) is source:
                            self.runner._session_sources.pop(key, None)
        return count + self.schedule_followups(platform)


async def resume_dispatch(runner, event):
    """Called only by the adapter for a private SQLite-restored event."""
    from gateway.session import build_session_context
    launch = event._hlt_provider_resume
    key, generation = launch['session_key'], launch['generation']
    service = owner(runner)
    if service.live.get(key) is not launch:
        raise ValueError('Slack continuation has no native owner')
    entry = runner.session_store._entries.get(key)
    if entry is None or entry.session_id != launch['session_id']:
        raise ValueError('Slack session changed while parked')
    control = runner._register_managed_turn_control(key, generation)
    control['provider_owner'] = launch
    control['provider_waiting'] = True
    # Construct the existing native consumer, but restore its acknowledged
    # transport BEFORE prime() can send a new response.
    control['provider_restore_delivery'] = launch
    try:
        return await runner._handle_message_with_agent(event, event.source, key, generation)
    finally:
        runner._release_running_agent_state(key, run_generation=generation)
        runner._release_turn_lease(key, generation)


def prepare_agent(ctx, agent, control):
    # Cached native agents may serve later internal turns. Checkpoint permission
    # belongs to this exact admitted owner, never to the reusable agent object.
    agent._hlt_provider_wait_enabled = bool(control and control.get('provider_owner'))
    if not agent._hlt_provider_wait_enabled:
        return
    agent._hlt_active_seconds = control['provider_owner']['active_seconds']
    if getattr(ctx, '_hlt_resume_turn', None) is not None:
        restore_tool_budget(ctx._hlt_resume_turn['loop']['turn_id'], control['provider_owner']['tool_budget'])
        agent._hlt_resume_turn = ctx._hlt_resume_turn
