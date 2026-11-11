"""Locally generated drafts support software rehearsal without hardware release."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed for Designer JavaScript checks")


def _function(html: str, name: str) -> str:
    match = re.search(rf"(?:async )?function {name}\([^\n]*\) \{{[\s\S]*?\n\}}", html)
    assert match, name
    return match.group(0)


def test_generated_draft_is_editable_and_can_simulate_without_hardware_release(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    script = tmp_path / "generated-draft.cjs"
    script.write_text(
        """
const assert = require('node:assert/strict');
const elements = new Map();
for (const id of ['btn-save','btn-save-as','btn-library','btn-draft','btn-import',
  'btn-export','btn-simulate','btn-execute','btn-play','btn-pause','btn-stop',
  'btn-step','btn-walkthrough','protocol-draft-banner','protocol-draft-review','protocol-draft-summary']) {
  elements.set(id,{disabled:false,hidden:false,dataset:{},textContent:'',title:''});
}
const label={textContent:''};
const document={body:{classList:{toggle(){}}},getElementById(id){
  if(id==='viewport-header') return {querySelector(){return {textContent:''}}};
  return elements.get(id);
},querySelector(selector){
  if(selector==='#viewport-header span') return {textContent:''};
  if(selector==='#protocol-draft-banner strong') return label;
  throw Error(selector);
}};
function requestAnimationFrame(){}
const tab={id:'generated-1',description:'Model draft',library:'',
  graph:{_nodes:[{type:'flow/Start'},{type:'liquid/Aspirate'},{type:'flow/End'}],serialize(){return {nodes:this._nodes,links:[]};}},
  deckConfig:{},protocolMetadata:{protocol_generated_draft:true,
    protocol_draft_status:'unreviewed',protocol_generated_root_id:'generated-1',
    protocol_generated_provenance:{source_kind:'text2wetlab',source_id:'task',model:'qwen'}}};
const getActiveTab=()=>tab;
let currentWorkflowId=tab.id,currentWorkflowName='Generated protocol';
const designerState={graph:tab.graph,deckConfig:{},graphCanvas:{read_only:true}};
"""
        + "\n".join(_function(html, name) for name in (
            "isCompiledProtocolPreviewTab", "isProtocolDraftTab",
            "isGeneratedProtocolDraftTab", "updateProtocolDraftControls",
            "serializeWorkflow",
        ))
        + """
assert.equal(isProtocolDraftTab(),false);
assert.equal(isGeneratedProtocolDraftTab(),true);
updateProtocolDraftControls();
assert.equal(elements.get('protocol-draft-banner').hidden,false);
assert.equal(elements.get('btn-save').disabled,false);
assert.equal(elements.get('btn-save-as').disabled,false);
assert.equal(elements.get('btn-simulate').disabled,false);
assert.equal(elements.get('btn-play').disabled,false);
assert.equal(elements.get('btn-stop').disabled,false);
assert.equal(elements.get('btn-execute').disabled,true);
assert.equal(elements.get('btn-walkthrough').hidden,false);
assert.equal(designerState.graphCanvas.read_only,false);
assert.equal(label.textContent,'Locally generated protocol · unreviewed');
const saved=serializeWorkflow();
assert.equal(saved.protocol_generated_draft,true);
assert.equal(saved.protocol_generated_root_id,'generated-1');
assert.equal(saved.protocol_generated_provenance.model,'qwen');
assert.match(elements.get('protocol-draft-summary').textContent,/stops on task errors/);
tab.protocolMetadata = {};
updateProtocolDraftControls();
assert.equal(elements.get('btn-execute').disabled,false);
assert.equal(elements.get('protocol-draft-banner').hidden,true);
assert.equal(elements.get('btn-walkthrough').hidden,true);
""",
        encoding="utf-8",
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_deserialize_preserves_generated_draft_metadata():
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    assert "'protocol_generated_draft', 'protocol_draft_status', 'protocol_generated_provenance', 'protocol_generated_root_id', 'protocol_draft_issues'" in html
    assert "if (!data.protocol_session_id && !data.protocol_generated_draft) return;" in html
    assert "if (isGeneratedProtocolDraftTab() && executionMode !== 'simulate' && !walkthrough)" in html
    assert "'protocol_simulation_target'" in html
    assert "if (isGeneratedProtocolDraftTab()) executionMode = 'simulate';" in html


def test_generated_draft_simulation_saves_provenance_and_uses_only_simulate_route(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    script = tmp_path / "generated-draft-routing.cjs"
    script.write_text(
        """
const assert = require('node:assert/strict');
const progress = {textContent:''};
const document = {getElementById(id) {return id === 'playback-progress' ? progress : {style:{}};}};
let executionMode = 'execute', currentWorkflowId = 'draft-1';
const designerState={graph:{_nodes:[]}};
let walkthroughResponse = false;
const API_BASE = '';
const calls = [];
function isCompiledProtocolPreviewTab(){return false;}
function isProtocolDraftTab(){return false;}
function isGeneratedProtocolDraftTab(){return true;}
function validateWorkflow(){return [];}
function clearActiveNodeHighlight(){}
function timelineReset(){}
let physicalObstructionRun=null;
function resetPhysicalObstructionPreview(){physicalObstructionRun=null;}
function physicalObstructionDeckDetails(){return {};}
function recordDrafterPatch(){}
function syncActiveTabMeta(){}
function serializeWorkflow(){return {name:'Draft',protocol_generated_draft:true,
  protocol_generated_provenance:{model:'qwen'},graph:{nodes:[],links:[]}};}
async function apiCall(path,method,body){calls.push({path,method,body});return {id:currentWorkflowId};}
async function fetch(path,options){calls.push({path,method:options.method});return {ok:true,json:async()=>({status:'started',physical_engine:walkthroughResponse?undefined:'SuperDex',mode:walkthroughResponse?'walkthrough':'simulate',simulation_kind:walkthroughResponse?'visual_walkthrough':'draft_rehearsal',qualification_granted:false,validation_passed:false})};}
"""
        + _function(html, "runWorkflow")
        + """
(async () => {
  await runWorkflow();
  assert.equal(calls.length,0);
  assert.match(progress.textContent,/Hardware execution requires/);
  executionMode = 'simulate';
  await runWorkflow();
  assert.equal(calls.length,2);
  assert.equal(calls[0].body.protocol_generated_draft,true);
  assert.equal(calls[0].body.protocol_generated_provenance.model,'qwen');
  assert.equal(calls[1].path,'/api/workflows/draft-1/simulate');
  assert.equal(progress.textContent,'Checking native task motion with SuperDex...');
  calls.length=0;
  walkthroughResponse=true;
  executionMode='execute';
  await runWorkflow({walkthrough:true});
  assert.equal(calls[1].path,'/api/workflows/draft-1/walkthrough');
  assert.match(progress.textContent,/tasks are not executed or validated/);
})().catch(error => {console.error(error);process.exitCode=1;});
""",
        encoding="utf-8",
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_missing_liquid_class_is_visible_and_selection_marks_draft_dirty(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    script = tmp_path / "missing-class.cjs"
    script.write_text(
        """
const assert = require('node:assert/strict');
const selects=[];
class Element {
  constructor(tag){this.tag=tag;this.style={};this.children=[];this.handlers={};}
  appendChild(child){this.children.push(child);}
  append(...children){this.children.push(...children);}
  setAttribute(){}
  addEventListener(event,callback){this.handlers[event]=callback;}
  get options(){return this.children;}
}
const body=new Element('div');
const document={getElementById(){return body;},createElement(tag){
  const element=new Element(tag);if(tag==='select')selects.push(element);return element;
}};
function isCompiledProtocolPreviewTab(){return false;}
let dirty=0;
function markActiveDirty(){dirty++;}
const designerState={liquidClasses:[{name:'EtOH'}]};
const node={type:'liquid/Aspirate',title:'Aspirate Isopropanol',
  properties:{liquid_class:'Isopropanol'},setDirtyCanvas(){}};
"""
        + _function(html, "renderPropertiesPanel")
        + """
renderPropertiesPanel(node);
assert.equal(selects.length,1);
const missing=selects[0].options.find(option=>option.selected);
assert.equal(missing.textContent,'Unavailable: Isopropanol');
assert.equal(missing.disabled,true);
assert.equal(node.properties.liquid_class,'Isopropanol');
assert.equal(dirty,0);
selects[0].value='EtOH';
selects[0].handlers.change();
assert.equal(node.properties.liquid_class,'EtOH');
assert.equal(dirty,1);
""",
        encoding="utf-8",
    )
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_liquid_class_options_are_scoped_to_the_selected_target(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    script = tmp_path / "scoped-classes.cjs"
    script.write_text("""
const assert=require('node:assert/strict');
const tab={protocolMetadata:{}};
const getActiveTab=()=>tab;
const designerState={liquidClasses:[],graphCanvas:{selected_nodes:{}}};
const calls=[];
async function apiCall(path){calls.push(path);return {liquid_classes:[{name:'Hardware class'}]};}
""" + _function(html, "loadLiquidClasses") + """
(async()=>{
  await loadLiquidClasses();
  assert.equal(calls[0],'/api/liquid_classes');
  tab.protocolMetadata.protocol_simulation_target={machine_id:'machine',head_type:'HT_96_D_200',tip_definition_id:'lt_250ul'};
  await loadLiquidClasses();
  const url=new URL(calls[1],'http://test');
  assert.equal(url.searchParams.get('machine_id'),'machine');
  assert.equal(url.searchParams.get('head_type'),'HT_96_D_200');
  assert.equal(url.searchParams.get('tip_id'),'lt_250ul');
  assert.equal(url.searchParams.has('all'),false);
})().catch(error=>{console.error(error);process.exitCode=1;});
""", encoding="utf-8")
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_designer_inline_javascript_parses(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>([\s\S]*?)</script>", html)
    checked = 0
    for index, (attributes, source) in enumerate(scripts):
        if "importmap" in attributes or not source.strip():
            continue
        path = tmp_path / f"designer-inline-{index}.mjs"
        path.write_text(source, encoding="utf-8")
        subprocess.run([NODE, "--check", str(path)], check=True, capture_output=True, text=True)
        checked += 1
    assert checked >= 2, "Check the Designer module and its inline initialization script"


def _tipbox_inventory_harness(html: str) -> str:
    """Execute production inventory/picker functions against a small native DAG."""
    geometry = re.search(r"const HEAD_GEOMETRY = \{[\s\S]*?\n\};", html)
    assert geometry
    return """
const assert = require('node:assert/strict');
const TIPBOX_BASE_CLASSES = new Set(['tip_box','tip_trash_bin','tip_trash']);
const TRASH_BASE_CLASSES = new Set(['tip_trash_bin','tip_trash']);
const mode = {subset_type:'single_barrel',subset_config:'back_left'};
function node(id,type,properties={}) {
  return {id,type,properties,inputs:[{link:null}],outputs:[{links:[]}]};
}
const start = node(1,'flow/Start');
const pickA1 = node(2,'tips/TipsOn',{location:1,head_mode:mode,tip_anchor_row:0,tip_anchor_col:0});
const returnA1 = node(3,'tips/TipsOff',{location:1,tip_anchor_row:0,tip_anchor_col:0});
const pickA2 = node(4,'tips/TipsOn',{location:1,head_mode:mode,tip_anchor_row:0,tip_anchor_col:1});
const returnElsewhere = node(5,'tips/TipsOff',{location:2,tip_anchor_row:0,tip_anchor_col:0});
const exhaustedPick = node(6,'tips/TipsOn',{location:1,head_mode:mode,tip_anchor_row:0,tip_anchor_col:0});
const nodes = [start,pickA1,returnA1,pickA2,returnElsewhere,exhaustedPick];
const graph = {_nodes:nodes,links:{},getNodeById(id){return nodes.find(n=>n.id===id);}};
for(let index=0;index<nodes.length-1;index++) {
  const linkId=index+1;
  nodes[index].outputs[0].links.push(linkId);
  nodes[index+1].inputs[0].link=linkId;
  graph.links[linkId]={origin_id:nodes[index].id,target_id:nodes[index+1].id};
}
const designerState = {headType:'HT_96_D_70',graph,
  labwareCatalog:[{id:'rack96',name:'96 ST rack',base_class:'tip_box',wells:96,rows:8,cols:12}],
  deckConfig:{
    '1':[{labware_id:'rack96',tipbox_fill_state:'full',available_tips:['A1','A2']}],
    '2':[{labware_id:'rack96',tipbox_fill_state:'empty'}],
  }};
class Element {
  constructor(){
    this.children=[];this.style={};this.handlers={};this.disabled=false;
    const classes=new Set();
    this.classList={add(value){classes.add(value);},remove(value){classes.delete(value);},contains(value){return classes.has(value);}};
  }
  appendChild(child){this.children.push(child);}
  addEventListener(event,callback){this.handlers[event]=callback;}
  set textContent(value){this.text=value;this.children=[];}
  get textContent(){return this.text;}
}
const elements = new Map();
const document={getElementById(id){
  if(!elements.has(id)) elements.set(id,new Element());
  return elements.get(id);
},createElement(){return new Element();}};
let tbDraft={},tbOnSave=null,tbLegalAnchors=[];
let activeTab={};
function getActiveTab(){return activeTab;}
const requests=[];
let response={legal_anchors:[],reachability:{assessed:true}};
let requestError=null;
async function apiCall(path){requests.push(new URL(path,'http://test'));if(requestError)throw requestError;return response;}
""" + geometry.group(0) + "\n" + "\n".join(_function(html, name) for name in (
        "getHeadGeometry", "normalizeHeadModeForUi", "describeHeadMode",
        "getTipboxDetailAtLocation", "getTipboxGeometryFromDetail", "tipboxCellsForAnchor",
        "findUpstreamFlowNode", "tipsOpFootprintCells", "simulateTipboxOccupancy",
        "openTipboxPicker", "closeTipboxPicker", "refreshTipboxLegalAnchors", "renderTipboxGrid",
    ))


def test_returned_tips_stay_spent_and_do_not_replenish_fresh_inventory(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    script = tmp_path / "tip-inventory.cjs"
    script.write_text(_tipbox_inventory_harness(html) + """
const sorted = cells => Array.from(cells).sort();
// A target node's own pickup has not happened yet.
let state = simulateTipboxOccupancy(pickA1,graph);
assert.deepEqual(sorted(state.get(1)),['0:0','0:1']);
assert.deepEqual(sorted(state.fresh.get(1)),['0:0','0:1']);
assert.equal(state.spent.get(1).size,0);
// Pickup leaves an empty hole until the used tip is returned.
state = simulateTipboxOccupancy(returnA1,graph);
assert.deepEqual(sorted(state.get(1)),['0:1']);
assert.deepEqual(sorted(state.fresh.get(1)),['0:1']);
assert.equal(state.spent.get(1).size,0);
// A1 is physically occupied again, but only A2 remains eligible for pickup.
state = simulateTipboxOccupancy(pickA2,graph);
assert.deepEqual(sorted(state.get(1)),['0:0','0:1']);
assert.deepEqual(sorted(state.fresh.get(1)),['0:1']);
assert.deepEqual(sorted(state.spent.get(1)),['0:0']);
// The second returned tip is spent even in a separate initially empty rack.
state = simulateTipboxOccupancy(exhaustedPick,graph);
assert.deepEqual(sorted(state.get(1)),['0:0']);
assert.equal(state.fresh.get(1).size,0);
assert.deepEqual(sorted(state.spent.get(1)),['0:0']);
assert.deepEqual(sorted(state.get(2)),['0:0']);
assert.equal(state.fresh.get(2).size,0);
assert.deepEqual(sorted(state.spent.get(2)),['0:0']);
// Looking up another node must start from the saved deck, not mutate it.
state = simulateTipboxOccupancy(pickA1,graph);
assert.deepEqual(sorted(state.fresh.get(1)),['0:0','0:1']);
assert.deepEqual(designerState.deckConfig['1'][0].available_tips,['A1','A2']);
""", encoding="utf-8")
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_pickup_picker_sends_freshness_and_disables_spent_or_exhausted_choices(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    script = tmp_path / "fresh-tip-picker.cjs"
    script.write_text(_tipbox_inventory_harness(html) + r"""
(async()=>{
  const save=document.getElementById('hm-tipbox-save');
  const grid=document.getElementById('hm-tipbox-grid');
  // Backend offers the remaining fresh A2 cell, despite A1 being occupied.
  response={legal_anchors:[{row:0,col:1,row_count:1,column_count:1,head_anchor:'back_left'}],reachability:{assessed:true}};
  openTipboxPicker(1,mode,0,0,()=>{}, {sourceNode:pickA2,purpose:'pickup'});
  assert.equal(save.disabled,true,'No selection can be saved while lookup is pending');
  await new Promise(resolve=>setImmediate(resolve));
  let params=requests.at(-1).searchParams;
  assert.equal(params.get('purpose'),'pickup');
  assert.equal(params.get('location'),'1');
  assert.equal(params.get('labware_id'),'rack96');
  assert.deepEqual(params.get('occupied_cells').split(',').sort(),['0:0','0:1']);
  assert.equal(params.get('fresh_cells'),'0:1');
  assert.equal(tbDraft.col,1,'The invalid spent anchor must snap to fresh A2');
  assert.equal(save.disabled,false);
  const spent=grid.children[0],fresh=grid.children[1];
  assert.match(spent.title,/spent tip \(unavailable for pickup\)/);
  assert.equal(spent.style.backgroundColor,'#bd8040');
  assert.equal(spent.classList.contains('legal'),false);
  assert.equal(spent.style.cursor,'not-allowed');
  assert.equal(spent.handlers.click,undefined);
  assert.match(fresh.title,/planned fresh tip/);
  assert.equal(fresh.classList.contains('legal'),true);
  assert.equal(typeof fresh.handlers.click,'function');
  // An explicit empty fresh set must not become the server's full-rack default.
  response={legal_anchors:[]};
  openTipboxPicker(1,mode,0,0,()=>{}, {sourceNode:exhaustedPick,purpose:'pickup'});
  await new Promise(resolve=>setImmediate(resolve));
  params=requests.at(-1).searchParams;
  assert.equal(params.has('fresh_cells'),true);
  assert.equal(params.get('fresh_cells'),'');
  assert.equal(params.get('occupied_cells'),'0:0');
  assert.equal(save.disabled,true);
  assert.equal(grid.children.every(cell=>cell.handlers.click===undefined),true);
  // A failed lookup also disables save instead of preserving old legal choices.
  requestError=Error('Catalog lookup unavailable');
  await refreshTipboxLegalAnchors();
  assert.equal(save.disabled,true);
  assert.equal(tbLegalAnchors.length,0);
})().catch(error=>{console.error(error);process.exitCode=1;});
""", encoding="utf-8")
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_spent_rack_fill_state_is_physically_occupied_but_never_fresh(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    script = tmp_path / "spent-rack.cjs"
    script.write_text(_tipbox_inventory_harness(html) + """
designerState.deckConfig['1'][0].tipbox_fill_state='spent';
delete designerState.deckConfig['1'][0].available_tips;
let state=simulateTipboxOccupancy(pickA1,graph);
assert.equal(state.get(1).size,96);
assert.equal(state.fresh.get(1).size,0);
assert.equal(state.spent.get(1).size,96);
assert.equal(state.spent.get(1).has('7:11'),true);
// A partial physical inventory in a spent box still cannot become fresh.
designerState.deckConfig['1'][0].available_tips=['A1','H12'];
state=simulateTipboxOccupancy(pickA1,graph);
assert.deepEqual(Array.from(state.get(1)).sort(),['0:0','7:11']);
assert.equal(state.fresh.get(1).size,0);
assert.deepEqual(Array.from(state.spent.get(1)).sort(),['0:0','7:11']);
(async()=>{
  openTipboxPicker(1,mode,0,0,()=>{}, {sourceNode:pickA1,purpose:'pickup'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests.at(-1).searchParams.get('fresh_cells'),'');
  assert.equal(document.getElementById('hm-tipbox-save').disabled,true);
  assert.match(document.getElementById('hm-tipbox-grid').children[0].title,/spent tip/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""", encoding="utf-8")
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_interleaved_pickup_and_return_forecast_only_the_selected_stride(tmp_path):
    html = (ROOT / "frontend" / "designer.html").read_text(encoding="utf-8")
    script = tmp_path / "interleaved-tipbox.cjs"
    script.write_text(_tipbox_inventory_harness(html) + """
designerState.labwareCatalog=[{id:'rack384',name:'384 ST rack',base_class:'tip_box',wells:384,rows:16,cols:24}];
designerState.deckConfig={'1':[{labware_id:'rack384',tipbox_fill_state:'full'}]};
const all={subset_type:'all_barrels',subset_config:'back_left'};
Object.assign(pickA1.properties,{head_mode:all,tip_anchor_row:1,tip_anchor_col:1,row_stride:2,col_stride:2});
Object.assign(returnA1.properties,{tip_anchor_row:1,tip_anchor_col:1,row_stride:2,col_stride:2});
const geom={rows:16,cols:24};
const normalized=normalizeHeadModeForUi(designerState.headType,all);
const footprint=tipsOpFootprintCells(pickA1,normalized,geom);
assert.equal(footprint.size,96);
assert.equal(footprint.has('1:1'),true);
assert.equal(footprint.has('15:23'),true);
assert.equal(footprint.has('1:2'),false);
assert.equal(footprint.has('2:1'),false);
let state=simulateTipboxOccupancy(returnA1,graph);
assert.equal(state.get(1).size,288);
assert.equal(state.get(1).has('15:23'),false);
assert.equal(state.get(1).has('1:2'),true);
state=simulateTipboxOccupancy(pickA2,graph);
assert.equal(state.get(1).size,384);
assert.equal(state.fresh.get(1).size,288);
assert.deepEqual(Array.from(state.spent.get(1)).sort(),Array.from(footprint).sort());
assert.equal(state.fresh.get(1).has('15:23'),false);
assert.equal(state.fresh.get(1).has('1:2'),true);
// Clamping uses the strided physical span, and the anchor corner is strided.
const corner=tipboxCellsForAnchor(geom,all,{row:99,col:99,row_count:8,column_count:12,row_stride:2,col_stride:2,head_anchor:'front_right'});
assert.equal(corner.clampedRow,1);
assert.equal(corner.clampedCol,1);
assert.equal(corner.anchorKey,'15:23');
assert.equal(corner.selected.size,96);
assert.deepEqual(Array.from(corner.selected).sort(),Array.from(footprint).sort());
(async()=>{
  response={legal_anchors:[{row:1,col:1,row_count:8,column_count:12,row_stride:2,col_stride:2,head_anchor:'front_right'}],reachability:{assessed:true}};
  openTipboxPicker(1,all,1,1,()=>{}, {sourceNode:pickA1,purpose:'pickup'});
  await new Promise(resolve=>setImmediate(resolve));
  const params=requests.at(-1).searchParams;
  assert.equal(params.get('row_stride'),'2');
  assert.equal(params.get('col_stride'),'2');
  const grid=document.getElementById('hm-tipbox-grid');
  assert.equal(grid.children.filter(cell=>cell.classList.contains('legal')).length,96);
  assert.equal(grid.children.filter(cell=>cell.classList.contains('selected')).length,96);
  assert.equal(grid.children[15*24+23].classList.contains('anchor'),true);
  assert.equal(grid.children[1*24+2].classList.contains('legal'),false);
  assert.equal(grid.children[1*24+2].handlers.click,undefined);
})().catch(error=>{console.error(error);process.exitCode=1;});
""", encoding="utf-8")
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)


def test_tip_picker_rejects_unreachable_and_typed_invalid_anchors(tmp_path):
    html = (ROOT / 'frontend' / 'designer.html').read_text(encoding='utf-8')
    script = tmp_path / 'tip-reachability.cjs'
    script.write_text(_tipbox_inventory_harness(html) + r"""
(async()=>{
  const save=document.getElementById('hm-tipbox-save');
  designerState.headType='HT_384_D_70';
  designerState.labwareCatalog=[{id:'rack384',name:'384 ST rack',base_class:'tip_box',wells:384,rows:16,cols:24}];
  designerState.deckConfig={'2':[{labware_id:'rack384',tipbox_fill_state:'full'}],'8':[{labware_id:'rack384',tipbox_fill_state:'full'}]};
  response={legal_anchors:[],reachability:{assessed:true},unreachable_anchors:[{row:15,col:23,target_y_mm:293.22}]};
  openTipboxPicker(8,mode,15,23,()=>{});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests.at(-1).searchParams.get('location'),'8');
  assert.equal(requests.at(-1).searchParams.get('labware_id'),'rack384');
  assert.equal(save.disabled,true);
  assert.match(document.getElementById('hm-tipbox-summary').textContent,/No tip placement is reachable at position 8/);
  response={legal_anchors:[{row:15,col:23,row_count:1,column_count:1}],reachability:{assessed:true}};
  openTipboxPicker(2,mode,15,23,()=>{});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(save.disabled,false);
  // Typing an explicit cell cannot bypass the offered legal anchors.
  tbDraft.row=0;tbDraft.col=0;renderTipboxGrid();
  assert.equal(save.disabled,true);
  tbDraft.row=15;tbDraft.col=23;renderTipboxGrid();
  assert.equal(save.disabled,false);
  // A layout-only response must never masquerade as a reachability check.
  response={legal_anchors:[{row:15,col:23,row_count:1,column_count:1}],reachability:{assessed:false}};
  await refreshTipboxLegalAnchors();
  assert.equal(save.disabled,true);
  assert.equal(tbLegalAnchors.length,0);
  assert.match(document.getElementById('hm-tipbox-summary').textContent,/reachability could not be checked/);
  activeTab={protocolMetadata:{protocol_simulation_target:{machine_id:'fixture',head_type:'HT_96_D_70',tip_definition_id:'st_10ul'}}};
  await refreshTipboxLegalAnchors();
  assert.deepEqual(JSON.parse(requests.at(-1).searchParams.get('simulation_target')),activeTab.protocolMetadata.protocol_simulation_target);
  response={legal_anchors:[{row:0,col:0,row_count:8,column_count:12}],reachability:{assessed:true}};
  openTipboxPicker(2,{subset_type:'all_barrels'},0,0,()=>{});
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(document.getElementById('hm-tipbox-summary').textContent,/Head footprint 8×12/);
})().catch(error=>{console.error(error);process.exitCode=1;});
""", encoding='utf-8')
    subprocess.run([NODE, str(script)], check=True, capture_output=True, text=True)
