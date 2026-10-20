/** Jahead report frontend release 2026-10-16. Only public answer/tool metadata. */
export function sanitizeEvent(e) {
  if (!e || typeof e !== 'object') return null;
  const a=e.payload&&typeof e.payload==='object'?e.payload:e;
  const n=a.payload&&typeof a.payload==='object'?a.payload:a;
  const type=e.type, at=e.create_time||a.create_time||n.create_time||null;
  const base={type,at};
  if(type==='text')return {...base,text:typeof e.content==='string'?e.content:''};
  if(type==='text_delta')return {...base,text:typeof n.text==='string'?n.text:''};
  if(type==='turn_completed')return {...base,text:typeof n.text==='string'?n.text:null};
  if(type==='tool_exec_started'||type==='tool_exec_completed')return {...base,tool_name:n.tool_name||'unknown',is_error:n.is_error===true};
  if(type==='task_usage')return {...base,billed_token_count:Number.isSafeInteger(e.usage?.token_count)?e.usage.token_count:null};
  if(type==='agent_suspended'&&n.reason==='waiting_for_user')return {...base,status:'needs_input'};
  if(type==='backend_document')return {...base,task_id:a.task_id||null,document_id:a.document_id||null};
  if(type==='error'||type==='stream_error')return {...base,message:n.message||a.message||e.message||'生成失败',reason:n.reason||a.reason||e.reason||null};
  if(type==='done')return base;
  return null; // never expose thinking/reasoning, auth, raw tool inputs or outputs
}

export function summarizeSession(data, sanitize=sanitizeEvent) {
  const s=data.session;
  if(!s)throw Error('会话响应缺少 session');
  const events=[],messages=[];
  for(const m of data.messages||[]) {
    if(m.message?.user)messages.push({role:'user',content:m.message.user.content||''});
    if(Array.isArray(m.message?.assistant))for(const e of m.message.assistant){const v=sanitize(e);if(v)events.push(v);}
    else if(m.role==='user'||m.role==='assistant')messages.push({role:m.role,content:typeof m.content==='string'?m.content:''});
  }
  const last=(data.messages||[]).at(-1),native=s.active_task?.status||last?.status||s.status;
  const waiting=(last?.message?.assistant||[]).some(e=>sanitize(e)?.status==='needs_input');
  const status=waiting?'needs_input':['completed','done','succeeded'].includes(native)?'completed':['failed','error'].includes(native)?'failed':['cancelled','canceled'].includes(native)?'cancelled':['waiting_for_user','suspended'].includes(native)?'needs_input':['running','generating','pending'].includes(native)?'running':'unknown';
  let text='',draft='';
  for(const e of events) {
    if(['text','text_delta'].includes(e.type))draft+=e.text;
    if(e.type==='turn_completed'){text+=e.text??draft;draft='';}
  }
  text+=draft;
  if(!text)text=messages.filter(m=>m.role==='assistant').map(m=>m.content).join('\n');
  const billed=last?.task_usage?.token_count??events.findLast(e=>e.type==='task_usage')?.billed_token_count??null;
  return {session_id:s.id||s._id||s.session_id,task_id:s.current_task_id||s.active_task?.task_id||last?.task_id||null,
    title:s.title||'',status,native_status:native||null,session_status:s.status||null,execution_warnings:events.filter(e=>e.type==='error'||e.type==='stream_error').map(e=>e.message),request_observed:messages.some(m=>m.role==='user')||events.length>0,
    final_answer:status==='completed'?text:'',answer_candidate:text,messages:messages.filter(m=>m.role==='user'),events,
    billed_token_count:billed,total_tokens:null,created_at:s.created_at||null,updated_at:s.updated_at||null};
}
