/** Integrated suggestion/review checks. Isolated browser and mocked HTTP only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const html = fs.readFileSync(path.join(__dirname,'../../frontend/protocol_assistant.html'),'utf8');
const copy = value => JSON.parse(JSON.stringify(value));
const plan = {name:'Stacked plate additions',materials:[
    {id:'diluent',name:'Diluent reservoir',role:'liquid',labware_id:'plate'},
    {id:'sample',name:'Sample plate',role:'liquid',labware_id:'plate'},
    {id:'target',name:'Assay plate 1',role:'liquid',labware_id:'plate'}],steps:[
    {id:'diluent-transfer',kind:'transfer',source:'diluent',destination:'target',volume_ul:5,source_anchor:'A1',destination_anchor:'A1',source_paragraph_ids:['p1']},
    {id:'sample-transfer',kind:'transfer',source:'sample',destination:'target',volume_ul:5,source_anchor:'A1',destination_anchor:'A1',source_paragraph_ids:['p1']}],questions:[],decisions:[]};
let session=null,pending=null,proposalCount=0,requests=[];
(async()=>{
    const browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'chrome',headless:true});
    const page=await browser.newPage({viewport:{width:1280,height:720}});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.route('http://agent-workspace.test/**',async route=>{
        const req=route.request(),url=new URL(req.url()),endpoint=url.pathname.replace('/api/protocols','');
        if(url.pathname==='/protocol-assistant')return route.fulfill({contentType:'text/html',body:html});
        const body=req.postData()?JSON.parse(req.postData()):null;
        requests.push({path:url.pathname,body});
        let data;
        if(endpoint==='/context')data={head_type:'HT_384_D_70',labware:[{id:'plate',name:'384-well plate',rows:16,cols:24}],tip_definitions:[],liquid_classes:[],tipbox_choices:[]};
        else if(endpoint==='/agent-status')data={available:true,model:'qwen',base_url:'http://sparky.local:8000/v1'};
        else if(endpoint==='/capabilities')data={context_hash:'fixture',assistant_operations:[],setup_options:{}};
        else if(endpoint==='/methods')data={methods:[]};
        else if(endpoint==='/setups'||endpoint==='/library')data={items:[]};
        else if(endpoint==='/setup-recommendations')data={recommendations:[],unresolved:[]};
        else if(endpoint==='/agent-tasks')data={tasks:[{id:'transfer',availability:'available',description:'Transfer liquid.'}],skills:[{id:'stacked-plate-processing'}]};
        else if(endpoint==='/agent-chat'){
            if(body.message==='Keep this as a draft'){
                assert.equal(body.proposal_id,pending.proposal_id);
                pending={...copy(pending),proposal_id:'suggestion-followup',reply:'This remains an unreviewed suggestion.'};
                pending.candidate.chat_messages.push({role:'user',content:body.message},{role:'assistant',content:pending.reply});
                return route.fulfill({contentType:'application/x-ndjson',body:JSON.stringify({type:'suggestion:ready',proposal:pending})+'\n'});
            }
            if(body.message==='What head is configured?'){
                session.chat_messages.push({role:'user',content:body.message},{role:'assistant',content:'The configured head is a 384-channel disposable-tip head.'});
                return route.fulfill({contentType:'application/x-ndjson',body:JSON.stringify({type:'assistant:reply',session})+'\n'});
            }
            proposalCount++;
            const candidate=session?copy(session):{id:'stack-demo',revision:0,source:{source_id:'source',paragraphs:[{id:'p1',text:body.message}]},selected_paragraph_ids:['p1'],plan:copy(plan),setup:{},chat_messages:[],history:[],approval:null,simulation:null};
            candidate.revision++;
            candidate.chat_messages.push({role:'user',content:body.message},{role:'assistant',content:'Proposed additions for review.'});
            if(session)candidate.setup={...candidate.setup,tip_strategy:'reuse_all',tip_reuse_reason:body.message};
            pending={proposal_id:'suggestion-'+proposalCount,base_revision:session?.revision??null,candidate,reply:'Proposed additions for review.'};
            const events=[{type:'turn:start',message:'Reading request'},
                {type:'tool:start',tool:'inspect_protocol',label:'Reading your protocol'},
                {type:'tool:complete',tool:'inspect_protocol',label:'Reading your protocol',status:'done'},
                {type:'draft:preview',plan:candidate.plan,setup:candidate.setup},
                {type:'suggestion:ready',proposal:pending}];
            return route.fulfill({contentType:'application/x-ndjson',body:events.map(event=>JSON.stringify(event)).join('\n')+'\n'});
        }else if(endpoint.startsWith('/chat-proposals/')&&req.method()==='GET')data=pending;
        else if(endpoint.endsWith('/accept')){session=copy(pending.candidate);data={session};}
        else if(endpoint.endsWith('/reject'))data={status:'rejected'};
        else if(endpoint==='/stack-demo'&&req.method()==='PATCH'){session={...session,...body,revision:session.revision+1};data=session;}
        else if(endpoint==='/stack-demo')data=session;
        else throw Error('Unhandled endpoint '+endpoint);
        await route.fulfill({json:copy(data)});
    });
    try{
        await page.goto('http://agent-workspace.test/protocol-assistant');
        await page.locator('#agent-example').click();
        assert.match(await page.locator('#agent-message').inputValue(),/stack of two 384/);
        await page.locator('#agent-send').click();
        await page.locator('#agent-accept').waitFor();
        assert.equal(session,null,'A suggestion cannot create a saved session before acceptance');
        assert.equal(await page.locator('#agent-plan-panel').isVisible(),true,'The proposed tasks are visible before acceptance');
        assert.equal(await page.locator('#agent-plan-title').innerText(),'Suggested protocol');
        assert.equal(await page.locator('#no-plan').isVisible(),false,'Pending cards replace the empty-plan message');
        const sendBounds=await page.locator('#agent-send').boundingBox();
        assert(sendBounds.y+sendBounds.height<=720,'Send must remain visible at laptop height');
        const firstCardBounds=await page.locator('#agent-plan-cards > article').first().boundingBox();
        assert(firstCardBounds.y<600,'Protocol steps must be visible above the fold');
        assert.match(await page.locator('#agent-activity').innerText(),/Reading your protocol/);
        await page.locator('#agent-message').fill('Keep this as a draft');
        assert.equal(await page.locator('#agent-send').isEnabled(),true,'A pending suggestion allows conversational replies');
        await page.locator('#agent-send').click();
        await page.waitForFunction(()=>location.search.includes('suggestion-followup'));
        assert.equal(session,null);
        await page.reload();
        await page.locator('#agent-accept').waitFor();
        assert.equal(session,null,'A pending suggestion resumes without committing it');
        await page.locator('#agent-reject').click();
        await page.locator('#agent-suggestion').waitFor({state:'hidden'});
        assert.equal(session,null);
        await page.locator('#agent-example').click();
        await page.locator('#agent-send').click();
        await page.locator('#agent-accept').click();
        await page.locator('#agent-plan-panel').waitFor();
        assert.equal(await page.locator('#agent-plan-cards > article').count(),2);
        assert.equal(await page.locator('#agent-plan-cards .well-grid i').count(),4*384);
        await page.waitForFunction(()=>document.querySelector('#session-status').textContent.includes('Revision 1'));
        assert.match(await page.locator('#session-status').innerText(),/Revision 1/);
        await page.locator('#agent-message').fill("Use the same tips, I don't care about carryover.");
        await page.locator('#agent-send').click();
        await page.locator('#agent-accept').waitFor();
        assert.equal(session.setup.tip_strategy,undefined);
        assert.match(await page.locator('#agent-suggestion').innerText(),/Suggested changes/);
        await page.locator('#agent-accept').click();
        await page.waitForFunction(()=>document.querySelector('#session-status').textContent.includes('Revision 2'));
        assert.equal(session.setup.tip_strategy,'reuse_all');
        const revision=session.revision;
        await page.locator('#agent-message').fill('What head is configured?');
        await page.locator('#agent-send').click();
        await page.waitForFunction(()=>document.querySelector('#agent-transcript').textContent.includes('384-channel disposable-tip head'));
        assert.equal(session.revision,revision,'Answering a question does not revise the protocol');
        assert.equal(await page.locator('#agent-suggestion').isVisible(),false);
        assert.equal(await page.locator('#agent-plan-cards input[type=number]').count(),2,'Volume fields are directly editable');
        await page.getByText('Available Bravo tasks',{exact:true}).click();
        await page.locator('#agent-load-tasks').click();
        await page.waitForFunction(()=>document.querySelector('#agent-tasks').textContent.includes('transfer'));
        assert.match(await page.locator('#agent-tasks').innerText(),/transfer/);
        assert(!requests.some(req=>/execute|approve|\/api\/move/.test(req.path)));
        await page.screenshot({path:'/tmp/pybravo-agent-interactive.png',fullPage:false});
        await page.setViewportSize({width:390,height:844});
        const overflow=await page.evaluate(()=>[...document.querySelectorAll('body *')].filter(node=>{const r=node.getBoundingClientRect();return r.width&&r.right>innerWidth+1;}).slice(0,12).map(node=>({tag:node.tagName,id:node.id,class:node.className,right:node.getBoundingClientRect().right})));
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),true,'Mobile overflow: '+JSON.stringify(overflow));
        assert.deepEqual(errors,[]);
        console.log('Interactive tool agent workspace: propose, reject, accept, edit tip policy, discover tasks, responsive layout passed.');
    }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
