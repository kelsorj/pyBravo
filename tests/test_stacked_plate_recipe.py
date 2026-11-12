"""The stacking demo's complete task chain and liquid accounting.

All setup values here are synthetic engineering fixtures, not local method
qualification or evidence that these consumables are loaded on an instrument.
"""
from copy import deepcopy

from pybravo.workflow.protocols.compiler import compile_plan
from pybravo.workflow.protocols.validation import validate_plan
from tests.test_protocol_compiler import _four_source_quadrant_fixture


def stacking_fixture():
    _, setup, context, _ = _four_source_quadrant_fixture()
    plate = context['labware'][0]
    # The fixture explicitly attests its residual estimate; production drafts
    # must collect plate-bound scientist review instead.
    plate['dead_volume_ul'] = 2.0
    plate['dead_volume_status'] = 'reviewed'
    rack = context['labware'][2]
    plan = {'name': 'Synthetic stacked 384-well additions', 'materials': [
        {'id': 'diluent', 'name': 'Diluent source', 'labware_id': plate['id'], 'deck_slot': 1,
         'initial_volume_ul': 20.0, 'dead_volume_ul': 2.0},
        {'id': 'sample', 'name': 'Sample source', 'labware_id': plate['id'], 'deck_slot': 2,
         'initial_volume_ul': 20.0, 'dead_volume_ul': 2.0},
        *[{'id': f'tips-{n}', 'name': f'Fresh rack {n}', 'role': 'tips', 'labware_id': rack['id'],
           'deck_slot': n+2, 'tip_definition_id': 'st_10ul', 'available_tips': 'full'} for n in range(1, 5)],
        *[{'id': f'plate-{n}', 'name': f'Destination {n}', 'labware_id': plate['id'], 'deck_slot': 7,
           'stack_order': n-1, 'initial_volume_ul': 0.0} for n in (1, 2)],
    ], 'steps': []}
    for index, plate_id in enumerate(('plate-2', 'plate-1')):
        plan['steps'].append({'id': f'stage-{index}', 'kind': 'destack_plate' if index == 0 else 'move_plate',
            'material': plate_id, 'destination_slot': 8, 'source_paragraph_ids': ['p1']})
        for source_id in ('diluent', 'sample'):
            plan['steps'].append({'id': f'{source_id}-{index}', 'kind': 'transfer',
                'source': source_id, 'destination': plate_id, 'source_anchor': 'A1', 'destination_anchor': 'A1',
                'volume_ul': 5.0, 'source_paragraph_ids': ['p1'],
                'source_values': [{'field': 'volume_ul', 'value': 5.0, 'unit': 'uL', 'paragraph_id': 'p1'}]})
        plan['steps'].append({'id': f'file-{index}', 'kind': 'move_plate' if index == 0 else 'stack_plate',
            'material': plate_id, 'destination_slot': 9, 'source_paragraph_ids': ['p1']})
    setup['tip_rack_ids'] = [f'tips-{n}' for n in range(1, 5)]
    setup['tip_reuse_reason'] = 'Synthetic fixture: each source has distinct fresh sets; returns are permanently spent.'
    sources = [{'id': 'p1', 'text': 'Add 5 uL diluent and 5 uL sample to every well of two empty 384-well destination plates. Process the top plate first using a work position and an output stack.'}]
    return plan, setup, context, sources


def test_stacking_demo_keeps_four_transfers_and_top_first_plate_identity():
    plan, setup, context, sources = stacking_fixture()
    workflow = compile_plan(plan, setup, context, sources=sources)
    nodes = workflow['graph']['nodes']
    assert [node['type'] for node in nodes if node['type'].startswith('plate/')] == [
        'plate/Destack', 'plate/PickPlace', 'plate/PickPlace', 'plate/Stack']
    assert len([node for node in nodes if node['type'] == 'liquid/Aspirate']) == 4
    assert len([node for node in nodes if node['type'] == 'liquid/Dispense']) == 4
    assert all(node['properties']['volume'] == 5 for node in nodes if node['type'].startswith('liquid/'))
    summary = workflow['protocol']['run_sheet']
    assert summary['final_deck_stacks']['9'] == ['plate-2', 'plate-1']
    assert '7' not in summary['final_deck_stacks'] and '8' not in summary['final_deck_stacks']
    assert summary['tips_required'] == 4 * 384
    assert summary['reagent_consumption_ul'] == {'diluent': 3840.0, 'sample': 3840.0}
    for plate_id in ('plate-1', 'plate-2'):
        assert len(summary['final_volumes_ul'][plate_id]) == 384
        assert set(summary['final_volumes_ul'][plate_id].values()) == {10.0}


def test_stacking_demo_rejects_a_buried_plate_and_exhausted_source():
    plan, setup, context, sources = stacking_fixture()
    buried = deepcopy(plan)
    buried['steps'][0]['material'] = 'plate-1'
    report = validate_plan(buried, setup, context, sources=sources)
    assert any(issue['code'] == 'buried_plate' for issue in report['issues'])
    plan['materials'][0]['initial_volume_ul'] = 6.0
    report = validate_plan(plan, setup, context, sources=sources)
    assert any(issue['code'] == 'insufficient_reagent' for issue in report['issues'])
