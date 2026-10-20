/** Explicit user policy: recommended answers and per-request tool approvals. */
const RECOMMENDED = /\(\s*recommended\s*\)|（\s*推荐\s*）|\(\s*推荐\s*\)|^推荐[：:]|\brecommended\s*$/i;
export function recommendedOptions(question) {
  if(question.isSecret || !Array.isArray(question.options))return null;
  const options=question.options.filter(o=>o && typeof o==='object' && (o.recommended===true||o.isRecommended===true||
    (!/not\s+recommended|不推荐|非推荐/i.test(o.label||'')&&RECOMMENDED.test(o.label||''))));
  if(!options.length || (!question.multiSelect && options.length!==1))return null;
  return options.map(o=>o.label).filter(x=>typeof x==='string'&&x.length>0);
}

export function answerRecommended(questions, format='codex') {
  if(!Array.isArray(questions)||!questions.length)return null;
  const answers={};
  for(const q of questions) {
    const selected=recommendedOptions(q);
    if(!q.id||!selected?.length)return null;
    answers[q.id]=format==='codex'?{answers:selected}:q.multiSelect?selected:selected[0];
  }
  return answers;
}

export function workbuddyDecision(pending, policy) {
  if(!pending?.id)return null;
  if(pending.category==='question' && policy.questions==='recommended') {
    const answers=answerRecommended(pending.questions,'workbuddy');
    return answers?{kind:'question',response:{optionId:'allow',answers}}:null;
  }
  if(policy.permissions==='approve' && ['tool','sandbox','plan'].includes(pending.category)) {
    if(pending.inputType==='plugin_recommendation' || pending.metadata?.weixinpay || pending.inputType==='weixinpay')return null;
    const options=pending.options||[];
    const option=options.find(o=>o.kind==='allow_once')||options.find(o=>['allow','allow_session','allow_always'].includes(o.kind))||
      options.find(o=>['allow','allow_once','allow_session','allow_always'].includes(o.optionId));
    return option?.optionId?{kind:'permission',response:{optionId:option.optionId}}:null;
  }
  return null;
}

export function codexDecision(method, params, policy, threadId) {
  if(!threadId||params.threadId!==threadId)return null;
  if(policy.permissions==='approve') {
    if(['item/commandExecution/requestApproval','item/fileChange/requestApproval'].includes(method)) {
      const available=params.availableDecisions;
      const decision=!available||available.includes('accept')?'accept':available.includes('acceptForSession')?'acceptForSession':null;
      return decision?{kind:'permission',response:{decision}}:null;
    }
    if(method==='item/permissions/requestApproval')return {kind:'permission',response:{permissions:params.permissions||{},scope:'turn'}};
  }
  if(['item/tool/requestUserInput','tool/requestUserInput'].includes(method)&&policy.permissions==='approve') {
    const answers={};let valid=Array.isArray(params.questions)&&params.questions.length>0;
    for(const q of params.questions||[]) {
      const options=q.options||[];
      const allow=options.filter(o=>/^(accept|allow|approve|批准|允许|同意)$/i.test(o.label?.trim()||''));
      const denies=options.some(o=>/^(decline|deny|reject|cancel|拒绝|取消)$/i.test(o.label?.trim()||''));
      if(q.isSecret||!q.id||allow.length!==1||!denies){valid=false;break}
      answers[q.id]={answers:[allow[0].label]};
    }
    if(valid)return {kind:'permission',response:{answers}};
  }
  if(['item/tool/requestUserInput','tool/requestUserInput'].includes(method)&&policy.questions==='recommended') {
    const answers=answerRecommended(params.questions,'codex');
    return answers?{kind:'question',response:{answers}}:null;
  }
  return null;
}
