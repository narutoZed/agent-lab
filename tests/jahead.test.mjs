import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {sanitizeEvent,summarizeSession} from '../scripts/jahead-protocol.mjs';
import {rendererCall,invocation,parseArgs,submit,launchCommand} from '../scripts/jahead-desktop.mjs';
const data={session:{id:'rs_x',status:'ready_to_generate'},messages:[{role:'conversation',status:'completed',task_usage:{token_count:123},message:{user:{content:'task'},assistant:[{type:'text_delta',payload:{payload:{text:'OK'}}},{type:'turn_completed',payload:{text:'OK'}},{type:'done'}]}}]};
test('macOS starts the signed app via LaunchServices and Windows retains native executable',()=>{
 const app={app:'/Users/test/Applications/即先 AI.app',executable:'/Users/test/Applications/即先 AI.app/Contents/MacOS/即先 AI'};
 assert.deepEqual(launchCommand(app,9336,'darwin'),['/usr/bin/open','-a',app.app,'--args','--remote-debugging-port=9336','--remote-debugging-address=127.0.0.1']);
 assert.equal(launchCommand(app,9336,'win32')[0],app.executable);
});
test('idle session with completed round preserves final text without doubling deltas',()=>{
 const s=summarizeSession(data);assert.equal(s.status,'completed');assert.equal(s.final_answer,'OK');assert.equal(s.total_tokens,null);assert.equal(s.billed_token_count,123);
});
test('active task takes priority over older completed round',()=>{
 const s=summarizeSession({...data,session:{...data.session,active_task:{status:'running'}}});assert.equal(s.status,'running');assert.equal(s.final_answer,'');
});
test('only answer and tool metadata leave the renderer',()=>{
 assert.equal(sanitizeEvent({type:'thinking_step_update',data:'private reasoning'}),null);
 assert.deepEqual(sanitizeEvent({type:'tool_exec_started',payload:{tool_name:'search',tool_input:{token:'secret'},metadata:{secret:'x'}}}),{type:'tool_exec_started',at:null,tool_name:'search',is_error:false});
});
test('RPC only permits known methods and serializes hostile prompt as data',()=>{
 assert.throws(()=>invocation('eval',{}));const prompt='${process.exit()} ` \\ "';assert.ok(invocation('start',{prompt}).includes(JSON.stringify(prompt)));assert.throws(()=>parseArgs(['submit','--mode','auto']));assert.throws(()=>parseArgs(['doctor','--port','80']));
});

test('report release is selected from inspected entrypoints only',()=>{
 assert.ok(invocation('sessions',{},'/assets/index-8hWVE1So.js').includes('/assets/index-8hWVE1So.js'));
 assert.ok(invocation('sessions',{},'/assets/index-vlalPs-Y.js').includes('/assets/index-vlalPs-Y.js'));
 assert.throws(()=>invocation('sessions',{},'/assets/unverified.js'),/未经核验/);
});
test('renderer uses existing module headers without returning credentials; status survives restart',async()=>{
 const original=globalThis.fetch;let request;
 globalThis.fetch=async(url,opts)=>{request={url,opts};return new Response(JSON.stringify({stat:1,data}));};
 try {const module='data:text/javascript,export const c=()=>({Authorization:"TEST_ONLY"})';const s=await rendererCall('status',{session:'rs_x'},module,sanitizeEvent,summarizeSession);assert.equal(s.final_answer,'OK');assert.equal(request.opts.headers.Authorization,'TEST_ONLY');assert.ok(!JSON.stringify(s).includes('TEST_ONLY'));}finally{globalThis.fetch=original;}
});
test('submission receipt is persistent, idempotent and locks uncertain writes',async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'jahead-test-')),opts={mode:'expert','web-search':'false','receipt-file':path.join(dir,'receipt.json')};let writes=0;
 const client={evaluate:async expression=>{if(expression.includes(')("create",')){writes++;return{session_id:'rs_test'};}if(expression.includes(')("status",'))return{request_observed:true};return{phase:'dispatch_requested'};}};
 try {const r=await submit(client,opts,'hello');assert.equal(r.submitted,true);assert.equal((await submit(client,opts,'hello')).reused,true);assert.equal(writes,1);await assert.rejects(submit(client,opts,'different'));const failed={...opts,'receipt-file':path.join(dir,'uncertain.json')};await assert.rejects(submit({evaluate:async()=>{throw Error('connection lost')}},failed,'hello'));const retry=await submit(client,failed,'hello');assert.equal(retry.submitted,false);assert.equal(writes,1);}finally{fs.rmSync(dir,{recursive:true,force:true});}
});
