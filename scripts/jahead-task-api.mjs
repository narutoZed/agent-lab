/** Native task adapters for create/search/developer. Runs inside the selected desktop module. */
export async function taskRenderer(module,config,action,args){
 const unwrap=x=>{if(x?.stat!==undefined&&x.stat!==1||x?.success===false||x?.code!==undefined&&![0,200,'0'].includes(x.code))throw Error(x.msg||x.message||x.error?.message||'模块接口拒绝请求');return x?.data??x};
 const safe=value=>{if(Array.isArray(value))return value.map(safe);if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).filter(([k])=>!/(token|secret|authorization|password|cookie|thought|reasoning)/i.test(k)).map(([k,v])=>[k,safe(v)]));return value};
 const stateOf=s=>['done','completed','success','succeeded','finished'].includes(s)?'completed':['failed','error'].includes(s)?'failed':['cancelled','canceled','stopped','response_cancelled'].includes(s)?'cancelled':['waiting_for_user','waiting_user_input','agent_suspended','suspended','takeover_required','waiting_user_takeover'].includes(s)?'needs_input':['running','working','replying','pending','queued','generating','processing'].includes(s)?'running':'unknown';
 const sdk=await import(config.api);
 const store=globalThis.__arcJaheadTasks??=Object.create(null);
 const client=sdk[config.export];
 const call=async(method,url,data,params)=>unwrap((await client.request({method,url,data,params})).data);
 if(action==='open'){
  if(!args.session&&!args.project)return {opened:true,module};
  if(module==='developer'){if(!args.project)throw Error('需要 --project');await globalThis.api.debugNavigateRoute('/developer'+sdk.s(encodeURIComponent(args.project)));return{opened:true,project_id:args.project};}
  if(module==='search'){setTimeout(()=>{location.pathname='/chat/'+encodeURIComponent(args.session)},100);return{type:'open.requested',session_id:args.session};}
  if(module==='create'){
   if(!args.assistant)throw Error('需要 --assistant 或回执');
   const list=await call('GET','/api/v1/chat/sessions',null,{assistant_id:args.assistant,limit:100});if(!(list.sessions||[]).some(s=>s.session_id===args.session))throw Error('未找到此助理会话');
   if(document.querySelector('textarea.master-input')?.value.trim())throw Error('桌面有未发送草稿，未切换会话');
   localStorage.setItem('jx_creator_master_active_session_'+args.assistant,args.session);setTimeout(()=>location.reload(),100);return{type:'open.requested',session_id:args.session};
  }
 }

 if(module==='search'){
  if(action==='list')return safe(await call('GET','/api/search/tasks/list',null,{end:args.limit||20}));
  if(action==='submit'){
   const parsed=await call('POST','/api/search/intent-parse',{conversation_id:'',query:args.prompt,task_type:'single_task',search_type:args.searchType||'full_web_search'});
   if(!parsed.task_id)throw Error('搜索未返回 task_id');
   const task=parsed.task_id,op=store[task]={status:parsed.needs_clarification?'needs_input':'running',events:[],prompt:args.prompt};
   if(parsed.clarification_message)op.answer=parsed.clarification_message;
   if(!parsed.needs_clarification){
    // Native welcome flow already creates the task in intent-parse; never POST a duplicate.
    if(!parsed.search_plan)throw Error('搜索缺少 search_plan；任务已存在，需核查');
    const query=new URLSearchParams({task_id:task,token:sdk.n()});
    const es=new EventSource(sdk.i('/api/search/tasks/stream?'+query));op.close=()=>es.close();
    es.onmessage=e=>{try{const v=JSON.parse(e.data);if(v.type==='status_update'){op.native_status=v.progress?.status;op.status=stateOf(op.native_status);}if(v.type==='result')op.result=safe(v.result);if(v.type==='done'){op.done=true;es.close();}if(v.type==='error')op.error=v.message;}catch{op.parse_error=true}};
    es.onerror=()=>{op.connection='disconnected';es.close()};
   }
   return {session_id:task,task_id:task,conversation_id:parsed.conversation_id||null,submitted:true,status:op.status,search_plan:safe(parsed.search_plan)};
  }
  if(action==='cancel'){const r=await call('POST',`/api/search/tasks/${encodeURIComponent(args.session)}/stop`);store[args.session]?.close?.();return{type:'cancel.requested',session_id:args.session,result:safe(r)}}
  if(action==='status'||action==='export'){
   const t=await call('GET',`/api/search/tasks/${encodeURIComponent(args.session)}`);const op=store[args.session];
   const native=t.status||t.task?.status||op?.native_status;const status=stateOf(native);
   let result=null;if(status==='completed'||action==='export'){try{result=await call('GET',`/api/search/tasks/${encodeURIComponent(args.session)}/result`)}catch(e){result={unavailable:e.message}}}
   const rows=(result?.chat_contents||[]).flat().filter(e=>['text_delta','text','completed','file','table','tool_invocation'].includes(e.type)).map(e=>({type:e.type,content:e.type==='tool_invocation'?undefined:e.content,create_time:e.create_time,tool_name:e.type==='tool_invocation'?e.tool_type:undefined}));
   const answer=rows.filter(e=>['text_delta','text'].includes(e.type)).map(e=>e.content||'').join('');
   return{session_id:args.session,task_id:args.session,status,native_status:native||null,request_observed:true,final_answer:status==='completed'?answer:'',answer_candidate:op?.answer||'',messages:(op?.prompt||args.prompt)?[{role:'user',content:op?.prompt||args.prompt}]:[],events:rows.filter(e=>e.type==='tool_invocation').map(e=>({type:'tool_exec_started',tool_name:e.tool_name,at:e.create_time})),result:{chat_contents:rows},task:{task_id:t.task_id,status:t.status,task_type:t.task_type},total_tokens:null};
  }
 }
 if(module==='create'){
  const assistants=async()=>{const o=await call('GET','/api/v1/assistant/list');return o.list||[]};
  const getAssistant=async()=>{if(args.assistant)return args.assistant;const a=(await assistants()).filter(x=>x.is_master===true||x.role==='master');if(a.length!==1)throw Error('请 --assistant 指定唯一助理');return a[0].id||a[0]._id};
  if(action==='list')return (await assistants()).map(x=>({id:x.id||x._id,name:x.name,role:x.role,is_master:x.is_master}));
  if(action==='sessions'){const assistant=await getAssistant();return safe(await call('GET','/api/v1/chat/sessions',null,{assistant_id:assistant,limit:args.limit||20}))}
  if(action==='prepare'){
   const assistant=await getAssistant(),items=(await call('GET','/api/v1/chat/sessions',null,{assistant_id:assistant,limit:100})).sessions||[];
   const textarea=document.querySelector('textarea.master-input');
   if(!textarea||textarea.value.trim())throw Error('创造 Master 输入框未就绪或含草稿；不覆盖草稿');
   const button=document.querySelector('button[aria-label="新建 Master 会话"]');if(!button||button.disabled)throw Error('新建会话不可用');
   const master=(await assistants()).find(x=>x.is_master===true||x.role==='master');if((master?.id||master?._id)!==assistant)throw Error('创造提交目前使用 Master，请选择 Master 助理');
   const active=localStorage.getItem('jx_creator_master_active_session_'+assistant);
   if(active&&items.some(s=>s.session_id===active)){const h=await call('GET','/api/v1/chat/history',null,{assistant_id:assistant,session_id:active,limit:1});if(!(h.messages||[]).length){store[active]={assistant,prompt:args.prompt};return{session_id:active,assistant_id:assistant,reused_empty_session:true}}}
   const old=new Set(items.map(s=>s.session_id));button.click();
   for(let i=0;i<40;i++){await new Promise(r=>setTimeout(r,250));const now=(await call('GET','/api/v1/chat/sessions',null,{assistant_id:assistant,limit:100})).sessions||[];const added=now.filter(s=>!old.has(s.session_id));if(added.length>1)throw Error('多个新会话，无法唯一绑定');if(added.length===1){const session=added[0].session_id;store[session]={assistant,prompt:args.prompt};return{session_id:session,assistant_id:assistant};}}
   throw Error('尚未确认新会话，禁止重试创建');
  }
  if(action==='send'){
   const op=store[args.session];if(!op||op.sent)throw Error('会话不由本次 CLI 创建或已经发送');
   // Observe the selected native session through the live session dropdown label and empty input.
   if(localStorage.getItem('jx_creator_master_active_session_'+op.assistant)!==args.session)throw Error('桌面会话已切换，停止发送');
   let textarea;for(let i=0;i<40;i++){textarea=document.querySelector('textarea.master-input');if(textarea&&!textarea.disabled)break;await new Promise(r=>setTimeout(r,250));}if(!textarea||textarea.disabled||textarea.value.trim())throw Error('创造输入框不可用');
   op.sent=true;Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(textarea,args.prompt);textarea.dispatchEvent(new Event('input',{bubbles:true}));textarea.dispatchEvent(new Event('change',{bubbles:true}));
   await new Promise(r=>setTimeout(r,100));
   if(textarea.value!==args.prompt.replace(/\r\n?/g,'\n')||localStorage.getItem('jx_creator_master_active_session_'+op.assistant)!==args.session)throw Error('输入或会话被修改，停止发送');
   const buttons=[...document.querySelectorAll('button')].filter(b=>b.innerText.replace(/\s/g,'')==='发送'&&b.getClientRects().length);
   if(buttons.length!==1||buttons[0].disabled)throw Error('发送按钮不唯一或未就绪；会话保留，请核查');buttons[0].click();return{dispatch_requested:true};
  }
  const assistant=args.assistant||store[args.session]?.assistant;if(!assistant)throw Error('需要 --assistant（或 --receipt-file 读取回执）');
  if(action==='cancel'){await call('POST','/api/v1/chat/conversation/interrupt',{assistant_id:assistant,session_id:args.session});return{type:'cancel.requested',session_id:args.session};}
  if(action==='status'||action==='export'){
   const history=await call('GET','/api/v1/chat/history',null,{assistant_id:assistant,session_id:args.session,limit:100});
   let native;try{native=await call('GET','/api/v1/chat/conversation/status',null,{assistant_id:assistant,session_id:args.session})}catch(e){if(e.response?.status!==404)throw e;}
   const messages=(history.messages||[]).map(x=>{let content=x.content||x.payload||x.message_content||{};if(typeof content==='string'){try{content=JSON.parse(content)}catch{content={content}}}return {id:x.message_id||x.messageID||x.id||x._id,role:x.role||((x.from_uid||x.from_id)===assistant?'assistant':'user'),content:typeof content==='string'?content:content.content||content.text||'',type:content.type??x.type}}).filter(m=>typeof m.content==='string');
   const ns=native?.status||native?.state||native?.connection_status||native?.reply_status;
   let status=stateOf(ns);const answer=messages.filter(m=>m.role==='assistant').map(m=>m.content).join('\n');
   const user=messages.findLast(m=>m.role==='user'),trace=store[args.session]??=( {assistant} );
   if(ns==='idle'&&user?.id&&answer&&!trace.terminal){
    const q=new URLSearchParams({assistant_id:assistant,session_id:args.session,client_run_id:String(user.id),after_sequence:'0'});
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2500);
    try{const response=await fetch('/api/v1/chat/trace/stream?'+q,{headers:{Accept:'text/event-stream','X-Auth-Token':sdk.b()},signal:controller.signal});if(response.ok){const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';try{for(;;){const {done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let m;while((m=/\r?\n\r?\n/.exec(buffer))){const block=buffer.slice(0,m.index);buffer=buffer.slice(m.index+m[0].length);const event=block.split(/\r?\n/).find(l=>l.startsWith('event:'))?.slice(6).trim();if(event==='trace_done'){const raw=block.split(/\r?\n/).filter(l=>l.startsWith('data:')).map(l=>l.slice(5)).join('\n');const record=JSON.parse(raw);trace.terminal=stateOf(record.status);break;}}if(trace.terminal)break;}}finally{await reader.cancel();}}}catch{}finally{clearTimeout(timer);}
   }
   if(trace.terminal)status=trace.terminal;
   else if(ns==='idle'&&answer)status='answer_observed';
   return{session_id:args.session,assistant_id:assistant,status,native_status:ns||null,native_state:safe(native),request_observed:messages.some(m=>m.role==='user'),messages,events:[],final_answer:status==='completed'?answer:'',answer_candidate:answer,total_tokens:null};
  }
 }
 if(module==='developer'){
  if(sdk.R)throw Error('拒绝开发 mock 环境');
  const context=sdk.N(args.project),stage=args.stage||'requirement';
  const projectCall=async(method,p,data)=>{const url=sdk.S(context,p);const body=data?new FormData():null;if(body)for(const[k,v]of Object.entries(data))if(v!==undefined)body.append(k,String(v));const r=await fetch(url,{method,headers:{token:context.token,Accept:'application/json'},credentials:'include',...(body?{body}: {})});if(!r.ok)throw Error('开发接口 HTTP '+r.status);return unwrap(await r.json())};
  if(action==='list')return safe(await projectCall('GET','/project/list'));
  if(action==='prepare'){
   let project=args.project;
   if(!project){const p=await projectCall('POST','/project/create',{company_id:context.companyId,user_id:context.userId,name:args.title||'CLI 验证系统',description:'通过桌面 CLI 创建',project_type:'normal'});project=p.project_id||p.project?._id;if(!project)throw Error('开发项目 ID 缺失');}
   const ctx=sdk.N(project);let conversation;
   if(stage==='coding'){
    const form=new FormData();for(const[k,v]of Object.entries({company_id:ctx.companyId,project_id:project,user_id:ctx.userId}))form.append(k,v);
    const response=await fetch(sdk.S(ctx,'/requirement/sse/start-coding'),{method:'POST',body:form,headers:{token:ctx.token,Accept:'text/event-stream'},credentials:'include'});
    if(!response.ok)throw Error('开始开发失败 HTTP '+response.status+'；需要先完成原生需求流程');
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',result;
    try{for(;;){const {done,value}=await reader.read();buffer+=decoder.decode(value,{stream:!done});let match;while((match=/\r?\n\r?\n/.exec(buffer))){const block=buffer.slice(0,match.index);buffer=buffer.slice(match.index+match[0].length);if(block.split(/\r?\n/).some(l=>l.trim()==='event: done')){const raw=block.split(/\r?\n/).filter(l=>l.startsWith('data:')).map(l=>l.slice(5)).join('\n');result=JSON.parse(raw);break;}}if(result||done)break;}}finally{await reader.cancel();}
    if(!result||result.success===false)throw Error(result?.error_message||result?.message||'原生开始开发未成功');
    conversation={conversation_id:result.coding_conversation_id};if(!conversation.conversation_id)throw Error('开始开发未返回 coding_conversation_id');
   }else conversation=await sdk.X({context:ctx,conversationType:stage,mode:args.mode||'expert'});
   return{session_id:conversation.conversation_id||project,project_id:project,conversation_id:conversation.conversation_id,stage};
  }
  if(action==='send'){
   if(store[args.session]&&!['completed','failed','cancelled'].includes(store[args.session].status))throw Error('该会话仍有本 CLI 任务，不重复发送');
   const op=store[args.session]={status:'connecting',events:[],answer:'',prompt:args.prompt,project:args.project,stage};
   const abort=new AbortController();op.abort=abort;
   (async()=>{let sent=false;for await(const e of sdk.Y(context,abort.signal)){
    const type=sdk.Z(e);if(type==='connect_message'&&!sent){op.record_id=sdk.$(e);if(!op.record_id)throw Error('开发流缺少 record_id');sent=true;const r=await sdk.a9({context,conversationType:stage,mode:args.mode||'expert',question:args.prompt,recordId:op.record_id,channel:sdk.a0(e,context,op.record_id)});op.submitted=['replying','queued'].includes(r.status);op.status=op.submitted?'running':'unknown';continue;}
    const p=e.data?.payload||e.data||{};
    if(['text_delta','completed'].includes(type)){const txt=sdk._(e);if(txt)op.answer+=txt;}
    if(type==='tool_exec_started'||type==='tool_exec_completed')op.events.push({type,tool_name:p.tool_name||'unknown',at:p.create_time||null});
    if(type==='agent_suspended'||type==='ask_user')op.status='needs_input';
    if(type==='error'||type==='stream_error'){op.error=p.message||'开发执行出错';op.status='failed';}
    if(type==='response_cancelled')op.status='cancelled';
    if(type==='done'){if(!['failed','cancelled','needs_input'].includes(op.status))op.status='completed';break;}
   }})().catch(e=>{op.error=e.message;op.status='connection_unknown'});return{dispatch_requested:true};
  }
  if(action==='cancel'){const record=args.record||store[args.session]?.record_id;if(!record)throw Error('需要 --record');const r=await sdk.a6({context,conversationType:stage,recordId:record});store[args.session]?.abort?.abort();return{type:'cancel.requested',session_id:args.session,result:safe(r)}}
  if(action==='status'||action==='export'){
   const op=store[args.session];const h=await sdk.a3({context,conversationType:stage,start:0,end:99});
   const record=args.record||op?.record_id;const match=(h.records||[]).find(r=>(r.record_id||r.id||r._id)===record);
   const native=match?.status;const nativeState=stateOf(native);const pending=match?.interactions?.some(x=>x.status==='pending');const status=pending||op?.status==='needs_input'?'needs_input':nativeState!=='unknown'?nativeState:op?.status||'unknown';
   const answer=typeof match?.reply==='string'?match.reply:typeof match?.answer==='string'?match.answer:op?.answer||'';
   return{session_id:args.session,project_id:args.project,record_id:record||null,stage,status,native_status:native||null,request_observed:!!op?.submitted||!!match,final_answer:status==='completed'?answer:'',answer_candidate:answer,messages:[{role:'user',content:match?.question||op?.prompt||''}],events:op?.events||[],record:match?{record_id:match.record_id||match._id,question:match.question,reply:match.reply,status:match.status,model:match.model,elapsed_seconds:match.elapsed_seconds}:null,error:op?.error||null,total_tokens:null};
  }
 }
 throw Error('该模块不支持此操作 '+module+':'+action);
}
export async function moduleRpc(connection,action,args={}){
 if(!['list','sessions','prepare','submit','send','status','export','cancel','open'].includes(action))throw Error('操作不在白名单');
 const key='rpc_'+Date.now()+'_'+Math.random().toString(36).slice(2);
 const expr=`(globalThis.__arcRpcKeepAlive??=Object.create(null))[${JSON.stringify(key)}]=(${taskRenderer.toString()})(${JSON.stringify(connection.module)},${JSON.stringify(connection.config)},${JSON.stringify(action)},${JSON.stringify(args)})`;
 const result=await connection.client.evaluate(expr,90000);
 await connection.client.evaluate(`delete globalThis.__arcRpcKeepAlive[${JSON.stringify(key)}]`);return result;
}
