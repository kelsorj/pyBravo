/** Browser contract check. Run with Playwright installed:
 * NODE_PATH=/path/to/node_modules node tests/browser/protocol_assistant.cjs
 * Uses isolated Chromium and mocked HTTP; it cannot connect to hardware.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const html = fs.readFileSync(path.join(__dirname, '../../frontend/protocol_assistant.html'), 'utf8');
const fresh = value => JSON.parse(JSON.stringify(value));
const setup = {name:'Water qualification',head_mode:{subset_type:'single_barrel',subset_config:'back_left',row_count:null,column_count:null},tip_strategy:'fresh_each_step',tip_rack_ids:['tips'],tip_disposal_id:'tips',liquid_class:'water',distance_from_bottom_mm:1,tip_reuse_reason:null};
const plan = {schema_version:'1',name:'Water transfer',description:'',materials:[{id:'source',name:'Water reservoir',role:'liquid',labware_id:'reservoir',deck_slot:1,initial_volume_ul:500,dead_volume_ul:20,well_volumes_ul:{}},{id:'target',name:'Destination plate',role:'liquid',labware_id:'plate',deck_slot:2,initial_volume_ul:0,dead_volume_ul:0,well_volumes_ul:{}},{id:'tips',name:'Tips',role:'tips',labware_id:'tiprack',deck_slot:3,tip_definition_id:'tip250',available_tips:['A1']}],steps:[{id:'transfer',kind:'transfer',description:'Transfer water',source:'source',destination:'target',source_anchor:'A1',destination_anchor:'A1',volume_ul:null,source_paragraph_ids:['p1'],source_values:[]},{id:'manual',kind:'manual',description:'Inspect the plate',message:'Inspect the plate and return it to position 2.',source_paragraph_ids:['p2'],source_values:[]}],decisions:[],questions:[{id:'volume',path:'/steps/0/volume_ul',prompt:'What transfer volume should be used?'},{id:'catalog-tipbox:tips',path:'/materials/2/labware_id',prompt:'Confirm the model-selected tipbox and tip definition.'}]};
const compatibleTipbox={labware_id:'tiprack',labware_name:'Head-compatible rack',tip_definition_id:'tip250',tip_name:'250 µL tips',rows:8,cols:12,wells:96,spacing_x_mm:9,spacing_y_mm:9,tip_capacity_ul:250,tip_length_mm:50};
let tipboxChoices=[compatibleTipbox,{...compatibleTipbox,tip_definition_id:'tip70',tip_name:'ST 70 µL tips',tip_capacity_ul:70,tip_length_mm:null,execution_ready:false,missing_metadata:['tip_length']},{...compatibleTipbox,labware_id:'bad-rack',labware_name:'Unverified geometry',spacing_x_mm:0}];
let tipboxReason='';
const tipboxCandidates=[{labware_id:'incomplete-rack',labware_name:'Incomplete catalog tipbox',rows:0,cols:0,wells:96,spacing_x_mm:9,spacing_y_mm:9,verified:false,missing_metadata:['rows_cols','tip_link']}];
let session, savedSetup, library = [], requests = [];
function makeSession(id='session1'){return {id,revision:1,name:'Water transfer',source:{source_id:'source1',name:'Water transfer',metadata:{original_pdf:true},paragraphs:[{id:'p1',text:'Transfer water into the destination plate.'},{id:'p2',text:'Inspect the plate and return it to position 2.',page:2}]},selected_paragraph_ids:['p1','p2'],plan:null,setup:{},issues:[],validation:null,simulation:null,approval:null,history:[]};}
async function run(){
    const browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'chrome',headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1050}});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.route('http://protocol-assistant.test/**',async route=>{
        const req=route.request(),url=new URL(req.url()),endpoint=url.pathname.replace('/api/protocols','');
        if(url.pathname==='/protocol-assistant')return route.fulfill({contentType:'text/html',body:html});
        let body=req.postData()?JSON.parse(req.postData()):null;
        requests.push({method:req.method(),endpoint,body});
        let result;
        if(endpoint==='/context')result={tipbox_choices:tipboxChoices,tipbox_choices_reason:tipboxReason,tipbox_catalog_candidates:tipboxCandidates,profile_name:'Test Bravo',head_type:'ST',labware:[{id:'reservoir',name:'Reservoir'},{id:'plate',name:'96-well plate'},{id:'tiprack',name:'Tip rack'}],tip_definitions:[{id:'tip250',name:'250 µL tips'},{id:'tip70',name:'ST 70 µL tips'},{id:'mismatched-tip',name:'Unverified pairing'}],liquid_classes:[{id:'water',name:'Water'}]};
        else if(endpoint==='/agent-status')result={available:true,model:'qwen',base_url:'http://sparky.local:8000/v1'};
        else if(endpoint==='/capabilities')result={context_hash:'mock-capabilities',machine:{geometry:{}},assistant_operations:[],setup_options:{tip_disposals:[{id:'return_to_source_rack',value_kind:'literal',setup_value:'return_to_source_rack',requires_tip_strategy:'fresh_each_source'}]}};
        else if(endpoint==='/setup-recommendations')result={context_hash:'mock-capabilities',plan_fingerprint:'mock-plan',recommendations:[],unresolved:[]};
        else if(endpoint==='/methods'&&req.method()==='GET')result={digest:'mock-registry',methods:[]};
        else if(endpoint==='/methods/lookup')result={registry_digest:'mock-registry',issues:[],candidates:[]};
        else if(endpoint==='/setups'&&req.method()==='GET')result={items:[{id:'setup1',name:'Water qualification',setup,materials:plan.materials}]};
        else if(endpoint==='/setups'){savedSetup=body;result={id:'saved',...body};}
        else if(endpoint==='/library')result={items:library};
        else if(endpoint==='/from-text'){session=makeSession();result=session;}
        else if(endpoint==='/library/library1/reuse'){session={...makeSession('reused'),setup:fresh(setup),plan:fresh(plan)};result=session;}
        else if(endpoint==='/'+session?.id&&req.method()==='PATCH'){
            assert.equal(body.revision,session.revision);
            assert.notEqual(body.plan,null,'Pre-extraction PATCH must omit null plan');
            session={...session,...body,revision:session.revision+1,validation:null,simulation:null,approval:null};result=session;
        }else if(endpoint.endsWith('/extract')){
            assert.deepEqual(Object.keys(body).sort(),['instructions','revision','selected_paragraph_ids']);
            session={...session,revision:session.revision+1,plan:fresh(plan),setup:fresh(setup)};result=session;
        }else if(endpoint.endsWith('/validate')){session.validation={ok:true,issues:[],questions:[],summary:{channels:1,tips_required:1,compiled_operations:5,manual_checkpoints:1,reagent_consumption_ul:{source:20}}};result=session;}
        else if(endpoint.endsWith('/simulate')){session.simulation={status:'running',run_id:'simulation1'};result=session;}
        else if(endpoint.endsWith('/approve')){assert.equal(body.reviewed,true);assert.equal(body.deck_confirmed,true);assert.equal(body.qualification,'qualification_run');session.approval={...body,approved_at:'2026-10-05'};result=session;}
        else if(endpoint.endsWith('/publish')){library=[{id:'library1',name:body.name,approval:session.approval,plan:session.plan}];result=library[0];}
        else if(endpoint==='/'+session?.id){if(session.simulation?.status==='running')session.simulation={status:'passed',run_id:'simulation1',events:[{type:'workflow:complete',status:'ok'}]};result=session;}
        else throw new Error('Unhandled mock endpoint '+endpoint);
        await route.fulfill({json:fresh(result)});
    });
    try{
        await page.goto('http://protocol-assistant.test/protocol-assistant');
        assert.equal(await page.locator('#tab-source').count(),0,'Source is no longer a preparation page');
        assert.equal(await page.locator('#tab-plan').getAttribute('aria-selected'),'true','Setup opens first');
        assert.match(await page.locator('#tab-plan').innerText(),/01\s*Setup & interpretation/);
        assert.match(await page.locator('#tab-review').innerText(),/02\s*Verify & approve/);
        await page.locator('#source-details > summary').click();
        await page.locator('#protocol-name').fill('Water transfer');
        await page.locator('#protocol-text').fill('Transfer water into the destination plate.\nInspect the plate.');
        await page.locator('#from-text').click();
        await page.locator('#source-panel').waitFor({state:'visible'});
        assert.equal(await page.locator('#original-pdf').getAttribute('href'),'/api/protocols/session1/source-pdf');
        assert.equal(await page.locator('#original-pdf').isVisible(),true);
        await page.locator('#select-none').click();
        await page.locator('#select-all').click();
        await page.locator('#source-reviewed').check();
        await page.locator('#save').click();
        await page.waitForFunction(()=>document.querySelector('#save').disabled);
        assert.equal(session.plan,null);
        await page.locator('#tab-plan').click();
        await page.locator('#setup-picker').selectOption('setup1');
        await page.locator('#apply-setup').click();
        assert.equal(await page.locator('[data-path="/materials/2/tip_definition_id"]').inputValue(),'tip250');
        await page.locator('#extract').click();
        await page.locator('[data-path="/steps/0/volume_ul"]').waitFor({state:'visible'});
        await page.waitForFunction(()=>!document.querySelector('[data-path="/steps/0/volume_ul"]').disabled);
        await page.locator('#steps .citations button').first().click();
        assert.equal(await page.locator('#tab-plan').getAttribute('aria-selected'),'true','A citation remains in Setup');
        assert.equal(await page.locator('#source-details').getAttribute('open'),'','A citation opens source evidence');
        await page.locator('#source-p1').waitFor({state:'visible'});
        assert.equal(await page.locator('#questions .question').count(),2,'Assigned model tipbox still needs explicit confirmation');
        await page.locator('#raw-questions summary').click();
        const confirmTipbox=page.getByRole('button',{name:'Confirm selected tipbox'});
        assert.equal(await confirmTipbox.isDisabled(),false);
        await confirmTipbox.click();
        assert.equal(await page.locator('#questions .question').count(),1);
        await page.locator('[data-path="/materials/2/tip_definition_id"]').selectOption('mismatched-tip');
        assert.equal(await page.getByRole('button',{name:'Confirm selected tipbox'}).isDisabled(),true,'A stale rack/tip mismatch cannot be confirmed');
        await page.locator('[data-path="/materials/2/tip_definition_id"]').selectOption('tip250');
        assert.equal(await page.getByRole('button',{name:'Confirm selected tipbox'}).count(),0);
        await page.locator('#materials > .row-card').nth(2).getByRole('button',{name:'Mark inspected full (96 tips)'}).click();
        assert.equal(await page.locator('#materials > .row-card').nth(2).getByLabel('Available tips for Tips').inputValue(),'full');
        await page.locator('#materials > .row-card').nth(2).locator('details > summary').click();
        await page.locator('#materials > .row-card').nth(2).getByLabel('Available tips for Tips').fill('A1, H12');
        await page.locator('#materials > .row-card').nth(2).getByLabel('Available tips for Tips').blur();
        await page.locator('#save').click();
        await page.waitForFunction(()=>document.querySelector('#save').disabled);
        assert.deepEqual(session.plan.materials[2].available_tips,['A1','H12'],'Partial rack inventory remains editable');
        await page.locator('#materials > .row-card').nth(2).getByRole('button',{name:'Mark inspected full (96 tips)'}).click();

        await page.locator('#correction-reason').fill('Scientist confirmed 20 µL for this water qualification.');
        await page.locator('[data-path="/steps/0/volume_ul"]').fill('20');
        await page.locator('[data-path="/steps/0/volume_ul"]').blur();
        await page.locator('#tab-review').click();
        assert.equal(await page.locator('#approve').isDisabled(),true);
        await page.locator('#validate').click();
        await page.waitForFunction(()=>document.querySelector('#validation-status').textContent==='Passed');
        assert.equal(session.plan.decisions.at(-1).path,'/steps/0/volume_ul');
        assert.equal(session.plan.decisions.at(-1).value,20);
        assert.ok(session.plan.decisions.some(d=>d.path==='/materials/2/labware_id'&&d.value==='tiprack'));
        assert.ok(session.plan.decisions.some(d=>d.path==='/materials/2/tip_definition_id'&&d.value==='tip250'));
        assert.equal(session.plan.materials[2].available_tips,'full','Full inspected rack is saved as a compact explicit inventory');
        assert.match(await page.locator('#supplies').innerText(),/Inspected full \(96 tips\)/);
        await page.locator('#simulate').click();
        await page.waitForFunction(()=>document.querySelector('#simulation-status').textContent==='Passed');
        await page.locator('#scientist').fill('Test Scientist');
        await page.locator('#approval-notes').fill('Supervised water run before samples.');
        await page.locator('#plan-reviewed').check();
        await page.locator('#deck-confirmed').check();
        await page.locator('#approve').click();
        await page.waitForFunction(()=>document.querySelector('#approval-status').textContent==='Approved');
        assert.equal(await page.locator('#export').isDisabled(),false);
        await page.locator('#publish-name').fill('Water transfer baseline');
        await page.locator('#publish').click();
        await page.locator('#tab-library').click();
        await page.getByRole('button',{name:'Reuse as new draft'}).click();
        await page.waitForFunction(()=>document.querySelector('#save-status').textContent.includes('reused'));
        assert.equal(session.approval,null,'Reusing approved protocol must start a draft');
        assert.equal(await page.locator('#plan-reviewed').isChecked(),false,'Review acknowledgment belongs to the exact session/revision');
        assert.equal(await page.locator('#deck-confirmed').isChecked(),false);
        await page.locator('#setup-name').fill('Working deck');
        await page.locator('#save-setup').click();
        await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('Reusable setup saved'));
        assert.equal(savedSetup.materials.length,3);
        assert.equal(await page.locator('#setup-tipbox-select option').count(),3,'One box can offer multiple tips; incomplete geometry is excluded');
        assert.equal(await page.locator('#setup-tipbox-select').inputValue(),'','Do not default to the first compatible tip');
        assert.match(await page.locator('#setup-tipbox-select').innerText(),/Planning only: tip length missing/);
        await page.locator('#setup-tipbox-candidates summary').click();
        assert.match(await page.locator('#setup-tipbox-candidates').innerText(),/Incomplete catalog tipbox/);
        assert.match(await page.locator('#setup-tipbox-candidates').innerText(),/rack rows and columns, linked tip definition/);
        assert.equal(await page.locator('#setup-tipbox-candidates button, #setup-tipbox-candidates select, #setup-tipbox-candidates input').count(),0,'Candidates cannot be selected as verified choices');

        await page.locator('#setup-tipbox-select').selectOption(JSON.stringify(['tiprack','tip70']));
        assert.match(await page.locator('#setup-tipbox-choices').innerText(),/record the missing tip length/);
        await page.locator('#setup-tipbox-choices').screenshot({path:'/tmp/protocol-assistant-tipbox-choices.png'});
        await page.locator('#add-compatible-tipbox').click();
        const lastMaterial=page.locator('#materials > .row-card').last();
        assert.equal(await lastMaterial.locator('[data-path$="/labware_id"]').inputValue(),'tiprack');
        assert.equal(await lastMaterial.locator('[data-path$="/tip_definition_id"]').inputValue(),'tip70');
        assert.equal(await lastMaterial.locator('[data-path$="/deck_slot"]').inputValue(),'','Adding compatibility choice must not invent deck placement');
        await page.locator('#save').click();
        await page.waitForFunction(()=>document.querySelector('#save').disabled);
        const selectedRack=session.plan.materials.at(-1);
        assert.equal(selectedRack.tip_definition_id,'tip70','Save retains the selected tip instead of the other tip for this box');
        assert.equal(selectedRack.deck_slot,null);
        assert.equal(selectedRack.available_tips,null);
        assert.ok(session.setup.tip_rack_ids.includes(selectedRack.id));
        assert.equal(session.validation,null);
        tipboxChoices=[];tipboxReason='No catalog-verified tipbox for active head ST.';
        await page.reload();
        await page.waitForFunction(()=>document.querySelector('#session-name').textContent==='Water transfer');
        await page.locator('#tab-plan').click();
        assert.match(await page.locator('#setup-tipbox-choices').innerText(),/No catalog-verified tipbox/);
        assert.equal(await page.locator('#setup-tipbox-select').count(),0);
        assert.match(await page.locator('#setup-tipbox-candidates').innerText(),/Catalog candidates to complete/);
        assert.equal(await page.locator('#setup-tipbox-candidates').getAttribute('open'),'');

        // Proposed stacks remain easy to inspect before the strict validator
        // accepts their catalog geometry and top-to-bottom move sequence.
        await page.locator('[data-path="/materials/0/stack_order"]').fill('0');
        await page.locator('[data-path="/materials/0/stack_order"]').blur();
        await page.locator('#add-material').click();
        const upper=page.locator('#materials > .row-card').last();
        await upper.locator('[data-path$="/name"]').fill('Upper source plate');
        await upper.locator('[data-path$="/name"]').blur();
        await upper.locator('[data-path$="/labware_id"]').selectOption('plate');
        await upper.locator('[data-path$="/deck_slot"]').fill('1');
        await upper.locator('[data-path$="/deck_slot"]').blur();
        await upper.locator('[data-path$="/stack_order"]').fill('1');
        await upper.locator('[data-path$="/stack_order"]').blur();
        await page.locator('#tab-review').click();
        const firstSlot=page.locator('#deck .deck-slot').first();
        assert.match(await firstSlot.innerText(),/Load bottom → top/);
        assert.match(await firstSlot.innerText(),/Water reservoir.*Level 0.*Upper source plate.*Level 1/s);
        assert.equal(await page.locator('[data-path="/setup/tip_disposal_id"] option[value="return_to_source_rack"]').count(),0,
            'Spent-tip return is offered only with a fresh set per source.');

        await page.screenshot({path:'/tmp/protocol-assistant-browser.png',fullPage:true});
        session={...makeSession('chat-session'),source:{source_id:'chat-source',name:'Designer chat',metadata:{parser:'chat'},paragraphs:[{id:'p1',text:'Transfer water into the destination plate.'},{id:'p2',text:'Inspect the plate.'}]},plan:fresh(plan),setup:fresh(setup)};
        await page.goto('http://protocol-assistant.test/protocol-assistant?session=chat-session');
        assert.equal(await page.locator('#tab-plan').getAttribute('aria-selected'),'true','Designer chat sessions open on Setup');
        assert.equal(await page.locator('#tab-source').count(),0);
        assert.equal(await page.locator('#source-panel').isVisible(),false,'Source evidence is optional in chat review');
        await page.locator('#steps .citations button').first().click();
        assert.equal(await page.locator('#source-details').getAttribute('open'),'');
        await page.locator('#source-p1').waitFor({state:'visible'});
        await page.locator('#new-session').click();
        assert.equal(await page.locator('#tab-plan').getAttribute('aria-selected'),'true');
        assert.equal(await page.locator('#source-details').getAttribute('open'),'','New document drafts open the optional drawer');
        assert.equal(await page.locator('#session-status').innerText(),'No protocol loaded');
        assert.deepEqual(errors,[],'UI must not throw browser errors');
        console.log(`Protocol Assistant browser flow passed (${requests.length} mocked API requests).`);
    }catch(error){console.error('Browser errors:',errors);console.error('Notice:',await page.locator('#notice').innerText());await page.screenshot({path:'/tmp/protocol-assistant-failure.png',fullPage:true});throw error;}finally{await browser.close();}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
