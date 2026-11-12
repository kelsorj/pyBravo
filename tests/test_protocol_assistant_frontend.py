"""Frontend checks for the reviewed protocol boundary (Node, no hardware)."""

from __future__ import annotations

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed to exercise frontend JavaScript")


def test_protocol_assistant_and_designer_javascript_parse(tmp_path):
    for name in ("protocol_assistant", "designer"):
        html = (ROOT / "frontend" / f"{name}.html").read_text()
        scripts = re.findall(r'<script(?: type="module")?>([\s\S]*?)</script>', html)
        assert scripts
        source = tmp_path / f"{name}.mjs"
        source.write_text("\n".join(scripts))
        subprocess.run([NODE, "--check", str(source)], check=True, capture_output=True, text=True)


def test_source_selection_invalidates_setup_recommendations(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    selection = re.search(r"function sourceSelectionChanged\(\)\{[^\n]*\}", html)
    schedule = re.search(r"function scheduleSetupRecommendations\(\)\{[\s\S]*?\n\}", html)
    assert selection and schedule
    source = (
        """
const assert = require('node:assert/strict');
const state={session:{id:'session',plan:{steps:[]},setup:{},selected_paragraph_ids:['p1']},
  capabilities:{context_hash:'catalog'},setupRecommendationKey:null,setupRecommendations:null,
  setupRecommendationRequest:0,setupRecommendationTimer:null};
const pending=[],requests=[];
const reviewed={checked:true};
let dirtyCalls=0;
function $(id){assert.equal(id,'source-reviewed');return reviewed;}
function dirty(){dirtyCalls++;}
function renderSetupRecommendations(){}
function renderPlan(){}
function renderSetup(){}
function refreshMethodPreview(){}
function renderGuidedSetup(){}
function notify(){}
function clearTimeout(){}
function setTimeout(callback){pending.push(callback);return pending.length;}
async function api(path,options){assert.equal(path,'/setup-recommendations');
  requests.push(JSON.parse(options.body));return {context_hash:'catalog',recommendations:[]};}
"""
        + selection.group(0)
        + "\n"
        + schedule.group(0)
        + """
(async()=>{
  sourceSelectionChanged();
  assert.equal(reviewed.checked,false);
  assert.equal(dirtyCalls,1);
  await pending[0]();
  assert.deepEqual(requests[0].selected_paragraph_ids,['p1']);
  assert.ok(state.setupRecommendations);
  state.session.selected_paragraph_ids=['p2'];reviewed.checked=true;
  sourceSelectionChanged();
  assert.equal(reviewed.checked,false);
  assert.equal(dirtyCalls,2);
  assert.equal(state.setupRecommendations,null);
  await pending[1]();
  assert.deepEqual(requests[1].selected_paragraph_ids,['p2']);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    script = tmp_path / "source-selection.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_method_lookup_uses_distribute_aliquots_and_records_offered_methods(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("methodQueryMissing", "methodQuery", "recordMethodSelection", "safeMethodSourceUrl"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert = require('node:assert/strict');
const clone=value=>JSON.parse(JSON.stringify(value));
const list=value=>Array.isArray(value)?value:[];
const recorded=[];
function decide(path,value,reason){recorded.push({path,value,reason});}
const state={session:{setup:{head_mode:{subset_type:'all_barrels'}},plan:{materials:[
  {id:'source',labware_id:'384-plate',reagent_family:'aqueous'},
  {id:'target-a',labware_id:'1536-plate'},
  {id:'target-b',labware_id:'1536-plate'},
  {id:'tips',role:'tips',labware_id:'384-st-box',tip_definition_id:'st10'}
]}}};
"""
        + "\n".join(functions)
        + """
const query=methodQuery({kind:'distribute',source:'source',source_anchor:'A1',
  dispenses:[{destination:'target-a',volume_ul:5},{destination:'target-b',volume_ul:5}]},'tips');
assert.equal(query.volume_ul,10);
assert.deepEqual(query.dispense_volumes_ul,[5,5]);
assert.equal(query.tip_id,'st10');
assert.equal(query.tipbox_id,'384-st-box');
const chosen={method_id:'aqueous-st10',revision:'sha-selected'};
const offered=[{...chosen,rank:99,match_kind:'exact',execution_ready:true,mismatches:[]},
  {method_id:'aqueous-st10-near',revision:'sha-near',match_kind:'closest',
   execution_ready:false,mismatches:[{field:'reagent_families',reason:'different'}]}];
recordMethodSelection('/steps/0',chosen,offered,'Scientist reviewed reagent difference.');
assert.equal(recorded[0].path,'/steps/0/method_selection');
assert.deepEqual(recorded[0].value.selected,chosen);
assert.deepEqual(recorded[0].value.offered.map(item=>item.rank),[1,2]);
assert.equal(recorded[0].value.offered[1].mismatches[0].field,'reagent_families');
assert.equal(recorded[0].reason,'Scientist reviewed reagent difference.');
assert.equal(safeMethodSourceUrl('javascript:alert(1)'),null);
assert.equal(safeMethodSourceUrl('https://example.org/method'),'https://example.org/method');
"""
    )
    script = tmp_path / "method-selection.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_bulk_method_suggestions_record_proposals_at_nested_step_paths(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("recordMethodSelection", "suggestMethods"):
        match = re.search(rf"(?:async )?function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert = require('node:assert/strict');
const clone=value=>JSON.parse(JSON.stringify(value));
const list=value=>Array.isArray(value)?value:[];
const decisions=[];
function decide(path,value,reason){decisions.push({path,value,reason});}
function methodQuery(){return {volume_ul:5};}
function suggestedMethodRack(){return 'tips';}
let dirtied=0;
function dirty(){dirtied++;}
function renderPlan(){}
function notify(){}
const state={session:{plan:{steps:[
  {kind:'transfer'},
  {kind:'repeat',steps:[{kind:'distribute'}]}
]}}};
async function api(){return {candidates:[{method_id:'reviewed-water',revision:'pin-1',
  method_ref:{method_id:'reviewed-water',revision:'pin-1'},execution_ready:true,
  match_kind:'exact',mismatches:[]}]};}
"""
        + "\n".join(functions)
        + """
(async()=>{
  await suggestMethods();
  assert.equal(dirtied,1);
  assert.deepEqual(decisions.filter(item=>item.path.endsWith('/method_selection'))
    .map(item=>item.path),['/steps/0/method_selection','/steps/1/steps/0/method_selection']);
  assert.ok(decisions.filter(item=>item.path.endsWith('/method_selection'))
    .every(item=>item.value.selection_mode==='bulk_proposal'&&item.reason.includes('resolver proposed')));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    script = tmp_path / "bulk-method-suggestions.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_review_shows_method_differences_fallback_strokes_and_software_deck_state(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    assert "rows.map(renderValidationIssue)" in html
    assert "panel.append(renderRuntimeSnapshot(data.runtime_snapshot))" in html
    functions = []
    for name in ("reviewMethodValue", "renderValidationIssue", "renderRuntimeSnapshot"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert = require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
const textValue=value=>value==null?'':typeof value==='object'?JSON.stringify(value):String(value);
function el(tag,attrs,...children){return {tag,attrs,children:children.flat(Infinity),
  append(...more){this.children.push(...more.flat(Infinity));}};}
function text(node){return node==null?'':typeof node==='string'||typeof node==='number'?String(node):
  node.children.map(text).join(' ');}
"""
        + "\n".join(functions)
        + """
const mismatch=renderValidationIssue({code:'method_mismatch',severity:'warning',path:'/steps/0/method_ref',
  message:'Method differs',differences:[
    {field:'reagent_families',actual:'viscous',method:['aqueous']},
    {field:'volume_ul',phase:'dispense',target_index:1,actual:12,
     method:{min_volume_ul:1,max_volume_ul:10}}
  ]});
assert.match(text(mismatch),/Reagent family.*viscous.*aqueous/s);
assert.match(text(mismatch),/Volume.*dispense, target 2.*12.*1–10 µL per stroke/s);
const fallback=renderValidationIssue({code:'distribute_calibration_fallback',severity:'warning',
  message:'Paired strokes required',reasons:['split_calibration','commanded_tip_capacity'],
  commanded_total_ul:11,commanded_dispenses_ul:[5,5]});
assert.match(text(fallback),/Combined aspiration correction differs/);
assert.match(text(fallback),/Corrected combined aspiration plus air gaps exceeds tip capacity/);
assert.match(text(fallback),/Commanded combined aspiration: 11 µL/);
assert.match(text(fallback),/ordered commanded dispenses: 5, 5 µL/);
const snapshot=renderRuntimeSnapshot({status:'software_known_unverified',physically_verified:false,
  occupied_slots:[{slot:5,labware_names:['ST tip box']}],
  tipbox_inventory:[{slot:5,labware_name:'ST tip box',tip_id:'st_10ul',
    software_occupied_count:2,well_count:384}]});
assert.match(text(snapshot),/not a physically verified load/);
assert.match(text(snapshot),/position 5.*2 occupied of 384 wells/s);
"""
    )
    script = tmp_path / "review-method-details.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_saved_plan_method_preview_is_read_only_and_deduplicates_lookups(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("renderMethodPreview", "refreshMethodPreview"):
        match = re.search(
            rf"async function {name}\([^\n]*\)\{{[\s\S]*?\n\}}|function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html
        )
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert = require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
function el(tag,attrs,...children){return {tag,attrs,children:children.flat(Infinity)};}
const panel={hidden:true,children:[],replaceChildren(...items){this.children=items;},
  append(...items){this.children.push(...items);}};
function $(id){assert.equal(id,'method-preview');return panel;}
let lookups=0;
async function api(path){assert.equal(path,'/methods/lookup');lookups++;
  return {issues:[],candidates:[{method_id:'water-st10',match_kind:'exact',execution_ready:true,
    mismatches:[]}]};}
function methodQuery(){return {operation:'transfer',tip_id:'st10',volume_ul:5};}
function proposedMethodRack(){return {id:'tips',basis:'selected setup rack'};}
function methodQueryMissing(){return [];}
function localLiquidClassGap(){return '';}
function renderGuidedSetup(){}
const state={session:{id:'s1',setup:{},plan:{materials:[],steps:[
  {id:'a',kind:'transfer',description:'Copy A'},
  {id:'b',kind:'transfer',description:'Copy B'}]}},
  capabilities:{context_hash:'catalog'},methodsRegistry:{digest:'registry'},
  methodPreview:null,methodPreviewKey:null,methodPreviewRequest:0,dirty:false};
"""
        + "\n".join(functions)
        + """
(async()=>{
  await refreshMethodPreview();
  assert.equal(lookups,1,'Identical queries should be shared');
  assert.equal(state.methodPreview.items.length,2);
  assert.ok(state.methodPreview.items.every(item=>item.status==='candidate'));
  assert.ok(state.session.plan.steps.every(step=>!step.method_ref),'Read-only preview must never pin');
  await refreshMethodPreview();
  assert.equal(lookups,1,'Unchanged saved plan should not requery');
  state.session.plan.steps[0].description='Changed saved plan';
  await refreshMethodPreview();
  assert.equal(lookups,2);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    script = tmp_path / "method-preview.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_source_coded_tip_racks_enable_read_only_method_lookup_and_show_class_gap(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in (
        "methodQueryMissing", "methodQuery", "proposedMethodRack", "suggestedMethodRack",
        "localLiquidClassGap", "liquidSourceOrder", "renderMethodPreview", "refreshMethodPreview",
    ):
        match = re.search(rf"(?:async )?function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
function el(tag,attrs,...children){return {tag,attrs,children:children.flat(Infinity)};}
function text(node){return node==null?'':typeof node==='string'?node:node.children?.map(text).join(' ')||'';}
const panel={hidden:true,children:[],replaceChildren(...nodes){this.children=nodes;},
  append(...nodes){this.children.push(...nodes);}};
function $(id){assert.equal(id,'method-preview');return panel;}
const materials=[
  ...[1,2,3,4].map(i=>({id:'source_'+i,role:'liquid',labware_id:'384-labcyte'})),
  ...[1,2].map(i=>({id:'destination_'+i,role:'liquid',labware_id:'1536-ldv'})),
  ...[4,3,2,1].map(i=>({id:'tips_source_'+i,role:'tips',labware_id:'384-st-rack',
    tip_definition_id:'st_10ul'}))];
const steps=[4,3,2,1].flatMap(i=>[1,2].map(j=>({kind:'transfer',source:'source_'+i,
  destination:'destination_'+j,volume_ul:5,source_anchor:'A1',
  destination_anchor:['A1','A2','B1','B2'][i-1]})));
const state={session:{id:'s1',setup:{},plan:{materials,steps}},
  context:{machine_id:'SIMULATED',head_type:'HT_384_D_70',liquid_classes:[]},
  capabilities:{context_hash:'catalog'},methodsRegistry:{digest:'registry'},
  methodPreview:null,methodPreviewKey:null,methodPreviewRequest:0,dirty:false};
let lookups=0;
async function api(path){assert.equal(path,'/methods/lookup');lookups++;return {issues:[],candidates:[]};}
function renderGuidedSetup(){}
"""
        + "\n".join(functions)
        + """
(async()=>{
  assert.equal(suggestedMethodRack(steps[0]),'tips_source_4');
  assert.equal(suggestedMethodRack(steps[7]),'tips_source_1');
  assert.deepEqual(methodQueryMissing(steps[0],'tips_source_4'),[]);
  assert.equal(methodQuery(steps[0],'tips_source_4').volume_ul,5);
  await refreshMethodPreview();
  assert.equal(lookups,1,'Identical catalog queries should be shared across eight actions');
  assert.equal(state.methodPreview.items.length,8);
  assert.ok(state.methodPreview.items.every(item=>item.status==='needs_curation'));
  assert.ok(state.methodPreview.items.every(item=>item.message.includes('No local liquid class')));
  assert.match(panel.children.map(text).join(' '),/physical load unconfirmed/);
  assert.ok(steps.every(step=>!step.method_ref));
  materials[5].labware_id=null;
  assert.deepEqual(methodQueryMissing(steps[1],'tips_source_4'),['destination labware']);
  state.session.setup.tip_rack_ids=['tips_source_4'];
  assert.equal(suggestedMethodRack(steps[0]),'',
    'A partial explicit setup order must not be overwritten by source-coded inference');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    script = tmp_path / "source-coded-method-preview.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_liquid_class_proposal_is_grouped_explained_and_planning_only(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in (
        "liquidSourceOrder", "sharedProposalTipRack", "liquidClassProposalQueries", "sharedSourceLiquidMissing",
        "applySharedSourceLiquidFamily", "liquidClassProposalPath", "liquidClassProposalDecision",
        "recordLiquidClassProposal", "renderLiquidClassProposals",
        "refreshLiquidClassProposals", "decide",
    ):
        match = re.search(rf"(?:async )?function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
const clone=value=>JSON.parse(JSON.stringify(value));
function el(tag,attrs={},...children){const node={tag,attrs,children:children.flat(Infinity).filter(x=>x!=null),
  value:'',disabled:!!attrs.disabled,listeners:{},append(...items){this.children.push(...items);},
  addEventListener(event,handler){this.listeners[event]=handler;}};return node;}
function text(node){return typeof node==='string'?node:node==null?'':
  (node.children||[]).map(text).join(' ');}
function buttons(node){return node==null||typeof node==='string'?[]:
  [...(node.tag==='button'?[node]:[]),...(node.children||[]).flatMap(buttons)];}
function inputs(node){return node==null||typeof node==='string'?[]:
  [...(node.tag==='input'?[node]:[]),...(node.children||[]).flatMap(inputs)];}
const panel={hidden:true,children:[],replaceChildren(...items){this.children=items;},
  append(...items){this.children.push(...items);}};
function $(id){assert.equal(id,'liquid-class-proposals');return panel;}
function localLiquidClassGap(){return 'No active class';}
function proposedMethodRack(){return {id:'tips_source_4'};}
function methodQuery(){return {tip_id:'st_10ul',volume_ul:5,reagent_family:'DMSO',
  source_labware_id:'384-pp',destination_labware_id:'1536-ldv'};}
function safeMethodSourceUrl(value){return value?.startsWith('https://')?value:null;}
let dirtied=0,lookups=0,notice='';
function dirty(){dirtied++;}
function notify(value){notice=value;}
function renderPlan(){}
const steps=Array.from({length:8},()=>({kind:'transfer',volume_ul:5}));
const state={session:{id:'draft',setup:{},plan:{steps,decisions:[],materials:[
  {id:'tips_source_4',role:'tips',labware_id:'384-st-rack',tip_definition_id:'st_10ul'}]}},
  context:{machine_id:'SIMULATED',head_type:'HT_384_D_70',context_hash:'catalog',liquid_classes:[],
    tipbox_choices:[{labware_id:'384-st-rack',tip_definition_id:'st_10ul',execution_ready:true}]},
  liquidClassProposals:null,liquidClassProposalKey:null,liquidClassProposalRequest:0,busy:false};
const candidate={liquid_class_id:'liq-physical',name:'ST10 low volume',
  source_machine_id:'04-91-62-CF-7B-B0',source_head_type:'HT_384_D_70',source_tip_id:'st_10ul',
  status:'imported_unverified',execution_ready:false,can_apply_to_plan:false,
  ranking_reason:'Nearest recorded calibration point, not reagent-qualified',
  reasons:['Matches exact ST10 tip and 5 µL range'],caveats:[],
  provenance:{source_type:'local_config',source_path:'config/liquid_classes.yaml',
    source_digest:'digest-1',note:'Physical class settings, not approved for simulation.'},
  aspirate:{w_velocity_ul_s:1},dispense:{w_velocity_ul_s:2},
  source_field_origins:{'aspirate.w_velocity_ul_s':'imported_config',
    'dispense.w_velocity_ul_s':'imported_config','equation.control_points':'imported_config'},
  equation:{control_points:[{desired_ul:5,commanded_ul:5.01}]},
  missing_fields:['DMSO PP-to-LDV method review']};
async function api(path,options){assert.equal(path,'/liquid-class-proposals');lookups++;
  const query=JSON.parse(options.body);assert.equal(query.reagent_family,'DMSO');
  return {summary_reason:'Planning-only cross-profile discovery',
    planning_liquid_class_candidates:[candidate],
    references:[{source_url:'https://example.org/dmso',note:'DMSO context only',
      qualifies_numeric_settings:false}]};}
"""
        + "\n".join(functions)
        + """
(async()=>{
  assert.equal(liquidClassProposalQueries().length,1);
  await refreshLiquidClassProposals();
  assert.equal(lookups,1,'Eight identical transfers should need one lookup');
  assert.equal(state.liquidClassProposals.items[0].step_count,8);
  const content=panel.children.map(text).join(' ');
  assert.match(content,/Planning candidate only/);
  assert.match(content,/04-91-62-CF-7B-B0/);
  assert.match(content,/DMSO suitability/);
  assert.match(content,/DMSO context only/);
  assert.match(content,/Nearest recorded calibration point, not reagent-qualified/);
  assert.match(content,/imported_config/);
  assert.match(content,/does not qualify the class settings/);
  assert.match(content,/config\\/liquid_classes.yaml/);
  const group=panel.children.find(node=>node.tag==='div'&&node.children?.[0]?.tag==='strong');
  const firstCard=group.children[1];
  assert.equal(text(firstCard.children[0]),'ST10 low volume',
    'The proposed class should appear before publication detail');
  assert.equal(firstCard.children.filter(node=>node.attrs?.class==='issue warn').length,1,
    'Keep one visible qualification warning; detailed caveats belong in disclosure');
  const evidenceIndex=group.children.findIndex(node=>node.tag==='details'&&
    text(node).includes('DMSO context only'));
  assert.ok(evidenceIndex>1,'Vendor note should be in a disclosure after the class');
  assert.equal(group.children[evidenceIndex].children[0].tag,'summary');
  assert.equal(state.session.setup.liquid_class,undefined);
  assert.ok(steps.every(step=>!step.method_ref));
  const keep=panel.children.flatMap(buttons).find(button=>text(button)==='Keep as candidate');
  assert.ok(keep);
  keep.attrs.onclick();
  assert.equal(dirtied,1);
  assert.match(notice,/No liquid class or method was applied/);
  const decision=state.session.plan.decisions[0];
  assert.match(decision.path,/^\\/planning\\/liquid_class_candidates\\//);
  assert.equal(decision.value.disposition,'shortlisted');
  assert.equal(decision.value.source_digest,'digest-1');
  assert.equal(decision.value.execution_ready,false);
  assert.equal(state.session.setup.liquid_class,undefined);
  assert.ok(steps.every(step=>!step.method_ref));
  const reject=panel.children.flatMap(buttons).find(button=>text(button)==='Reject suggestion');
  reject.attrs.onclick();
  assert.equal(state.session.plan.decisions.length,1,'Reject replaces this candidate decision');
  assert.equal(state.session.plan.decisions[0].value.disposition,'rejected');
  state.session.plan.materials=[1,2,3,4].map(i=>({id:'source_'+i,role:'liquid',reagent_family:null}));
  steps.forEach((step,index)=>{step.source='source_'+(Math.floor(index/2)+1);});
  assert.equal(sharedSourceLiquidMissing(),true);
  renderLiquidClassProposals();
  assert.match(panel.children.map(text).join(' '),/What liquid is in the source plates/);
  const input=panel.children.flatMap(inputs)[0];
  input.value='DMSO';input.listeners.input();
  const apply=panel.children.flatMap(buttons).find(button=>text(button)==='Use for all source plates');
  assert.ok(apply&&!apply.disabled);
  apply.attrs.onclick();
  assert.deepEqual(state.session.plan.materials.map(item=>item.reagent_family),['DMSO','DMSO','DMSO','DMSO']);
  assert.equal(state.session.plan.decisions.filter(item=>item.path.endsWith('/reagent_family')).length,4);
  assert.equal(state.session.setup.liquid_class,undefined);
  assert.ok(steps.every(step=>!step.method_ref));
  const original=state.liquidClassProposals.items[0].query;
  const different={...original,reagent_family:'aqueous'};
  recordLiquidClassProposal(candidate,different,'shortlisted');
  const planning=state.session.plan.decisions.filter(item=>item.path.startsWith('/planning/liquid_class_candidates/'));
  assert.equal(planning.length,2,'The same class may be evaluated for two distinct liquid queries');
  assert.equal(liquidClassProposalDecision(candidate,original),'rejected');
  assert.equal(liquidClassProposalDecision(candidate,different),'shortlisted');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    script = tmp_path / "liquid-class-proposal.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_liquid_class_proposal_uses_common_tip_identity_before_tip_policy_is_confirmed(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in (
        "methodQueryMissing", "methodQuery", "proposedMethodRack",
        "sharedProposalTipRack", "liquidClassProposalQueries", "localLiquidClassGap",
        "liquidSourceOrder",
    ):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
const sources=[1,2,3,4].map(i=>({id:'source_'+i,role:'liquid',
  labware_id:'384-labcyte-pp',reagent_family:'DMSO'}));
const destinations=[1,2].map(i=>({id:'destination_'+i,role:'liquid',labware_id:'1536-labcyte-ldv'}));
const racks=[4,3,2,1].map(i=>({id:'tips_source_'+i,role:'tips',
  labware_id:'384-st-box',tip_definition_id:'st_10ul'}));
const steps=[4,3,2,1].flatMap(i=>[1,2].map(j=>({kind:'transfer',source:'source_'+i,
  destination:'destination_'+j,volume_ul:5,source_anchor:'A1',destination_anchor:'A1'})));
const selected=racks.map(rack=>rack.id);
const state={session:{id:'draft',setup:{tip_rack_ids:selected,tip_strategy:null,
  head_mode:{subset_type:'all_barrels'}},plan:{materials:[...sources,...destinations,...racks],steps}},
  context:{machine_id:'SIMULATED',head_type:'HT_384_D_70',liquid_classes:[],
    tipbox_choices:[{labware_id:'384-st-box',tip_definition_id:'st_10ul',execution_ready:true}]},
  setupRecommendations:null,capabilities:null};
"""
        + "\n".join(functions)
        + """
assert.equal(proposedMethodRack(steps[0]).id,'',
  'Method lookup still needs a source-to-rack policy');
assert.equal(sharedProposalTipRack(),'tips_source_4');
const queries=liquidClassProposalQueries();
assert.equal(queries.length,1,'One common tip/box should yield one planning query for eight transfers');
assert.equal(queries[0].step_count,8);
assert.deepEqual(queries[0].query,{tip_id:'st_10ul',volume_ul:5,reagent_family:'DMSO',
  source_labware_id:'384-labcyte-pp',destination_labware_id:'1536-labcyte-ldv'});
assert.equal(state.session.setup.tip_strategy,null);
assert.ok(steps.every(step=>!step.method_ref));
racks[3].tip_definition_id='st_30ul';
assert.equal(sharedProposalTipRack(),null,'Ambiguous rack tips cannot drive a class proposal');
assert.deepEqual(liquidClassProposalQueries(),[]);
"""
    )
    script = tmp_path / "unconfirmed-tip-policy-class-proposal.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_geometry_proposal_requires_analyzable_source_use_and_disjoint_destination_writes(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("sharedProposalTipRack", "liquidClassProposalQueries"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
const state={session:{setup:{tip_rack_ids:['rack'],head_mode:{subset_type:'all_barrels'}},
  plan:{materials:[
    {id:'source',role:'liquid',labware_id:'pp',reagent_id:'neat DMSO',
      reagent_family:'DMSO',initial_volume_ul:45,dead_volume_ul:6.5},
    {id:'dest',role:'liquid',labware_id:'ldv',initial_volume_ul:0},
    {id:'rack',role:'tips',labware_id:'stbox',tip_definition_id:'st_10ul'}],steps:[]}},
  context:{head_type:'HT_384_D_70',
    tipbox_choices:[{labware_id:'stbox',tip_definition_id:'st_10ul',execution_ready:true}],
    labware:[{id:'ldv',wells:1536,rows:32,cols:48,spacing_x_mm:2.25,spacing_y_mm:2.25}]}};
function methodQuery(step){return {tip_id:'st_10ul',volume_ul:5,reagent_id:'neat DMSO',
  reagent_family:'DMSO',source_labware_id:'pp',destination_labware_id:'ldv'};}
const transfer=anchor=>({kind:'transfer',source:'source',source_anchor:'A1',
  destination:'dest',destination_anchor:anchor,volume_ul:5});
"""
        + "\n".join(functions)
        + """
state.session.plan.steps=[transfer('A1'),transfer('A2')];
let rows=liquidClassProposalQueries();
assert.equal(rows.length,1);
assert.equal(rows[0].step_count,2);
assert.equal(rows[0].query.total_source_withdrawal_ul,10);
assert.equal(rows[0].query.destination_initial_volume_ul,0);

state.session.plan.steps.push({kind:'distribute',source:'source',source_anchor:'A1',
  dispenses:[{destination:'dest',volume_ul:1}]});
rows=liquidClassProposalQueries();
assert.ok(rows.every(row=>row.query.source_initial_volume_ul===undefined),
  'A transfer-plus-distribute source cannot use an understated residual estimate');

state.session.plan.steps=[transfer('A1'),transfer('A1')];
rows=liquidClassProposalQueries();
assert.equal(rows[0].query.destination_initial_volume_ul,undefined,
  'Repeated writes to one destination footprint cannot use starting volume as final occupancy');

state.session.plan.steps=[transfer('A1'),transfer('A2')];
state.context.labware[0].wells=384;
rows=liquidClassProposalQueries();
assert.equal(rows[0].query.destination_initial_volume_ul,undefined,
  'Distinct anchors alone do not prove disjoint footprints for arbitrary plates');
"""
    )
    script = tmp_path / "geometry-volume-use.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_bulk_setup_proposals_only_fill_review_draft_choices(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("currentSetupRecommendations", "sameSourceReuseDecision", "applyKnowledgeBackedProposals"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
const clone=value=>JSON.parse(JSON.stringify(value));
const state={session:{id:'s1',plan:{decisions:[],materials:[
  {id:'source',deck_slot:null,initial_volume_ul:null},
  {id:'tips',deck_slot:4,available_tips:null}]},setup:{},selected_paragraph_ids:['p1']},
  capabilities:{context_hash:'catalog'},busy:false};
const recommendations=[
  {path:'/setup/head_mode',value:{subset_type:'all_barrels',subset_config:'back_left'},rule_id:'head'},
  {path:'/setup/tip_strategy',value:'fresh_each_source',rule_id:'strategy'},
  {path:'/setup/tip_rack_ids',value:['tips'],rule_id:'racks'},
  {path:'/materials/0/deck_slot',value:9,rule_id:'deck'},
  {path:'/materials/1/deck_slot',value:2,rule_id:'deck-conflict'},
  {path:'/materials/0/initial_volume_ul',value:100,rule_id:'unsupported'},
  {path:'/materials/1/available_tips',value:'full',rule_id:'physical'},
  {path:'/setup/liquid_class',value:'generic',rule_id:'method'},
  {path:'/setup/distance_from_bottom_mm',value:1,rule_id:'height'}];
state.setupRecommendations={context_hash:'catalog',plan_fingerprint:'fp',recommendations};
state.setupRecommendationKey=JSON.stringify(['s1',state.session.plan,state.session.setup,['p1'],'catalog']);
function recommendationRoot(path){return path.startsWith('/materials/')?state.session.plan:state.session;}
function pointerGet(root,path){return path.split('/').slice(1).reduce((row,key)=>row?.[key],root);}
function pointerSet(root,path,value){const keys=path.split('/').slice(1);let row=root;
  for(const key of keys.slice(0,-1))row=row[key]??=(/^\\d+$/.test(key)?[]:{});
  row[keys.at(-1)]=value;}
const decisions=[];
function decide(path,value,reason){decisions.push({path,value,reason});}
let dirtied=0;
function dirty(){dirtied++;}
function renderPlan(){}
function renderSetup(){}
function renderReview(){}
let notice='';
function notify(message){notice=message;}
"""
        + "\n".join(functions)
        + """
applyKnowledgeBackedProposals();
assert.equal(dirtied,1);
assert.equal(state.session.setup.head_mode.subset_type,'all_barrels');
assert.equal(state.session.setup.tip_strategy,undefined,'reuse requires its own scientist assessment');
assert.deepEqual(state.session.setup.tip_rack_ids,['tips']);
assert.equal(state.session.plan.materials[0].deck_slot,9);
assert.equal(state.session.plan.materials[1].deck_slot,4);
assert.equal(state.session.plan.materials[0].initial_volume_ul,null);
assert.equal(state.session.plan.materials[1].available_tips,null);
assert.equal(state.session.setup.liquid_class,undefined);
assert.equal(state.session.setup.distance_from_bottom_mm,undefined);
assert.equal(decisions.length,3);
assert.ok(decisions.every(row=>row.reason.includes('physical deck and tip inventory remain unconfirmed')));
assert.ok(notice.includes('3 knowledge-backed setup/deck proposals'));
state.session.plan.decisions.push({path:'/setup/same_source_reuse_authorized',actor:'scientist',
  value:{authorized:true,plan_fingerprint:'fp'}});
state.setupRecommendationKey=JSON.stringify(['s1',state.session.plan,state.session.setup,['p1'],'catalog']);
applyKnowledgeBackedProposals();
assert.equal(state.session.setup.tip_strategy,'fresh_each_source');
assert.equal(decisions.length,4);
"""
    )
    script = tmp_path / "bulk-setup-proposals.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_invalid_validation_does_not_present_partial_counts_as_executable(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    match = re.search(r"function validationSummaryText\([^\n]*\)\{[\s\S]*?\n\}", html)
    assert match
    script = tmp_path / "validation-summary.cjs"
    script.write_text(
        "const assert=require('node:assert/strict');\n"
        + match[0]
        + "\n"
        + "const partial=validationSummaryText({ok:false,summary:{channels:0,tips_required:0,compiled_operations:8}});\n"
        + "assert.match(partial,/Validation is incomplete/);\n"
        + "assert.doesNotMatch(partial,/0 channels|8 compiled operations/);\n"
        + "assert.match(validationSummaryText({ok:true,summary:{channels:384,tips_required:1536,compiled_operations:32}}),/384 channels.*1536 tips required.*32 compiled operations/);\n"
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_guided_quadrant_answers_require_exact_mapping_and_explicit_facts(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in (
        "recordDeadVolumeReview", "liquidSourceOrder", "proposedMethodRack", "currentSetupRecommendations",
        "guidedQuadrantSetup", "guidedPhysicalLayout", "applyGuidedSourceVolumes",
        "applyGuidedEmptyDestinations", "applyGuidedFullRacks", "applyGuidedTipReuse",
    ):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
const materials=[
  ...[1,2,3,4].map(i=>({id:'source_'+i,role:'liquid',labware_id:'384',deck_slot:9,
    stack_order:i-1,initial_volume_ul:null,dead_volume_ul:null,well_volumes_ul:{}})),
  ...[1,2].map((i)=>({id:'destination_'+i,role:'liquid',labware_id:'1536',
    deck_slot:i===1?5:8,initial_volume_ul:null,well_volumes_ul:{}})),
  ...[4,3,2,1].map((i,index)=>({id:'tips_source_'+i,role:'tips',labware_id:'st-box',
    tip_definition_id:'st_10ul',deck_slot:index+1,available_tips:null}))];
const anchors={1:'A1',2:'A2',3:'B1',4:'B2'};
const steps=[4,3,2,1].flatMap(i=>[1,2].map(j=>({kind:'transfer',source:'source_'+i,
  destination:'destination_'+j,source_anchor:'A1',destination_anchor:anchors[i],volume_ul:5})));
const state={session:{id:'s',plan:{materials,steps,decisions:[]},setup:{},selected_paragraph_ids:['p']},
  context:{labware:[{id:'384',wells:384},{id:'1536',wells:1536}]},
  capabilities:{context_hash:'catalog'},setupRecommendations:null,setupRecommendationKey:null};
function verifiedTipboxChoices(){return [{labware_id:'st-box',tip_definition_id:'st_10ul',
  wells:384,tip_length_mm:20,tip_capacity_ul:10}];}
function decide(path,value,reason){state.session.plan.decisions.push({path,value,reason,actor:'scientist'});}
let dirtied=0;
function dirty(){dirtied++;}
function renderPlan(){}
function notify(){}
"""
        + "\n".join(functions)
        + """
assert.ok(guidedQuadrantSetup());
assert.equal(guidedPhysicalLayout(guidedQuadrantSetup()).ok,true);
state.session.setup.tip_rack_ids=['tips_source_4','tips_source_3','tips_source_2','tips_source_1'];
assert.equal(guidedPhysicalLayout(guidedQuadrantSetup()).ok,true,
  'Selecting the proposed rack order must not hide its source pairing');
state.session.setup.tip_rack_ids=['tips_source_3','tips_source_4','tips_source_2','tips_source_1'];
assert.equal(guidedPhysicalLayout(guidedQuadrantSetup()).ok,false,
  'A rack order that changes source ownership must require individual review');
state.session.setup.tip_rack_ids=['tips_source_4','tips_source_3','tips_source_2','tips_source_1'];
steps[1].destination_anchor='B1';
steps[3].destination_anchor='B2';
assert.equal(guidedQuadrantSetup(),null,
  'Each destination may have all four quadrants yet swap two source mappings');
steps[1].destination_anchor='B2';steps[3].destination_anchor='B1';
assert.ok(guidedQuadrantSetup());
assert.throws(()=>applyGuidedSourceVolumes(20,2,false),/Confirm/);
applyGuidedSourceVolumes(20,2,true);
assert.ok(materials.slice(0,4).every(row=>row.initial_volume_ul===20&&row.dead_volume_ul===2));
assert.equal(state.session.plan.decisions.filter(row=>row.path.endsWith('/dead_volume_review')).length,4);
assert.throws(()=>applyGuidedSourceVolumes(20,11,true),/at least 10 µL/);
materials[0].well_volumes_ul={A1:25};
assert.throws(()=>applyGuidedSourceVolumes(20,2,true),/individual source well/);
materials[0].well_volumes_ul={};
assert.throws(()=>applyGuidedEmptyDestinations(false),/Confirm/);
applyGuidedEmptyDestinations(true);
assert.ok(materials.slice(4,6).every(row=>row.initial_volume_ul===0));
materials[6].available_tips=['A1'];
assert.equal(guidedPhysicalLayout(guidedQuadrantSetup()).ok,false);
assert.throws(()=>applyGuidedFullRacks(true),/partial tip inventory/);
materials[6].available_tips=null;
applyGuidedFullRacks(true);
assert.ok(materials.slice(6).every(row=>row.available_tips==='full'));
assert.equal(state.session.plan.decisions.filter(row=>row.path.endsWith('/available_tips')).length,4);
state.setupRecommendations={context_hash:'catalog',plan_fingerprint:'fingerprint',recommendations:[]};
state.setupRecommendationKey=JSON.stringify(['s',state.session.plan,state.session.setup,['p'],'catalog']);
applyGuidedTipReuse(true);
assert.equal(state.session.setup.tip_strategy,'fresh_each_source');
assert.equal(state.session.setup.tip_disposal_id,'return_to_source_rack');
assert.match(state.session.setup.tip_reuse_reason,/without contacting liquid/);
assert.ok(state.session.plan.decisions.some(row=>row.path==='/setup/same_source_reuse_authorized'&&
  row.value.plan_fingerprint==='fingerprint'));
state.session.setup.tip_reuse_reason='Custom conflicting assessment';
state.setupRecommendationKey=JSON.stringify(['s',state.session.plan,state.session.setup,['p'],'catalog']);
assert.throws(()=>applyGuidedTipReuse(true),/different tip-reuse assessment/);
state.session.setup.tip_reuse_reason='Both 1536 plates start empty. One clean ST10 tip set touches only one source, dispenses into both destinations without contacting liquid, and is discarded before the next source.';
state.session.setup.tip_disposal_id='custom-waste';
state.setupRecommendationKey=JSON.stringify(['s',state.session.plan,state.session.setup,['p'],'catalog']);
assert.throws(()=>applyGuidedTipReuse(true),/different spent-tip destination/);
assert.ok(dirtied>=4);
"""
    )
    script = tmp_path / "guided-quadrant.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_guided_simulation_setup_uses_confirmed_layout_and_ranked_active_class(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("guidedQuadrantSetup", "guidedPhysicalLayout", "guidedSimulationSetup",
                 "applyGuidedSimulationSetup"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
const materials=[
  ...[1,2,3,4].map(i=>({id:'source_'+i,role:'liquid',labware_id:'384',deck_slot:9,
    stack_order:i-1,initial_volume_ul:45,dead_volume_ul:6.5,reagent_family:'DMSO',reagent_id:null})),
  ...[1,2].map(i=>({id:'destination_'+i,role:'liquid',labware_id:'1536',
    deck_slot:i===1?5:8,initial_volume_ul:0})),
  ...[4,3,2,1].map((i,index)=>({id:'tips_source_'+i,role:'tips',labware_id:'st-box',
    tip_definition_id:'st_10ul',deck_slot:index+1,available_tips:'full'}))];
const anchors={1:'A1',2:'A2',3:'B1',4:'B2'};
const steps=[4,3,2,1].flatMap(i=>[1,2].map(j=>({kind:'transfer',source:'source_'+i,
  destination:'destination_'+j,source_anchor:'A1',destination_anchor:anchors[i],volume_ul:5})));
const decisions=[];
function record(path,value){decisions.push({path,value,actor:'scientist'});}
for(let i=0;i<4;i++){
  record(`/materials/${i}/deck_slot`,9);record(`/materials/${i}/stack_order`,i);
}
for(let i=4;i<6;i++){
  record(`/materials/${i}/deck_slot`,materials[i].deck_slot);
  record(`/materials/${i}/initial_volume_ul`,0);
}
for(let i=6;i<10;i++)for(const field of
  ['labware_id','tip_definition_id','deck_slot','available_tips'])
    record(`/materials/${i}/${field}`,materials[i][field]);
record('/setup/initially_empty_slots',[6,7]);
record('/setup/tip_strategy','fresh_each_source');
record('/setup/tip_disposal_id','return_to_source_rack');
const query={tip_id:'st_10ul',volume_ul:5,reagent_family:'DMSO',
  source_labware_id:'384',destination_labware_id:'1536'};
function liquidClassProposalQueries(){return [{query,step_count:8}];}
function candidate(id,distance){return {liquid_class_id:id,name:id,
  catalog_relation:'active_machine',is_active:true,status:'imported_unverified',
  machine_id:'robot',head_type:'HT_384_D_70',tip_id:'st_10ul',
  score_scope:'calibration_control_point_proximity_only',
  calibration_control_point_distance_ul:distance,nearest_control_point_ul:5-distance,
  equation:{control_points:[{desired_ul:0},{desired_ul:5-distance},{desired_ul:10}]},
  provenance:{source_type:'local_config',source_path:'config/liquid_classes.yaml',
    source_digest:'digest-'+id},ranking_reason:'Calibration proximity only'};}
const broad=candidate('liq-broad',4),near=candidate('liq-near',0.02);
const state={session:{id:'draft',plan:{materials,steps,decisions},setup:{
    tip_strategy:'fresh_each_source',tip_disposal_id:'return_to_source_rack'}},
  context:{controller_type:'simulation',machine_id:'robot',head_type:'HT_384_D_70',
    labware:[{id:'384',wells:384,well_depth_mm:11.4},
      {id:'1536',wells:1536,well_depth_mm:5.2}],
    liquid_classes:[broad,near],tipbox_choices:[]},
  capabilities:{setup_options:{head_modes:[{id:'all_barrels',subset_config_values:['back_left']}]}},
  liquidClassProposals:{items:[{query,step_count:8,status:'ready',candidates:[broad,near]}]}};
function verifiedTipboxChoices(){return [{labware_id:'st-box',tip_definition_id:'st_10ul',
  wells:384,tip_length_mm:20,tip_capacity_ul:10}];}
function decide(path,value,reason){record(path,value);assert.match(reason,/simulation|SIMULATED|provisional|placeholder|confirmed layout|rack positions/i);}
let dirtied=0,rendered=0,notice='';
function dirty(){dirtied++;}
function renderPlan(){rendered++;}
function notify(value){notice=value;}
"""
        + "\n".join(functions)
        + """
const model=guidedQuadrantSetup(),layout=guidedPhysicalLayout(model);
assert.equal(layout.ok,true);
let proposal=guidedSimulationSetup(model,layout);
assert.equal(proposal.candidate.liquid_class_id,'liq-near',
  'Use active calibration ranking, not a hardcoded class ID or shortlist decision');
assert.equal(proposal.minimumDepth,5.2);
assert.deepEqual(proposal.rackIds,['tips_source_4','tips_source_3','tips_source_2','tips_source_1']);
assert.throws(()=>applyGuidedSimulationSetup(false),/explicitly/);
applyGuidedSimulationSetup(true);
assert.deepEqual(state.session.setup.head_mode,{subset_type:'all_barrels',subset_config:'back_left'});
assert.deepEqual(state.session.setup.tip_rack_ids,proposal.rackIds);
assert.equal(state.session.setup.liquid_class,'liq-near');
assert.equal(state.session.setup.distance_from_bottom_mm,1);
assert.deepEqual(state.session.setup.simulation_only_liquid_assumption,{
  liquid_class_id:'liq-near',distance_from_bottom_mm:1,
  basis:'unqualified_geometric_placeholder'});
assert.ok(materials.slice(0,4).every(row=>row.reagent_id===null));
assert.ok(steps.every(step=>!step.method_ref));
assert.equal(dirtied,1);assert.equal(rendered,1);
assert.match(notice,/not qualified for physical use/);
assert.equal(guidedSimulationSetup(model,layout).alreadyApplied,true);
state.session.setup.tip_rack_ids=['tips_source_4'];
assert.equal(guidedPhysicalLayout(model).ok,false,
  'A partial rack order cannot be silently replaced');
state.session.setup.tip_rack_ids=proposal.rackIds;
state.context.controller_type='agile_srt';
assert.match(guidedSimulationSetup(model,layout).reason,/SIMULATED controller/);
state.context.controller_type='simulation';
state.context.labware[1].well_depth_mm=1;
assert.match(guidedSimulationSetup(model,layout).reason,/not inside every addressed/);
state.context.labware[1].well_depth_mm=5.2;
state.session.setup.liquid_class='different-class';
assert.match(guidedSimulationSetup(model,layout).reason,/different liquid class/);
state.session.setup.liquid_class='liq-near';
state.liquidClassProposals.items[0].candidates=[{...near,is_active:false}];
assert.match(guidedSimulationSetup(model,layout).reason,/No active/);
"""
    )
    script = tmp_path / "guided-simulation-setup.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_simulation_only_marker_keeps_strict_simulation_and_blocks_release(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    assert 'id="approval-provisional-note"' in html
    functions = []
    for name in ("renderValidationIssues", "gates"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
function el(tag,attrs,...children){return {tag,attrs,children:children.flat(Infinity)};}
function text(node){return node==null?'':typeof node==='string'?node:node.children?.map(text).join(' ')||'';}
const nodes={
  'issues-details':{hidden:false,open:false,addEventListener(){}},
  'blocking-summary':{children:[],replaceChildren(...rows){this.children=rows;}},
  'issues-summary':{textContent:''},
  issues:{replaceChildren(){}},
  'method-differences-label':{hidden:true},
  'approval-provisional-note':{hidden:true},
  'approval-status':{textContent:'',style:{}},
  'session-status':{textContent:'',className:''},
  scientist:{value:'Scientist'},'approval-notes':{value:'Reviewed'},
  'publish-name':{value:'Published'},'setup-name':{value:'Setup'},
  'setup-picker':{value:''},'protocol-text':{value:''},
  'protocol-file':{files:[]},'plan-reviewed':{checked:true},
  'deck-confirmed':{checked:true},'source-reviewed':{checked:false}
};
function $(id){return nodes[id]??(nodes[id]={disabled:false,value:'',style:{}});}
function freezeBusyControls(){}
function readinessIssueGroup(){return 'Simulation-only method review';}
function readinessRepeatedReason(){return null;}
function renderValidationIssue(issue){return el('p',{},issue.message);}
function guidedQuadrantSetup(){return {};}
const state={busy:false,dirty:false,session:{id:'draft',revision:4,plan:{},
  selected_paragraph_ids:[],setup:{simulation_only_liquid_assumption:{
    liquid_class_id:'liq-near',distance_from_bottom_mm:1,
    basis:'unqualified_geometric_placeholder'}},
  validation:{ok:true},simulation:{status:'passed'},approval:{scientist:'Scientist'}}};
"""
        + "\n".join(functions)
        + """
gates();
assert.equal(nodes.simulate.disabled,false,'A warning-only provisional draft may strictly simulate');
for(const id of ['approve','export','publish'])assert.equal(nodes[id].disabled,true,id);
assert.equal(nodes['approval-provisional-note'].hidden,false);
assert.equal(nodes['deck-confirmed'].disabled,true,'Do not invite physical confirmation');
assert.equal(nodes['approval-status'].textContent,'Simulation only');
renderValidationIssues([{code:'simulation_only_liquid_assumption',severity:'warning',
  path:'/setup/simulation_only_liquid_assumption',message:'Unqualified simulation assumption'}]);
assert.match(nodes['blocking-summary'].children.map(text).join(' '),
  /review notes do not block strict simulation/i);
delete state.session.setup.simulation_only_liquid_assumption;
gates();
for(const id of ['approve','export','publish'])assert.equal(nodes[id].disabled,false,id);
assert.equal(nodes['approval-provisional-note'].hidden,true);
"""
    )
    script = tmp_path / "simulation-only-release.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_catalog_dead_volume_confirmation_binds_plate_and_value(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("recordDeadVolumeReview", "hasDeadVolumeReview", "catalogDeadVolumeFor",
                 "confirmCatalogDeadVolume", "confirmCurrentDeadVolume"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
const state={busy:false,context:{labware:[
  {id:'plate',dead_volume_ul:6.5,dead_volume_status:'placeholder'},
  {id:'new-plate',dead_volume_ul:6.5,dead_volume_status:'placeholder'}]},
  session:{plan:{materials:[{id:'source',name:'Source',role:'liquid',labware_id:'plate',dead_volume_ul:null}],decisions:[]}}};
function clone(value){return JSON.parse(JSON.stringify(value));}
function decide(path,value,reason){
  state.session.plan.decisions=state.session.plan.decisions.filter(row=>row.path!==path);
  state.session.plan.decisions.push({path,value:clone(value),reason,actor:'scientist'});
}
let renders=0,dirtied=0;
function dirty(){dirtied++;}
function renderPlan(){renders++;}
function notify(){}
"""
        + "\n".join(functions)
        + """
const material=state.session.plan.materials[0];
assert.equal(material.dead_volume_ul,null);
assert.equal(catalogDeadVolumeFor(material).status,'placeholder');
confirmCatalogDeadVolume(0,6.5);
assert.equal(material.dead_volume_ul,6.5);
assert.ok(hasDeadVolumeReview(0,material,6.5));
material.labware_id='new-plate';
assert.equal(hasDeadVolumeReview(0,material,6.5),false);
confirmCurrentDeadVolume(0);
assert.ok(hasDeadVolumeReview(0,material,6.5));
material.dead_volume_ul=8;
assert.equal(hasDeadVolumeReview(0,material,8),false);
confirmCurrentDeadVolume(0);
assert.ok(hasDeadVolumeReview(0,material,8));
assert.equal(renders,3);
assert.equal(dirtied,3);
"""
    )
    script = tmp_path / "catalog-dead-volume.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_guided_details_keep_individual_editors_and_questions_in_plan():
    class Structure(HTMLParser):
        void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

        def __init__(self):
            super().__init__()
            self.stack = []
            self.parents = {}
            self.review_action = None

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if attributes.get("id"):
                self.parents[attributes["id"]] = {identity for _, identity in self.stack if identity}
            if tag == "button" and attributes.get("data-go") == "review":
                self.review_action = {identity for _, identity in self.stack if identity}
            if tag not in self.void:
                self.stack.append((tag, attributes.get("id")))

        def handle_endtag(self, tag):
            assert self.stack and self.stack[-1][0] == tag
            self.stack.pop()

    parsed = Structure()
    parsed.feed((ROOT / "frontend" / "protocol_assistant.html").read_text())
    assert not parsed.stack
    for identity in ("materials", "steps", "raw-questions", "questions"):
        assert "plan-content" in parsed.parents[identity]
    assert "plan-details" in parsed.parents["materials"]
    assert "plan-details" in parsed.parents["steps"]
    assert "questions-panel" in parsed.parents["raw-questions"]
    assert "raw-questions" in parsed.parents["questions"]
    assert "plan-content" in parsed.review_action


def test_guided_readiness_groups_repeated_checks_but_keeps_details(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("readinessIssueGroup", "readinessIssueContext", "readinessReasonLabel",
                 "readinessReasonGuidance", "readinessRepeatedReason", "renderValidationIssues"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
function el(tag,attrs,...children){return {tag,attrs,children:children.flat(Infinity)};}
function text(node){return node==null?'':typeof node==='string'?node:node.children?.map(text).join(' ')||'';}
function renderValidationIssue(issue){return el('p',{},issue.message);}
const nodes={
  issues:{children:[],replaceChildren(...items){this.children=items;}},
  'issues-details':{hidden:false,open:true,addEventListener(name,callback){this.onToggle=callback;}},
  'blocking-summary':{children:[],replaceChildren(...items){this.children=items;}},
  'issues-summary':{textContent:''}};
function $(id){return nodes[id];}
function guidedQuadrantSetup(){return {};}
const state={session:{id:'s',revision:2},dirty:false,issueDetailsKey:null};
"""
        + "\n".join(functions)
        + """
const issues=[
  {path:'/materials/0/initial_volume_ul',message:'Source one volume missing',severity:'error'},
  {path:'/materials/1/initial_volume_ul',message:'Source two volume missing',severity:'error'},
  {path:'/materials/6/available_tips',message:'Rack tips not inspected',severity:'error'},
  {path:'/setup/liquid_class',message:'No reviewed liquid method',severity:'error'}];
renderValidationIssues(issues);
assert.equal(nodes['issues-details'].open,false,'Guided pattern collapses raw checks');
assert.equal(nodes.issues.children.length,0,'Collapsed checks are not built eagerly');
assert.ok(nodes['blocking-summary'].children.map(text).join(' ').includes('Confirm plate starting and dead volumes (2)'));
assert.ok(nodes['blocking-summary'].children.map(text).join(' ').includes('Inspect fresh-tip inventory (1)'));
assert.match(nodes['issues-summary'].textContent,/4/);
nodes['issues-details'].open=true;
nodes['issues-details'].onToggle();
renderValidationIssues(issues);
assert.equal(nodes['issues-details'].open,true,'User-expanded details stay open');
assert.ok(nodes.issues.children.some(node=>text(node)==='Rack tips not inspected'));
const repeated=Array.from({length:3072},(_,index)=>({
  code:'insufficient_reagent',severity:'error',path:`/steps/${Math.floor(index/96)}/volume_ul`,
  message:`source:A${index+1} would fall below its 10 uL dead volume.`}));
const other=Array.from({length:8},(_,index)=>({code:'unreachable_wells',severity:'error',
  path:`/steps/${index}/destination_anchor`,message:'The active head cannot reach these wells.'}));
renderValidationIssues([...repeated,...other]);
assert.equal(nodes['issues-details'].open,false,'Thousands of checks start collapsed');
assert.equal(nodes.issues.children.length,0,'Large detail list is rendered on demand');
const summary=nodes['blocking-summary'].children.map(text).join(' ');
assert.match(summary,/3080 blocking checks/);
assert.match(summary,/Review transfer mapping and motion \\(3080\\)/);
assert.match(summary,/Source wells fall below their dead volume \\(3072 checks\\)/);
assert.match(summary,/Review source starting volumes, dead volumes, and transfer volumes/);
assert.match(summary,/Step 1: source:A1/);
assert.match(summary,/Step 16: source:A1536/);
assert.match(summary,/Step 32: source:A3072/);
nodes['issues-details'].open=true;
nodes['issues-details'].onToggle();
assert.equal(nodes.issues.children.length,3080,'Every detailed issue remains available');
assert.equal(text(nodes.issues.children.at(-1)),'The active head cannot reach these wells.');
"""
    )
    script = tmp_path / "readiness-groups.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_guided_answers_hide_stale_saved_validation_questions(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("questionAnsweredInDraft", "allQuestions"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    source = (
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
function tipboxConfirmation(){return null;}
function sameSourceReuseDecision(){return false;}
function currentSetupRecommendations(){return null;}
function pointerGet(root,path){return path.split('/').slice(1).reduce((row,key)=>row?.[key],root);}
const initial={path:'/materials/0/initial_volume_ul',prompt:'Starting volume'};
const tips={path:'/materials/1/available_tips',prompt:'Fresh tip inventory'};
const liquid={path:'/setup/liquid_class',prompt:'Liquid class'};
const head={path:'/setup/head_mode',prompt:'Head footprint'};
const racks={path:'/setup/tip_rack_ids',prompt:'Rack order'};
const exception={path:'/materials/2/deck_slot',prompt:'Check deck position'};
const state={dirty:true,session:{setup:{},plan:{materials:[
  {initial_volume_ul:20},{available_tips:'full'},{deck_slot:null}],
  questions:[initial,liquid,head,racks,exception],decisions:[
  {path:initial.path,value:20,actor:'scientist'},
  {path:tips.path,value:'full',actor:'scientist'}]},
  validation:{questions:[initial,tips,liquid,head,racks,exception]}}};
"""
        + "\n".join(functions)
        + """
assert.deepEqual(allQuestions().map(row=>row.path),[exception.path]);
state.dirty=false;
assert.deepEqual(allQuestions().map(row=>row.path),[exception.path],
  'Saved validation questions with matching scientist decisions stay hidden');
state.session.plan.materials[1].available_tips=['A1'];
assert.deepEqual(allQuestions().map(row=>row.path),[exception.path,tips.path],
  'Changing a reviewed value makes its saved question open again');
"""
    )
    script = tmp_path / "stale-guided-questions.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_only_structured_setup_questions_hide_individual_exceptions_panel(tmp_path):
    html = (ROOT / "frontend" / "protocol_assistant.html").read_text()
    functions = []
    for name in ("questionAnsweredInDraft", "allQuestions", "renderQuestions"):
        match = re.search(rf"function {name}\([^\n]*\)\{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    script = tmp_path / "structured-setup-questions.cjs"
    script.write_text(
        """
const assert=require('node:assert/strict');
const list=value=>Array.isArray(value)?value:[];
const state={session:{setup:{},plan:{materials:[],questions:[
  {path:'/setup/liquid_class',prompt:'Choose a liquid class'},
  {path:'/setup/head_mode',prompt:'Confirm head mode'},
  {path:'/setup/tip_rack_ids',prompt:'Confirm rack order'}],decisions:[]},
  validation:{questions:[{path:'/setup/head_mode',prompt:'Confirm head mode'}]}}};
function tipboxConfirmation(){return null;}
function sameSourceReuseDecision(){return false;}
function currentSetupRecommendations(){return null;}
function pointerGet(root,path){return path.split('/').slice(1).reduce((row,key)=>row?.[key],root);}
const nodes={'questions-panel':{hidden:false},'raw-questions-summary':{textContent:''},
  questions:{children:[],replaceChildren(...items){this.children=items;}}};
function $(id){return nodes[id];}
function el(tag,attrs,...children){return {tag,attrs,children};}
"""
        + "\n".join(functions)
        + """
renderQuestions();
assert.equal(nodes['questions-panel'].hidden,true);
assert.match(nodes['raw-questions-summary'].textContent,/0 individual questions/);
assert.equal(nodes.questions.children[0].tag,'p');
"""
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_designer_preserves_reviewed_properties_and_metadata(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text()
    functions = []
    for name in ("serializeWorkflow", "configureWorkflowGraph", "deserializeWorkflow"):
        match = re.search(rf"function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", html)
        assert match, name
        functions.append(match[0])
    # Emulate the significant LiteGraph behavior: configure merges constructor
    # defaults into the supplied properties. The real helper must remove only
    # the injected defaults for a reviewed workflow, keeping explicit edits.
    source = (
        """
const assert = require('node:assert/strict');
class LGraph {
    constructor(){this.nodes=[];}
    configure(graph){this.nodes=graph.nodes.map(n=>({...n,properties:{volume:50,tip_touch:true,pipette_technique:'',...n.properties}}));}
    getNodeById(id){return this.nodes.find(n=>n.id===id);}
    serialize(){return {nodes:this.nodes};}
}
const openTabs=[];
let activeTabIdx=-1, currentWorkflowId=null, currentWorkflowName='';
const designerState={graph:null,deckConfig:{}};
const getActiveTab=()=>openTabs[activeTabIdx];
const tabIndexForId=id=>openTabs.findIndex(tab=>tab.id===id);
function switchToTab(i){activeTabIdx=i;const tab=getActiveTab();designerState.graph=tab.graph;designerState.deckConfig=tab.deckConfig;currentWorkflowId=tab.id;currentWorkflowName=tab.name;}
function attachDirtyHooks(){}
function updateDeckCell(){}
function syncDeckTo3D(){}
function renderTabStrip(){}
function loadLiquidClasses(){}
"""
        + "\n".join(functions)
        + """
const reviewed={id:'wf',name:'Reviewed',description:'A protocol',protocol_session_id:'session',protocol_revision:3,protocol:{run_sheet:{tips_required:1}},deck:{'1':[{labware_id:'tips',tip_definition_id:'tip70',tipbox_fill_state:{available_wells:['A1']}}]},graph:{nodes:[{id:1,type:'liquid/Aspirate',properties:{location:1,volume:20,tip_touch:false,_source_citation:{paragraph_id:'p1'}}}]}};
deserializeWorkflow(reviewed);
let result=serializeWorkflow();
assert.deepEqual(result.graph.nodes,reviewed.graph.nodes);
assert.deepEqual(result.deck,reviewed.deck);
for(const key of ['protocol_session_id','protocol_revision','protocol'])assert.deepEqual(result[key],reviewed[key]);
getActiveTab().graph.getNodeById(1).properties.volume=21;
assert.equal(serializeWorkflow().graph.nodes[0].properties.volume,21);
// Refreshing an existing tab, and loading a pristine initial tab, use the same helper.
deserializeWorkflow(reviewed);
assert.deepEqual(serializeWorkflow().graph.nodes,reviewed.graph.nodes);
const ordinary=new LGraph();configureWorkflowGraph(ordinary,{graph:reviewed.graph});
assert.equal(ordinary.getNodeById(1).properties.pipette_technique,'');
const preview={...reviewed,id:'chat-preview',protocol_chat_draft:true,protocol_chat_session_id:'chat1',protocol_materials:[{id:'tips',labware_id:'shared-st-box',tip_definition_id:'st70'}]};
delete preview.protocol_session_id;
deserializeWorkflow(preview);
assert.equal(serializeWorkflow().protocol_chat_draft,true);
assert.equal(serializeWorkflow().protocol_chat_session_id,'chat1');
assert.deepEqual(serializeWorkflow().protocol_materials,preview.protocol_materials);
"""
    )
    script = tmp_path / "designer-roundtrip.cjs"
    script.write_text(source)
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)
