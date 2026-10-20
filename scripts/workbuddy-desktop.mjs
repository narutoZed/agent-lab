#!/usr/bin/env node
/** Control WorkBuddy Desktop 5.8.x through its own renderer/daemon bridge. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { assertCompatible, connectDesktop, delay, endpoint, rpc, targets } from './desktop_cdp.mjs';
import { workbuddyDecision } from './interaction_policy.mjs';

const VERSION = '1.0.2';
const DEFAULT_PORT = 9335;
const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
const emit = value => process.stdout.write(JSON.stringify(value)+'\n');

export async function handlePending(client,sessionId,policy,seen=new Set()) {
  const pending=await rpc(client,'getPendingInfo',[sessionId]);
  if(!pending?.id||seen.has(pending.id))return false;
  const decision=workbuddyDecision(pending,policy);
  if(!decision)return false;
  seen.add(pending.id);
  await rpc(client,'resolvePending',[sessionId,pending.id,decision.response]);
  const event={type:'interaction.resolved',platform:'workbuddy',session_id:sessionId,request_id:pending.id,
    kind:decision.kind,policy:'user_authorized',response:decision.response,at:new Date().toISOString()};
  const audit=policy.auditFile||path.join(os.homedir(),'.workbuddy-desktop-cli','interactions',crypto.createHash('sha256').update(sessionId).digest('hex').slice(0,16)+'.jsonl');
  fs.mkdirSync(path.dirname(audit),{recursive:true});fs.appendFileSync(audit,JSON.stringify(event)+'\n',{encoding:'utf8',mode:0o600});
  emit(event);
  return true;
}

export function uuid7() {
  const b = crypto.randomBytes(16); b.writeUIntBE(Date.now(), 0, 6);
  b[6] = (b[6] & 15) | 112; b[8] = (b[8] & 63) | 128;
  return b.toString('hex');
}

export function atomicJson(file, value, indent=2) {
  fs.mkdirSync(path.dirname(file), {recursive:true});
  const tmp = file+'.'+crypto.randomUUID()+'.tmp';
  try { fs.writeFileSync(tmp, JSON.stringify(value,null,indent), {encoding:'utf8', mode:0o600}); fs.renameSync(tmp,file); }
  finally { if(fs.existsSync(tmp)) fs.unlinkSync(tmp); }
}

export function discoverApp(explicit, platform=process.platform, home=os.homedir(), env=process.env) {
  const candidates = explicit ? [path.resolve(explicit)] : platform==='darwin' ? ['/Applications/WorkBuddy.app','/Applications/WorkBuddy AI.app',path.join(home,'Applications/WorkBuddy.app')] : platform==='win32' ? [
    path.join(env.LOCALAPPDATA||'', 'Programs/WorkBuddy/WorkBuddy.exe'),
    path.join(env.LOCALAPPDATA||'', 'WorkBuddy/WorkBuddy.exe'),
    path.join(env.PROGRAMFILES||'C:/Program Files','WorkBuddy/WorkBuddy.exe'),
    path.join(env.PROGRAMFILES||'C:/Program Files','Tencent/WorkBuddy/WorkBuddy.exe')
  ] : [];
  for(const candidate of candidates) {
    const executable = candidate.endsWith('.app') ? path.join(candidate,'Contents/MacOS/Electron') : candidate;
    if(fs.existsSync(executable) && fs.statSync(executable).isFile()) return {app:candidate,executable};
  }
  return null;
}

export function safeInfo(snapshot) {
  const i=snapshot?.info||snapshot||{},c=snapshot?.configManager||{};
  return {session_id:i.id||i.sessionId,title:i.title,state:i.state||i.status,cwd:i.cwd,transport:i.transport,
    model:c.model||i.model,mode:c.mode||i.mode,permission_mode:c.permissionMode,
    has_active_child_agents:i.hasActiveChildAgents===true,has_running_background_tasks:i.hasRunningBackgroundTasks===true};
}

export async function doctor(opts) {
  const app=discoverApp(opts.app);
  let desktop;
  try {
    desktop=await connectDesktop(opts.port,opts.target);
    const {info,targetId}=desktop;
    const probe=await rpc(desktop.client,'list',[{transport:'local',page:1,size:1}]);
    if(!Array.isArray(probe?.items))throw Error('桌面会话查询契约不兼容');
    return {ok:true,cli_version:VERSION,app:app?.app,desktop_version:info.version,config_dir:info.configDir,
      platform:info.platform,target_id:targetId,endpoint:endpoint(opts.port),profile:/^5\.8\./.test(info.version)?'workbuddy-modern-5.8':'unsupported',
      verified_desktop_version:'5.8.3',transport:'desktop_renderer_bridge',
      compatible_window_count:desktop.compatibleWindowCount,window_selection:desktop.windowSelection};
  } catch(error) {
    let candidates=[];
    try { candidates=(await targets(opts.port)).map(t=>({id:t.id,title:t.title,path:new URL(t.url).pathname})); } catch {}
    return {ok:false,cli_version:VERSION,app:app?.app||null,endpoint:endpoint(opts.port),error:error.message,
      targets:candidates,
      next_step:app?'退出空闲的 WorkBuddy 后执行 launch；不自动关闭现有任务':'使用 --app 指定 WorkBuddy 桌面程序路径'};
  } finally {desktop?.client.close();}
}

export async function launchApp(opts) {
  try { const connected=await connectDesktop(opts.port,opts.target); connected.client.close(); return doctor(opts); } catch {}
  const app=discoverApp(opts.app); if(!app) throw Error('未找到桌面客户端，请通过 --app 指定 .app 或 WorkBuddy.exe');
  // Only this launch gets the debug port; no persistent environment or config writes.
  const env={...process.env,WORKBUDDY_REMOTE_DEBUGGING_PORT:String(opts.port)};
  delete env.ELECTRON_RUN_AS_NODE;
  const child=spawn(app.executable,[`--remote-debugging-address=127.0.0.1`],{env,detached:true,stdio:'ignore',shell:false});
  await new Promise((resolve,reject)=>{child.once('spawn',resolve);child.once('error',reject)}); child.unref();
  const deadline=Date.now()+Number(opts.timeout||30)*1000;
  while(Date.now()<deadline) {
    try {const connected=await connectDesktop(opts.port,opts.target);connected.client.close();return doctor(opts)} catch {}
    await delay(500);
  }
  throw Error('桌面调试端口未就绪。若 WorkBuddy 已运行，请先正常退出后重试；本命令不会强杀客户端。');
}

export function operationExpression(sessionId, requestId, text) {
  const args=JSON.stringify({sessionId,requestId,text});
  return `(() => {
    const a=${args};
    const store=globalThis.__agentRunCompareDesktopOps ??= Object.create(null);
    if(store[a.requestId]) return {existing:true,...store[a.requestId]};
    const item=store[a.requestId]={session_id:a.sessionId,request_id:a.requestId,phase:'dispatch_requested',started_at:Date.now()};
    Promise.resolve().then(()=>globalThis.__wbInvoke('wb:conversations:sendPrompt',{},a.sessionId,[{type:'text',text:a.text}],{
      clientRequestId:a.requestId, _meta:{'codebuddy.ai/conversationRequestId':a.requestId}
    })).then(result=>{
      if(result?.errorCode||result?.__wbError) {item.phase='failed';item.error=String(result.errorCode||result.code)+': '+String(result.message||'');}
      else {item.phase='settled';item.disposition=result?.disposition||null;}
      item.settled_at=Date.now();
    },error=>{item.phase='failed';item.error=String(error?.message||error);item.settled_at=Date.now();});
    return {...item};
  })()`;
}

export function requestState(value) {
  const s=typeof value==='string'?value:typeof value?.status==='string'?value.status:typeof value?.state==='string'?value.state:null;
  if(['completed','complete','success','succeeded','done'].includes(s)) return 'completed';
  if(['failed','error'].includes(s))return 'failed';
  if(['cancelled','canceled','aborted','stopped'].includes(s))return 'cancelled';
  return null;
}

export async function taskStatus(client,sessionId,requestId) {
  const [snap,history,op] = await Promise.all([
    rpc(client,'get',[sessionId]),rpc(client,'requests',[sessionId]),
    requestId?client.evaluate(`globalThis.__agentRunCompareDesktopOps?.[${JSON.stringify(requestId)}] ?? null`):Promise.resolve(null)
  ]);
  if(!snap)throw Error('桌面客户端中不存在该会话');
  const info=safeInfo(snap),items=Array.isArray(history)?history:history?.items||[];
  const matched=requestId?items.filter(r=>[r.id,r.requestId,r.clientRequestId,r.conversationRequestId].includes(requestId)):items;
  // New sessions have exactly one request. A mismatch among multiple requests is ambiguous.
  const current=matched.at(-1)||null;
  let status=current?requestState(current.state):null;
  if(!status && op?.phase==='failed')status='failed';
  if(!status && ['pending','waiting_for_approval'].includes(info.state))status='needs_input';
  if(!status && ['working','planning','running'].includes(info.state))status='running';
  if(!status)status=current?'request_observed':'awaiting_request';
  if(info.has_active_child_agents||info.has_running_background_tasks)status='running';
  const assistant=current?.assistantMessage;
  const text=typeof assistant?.content==='string'?assistant.content:Array.isArray(assistant?.content)?assistant.content.filter(b=>b.type==='text').map(b=>b.text||'').join('\n'):typeof assistant?.text==='string'?assistant.text:'';
  return {...info,request_id:requestId||current?.id,status,request_observed:!!current,request_count:items.length,
    native_request_state:current?.state??null,operation_phase:op?.phase??null,
    started_at:current?.startedAt??current?.timestamp??op?.started_at,completed_at:current?.completedAt??null,
    final_answer:TERMINAL.has(status)?text:'',output_kind:'assistant_message_text',error:op?.error||null};
}

export async function submitWithClient(client,identity,opts,text) {
  assertCompatible(identity);
  const workspace=path.resolve(opts.workspace||process.cwd());
  if(!fs.existsSync(workspace)||!fs.statSync(workspace).isDirectory())throw Error('workspace 必须是已存在的目录');
  const receiptFile=path.resolve(opts['receipt-file']||path.join(os.homedir(),'.workbuddy-desktop-cli','runs',crypto.randomUUID()+'.json'));
  const fingerprint=crypto.createHash('sha256').update(JSON.stringify({text,workspace,model:opts.model,mode:opts.mode,title:opts.title})).digest('hex');
  if(fs.existsSync(receiptFile)) {
    const existing=JSON.parse(fs.readFileSync(receiptFile,'utf8'));
    if(existing.fingerprint!==fingerprint)throw Error('回执已属于另一个任务，不覆盖或重发');
    return {...existing,reused:true};
  }
  fs.mkdirSync(path.dirname(receiptFile),{recursive:true});
  let fd; try {fd=fs.openSync(receiptFile+'.lock','wx',0o600);} catch {throw Error('该回执已有发布锁；先核查原任务，勿重复提交');}
  fs.writeFileSync(fd,new Date().toISOString());fs.closeSync(fd);
  const receipt={schema_version:1,type:'submission.receipt',transport:'workbuddy_desktop',desktop_version:identity.version,
    session_id:crypto.randomUUID(),request_id:uuid7(),workspace,config_dir:identity.configDir,
    fingerprint,receipt_file:receiptFile,submitted:false,phase:'creating',created_at:new Date().toISOString()};
  atomicJson(receiptFile,receipt);
  try {
    const config={conversationId:receipt.session_id,title:opts.title||'CLI 桌面任务',cwd:workspace,transport:'local'};
    if(opts.model)config.model=opts.model;
    if(opts.mode)config.mode=opts.mode;
    const created=await rpc(client,'create',[config],90000);
    if(created?.info?.id!==receipt.session_id)throw Error('客户端返回的会话 ID 与创建 ID 不一致；停止发送');
    receipt.desktop_config=safeInfo(created);receipt.phase='created';atomicJson(receiptFile,receipt);
    receipt.phase='dispatch_requested';atomicJson(receiptFile,receipt);
    await client.evaluate(operationExpression(receipt.session_id,receipt.request_id,text),15000);
    const deadline=Date.now()+Number(opts['ack-timeout']||30)*1000,handled=new Set();
    while(Date.now()<deadline) {
      await handlePending(client,receipt.session_id,opts,handled);
      const state=await taskStatus(client,receipt.session_id,receipt.request_id);
      if(state.request_observed) {
        receipt.submitted=true;receipt.phase='submitted';receipt.submitted_at=new Date().toISOString();
        receipt.status=state.status;receipt.selector={request_id:receipt.request_id};atomicJson(receiptFile,receipt);return receipt;
      }
      if(state.status==='failed')throw Error(state.error||'桌面端拒绝任务');
      await delay(300);
    }
    throw Error('尚未观察到桌面请求记录，提交结果待确认；使用 status 查询，勿自动重发');
  } catch(error) {
    receipt.phase=receipt.phase==='creating'?'create_unknown':'dispatch_unknown';receipt.error=error.message;
    atomicJson(receiptFile,receipt);throw Object.assign(error,{receipt});
  }
}

async function waitTask(client,opts) {
  const end=Date.now()+Number(opts.timeout||300)*1000,handled=new Set();let last;
  while(Date.now()<end) {
    await handlePending(client,opts.session,opts,handled);
    const state=await taskStatus(client,opts.session,opts.request);
    const fingerprint=JSON.stringify(state);
    if(fingerprint!==last){emit({type:TERMINAL.has(state.status)?`execution.${state.status}`:state.status==='needs_input'?'execution.needs_input':'execution.status',...state});last=fingerprint;}
    if(TERMINAL.has(state.status))return state;
    await delay(Number(opts.interval||1)*1000);
  }
  emit({type:'observation.timeout',session_id:opts.session,note:'仅停止等待，桌面任务没有自动取消'});
  return null;
}

export function parseArgs(argv) {
  const [command='help',...rest]=argv, opts={port:DEFAULT_PORT,questions:'recommended',permissions:'approve'};
  for(let i=0;i<rest.length;i++) {
    const arg=rest[i];if(!arg.startsWith('--'))throw Error('参数必须使用 --name value');
    const key=arg.slice(2);
    if(['wait'].includes(key))opts[key]=true;
    else {if(rest[i+1]===undefined||rest[i+1].startsWith('--'))throw Error(`${arg} 缺少值`);opts[key]=rest[++i];}
  }
  endpoint(opts.port);
  if(!['recommended','manual'].includes(opts.questions)||!['approve','manual'].includes(opts.permissions))throw Error('questions 支持 recommended/manual；permissions 支持 approve/manual');
  for(const key of ['timeout','ack-timeout','interval'])if(opts[key]!==undefined&&(!Number.isFinite(Number(opts[key]))||Number(opts[key])<=0))throw Error(`${key} 必须为正数`);
  return {command,opts};
}

export async function main(argv=process.argv.slice(2)) {
  let connection;
  try {
    if(Number(process.versions.node.split('.')[0])<22)throw Error('需要 Node.js 22 或更高版本');
    const {command,opts}=parseArgs(argv);
    if(command==='help'||command==='--help') {
      console.log(`WorkBuddy Desktop CLI ${VERSION} (Node 22+)\n\n  doctor [--app path] [--port 9335]\n  launch [--app path] [--port 9335]\n  sessions [--limit 20]\n  submit --prompt-file task.txt --workspace DIR --receipt-file receipt.json [--wait]\n  status --session ID [--request ID]\n  wait --session ID [--request ID] [--timeout 300]\n  cancel --session ID\n\n所有调用经过 WorkBuddy 桌面窗口。登录由桌面处理；默认 --questions recommended --permissions approve，可改为 manual。不提供任意 eval 或数据库写入。`);return 0;
    }
    if(command==='doctor'){const d=await doctor(opts);emit(d);return d.ok?0:2;}
    if(command==='launch'){emit(await launchApp(opts));return 0;}
    connection=await connectDesktop(opts.port,opts.target);
    const {client,info}=connection;
    if(command==='sessions') {
      const limit=Number(opts.limit||20);if(!Number.isInteger(limit)||limit<1||limit>100)throw Error('limit 必须为 1–100');
      const list=await rpc(client,'list',[{transport:'local',page:1,size:limit}]);
      emit({items:(list?.items||[]).map(safeInfo),total:list?.total});return 0;
    }
    if(command==='submit') {
      const text=opts['prompt-file']?fs.readFileSync(path.resolve(opts['prompt-file']),'utf8'):fs.readFileSync(0,'utf8');
      if(!text.trim()||Buffer.byteLength(text)>1_000_000)throw Error('任务正文必须非空且不超过 1MB');
      const receipt=await submitWithClient(client,info,opts,text);emit(receipt);
      if(opts.wait&&receipt.submitted){const state=await waitTask(client,{...opts,session:receipt.session_id,request:receipt.request_id});return !state?3:state.status==='completed'?0:4;}
      return receipt.submitted?0:3;
    }
    if(!opts.session)throw Error('需要 --session 指定唯一会话');
    if(command==='status'){emit(await taskStatus(client,opts.session,opts.request));return 0;}
    if(command==='wait'){const s=await waitTask(client,opts);return !s?3:s.status==='completed'?0:4;}
    if(command==='cancel'){assertCompatible(info);await rpc(client,'cancel',[opts.session]);emit({type:'cancel.requested',session_id:opts.session,note:'已调用桌面停止接口，随后查询 status 确认'});return 0;}
    throw Error(`未知命令 ${command}`);
  } catch(error) {
    emit({type:'cli.error',error:error.message,...error.receipt?{receipt:error.receipt}: {}});return 2;
  } finally {connection?.client.close();}
}

if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)process.exitCode=await main();
