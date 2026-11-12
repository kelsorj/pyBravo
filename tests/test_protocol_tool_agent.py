"""The browser agent uses real tool turns and never grants hardware access."""
import copy
import json

import httpx
import pytest

from pybravo.workflow.protocols.agent_chat import TOOLS, apply_edits, run_turn, volume_summary
from pybravo.workflow.protocols.models import ProtocolPlan
from tests.test_protocol_compiler import protocol_fixture


def record_fixture():
    plan, setup, context, sources = protocol_fixture()
    record = {'id': 'tool-test', 'revision': 3, 'name': plan['name'],
              'source': {'source_id': 'source', 'name': plan['name'], 'metadata': {}, 'paragraphs': sources},
              'selected_paragraph_ids': ['p1'], 'plan': ProtocolPlan.model_validate(plan).model_dump(),
              'setup': setup, 'chat_messages': [], 'validation': None, 'simulation': None}
    return record, context


def response(message, reason='tool_calls'):
    return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': reason}]})


def call(name, arguments=None):
    return response({'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'call-'+name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments or {})}}]})


async def test_agent_inspects_edits_checks_and_replies_without_mutating_saved_protocol():
    record, context = record_fixture()
    original = copy.deepcopy(record)
    requests, events = [], []
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body['tool_choice'] == 'auto' and body['tools']
            assert 'response_format' not in body
            inspected = json.loads(body['messages'][-1]['content'])
            paragraph = inspected['latest_user_paragraph_id']
            return call('edit_protocol', {'edits': [
                {'path': '/plan/steps/0/volume_ul', 'value': 10.0},
                {'path': '/plan/steps/0/source_paragraph_ids', 'value': [paragraph]},
                {'path': '/plan/steps/0/source_values', 'value': [
                    {'field': 'volume_ul', 'value': 10.0, 'unit': 'uL', 'paragraph_id': paragraph}]}]})
        if len(requests) == 2:
            return call('validate_draft')
        assert json.loads(body['messages'][-1]['content'])['ok'] is True
        return response({'role': 'assistant', 'content': 'I changed the transfer to 10 µL per channel. The draft checks pass; review the suggestion.'}, 'stop')
    async def emit(event):
        events.append(event)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await run_turn(record, 'Change the transfer to 10 uL per channel.', context, emit, http_client=client)
    assert result.changed is True
    assert result.candidate['plan']['steps'][0]['volume_ul'] == 10
    assert result.reply.startswith('I changed')
    assert [entry['tool'] for entry in result.trace] == ['inspect_protocol', 'edit_protocol', 'validate_draft']
    assert any(event['type'] == 'draft:preview' for event in events)
    assert record == original
    assert result.candidate['approval'] is None


async def test_question_is_a_natural_reply_without_a_plan_proposal():
    record, context = record_fixture()
    async def emit(event):
        pass
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response({
        'role': 'assistant', 'content': 'Yes. Tell me which transfer you want to change.'}, 'stop'))) as client:
        result = await run_turn(record, 'Can I change the volume?', context, emit, http_client=client)
    assert result.changed is False
    assert result.reply == 'Yes. Tell me which transfer you want to change.'


async def test_even_a_direct_reply_receives_fresh_state_and_computed_quantities():
    record, context = record_fixture()
    def handle(request):
        messages = json.loads(request.content)['messages']
        assert messages[-1]['role'] == 'tool'
        snapshot = json.loads(messages[-1]['content'])
        assert snapshot['plan'] == record['plan']
        assert snapshot['volume_summary']['volume_semantics'] == 'per active channel'
        return response({'role': 'assistant', 'content': 'The volume is per active channel.'}, 'stop')
    async def emit(event):
        pass
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await run_turn(record, 'Is this per channel?', context, emit, http_client=client)
    assert result.changed is False
    assert result.trace[0]['tool'] == 'inspect_protocol'


@pytest.mark.parametrize('edit', [
    {'path': '/approval', 'value': True},
    {'path': '/plan/decisions', 'value': []},
    {'path': '/setup/liquid_class', 'value': {'arbitrary': 'code'}},
    {'path': '/plan/__proto__/x', 'value': 1},
    {'path': '/plan/steps/-1/volume_ul', 'value': 10},
])
def test_edits_are_typed_bounded_and_atomic(edit):
    record, _ = record_fixture()
    original = copy.deepcopy(record)
    with pytest.raises((ValueError, KeyError)):
        apply_edits(record, [edit])
    assert record == original


def test_tools_exclude_hardware_approval_and_arbitrary_execution():
    names = {tool['function']['name'] for tool in TOOLS}
    assert names == {'inspect_protocol', 'get_capabilities', 'find_labware', 'lookup_methods', 'draft_protocol',
                     'edit_protocol', 'validate_draft', 'rehearse_draft'}


def test_volume_summary_totals_four_plates_without_inventing_channel_selection():
    plan = ProtocolPlan(name='Four plates', steps=[
        {'id': f'{source}-{plate}', 'kind': 'transfer', 'source': source,
         'destination': f'plate-{plate}', 'volume_ul': 5.0}
        for plate in range(4) for source in ('sample', 'diluent')
    ]).model_dump()
    summary = volume_summary(plan, {}, {'head_type': 'HT_384_D_70'})
    assert summary['volume_semantics'] == 'per active channel'
    assert summary['channel_scope'] == 'hypothetical_full_head'
    assert summary['channels'] == 384
    assert summary['totals_by_source_ul'] == {'sample': 7680, 'diluent': 7680}
    assert summary['totals_by_destination_ul'] == {f'plate-{i}': 3840 for i in range(4)}
    assert summary['total_delivered_ul'] == 15360


def test_volume_summary_counts_repeats_and_distributes_for_selected_channels():
    plan = ProtocolPlan(name='Repeated distribute', steps=[{
        'id': 'repeat', 'kind': 'repeat', 'repeat': 3, 'steps': [
            {'id': 'distribute', 'kind': 'distribute', 'source': 'sample', 'repeat': 2,
             'dispenses': [{'destination': 'a', 'destination_anchor': 'A1', 'volume_ul': .1},
                          {'destination': 'b', 'destination_anchor': 'A1', 'volume_ul': .2}]},
            {'id': 'mix', 'kind': 'mix', 'material': 'a', 'volume_ul': 10.0, 'cycles': 5},
        ],
    }]).model_dump()
    setup = {'head_mode': {'subset_type': 'single_barrel', 'subset_config': 'back_left'}}
    summary = volume_summary(plan, setup, {'head_type': 'HT_384_D_70'})
    assert summary['channel_scope'] == 'selected_head_mode'
    assert summary['channels'] == 1
    assert summary['totals_by_source_ul'] == {'sample': 1.8}
    assert summary['totals_by_destination_ul'] == {'a': .6, 'b': 1.2}


def test_volume_summary_does_not_present_partial_totals_as_complete():
    plan = ProtocolPlan(name='Unknown volume', steps=[{
        'id': 'transfer', 'kind': 'transfer', 'source': 'a', 'destination': 'b',
    }]).model_dump()
    summary = volume_summary(plan, {}, {'head_type': 'HT_384_D_70'})
    assert summary['available'] is False
    assert 'total_delivered_ul' not in summary


async def test_draft_with_missing_measurements_finishes_with_a_question_instead_of_looping(monkeypatch):
    from pybravo.workflow.protocols import agent_chat
    from pybravo.workflow.protocols.llm import ExtractionResult
    _, context = record_fixture()
    async def draft(source, **kwargs):
        plan, _, _, _ = protocol_fixture()
        plan['materials'][0]['initial_volume_ul'] = None
        plan['steps'][0]['source_paragraph_ids'] = [source.paragraphs[-1].id]
        plan['steps'][0]['source_values'] = [{'field': 'volume_ul', 'value': 20.0,
            'unit': 'uL', 'paragraph_id': source.paragraphs[-1].id}]
        return ExtractionResult(plan=ProtocolPlan.model_validate(plan), metadata={})
    monkeypatch.setattr(agent_chat, 'extract_protocol_plan', draft)
    requests = []
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return call('inspect_protocol')
        if len(requests) == 2:
            return call('draft_protocol', {'mode': 'replace'})
        assert body['tool_choice'] == 'none', 'Unknown setup must end tool editing and return to the user'
        assert not any(message['role'] == 'system' for message in body['messages'][1:]), 'Qwen requires system messages at the beginning'
        return response({'role': 'assistant', 'content': 'I prepared the draft for review. What starting volume is in each source well?'}, 'stop')
    async def emit(event):
        pass
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await run_turn(None, 'Prepare a 20 uL transfer; starting volume is unknown.', context, emit, http_client=client)
    assert result.changed is True
    assert result.candidate['plan']['materials'][0]['initial_volume_ul'] is None
    assert len(requests) == 3
    assert result.trace[-1]['status'] == 'needs_input'
    assert 'starting volume' in result.reply


async def test_a_final_reply_failure_keeps_the_typed_draft_reviewable():
    record, context = record_fixture()
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return call('inspect_protocol')
        if len(requests) == 2:
            return call('edit_protocol', {'edits': [{'path': '/plan/name', 'value': 'Renamed draft'}]})
        return httpx.Response(500, text='Model reply unavailable')
    async def emit(event):
        pass
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await run_turn(record, 'Rename the draft.', context, emit, http_client=client)
    assert result.changed is True
    assert result.candidate['plan']['name'] == 'Renamed draft'
    assert result.candidate['approval'] is None
    assert result.trace[-1]['status'] == 'error'
    assert "couldn't finish" in result.reply
