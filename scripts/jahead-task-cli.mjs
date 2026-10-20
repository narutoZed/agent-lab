import fs from 'node:fs';import path from 'node:path';import os from 'node:os';import crypto from 'node:crypto';
import {connectModule,MODULES,moduleName} from './jahead-modules.mjs';
import {moduleRpc} from './jahead-task-api.mjs';
import {atomicJson} from './desktop_fs.mjs';
import {delay} from './desktop_cdp.mjs';
const terminal=new Set(['completed','failed','cancelled']);
const emit=o=>process.stdout.write(JSON.stringify({observed_at:new Date().toISOString(),...o})+'\n');
export function taskArgs(opts){return {session:opts.session,assistant:opts.assistant,project:opts.project,record:opts.record,stage:opts.stage||'requirement',mode:opts.mode,searchType:opts['search-type'],title:opts.title,limit:Number(opts.limit||20),prompt:opts.prompt}}
export async function submitModule(c,opts,prompt){
 const file=path.resolve(opts['receipt-file']||path.join(os.homedir(),'.jahead-desktop-cli',crypto.randomUUID()+'.json'));
 const args={...taskArgs(opts),prompt},fingerprint=crypto.createHash('sha256').update(JSON.stringify({module:c.module,args})).digest('hex');
 if(fs.existsSync(file)){const prior=JSON.parse(fs.readFileSync(file,'utf8'));if(prior.fingerprint!==fingerprint)throw Error('回执属于另一任务');return {...prior,reused:true};}
 fs.mkdirSync(path.dirname(file),{recursive:true});fs.closeSync(fs.openSync(file+'.lock','wx',0o600));
 const r={schema_version:1,type:'submission.receipt',transport:'jahead_desktop',module:c.module,fingerprint,receipt_file:file,created_at:new Date().toISOString(),submitted:false,phase:'creating',prompt,workspace_binding:'即先云端任务；本地工作区不自动同步'};atomicJson(file,r);
 try{
  if(c.module==='search'){Object.assign(r,await moduleRpc(c,'submit',args));}
  else {
   Object.assign(r,await moduleRpc(c,'prepare',args));r.phase='created';atomicJson(file,r);
   args.session=r.session_id;args.assistant=r.assistant_id||args.assistant;args.project=r.project_id||args.project;
   r.phase='dispatch_requested';atomicJson(file,r);await moduleRpc(c,'send',args);
   const end=Date.now()+Number(opts['ack-timeout']||45)*1000;
   while(Date.now()<end){const state=await moduleRpc(c,'status',args);if(state.record_id){r.record_id=state.record_id;args.record=state.record_id;atomicJson(file,r);}if(state.request_observed){r.submitted=true;r.status=state.status;break;}if(state.error)throw Error(state.error);await delay(500);}
   if(!r.submitted)throw Error('尚未在原生历史中确认请求；不要重发，请按回执查询');
  }
  r.phase='submitted';r.submitted_at=new Date().toISOString();r.selector={session_id:r.session_id};atomicJson(file,r);return r;
 }catch(e){r.phase='dispatch_unknown';r.error=String(e.message).split('\n')[0];atomicJson(file,r);throw Object.assign(e,{receipt:r});}
}
export async function moduleMain(command,opts){
 let c;try{
  opts.module=moduleName(opts.module);
  if(command==='modules'){emit({modules:Object.entries(MODULES).map(([id,c])=>({id,label:c.label,kind:['report','create','developer','search'].includes(id)?'task':'resource'}))});return 0;}
  if(opts['receipt-file']&&command!=='submit'){
   const r=JSON.parse(fs.readFileSync(opts['receipt-file'],'utf8'));for(const[k,v]of Object.entries({session:r.session_id,assistant:r.assistant_id,project:r.project_id,record:r.record_id,stage:r.stage,module:r.module,prompt:r.prompt||r.search_plan?.question}))if(opts[k]===undefined&&v!==undefined)opts[k]=v;
  }
  c=await connectModule(opts);
  if(command==='doctor'||command==='list'||command==='sessions'){
   const result=await moduleRpc(c,command==='sessions'&&c.module==='create'?'sessions':'list',taskArgs(opts));
   emit(command==='doctor'?{ok:true,module:c.module,profile:c.profile,authenticated:true}:{module:c.module,data:result});return 0;
  }
  if(command==='submit'){
   if(c.module==='developer'&&['coding','console'].includes(opts.stage)&&!opts.project)throw Error('编码/控制台任务需要 --project 指定已有项目；先完成需求生成');
   const prompt=fs.readFileSync(opts['prompt-file']||0,'utf8');if(!prompt.trim()||Buffer.byteLength(prompt)>1e6)throw Error('任务正文不能为空且不得超过 1MB');
   const r=await submitModule(c,opts,prompt);emit(r);if(!opts.wait||!r.submitted)return r.submitted?0:3;
   Object.assign(opts,{session:r.session_id,assistant:r.assistant_id,project:r.project_id,record:r.record_id,stage:r.stage||opts.stage});command='wait';
  }
  if(command==='open'){emit(await moduleRpc(c,'open',taskArgs(opts)));return 0;}
  if(!opts.session)throw Error('需要 --session 或 --receipt-file');
  if(command==='status'||command==='cancel'||command==='export'){
   const s=await moduleRpc(c,command,taskArgs(opts));
   if(command==='export'){if(!opts.output)throw Error('需要 --output');atomicJson(path.resolve(opts.output),{type:'jahead.desktop.snapshot',module:c.module,...s},0);emit({exported:path.resolve(opts.output),session_id:s.session_id});}else emit({module:c.module,...s});return 0;
  }
  if(command==='wait'){
   const end=Date.now()+Number(opts.timeout)*1000;let prior;
   while(Date.now()<end){const s=await moduleRpc(c,'status',taskArgs(opts));const key=JSON.stringify(s);if(key!==prior){emit({type:terminal.has(s.status)?'execution.'+s.status:s.status==='needs_input'?'execution.needs_input':'execution.status',module:c.module,...s});prior=key;}if(terminal.has(s.status))return s.status==='completed'?0:4;await delay(Number(opts.interval)*1000);}
   emit({type:'observation.timeout',session_id:opts.session});return 3;
  }
  throw Error('此模块不支持命令 '+command);
 }finally{c?.client.close()}
}
