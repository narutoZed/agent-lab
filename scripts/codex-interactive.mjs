#!/usr/bin/env node
/** Codex app-server client with the user's task-scoped interaction policy. */
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import { setTimeout as delay } from 'node:timers/promises';
import { codexDecision } from './interaction_policy.mjs';

const emit=o=>process.stdout.write(JSON.stringify(o)+'\n');
const opts={questions:'recommended',permissions:'approve',sandbox:'workspace-write',timeout:900};
const args=process.argv.slice(2);
for(let i=0;i<args.length;i+=2){if(!args[i].startsWith('--')||args[i+1]===undefined)throw Error('参数需 --name value');opts[args[i].slice(2)]=args[i+1]}
if(!opts['server-argv']||!opts.workspace)throw Error('需要 --server-argv JSON 和 --workspace');
if(!['recommended','manual'].includes(opts.questions)||!['approve','manual'].includes(opts.permissions))throw Error('交互策略无效');
if(!['read-only','workspace-write'].includes(opts.sandbox))throw Error('sandbox 仅支持 read-only/workspace-write');
const command=JSON.parse(opts['server-argv']);if(!Array.isArray(command)||!command.length||command.some(x=>typeof x!=='string'))throw Error('server-argv 无效');
const prompt=fs.readFileSync(0,'utf8'),workspace=path.resolve(opts.workspace);
const child=spawn(command[0],command.slice(1),{cwd:workspace,stdio:['pipe','pipe','pipe'],shell:false});
let seq=0,threadId,turnId,finished=false,interrupted=false,lastAnswer='',usage={},fatal;
const pending=new Map(),unanswered=new Map(),seenRequests=new Set();
const root=opts.output?path.dirname(path.resolve(opts.output)):workspace;
const pendingFile=path.join(root,'pending-input.json'),answersFile=path.join(root,'manual-responses.json');
function send(o){child.stdin.write(JSON.stringify(o)+'\n')}
function persistPending(){fs.writeFileSync(pendingFile,JSON.stringify([...unanswered.values()],null,2))}
function request(method,params,timeout=60000){const id=`arc-${++seq}`;return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{pending.delete(id);reject(Error(`${method} 超时`))},timeout);pending.set(id,{resolve,reject,timer});send({id,method,params})})}
function answer(id,response,kind,method){send({id,result:response});emit({type:'interaction.resolved',platform:'codex',session_id:threadId,request_id:id,method,kind,policy:'user_authorized',response,at:new Date().toISOString()})}
function onMessage(m){
  if(m.id!==undefined&&!m.method){const p=pending.get(m.id);if(p){clearTimeout(p.timer);pending.delete(m.id);m.error?p.reject(Error(m.error.message)):p.resolve(m.result)}return;}
  const p=m.params||{};
  if(m.id!==undefined&&m.method){
    if(seenRequests.has(m.id))return;seenRequests.add(m.id);
    const decision=codexDecision(m.method,p,opts,threadId);
    if(decision){answer(m.id,decision.response,decision.kind,m.method)}
    else{unanswered.set(String(m.id),{id:m.id,method:m.method,params:p});persistPending();emit({type:'execution.needs_input',request_id:m.id,method:m.method,pending_file:pendingFile})}
    return;
  }
  if(p.threadId&&p.threadId!==threadId)return;
  if(m.method==='serverRequest/resolved'){unanswered.delete(String(p.requestId));persistPending();return;}
  if(m.method==='turn/started'){turnId=p.turn?.id||turnId;emit({type:'turn.started',turn_id:turnId});return;}
  if(m.method==='thread/tokenUsage/updated'){
    const u=p.tokenUsage?.total;if(u)usage={input_tokens:u.inputTokens,output_tokens:u.outputTokens,total_tokens:u.totalTokens,cached_input_tokens:u.cachedInputTokens,reasoning_output_tokens:u.reasoningOutputTokens};return;
  }
  if(['item/started','item/completed'].includes(m.method)){
    const item=p.item||{},type={agentMessage:'agent_message',commandExecution:'command_execution',fileChange:'file_change',mcpToolCall:'mcp_tool_call',webSearch:'web_search'}[item.type];
    if(type){
      const normalized={id:item.id,type,status:item.status,text:item.text,command:item.command,aggregated_output:item.aggregatedOutput,exit_code:item.exitCode,name:item.tool};
      if(type==='agent_message'&&m.method==='item/completed'&&item.phase!=='commentary')lastAnswer=item.text||lastAnswer;
      emit({type:m.method.replace('/','.'),item:normalized});
    }return;
  }
  if(m.method==='turn/completed'){
    finished=true;const status=p.turn?.status;
    if(opts.output)fs.writeFileSync(path.resolve(opts.output),lastAnswer,'utf8');
    emit({type:status==='completed'?'turn.completed':status==='interrupted'?'execution.cancelled':'turn.failed',usage,status,error:p.turn?.error});
    if(status!=='completed')process.exitCode=4;
  }
}
const lines=readline.createInterface({input:child.stdout});lines.on('line',line=>{try{onMessage(JSON.parse(line))}catch(error){fatal=error;emit({type:'cli.error',error:error.message})}});
child.stderr.on('data',chunk=>process.stderr.write(chunk));
child.on('error',error=>{fatal=error});
child.on('exit',code=>{if(!finished){fatal??=Error(`app-server 提前退出 ${code}`)}for(const p of pending.values()){clearTimeout(p.timer);p.reject(fatal||Error('app-server 已退出'))}pending.clear()});
for(const signal of ['SIGTERM','SIGINT'])process.on(signal,()=>{interrupted=true});
try{
  await request('initialize',{clientInfo:{name:'agent_run_compare',title:'Agent Run Compare',version:'3.1.0'},capabilities:{experimentalApi:true}});
  send({method:'initialized',params:{}});
  const started=await request('thread/start',{cwd:workspace,approvalPolicy:'on-request',sandbox:opts.sandbox,...opts.model?{model:opts.model}:{}});
  threadId=started.thread.id;emit({type:'thread.started',thread_id:threadId});
  const turn=await request('turn/start',{threadId,input:[{type:'text',text:prompt}]});turnId=turn.turn.id;
  const end=Date.now()+Number(opts.timeout)*1000;
  while(!finished&&!fatal&&!interrupted&&Date.now()<end){
    if(unanswered.size&&fs.existsSync(answersFile)){
      let responses;try{responses=JSON.parse(fs.readFileSync(answersFile,'utf8'))}catch{}
      if(responses&&typeof responses==='object')for(const [key,p] of unanswered){
        if(Object.hasOwn(responses,key)){send({id:p.id,result:responses[key]});unanswered.delete(key);emit({type:'interaction.resolved',platform:'codex',kind:'manual',request_id:p.id});persistPending()}
      }
    }
    await delay(100);
  }
  if(fatal)throw fatal;
  if(!finished){if(threadId&&turnId)await request('turn/interrupt',{threadId,turnId},5000).catch(()=>{});emit({type:'observation.timeout',note:'已请求停止本实验轮次'});process.exitCode=3}
}catch(error){emit({type:'cli.error',error:error.message});process.exitCode=2}
finally{lines.close();child.stdin.end();child.kill();for(const p of pending.values())clearTimeout(p.timer)}
