"""Native Slack admission consumes one shared Katailyst2 decision."""
from copy import deepcopy
import sys
import pytest
from tests.test_slack_agent_lead import _load_plugin, _raw, _event


@pytest.fixture
def coordination():
    plugin = _load_plugin(stub_coordination=False)
    return sys.modules[plugin.__name__ + '.coordination']


def arguments():
    return {'callerAgentRef': 'agent:cleo', 'mission': '<@UCLEO> and <@UVIC> research staffing',
            'participantSlackUserIds': ['UCLEO', 'UVIC'], 'source': {
                'platform': 'slack', 'teamId': 'T', 'channelId': 'C', 'threadTs': '100.1',
                'messageTs': '100.2', 'humanUserId': 'UHUMAN', 'actorKind': 'human'}}


def decision(args, role='contributor'):
    return {'schemaVersion': 'agent_coordination.v1', 'coordinationId': 'coordination-one',
            'revision': 1, 'source': deepcopy(args['source']), 'leadAgentRef': 'agent:victoria',
            'callerRole': role, 'mayRespond': role != 'observer', 'mayComplete': role == 'lead'}


def wire(monkeypatch, module, names, outcomes, *, repo='katailyst2'):
    calls = []
    def post(url, token, request, *, session_id, timeout):
        assert 0 < timeout <= module.COORDINATION_TIMEOUT_SECONDS
        calls.append(deepcopy(request))
        method = request['method']
        if method == 'initialize':
            return {'result': {}}, 'session', {'x-katailyst-repo': repo}
        assert session_id == 'session'
        if method == 'tools/list':
            return {'result': {'tools': [{'name': n} for n in names]}}, 'session', {}
        return {'result': outcomes.pop(0)}, 'session', {}
    monkeypatch.setattr(module, '_post', post)
    return calls


@pytest.mark.parametrize('name', ['agents.coordination.claim', 'agents_coordination_claim', 'tool.execute', 'tool_execute'])
def test_exact_mcp_surface_and_wrapped_output(coordination, monkeypatch, name):
    args = arguments()
    result = decision(args)
    direct = name.startswith('agents')
    payload = result if direct else {'status': 'ok', 'state': 'ready', 'output': result,
                                    'outcome': {'kind': 'succeeded', 'output': result}}
    calls = wire(monkeypatch, coordination, [name], [{'structuredContent': payload}])
    assert coordination.claim_coordination('https://k2.invalid/mcp', 'test-token', args) == result
    expected = args if direct else {'verb': 'agents.coordination.claim', 'args': args}
    assert calls[-1]['params'] == {'name': name, 'arguments': expected}
    assert args['mission'] == '<@UCLEO> and <@UVIC> research staffing'


def test_busy_retries_same_identity_without_local_election(coordination, monkeypatch):
    args = arguments()
    busy = {'isError': True, 'structuredContent': {'code': 'conflict', 'meta': {
        'reason': 'coordination_busy', 'retryAfterMs': 1000}}}
    calls = wire(monkeypatch, coordination, ['agents.coordination.claim'],
                 [deepcopy(busy), deepcopy(busy), {'structuredContent': decision(args)}])
    delays = []
    monkeypatch.setattr(coordination.time, 'sleep', delays.append)
    assert coordination.claim_coordination('https://k2.invalid/mcp', 'token', args)['leadAgentRef'] == 'agent:victoria'
    claims = [r['params'] for r in calls if r['method'] == 'tools/call']
    assert len(claims) == 3 and claims[0] == claims[1] == claims[2]
    assert delays == [1.0, 1.0]


@pytest.mark.parametrize('fault', ['wrong-source', 'wrong-schema', 'denied', 'wrong-repo', 'missing-tool'])
def test_untrusted_or_unavailable_decision_fails_closed(coordination, monkeypatch, fault):
    args = arguments()
    result = decision(args)
    if fault == 'wrong-source': result['source']['messageTs'] = 'different'
    if fault == 'wrong-schema': result['schemaVersion'] = 'unrecognized'
    outcome = {'structuredContent': result}
    if fault == 'denied': outcome = {'isError': True, 'structuredContent': {'code': 'forbidden'}}
    calls = wire(monkeypatch, coordination,
                 [] if fault == 'missing-tool' else ['agents.coordination.claim'], [outcome],
                 repo='katailyst1' if fault == 'wrong-repo' else 'katailyst2')
    with pytest.raises(RuntimeError):
        coordination.claim_coordination('https://k2.invalid/mcp', 'token', args)
    assert sum(r['method'] == 'tools/call' for r in calls) <= 1


def test_slack_contributor_receives_shared_lead_and_original_human_mission(monkeypatch, tmp_path):
    plugin = _load_plugin(stub_coordination=False)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HLT_AGENT_REF', 'agent:cleo')
    seen = []
    def claim(_url, _token, args):
        seen.append(deepcopy(args))
        return decision(args)
    monkeypatch.setattr(plugin, 'claim_coordination', claim)
    raw = _raw('<@U0AHLTX283E> and <@U0BM3ULM210> research staffing')
    event = _event(raw)
    result = plugin._pre_gateway_dispatch(event=event)
    assert result['action'] == 'rewrite'
    assert seen[0]['mission'] == raw['text']
    assert seen[0]['continuesThreadRequest'] is False
    assert len(seen[0]['participantSlackUserIds']) == 2
    assert 'your role: contributor' in result['channel_context']
    assert 'lead: agent:victoria' in result['channel_context']
    assert 'mayComplete: false' in result['channel_context']
    assert result['text'] == event.text
    followup = _event(_raw('Continue with the staffing evidence.', ts='1001.3', thread_ts=raw['ts']))
    assert plugin._pre_gateway_dispatch(event=followup)['action'] == 'rewrite'
    assert seen[1]['continuesThreadRequest'] is True
    assert seen[1]['mission'] == followup.raw_message['text']


def test_failed_claim_does_not_tombstone_retry_and_bot_never_claims(monkeypatch, tmp_path):
    plugin = _load_plugin(stub_coordination=False)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HLT_AGENT_REF', 'agent:cleo')
    attempts = []
    def claim(_url, _token, args):
        attempts.append(args)
        if len(attempts) == 1: raise TimeoutError('synthetic coordination outage')
        return decision(args)
    monkeypatch.setattr(plugin, 'claim_coordination', claim)
    event = _event(_raw('<@U0BM3ULM210> research staffing'))
    assert plugin._pre_gateway_dispatch(event=event)['reason'] == 'lead_selection_unavailable'
    assert plugin._pre_gateway_dispatch(event=event)['action'] == 'rewrite'
    before = len(attempts)
    bot = _event(_raw('<@U0BM3ULM210> proceed', ts='999.1', bot_id='B_AGENT', subtype='bot_message'))
    assert plugin._pre_gateway_dispatch(event=bot)['action'] == 'skip'
    assert len(attempts) == before


def _plugin_wire(monkeypatch, tmp_path, expected, *, name='agents.coordination.claim', role='lead'):
    """Exercise the real plugin and MCP adapter; only the remote response is fixed.

    These request shapes also run through Katailyst2's actual coordination
    service tests, including old-lead readback after a bare-mention handoff.
    """
    plugin = _load_plugin(stub_coordination=False)
    module = sys.modules[plugin.__name__ + '.coordination']
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HLT_AGENT_REF', 'agent:cleo')
    monkeypatch.setenv('KATAILYST2_MCP_URL', 'https://k2.invalid/mcp')
    monkeypatch.setenv('KATAILYST2_MCP_TOKEN', 'test-token')
    result = decision(expected, role=role)
    result['coordinationId'] = '11111111-1111-4111-a111-111111111111'
    if role == 'lead':
        result['leadAgentRef'] = 'agent:cleo'
    payload = {'output': result} if name == 'tool_execute' else result
    calls = wire(monkeypatch, module, [name], [{'structuredContent': payload}])
    return plugin, calls


@pytest.mark.parametrize('name', ['agents.coordination.claim', 'tool_execute'])
def test_owned_dm_includes_implicit_owner_at_the_k2_wire_boundary(monkeypatch, tmp_path, name):
    raw = _raw('<@U0AHLTX283E> has the operations context.',
               channel_type='im', channel='D0BM1V250G6')
    expected = {
        'callerAgentRef': 'agent:cleo', 'continuesThreadRequest': False,
        'source': {'platform': 'slack', 'teamId': 'T_HLT', 'channelId': raw['channel'],
                   'threadTs': raw['ts'], 'messageTs': raw['ts'],
                   'humanUserId': raw['user'], 'actorKind': 'human'},
        'mission': raw['text'],
        'participantSlackUserIds': ['U0AHLTX283E', 'U0BM3ULM210'],
    }
    plugin, calls = _plugin_wire(monkeypatch, tmp_path, expected, name=name, role='contributor')

    result = plugin._pre_gateway_dispatch(event=_event(raw))

    assert result['action'] == 'rewrite'
    assert 'your role: contributor' in result['channel_context']
    assert 'lead: agent:victoria' in result['channel_context']
    arguments = {'verb': 'agents.coordination.claim', 'args': expected} if name == 'tool_execute' else expected
    assert calls[-1]['params'] == {'name': name, 'arguments': arguments}


def test_bare_thread_handoff_sends_explicit_shared_lead_before_rewriting(monkeypatch, tmp_path):
    raw = _raw('<@U0BM3ULM210>', thread_ts='1787140000.000100')
    expected = {
        'callerAgentRef': 'agent:cleo', 'continuesThreadRequest': True,
        'explicitLeadSlackUserId': 'U0BM3ULM210',
        'source': {'platform': 'slack', 'teamId': 'T_HLT', 'channelId': raw['channel'],
                   'threadTs': raw['thread_ts'], 'messageTs': raw['ts'],
                   'humanUserId': raw['user'], 'actorKind': 'human'},
        'mission': raw['text'], 'participantSlackUserIds': ['U0BM3ULM210'],
    }
    plugin, calls = _plugin_wire(monkeypatch, tmp_path, expected)
    event = _event(raw)
    event.text = ''  # Native Slack strips this app's mention.
    event.channel_context = 'Alec: Finish the sourced Nursing Mastery brief.'

    result = plugin._pre_gateway_dispatch(event=event)

    assert calls[-1]['params']['arguments'] == expected
    assert result['action'] == 'rewrite'
    assert event.channel_context in result['text']
    assert 'explicitly transferred this thread to cleo' in result['text']
    assert 'lead: agent:cleo' in result['channel_context']
    assert 'mayComplete: true' in result['channel_context']


@pytest.mark.parametrize(('text', 'thread_ts'), [
    ('<@U0BM3ULM210>', None),
    ('<@U0BM3ULM210> start a separate research request.', '1787140000.000100'),
    ('<@U0AHLTX283E> <@U0BM3ULM210>', '1787140000.000100'),
])
def test_only_a_bare_single_human_thread_mention_asserts_transfer(monkeypatch, tmp_path, text, thread_ts):
    raw = _raw(text, **({'thread_ts': thread_ts} if thread_ts else {}))
    expected = {
        'callerAgentRef': 'agent:cleo', 'continuesThreadRequest': False,
        'source': {'platform': 'slack', 'teamId': 'T_HLT', 'channelId': raw['channel'],
                   'threadTs': thread_ts or raw['ts'], 'messageTs': raw['ts'],
                   'humanUserId': raw['user'], 'actorKind': 'human'},
        'mission': raw['text'],
        'participantSlackUserIds': (['U0AHLTX283E', 'U0BM3ULM210']
                                    if 'U0AHLTX283E' in text else ['U0BM3ULM210']),
    }
    plugin, calls = _plugin_wire(monkeypatch, tmp_path, expected)
    event = _event(raw)
    event.text = ''  # An empty adapter field alone is not transfer authority.
    event.channel_context = 'Earlier discussion is available.'

    result = plugin._pre_gateway_dispatch(event=event)

    assert result['action'] == 'rewrite'
    assert calls[-1]['params']['arguments'] == expected
    assert '[Ownership transfer]' not in result['text']


def test_quoted_mention_cannot_start_a_shared_handoff(monkeypatch, tmp_path):
    plugin, calls = _plugin_wire(monkeypatch, tmp_path, arguments())
    event = _event(_raw('> <@U0BM3ULM210>', thread_ts='1787140000.000100'))
    event.text = ''
    event.channel_context = 'Earlier discussion is available.'

    assert plugin._pre_gateway_dispatch(event=event)['action'] == 'skip'
    assert calls == []
