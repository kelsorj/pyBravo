"""Typed agent plans and transport never expose approval or hardware dispatch."""
import io
from urllib.error import HTTPError

import httpx
import pytest

from pybravo.bravo import Bravo
from pybravo.web import server
from pybravo.workflow.protocols import api
from pybravo.workflow.protocols.agent_contract import task_catalog
from pybravo.workflow.protocols.context import machine_context
from pybravo.workflow.protocols.mcp_agent import dispatch
from pybravo.workflow.protocols.store import ProtocolStore


def rpc(name, arguments):
    return {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}


def test_agent_tools_have_bounded_paths_and_preserve_error_diagnostics():
    listed = dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
    names = {tool['name'] for tool in listed}
    assert {'bravo_propose_plan', 'bravo_simulate_protocol', 'bravo_get_capabilities'} <= names
    assert not any('execute' in name or 'approve' in name for name in names)
    calls = []
    def fetch(path, payload):
        calls.append((path, payload))
        return {'ok': True}
    dispatch(rpc('bravo_simulate_protocol', {'session_id': 'draft', 'revision': 4}), fetch)
    assert calls == [('/api/protocols/draft/simulate', {'revision': 4})]
    for arguments in ({'session_id': '../execute', 'revision': 4},
                      {'session_id': 'draft', 'revision': '4'},
                      {'session_id': 'draft', 'revision': 4, 'approval': True}):
        assert dispatch(rpc('bravo_simulate_protocol', arguments), fetch)['error']['code'] == -32602
    assert len(calls) == 1
    def rejected(path, payload):
        raise HTTPError(path, 409, 'Conflict', {}, io.BytesIO(b'{"detail":{"issues":[{"code":"insufficient_reagent"}]}}'))
    result = dispatch(rpc('bravo_simulate_protocol', {'session_id': 'draft', 'revision': 4}), rejected)['result']
    assert result['isError'] is True
    assert 'insufficient_reagent' in result['content'][0]['text']


def test_task_catalog_broadcasts_real_tasks_schemas_and_skills():
    bravo = Bravo(mode='simulation')
    catalog = task_catalog(machine_context(bravo))
    assert {'transfer', 'stack_plate', 'destack_plate'} <= {task['id'] for task in catalog['tasks']}
    assert all(task['preconditions'] and task['effects'] for task in catalog['tasks'])
    assert any(skill['id'] == 'stacked-plate-processing' and skill['content'] for skill in catalog['skills'])
    assert catalog['plan_schema']['properties']['steps']
    assert len(catalog['digest']) == 64


@pytest.mark.asyncio
async def test_external_task_chain_returns_repair_feedback_and_requires_acceptance(tmp_path, monkeypatch):
    bravo = Bravo(mode='simulation')
    monkeypatch.setattr(api, '_bravo', lambda: bravo)
    store = ProtocolStore(tmp_path / 'protocols')
    monkeypatch.setattr(api, '_store', store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        original = (await client.post('/api/protocols/from-text', json={
            'text': 'Transfer 5 uL of water to a plate.', 'name': 'Typed plan'})).json()
        plan = {'name': 'Typed plan', 'steps': [{'id': 'transfer', 'kind': 'transfer',
            'volume_ul': 5.0, 'source_paragraph_ids': [original['source']['paragraphs'][0]['id']]}]}
        path = '/api/protocols/' + original['id']
        response = await client.post(path + '/plan-proposals', json={'revision': 1, 'plan': plan})
        assert response.status_code == 200, response.text
        proposal = response.json()
        assert proposal['validation']['issues']
        assert store.get('sessions', original['id']) == original
        saved = (await client.post('/api/protocols/chat-proposals/' + proposal['proposal_id'] + '/accept')).json()['session']
        assert saved['revision'] == 2
        assert saved['plan']['steps'][0]['kind'] == 'transfer'
        assert saved['approval'] is None
        assert (await client.post(path + '/simulate', json={'revision': 1})).status_code == 409
        assert (await client.post(path + '/validate', json={'revision': 1})).status_code == 409
        assert (await client.post(path + '/simulate', json={'revision': 2})).status_code == 409
        # Older browser clients send an empty object for these actions.
        assert (await client.post(path + '/validate', json={})).status_code == 200
        assert (await client.post(path + '/simulate', json={})).status_code == 409
        plan['decisions'] = [{'path': '/steps/0/volume_ul', 'value': 5.0, 'reason': 'An agent guesses'}]
        assert (await client.post(path + '/plan-proposals', json={'revision': 2, 'plan': plan})).status_code == 422
        assert (await client.get('/api/protocols/agent-tasks')).status_code == 200
