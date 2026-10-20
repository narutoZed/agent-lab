#!/usr/bin/env node
/** Jahead desktop CLI. Reuses the authenticated report frame; no credentials leave it. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {CDP,endpoint,delay} from './desktop_cdp.mjs';
import {atomicJson} from './desktop_fs.mjs';
import {sanitizeEvent,summarizeSession} from './jahead-protocol.mjs';
import {connectModule,moduleName,MODULES} from './jahead-modules.mjs';
import {moduleMain} from './jahead-task-cli.mjs';
import {resourceMain} from './jahead-resources.mjs';
export const REPORT_ORIGIN='https://proj-prod-6a436e5d0b40a02275e7428a.site.jahead.com';
export const MODULE_PATH=MODULES.report.entry;
const TERMINAL=new Set(['completed','failed','cancelled']);
const emit=x=>process.stdout.write(JSON.stringify({observed_at:new Date().toISOString(),...x})+'\n');
const cleanError=e=>String(e?.message||e).replace(/([?&](?:tqai_token|token|auth|key)=)[^\s&#]+/gi,'$1[REDACTED]');

export function discoverApp(explicit,platform=process.platform,env=process.env) {
  const candidates=explicit?[path.resolve(explicit)]:platform==='darwin'?[path.join(os.homedir(),'Applications/即先 AI.app'),'/Applications/即先 AI.app']:platform==='win32'?[
    path.join(env.LOCALAPPDATA||'','Programs/即先 AI/即先 AI.exe'),path.join(env.LOCALAPPDATA||'','Programs/jahead/即先 AI.exe'),
    path.join(env.PROGRAMFILES||'C:/Program Files','即先 AI/即先 AI.exe')]:[];
  for(const app of candidates){const executable=app.endsWith('.app')?path.join(app,'Contents/MacOS/即先 AI'):app;if(fs.existsSync(executable))return{app,executable};}return null;
}

export function launchCommand(app,port,platform=process.platform) {
  const flags=[`--remote-debugging-port=${port}`,'--remote-debugging-address=127.0.0.1'];
  // LaunchServices gives the signed app its normal macOS process/permission identity.
  // Running its Mach-O directly makes it inherit the invoking terminal's attribution.
  return platform==='darwin'?['/usr/bin/open','-a',app.app,'--args',...flags]:[app.executable,...flags];
}

export async function connectDesktop(opts) {
  const c=await connectModule({...opts,module:'report'});
  c.client.reportModulePath=c.config.entry;
  return {...c,profile:'jahead-report:'+c.config.entry,transport:'desktop_authenticated_report_frame'};
}

// This function runs entirely inside the existing report frame. Header values stay there.
export async function rendererCall(method,args,modulePath,sanitize,summarize) {
  const mod=await import(modulePath);
  if(typeof mod.c!=='function')throw Error('报告接口不兼容');
  const api='/api/ai-reporting';
  const request=async(method,url,body)=>{
    const r=await fetch(url,{method,headers:mod.c({'Content-Type':'application/json'}),...(body?{body:JSON.stringify(body)}:{}),redirect:'error'});
    if(!r.ok)throw Error('报告接口 HTTP '+r.status);
    const obj=await r.json();
    if(obj.stat!==undefined&&obj.stat!==1||obj.success===false||obj.code!==undefined&&![0,200,'0'].includes(obj.code)||obj.err!==undefined&&![0,'0'].includes(obj.err))throw Error('报告接口拒绝请求');
    return obj.data??obj;
  };
  if(method==='sessions') {
    const obj=await request('GET',api+'/report-sessions?page=1&page_size='+args.limit);
    return {items:(obj.items||[]).map(s=>({session_id:s.id||s._id||s.session_id,title:s.title,status:s.status})),total:obj.total};
  }
  if(method==='create') {
    const obj=await request('POST',api+'/report-sessions',{mode:args.mode,title:args.title,metadata:{scene:'REPORT_GENERATION'}});
    return {session_id:obj.id||obj._id||obj.session_id};
  }
  const id=encodeURIComponent(args.session);
  if(method==='status') {
    const obj=await request('GET',api+'/report-sessions/'+id+'?message_limit=100');
    const summary=summarize(obj,sanitize),op=globalThis.__arcJaheadOps?.[args.session];
    if(op) {
      summary.stream_phase=op.phase;summary.stream_event_count=op.events.length;
      // Persistent history is authoritative. Use the live stream while history is lagging.
      if(op.events.length>summary.events.length)summary.events=op.events;
      if(op.events.some(e=>e.status==='needs_input')&&!TERMINAL_STATUS(summary.status))summary.status='needs_input';
      if(op.error)summary.stream_error=op.error;
    }
    return summary;
  }
  if(method==='start') {
    const store=globalThis.__arcJaheadOps??=Object.create(null);
    if(store[args.session])throw Error('该会话已经发送过，禁止重发');
    const op=store[args.session]={phase:'dispatch_requested',events:[]};
    (async()=>{
      const r=await fetch(api+'/report-sessions/'+id+'/generate/stream',{method:'POST',headers:mod.c({'Content-Type':'application/json',Accept:'text/event-stream'}),body:JSON.stringify({user_query:args.prompt.trim(),web_search:args.webSearch,max_sources:null,scene:'REPORT_GENERATION',with_connection_engine:false}),redirect:'error'});
      if(!r.ok)throw Error('生成接口 HTTP '+r.status);
      op.phase='accepted';
      const reader=r.body.getReader(),decoder=new TextDecoder();let buffer='';
      const consume=block=>{const data=block.split(/\r?\n/).filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trimStart()).join('\n');if(!data||data==='[DONE]')return;try{const e=sanitize(JSON.parse(data));if(e)op.events.push(e)}catch{op.parse_error=true}};
      try {for(;;){const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let m;while((m=/\r?\n\r?\n/.exec(buffer))){consume(buffer.slice(0,m.index));buffer=buffer.slice(m.index+m[0].length)}if(buffer.length>4_000_000)throw Error('流式单条事件过大');}if(buffer.trim())consume(buffer);}finally{reader.releaseLock();}
      op.phase='stream_closed';window.dispatchEvent(new CustomEvent('report-session-created',{detail:{sessionId:args.session}}));
    })().catch(e=>{op.phase='stream_error';op.error=String(e?.message||'流式连接失败')});
    return {phase:op.phase};
  }
  if(method==='cancel'){await request('POST',api+'/report-sessions/'+id+'/generate/cancel',args.task?{task_id:args.task}:{});return {type:'cancel.requested',session_id:args.session};}
  if(method==='open'){setTimeout(()=>{location.hash='/report/chat/'+id},100);return {type:'open.requested',session_id:args.session};}
  throw Error('不支持的方法');
  function TERMINAL_STATUS(s){return ['completed','failed','cancelled'].includes(s);}
}
export function invocation(method,args,modulePath=MODULE_PATH) {
  if(!['sessions','create','start','status','cancel','open'].includes(method))throw Error('CLI 不提供该方法');
  if(!MODULES.report.entries.includes(modulePath))throw Error('未经核验的报告前端版本');
  return `(${rendererCall.toString()})(${JSON.stringify(method)},${JSON.stringify(args)},${JSON.stringify(modulePath)},${sanitizeEvent.toString()},${summarizeSession.toString()})`;
}
export const rpc=(client,method,args)=>client.evaluate(invocation(method,args,client.reportModulePath||MODULE_PATH),30000);

export async function submit(client,opts,prompt) {
  const receiptFile=path.resolve(opts['receipt-file']||path.join(os.homedir(),'.jahead-desktop-cli',crypto.randomUUID()+'.json'));
  const fingerprint=crypto.createHash('sha256').update(JSON.stringify({prompt,mode:opts.mode,webSearch:opts['web-search'],title:opts.title})).digest('hex');
  if(fs.existsSync(receiptFile)){const r=JSON.parse(fs.readFileSync(receiptFile,'utf8'));if(r.fingerprint!==fingerprint)throw Error('回执属于另一个任务，不能覆盖');return{...r,reused:true};}
  fs.mkdirSync(path.dirname(receiptFile),{recursive:true});const lock=fs.openSync(receiptFile+'.lock','wx',0o600);fs.closeSync(lock);
  const r={schema_version:1,type:'submission.receipt',transport:'jahead_desktop',profile:'jahead-report:'+(client.reportModulePath||MODULE_PATH),submitted:false,phase:'creating',fingerprint,receipt_file:receiptFile,created_at:new Date().toISOString(),mode:opts.mode,web_search:opts['web-search']==='true',workspace_binding:'即先报告运行在云端，不绑定本地 workspace；本地文件不会自动上传'};
  atomicJson(receiptFile,r);
  try {
    const made=await rpc(client,'create',{mode:opts.mode,title:opts.title||'CLI 评测任务'});
    if(!made.session_id)throw Error('创建接口未返回 session_id');
    r.session_id=made.session_id;r.phase='dispatch_requested';atomicJson(receiptFile,r);
    await rpc(client,'start',{session:r.session_id,prompt,webSearch:r.web_search});
    const end=Date.now()+Number(opts['ack-timeout']||30)*1000;
    while(Date.now()<end){const s=await rpc(client,'status',{session:r.session_id});if(s.request_observed){r.submitted=true;r.phase='submitted';r.submitted_at=new Date().toISOString();r.selector={session_id:r.session_id};atomicJson(receiptFile,r);return r;}if(s.stream_error)throw Error(s.stream_error);await delay(500);}
    throw Error('提交尚未得到会话记录确认，请 status 核查，勿重发');
  }catch(e){r.phase=r.session_id?'dispatch_unknown':'create_unknown';r.error=cleanError(e);atomicJson(receiptFile,r);throw Object.assign(e,{receipt:r});}
}

async function waitTask(client,opts) {
  const end=Date.now()+Number(opts.timeout)*1000;let last;
  while(Date.now()<end){const s=await rpc(client,'status',{session:opts.session});const key=JSON.stringify(s);if(key!==last){emit({type:TERMINAL.has(s.status)?'execution.'+s.status:s.status==='needs_input'?'execution.needs_input':'execution.status',...s});last=key;}if(TERMINAL.has(s.status))return s;await delay(Number(opts.interval)*1000);}
  emit({type:'observation.timeout',session_id:opts.session,note:'停止等待，不自动停止即先任务'});return null;
}
export function parseArgs(argv) {
  const [command='help',...rest]=argv,opts={port:9336,mode:'expert','web-search':'true',timeout:900,interval:2};
  const keys=new Set(['port','app','target','mode','web-search','timeout','interval','ack-timeout','title','prompt-file','receipt-file','session','task','output','limit','workspace','module','stage','project','assistant','record','search-type','action','id','data-file','file']);
  for(let i=0;i<rest.length;i++){const k=rest[i].slice(2);if(['--wait','--dry-run'].includes(rest[i])){opts[k]=true;continue;}if(!rest[i].startsWith('--')||!keys.has(k)||!rest[i+1]||rest[i+1].startsWith('--'))throw Error('无效或缺值的参数 '+rest[i]);opts[k]=rest[++i];}
  endpoint(opts.port);if(opts['search-type']&&!['full_web_search','single_source_direct','multi_source_direct'].includes(opts['search-type']))throw Error('search-type 不受支持');if(!['fast','expert'].includes(opts.mode)||!['true','false'].includes(opts['web-search']))throw Error('mode 必须 fast/expert，web-search 必须 true/false');
  if(opts.limit!==undefined&&(!Number.isInteger(Number(opts.limit))||Number(opts.limit)>100))throw Error('limit 必须为 1–100');
  for(const k of ['timeout','interval','ack-timeout','limit'])if(opts[k]!==undefined&&(!Number.isFinite(Number(opts[k]))||Number(opts[k])<=0))throw Error(k+' 必须为正数');return{command,opts};
}
export async function main(argv=process.argv.slice(2)) {
  let c;
  try {
    if(Number(process.versions.node.split('.')[0])<22)throw Error('需要 Node.js 22+');
    const {command,opts}=parseArgs(argv);
    if(!opts.module&&opts['receipt-file']&&command!=='submit'&&fs.existsSync(opts['receipt-file']))opts.module=JSON.parse(fs.readFileSync(opts['receipt-file'],'utf8')).module;
    if(opts.stage&&!['requirement','coding','console'].includes(opts.stage))throw Error('stage 必须 requirement/coding/console');
    if(command==='manage')return await resourceMain({...opts,module:moduleName(opts.module)});
    if(command==='modules'||(opts.module&&moduleName(opts.module)!=='report'&&!['help','--help','launch'].includes(command)))return await moduleMain(command,opts);
    if(command==='help'||command==='--help'){console.log('Jahead Desktop CLI 2.0.2 (创造 / 开发 / 报告 / 搜索)\n modules\n 使用 --module create|developer|report|search 选择任务模块\n doctor | launch [--app .app/exe] [--port 9336]\n sessions [--limit 20]\n manage --module create|experts|skills|knowledge|hardware --action list|get|create|update|delete [--id ID] [--data-file data.json] [--dry-run]\n submit --prompt-file task.txt --receipt-file receipt.json [--wait] [--mode expert] [--web-search true]\n status | wait | cancel | open --session ID\n export --session ID --output result.json\n无需 API key；复用即先桌面各模块登录。仅支持已核对的前端版本；创造默认 Master，开发默认需求阶段，可 --stage coding。');return 0;}
    if(command==='launch') {
      const connectRequested=()=>opts.module?connectModule({...opts,module:moduleName(opts.module)}):connectDesktop(opts);
      try{c=await connectRequested();}catch{
        const app=discoverApp(opts.app);if(!app)throw Error('未找到即先桌面，请 --app 指定 .app 或 .exe');
        const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
        const command=launchCommand(app,opts.port);
        const p=spawn(command[0],command.slice(1),{env,detached:true,stdio:'ignore',shell:false});await new Promise((resolve,reject)=>{p.once('spawn',resolve);p.once('error',reject)});p.unref();
        for(let i=0;i<40;i++){try{c=await connectRequested();break;}catch{}await delay(500);}
        if(!c)throw Error('请正常退出已运行即先，再 launch；打开“报告”后 doctor。本命令不会强杀客户端');
      }
      emit({ok:true,profile:c.profile,target_id:c.targetId});return 0;
    }
    c=await connectDesktop(opts);
    if(command==='doctor'){const s=await rpc(c.client,'sessions',{limit:1});emit({ok:true,profile:c.profile,target_id:c.targetId,transport:c.transport,authenticated:true,session_count:s.total??null,metrics:'模型 Token 与模型延迟未提供；计费 Token 单独记录'});return 0;}
    if(command==='sessions'){const limit=Number(opts.limit||20);if(!Number.isInteger(limit)||limit>100)throw Error('limit 必须 1–100');emit(await rpc(c.client,'sessions',{limit}));return 0;}
    if(command==='submit'){
      const prompt=fs.readFileSync(opts['prompt-file']||0,'utf8');if(!prompt.trim()||Buffer.byteLength(prompt)>1e6)throw Error('任务正文必须非空且不超过 1MB');
      const r=await submit(c.client,opts,prompt);emit(r);if(opts.wait&&r.submitted){const s=await waitTask(c.client,{...opts,session:r.session_id});return !s?3:s.status==='completed'?0:4;}return r.submitted?0:3;
    }
    if(!opts.session||! /^[a-zA-Z0-9_-]{1,128}$/.test(opts.session))throw Error('需要有效 --session');
    if(command==='status'){emit(await rpc(c.client,'status',{session:opts.session}));return 0;}
    if(command==='wait'){const s=await waitTask(c.client,opts);return !s?3:s.status==='completed'?0:4;}
    if(command==='cancel'||command==='open'){emit(await rpc(c.client,command,{session:opts.session,task:opts.task}));return 0;}
    if(command==='export'){if(!opts.output)throw Error('需要 --output');const s=await rpc(c.client,'status',{session:opts.session});atomicJson(path.resolve(opts.output),{type:'jahead.desktop.snapshot',...s},0);emit({exported:path.resolve(opts.output),session_id:s.session_id});return 0;}
    throw Error('未知命令 '+command);
  }catch(e){emit({type:'cli.error',error:cleanError(e),...e.receipt?{receipt:e.receipt}:{}});return 2;}finally{c?.client.close();}
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)process.exitCode=await main();
