import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { CDP, validateSocket, endpoint, assertCompatible, invocation, selectDesktopWindow } from '../scripts/desktop_cdp.mjs';
import { uuid7, parseArgs, safeInfo, submitWithClient, taskStatus, discoverApp, requestState } from '../scripts/workbuddy-desktop.mjs';

test('only loopback endpoints on the expected port are accepted',()=>{
  assert.equal(endpoint(9335),'http://127.0.0.1:9335');
  assert.throws(()=>endpoint(0));
  assert.throws(()=>validateSocket('ws://example.com:9335/x',9335));
  assert.throws(()=>validateSocket('ws://127.0.0.1:9336/x',9335));
  assert.throws(()=>validateSocket('ws://name:secret@127.0.0.1:9335/x',9335));
  assert.equal(validateSocket('ws://127.0.0.1:9335/x',9335),'ws://127.0.0.1:9335/x');
});

test('multiple windows sharing one profile remain usable, different profiles stay explicit',()=>{
 const info={configDir:'/user/workbuddy',version:'5.8.3',platform:'darwin',visibility:'visible'};
 const a={targetId:'a',info:{...info,focused:false}},b={targetId:'b',info:{...info,focused:true}};
 assert.equal(selectDesktopWindow([a,b]),b);
 b.info.focused=false;assert.equal(selectDesktopWindow([b,a]),a);
 assert.throws(()=>selectDesktopWindow([a,{...b,info:{...info,configDir:'/different-profile'}}]),/不同配置/);
 assert.throws(()=>selectDesktopWindow([{targetId:'a',info:{}},{targetId:'b',info:{}}]),/不同配置/);
 assert.equal(selectDesktopWindow([b]),b);
});

test('unsupported versions and arbitrary methods cannot initiate writes',()=>{
  assertCompatible({version:'5.8.3',modernBridge:true});
  assert.throws(()=>assertCompatible({version:'6.0.0',modernBridge:true}));
  assert.throws(()=>invocation('delete',['session']));
});

test('prompt strings cannot escape into renderer code',async()=>{
  const dangerous="');globalThis.pwned=true;//</script>\n中文";
  let captured;
  const context=vm.createContext({__wbInvoke:async(...args)=>{captured=args;return {ok:true}}});
  await vm.runInContext(invocation('sendPrompt',['session',[{type:'text',text:dangerous}]]),context);
  assert.equal(captured[3][0].text,dangerous);
  assert.equal(context.pwned,undefined);
});

test('request identifiers are valid unique UUIDv7 hex',()=>{
  const ids=Array.from({length:100},uuid7);
  assert.equal(new Set(ids).size,100);
  ids.forEach(id=>assert.match(id,/^[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}$/));
});

test('desktop output is a whitelist and never includes connector credentials',()=>{
  const result=safeInfo({info:{id:'s',title:'task',state:'idle'},configManager:{model:'desktop-default',connectors:[{token:'secret'}],apiKey:'secret'}});
  assert.equal(result.model,'desktop-default');assert.ok(!JSON.stringify(result).includes('secret'));
});

test('Windows app discovery supports explicit exe path with spaces',()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'wb-test-'));
  try {const exe=path.join(root,'WorkBuddy 国内版.exe');fs.writeFileSync(exe,'fixture');assert.equal(discoverApp(exe,'win32').executable,exe);}
  finally{fs.rmSync(root,{recursive:true,force:true})}
});

test('CLI parses quoted argument values without a shell',()=>{
  const {command,opts}=parseArgs(['submit','--workspace','C:/任务 & 数据','--prompt-file','input.txt','--wait']);
  assert.equal(command,'submit');assert.equal(opts.workspace,'C:/任务 & 数据');assert.equal(opts.wait,true);
  assert.throws(()=>parseArgs(['wait','--timeout','-1']));
});

test('idle or unknown native states do not prove task completion',()=>{
  assert.equal(requestState('idle'),null);
  assert.equal(requestState('streaming'),null);
  assert.equal(requestState('error'),'failed');
  assert.equal(requestState('completed'),'completed');
});

test('native create/send is used once and receipts prevent replay',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'wb-submit-'));
  const calls=[];let info,requests=[];
  const context=vm.createContext({__wbInvoke:async(channel,context,...args)=>{
    assert.ok(channel.startsWith('wb:conversations:'));assert.equal(typeof context,'object');
    const method=channel.slice('wb:conversations:'.length);calls.push(method);
    if(method==='create'){info={id:args[0].conversationId,state:'idle',cwd:args[0].cwd,transport:'local'};return {info,configManager:{model:'native',permissionMode:'default'}}}
    if(method==='get')return {info};
    if(method==='requests')return {items:requests};
    if(method==='sendPrompt'){requests=[{id:args[2].clientRequestId,state:'completed',assistantMessage:{content:[{type:'text',text:'OK'}]}}];return {stopReason:'end_turn'}}
  }});
  const client={evaluate:async expression=>await vm.runInContext(expression,context)};
  const opts={workspace:root,'receipt-file':path.join(root,'receipt.json'),'ack-timeout':2};
  try{
    const a=await submitWithClient(client,{version:'5.8.3',modernBridge:true,configDir:root},opts,'hello');
    assert.equal(a.submitted,true);assert.equal(calls.filter(x=>x==='create').length,1);assert.equal(calls.filter(x=>x==='sendPrompt').length,1);
    const b=await submitWithClient(client,{version:'5.8.3',modernBridge:true},opts,'hello');assert.equal(b.reused,true);
    assert.equal(calls.filter(x=>x==='sendPrompt').length,1);
    assert.equal((await taskStatus(client,a.session_id,a.request_id)).status,'completed');
    await assert.rejects(()=>submitWithClient(client,{version:'5.8.3',modernBridge:true},opts,'different'));
  }finally{fs.rmSync(root,{recursive:true,force:true})}
});

test('CDP responses correlate by id and transport errors reject pending calls',async()=>{
  class Socket extends EventTarget {send(text){this.sent=JSON.parse(text)}close(){this.dispatchEvent(new Event('close'))}}
  const socket=new Socket(),client=new CDP(socket);
  const result=client.call('Runtime.evaluate',{},1000);
  socket.dispatchEvent(new MessageEvent('message',{data:JSON.stringify({id:socket.sent.id,result:{value:42}})}));
  assert.deepEqual(await result,{value:42});
  const pending=client.call('Runtime.evaluate',{},1000);client.close();await assert.rejects(pending,/关闭|断开/);
});

// A preload function existing is insufficient: native error envelopes must reject.
test('native wb error envelope cannot masquerade as a created conversation',async()=>{
 const context=vm.createContext({__wbInvoke:async()=>({__wbError:true,code:4001,message:'No handler'})});
 await assert.rejects(()=>vm.runInContext(invocation('list',[]),context),/4001: No handler/);
});
