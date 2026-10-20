import test from 'node:test';import assert from 'node:assert/strict';
import {moduleName,MODULES} from '../scripts/jahead-modules.mjs';
import {taskRenderer} from '../scripts/jahead-task-api.mjs';
import {resourceSpec,resourceRenderer} from '../scripts/jahead-resources.mjs';
import {parseArgs} from '../scripts/jahead-desktop.mjs';
const mod=source=>'data:text/javascript,'+encodeURIComponent(source);
test('four task modules and Chinese aliases are separate',()=>{
 for(const name of ['create','developer','report','search'])assert.ok(MODULES[name]);
 assert.equal(moduleName('创作'),'create');assert.equal(moduleName('开发'),'developer');assert.equal(parseArgs(['submit','--module','developer','--stage','coding']).opts.stage,'coding');
});
test('resource CRUD stays on the documented endpoint and requires exact identities',()=>{
 assert.equal(resourceSpec('knowledge','update','kb1',{title:'new'}).method,'PUT');
 assert.equal(resourceSpec('hardware','delete','dev1',{}).url,'/api/v1/hardware/devices/dev1');
 assert.equal(resourceSpec('skills','create',null,{}).upload,true);
 assert.equal(resourceSpec('experts','create-assistant','expert1',{}).targetModule,'create');
 assert.throws(()=>resourceSpec('experts','delete','official',{}));
 assert.throws(()=>resourceSpec('hardware','create',null,{}));
 assert.throws(()=>resourceSpec('knowledge','create',null,{title:'no scope'}));
 for(const id of ['../escape','..','/','x?secret'])assert.throws(()=>resourceSpec('knowledge','get',id));
});
test('search native intent-parse task is not created again; auth stays in the renderer',async()=>{
 const saved={request:globalThis.__testRequest,EventSource:globalThis.EventSource};const requests=[];
 globalThis.__testRequest=async a=>{requests.push(a);return{data:{success:true,data:{task_id:'task1',search_plan:{topic:'test'}}}}};
 let url;globalThis.EventSource=class{constructor(u){url=u;}close(){}};
 const api=mod('export const t={request:a=>globalThis.__testRequest(a)};export const n=()=>"TEST_AUTH";export const i=x=>x;');
 try{const r=await taskRenderer('search',{api,export:'t'},'submit',{prompt:'test'});assert.equal(r.submitted,true);assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/search/intent-parse');assert.ok(url.includes('TEST_AUTH'));assert.ok(!JSON.stringify(r).includes('TEST_AUTH'));}finally{globalThis.__testRequest=saved.request;globalThis.EventSource=saved.EventSource;delete globalThis.__arcJaheadTasks;}
});
test('search result excludes raw backend traces and yields public answer',async()=>{
 globalThis.__testRequest=async a=>({data:{success:true,data:a.url.endsWith('/result')?{chat_contents:[[{type:'in_thought',content:'HIDDEN'},{type:'text_delta',content:'OK'}]],chat_list:{raw:{secret:'SECRET'}}}:{status:'completed',task_id:'t1'}}});
 const api=mod('export const t={request:a=>globalThis.__testRequest(a)}');
 try{const r=await taskRenderer('search',{api,export:'t'},'status',{session:'t1',prompt:'test'});assert.equal(r.final_answer,'OK');assert.equal(r.messages[0].content,'test');assert.ok(!JSON.stringify(r).includes('HIDDEN'));assert.ok(!JSON.stringify(r).includes('SECRET'));}finally{delete globalThis.__testRequest;}
});
test('developer restart reads persisted reply; never exports raw reasoning chunks',async()=>{
 const api=mod(`export const R=false;export const N=()=>({});export const a3=async()=>({records:[{record_id:'r1',status:'success',reply:'CODE_OK',question:'build',raw_chunks:[{type:'in_thought',text:'PRIVATE'}]}]});`);
 const r=await taskRenderer('developer',{api},'status',{session:'c1',project:'p1',record:'r1',stage:'coding'});
 assert.equal(r.status,'completed');assert.equal(r.final_answer,'CODE_OK');assert.ok(!JSON.stringify(r).includes('PRIVATE'));
});
test('coding initialization follows start-coding stream, never creates coding conversation directly',async()=>{
 const old=globalThis.fetch;let url;
 globalThis.fetch=async u=>{url=u;return new Response('event: done\ndata: {"success":true,"coding_conversation_id":"coding1"}\n\n')};
 const api=mod(`export const R=false;export const N=projectId=>({projectId,companyId:'c',userId:'u',token:'TEST'});export const S=(c,p)=>'https://native.test'+p;export const X=()=>{throw Error('must not directly create')};`);
 try{const r=await taskRenderer('developer',{api},'prepare',{project:'p1',stage:'coding'});assert.equal(r.session_id,'coding1');assert.ok(url.endsWith('/requirement/sse/start-coding'));}finally{globalThis.fetch=old;}
});
test('resource requests do not export auth fields from service results',async()=>{
 const oldFetch=globalThis.fetch,oldStorage=globalThis.localStorage,oldLocation=globalThis.location;
 globalThis.localStorage={getItem:()=> 'TEST'};globalThis.location={origin:'https://hardware.test'};
 globalThis.fetch=async(u,o)=>{assert.equal(o.headers['X-Auth-Token'],'TEST');return new Response(JSON.stringify({code:0,data:{items:[{id:'d',token:'hidden',name:'device'}]}}))};
 try{const r=await resourceRenderer({authStorage:'tqai_backend_token'},resourceSpec('hardware','list'));assert.deepEqual(r,{items:[{id:'d',name:'device'}]});}finally{globalThis.fetch=oldFetch;globalThis.localStorage=oldStorage;globalThis.location=oldLocation;}
});
