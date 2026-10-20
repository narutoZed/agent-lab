'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="console-token"]').content;
const names = {codex: 'Codex', workbuddy: 'WorkBuddy', jahead: '即先 AI', mesh: 'Mesh API', epsilon: 'Epsilon API'};
const labels = {queued:'排队中', dispatching:'正在提交', running:'执行中', submitted:'已提交', completed:'已完成', failed:'执行失败', cancelled:'已取消', needs_input:'等待处理', needs_configuration:'配置待修复', dispatch_failed:'未能发布', launch_failed:'启动失败', dispatch_unknown:'提交待确认', needs_confirmation:'需核对原任务', answer_observed:'已记录回答', request_observed:'已收到请求'};
const terminal = new Set(['completed','failed','cancelled','needs_configuration','dispatch_failed','launch_failed']);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const badge = s => `<span class="tag ${esc(s)}">${esc(labels[s] || s)}</span>`;
const num = v => typeof v === 'number' ? v.toLocaleString('zh-CN') : '—';
let workbook = null, filename = '', rows = [], selected = new Set(), previewPayload = null, polling = false;
let latestBatches = [], comparisonRequest = 0;
let refreshPromise = Promise.resolve();
const navigationKey = 'agent-console-navigation';
const navigation = {view:'prepare',batchId:'',tasks:{}};
try {
  const saved = JSON.parse(sessionStorage.getItem(navigationKey) || 'null');
  if (saved && ['prepare','history','comparison'].includes(saved.view)) navigation.view=saved.view;
  if (saved && /^[0-9a-f-]{36}$/.test(saved.batchId || '')) navigation.batchId=saved.batchId;
  for (const [id,index] of Object.entries(saved?.tasks || {})) {
    if (/^[0-9a-f-]{36}$/.test(id) && Number.isInteger(index) && index>=0 && index<100) navigation.tasks[id]=index;
  }
} catch {}
const route = new URLSearchParams(location.hash.slice(1));
if (['prepare','history','comparison'].includes(route.get('view'))) navigation.view=route.get('view');
if (/^[0-9a-f-]{36}$/.test(route.get('batch') || '')) {
  navigation.batchId=route.get('batch');
  const index=Number(route.get('task'));
  if (Number.isInteger(index) && index>=0 && index<100) navigation.tasks[navigation.batchId]=index;
}
function saveNavigation() {
  try { sessionStorage.setItem(navigationKey,JSON.stringify(navigation)); } catch {}
  const hash=new URLSearchParams({view:navigation.view});
  if (navigation.batchId) {
    hash.set('batch',navigation.batchId);
    hash.set('task',String(navigation.tasks[navigation.batchId] || 0));
  }
  history.replaceState(null,'','#'+hash.toString());
}
let pendingPayload = null;
try { pendingPayload = JSON.parse(sessionStorage.getItem('agent-console-pending') || 'null'); } catch {}
$('recover').hidden = !pendingPayload;

function message(text, success=false) {
  $('message').textContent = text;
  $('message').className = success ? 'success' : '';
  $('message').hidden = !text;
}
async function api(path, body, raw=false) {
  const response = await fetch(path, body === undefined ? {cache:'no-store'} : {
    method:'POST', headers:{'X-Console-Token':token, 'Content-Type':raw ? 'application/octet-stream' : 'application/json'},
    body:raw ? body : JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data;
}
function showView(view) {
  const selectedView = view===true?'history':view===false?'prepare':view;
  navigation.view=selectedView;
  saveNavigation();
  for (const name of ['prepare','history','comparison']) {
    $(name+'-view').hidden = name!==selectedView;
    $('nav-'+name).classList.toggle('active',name===selectedView);
  }
  const titles = {prepare:['把任务交给合适的 Agent','导入任务清单，选择需要执行的任务，一次发布到多个平台。'],history:['每个任务，都有迹可循','跟踪发布、执行和结果，保留每个任务的独立记录。'],comparison:['横向指标对比','沿用原报告的统计口径，按任务并排查看各平台的指标与结果。']};
  $('page-title').textContent = titles[selectedView][0];
  $('page-subtitle').textContent = titles[selectedView][1];
  if (selectedView==='history') refreshBatches();
}
$('nav-prepare').onclick = () => showView(false);
$('nav-history').onclick = () => showView(true);
$('back-prepare').onclick = () => showView(false);
$('nav-comparison').onclick = () => {
  showView('comparison');
  if ($('comparison-batch').value) openComparison($('comparison-batch').value);
};
async function openComparison(batchId, taskIndex) {
  const batch=latestBatches.find(b=>b.id===batchId);
  if (!batch) return;
  const requested=taskIndex ?? navigation.tasks[batchId] ?? 0;
  const selectedTask=Number.isInteger(requested)&&requested>=0&&requested<batch.tasks.length?requested:0;
  navigation.batchId=batchId;
  navigation.tasks[batchId]=selectedTask;
  showView('comparison');
  const request = ++comparisonRequest;
  $('comparison-batch').value = batchId;
  $('comparison-note').textContent = '正在读取任务日志并生成完整横向对比，不会重新执行任务…';
  $('comparison-frame').hidden = true;
  $('comparison-open').hidden = $('comparison-json').hidden = true;
  $('comparison-refresh').disabled = true;
  try {
    const result = await api(`/api/batches/${batchId}/comparison`,{});
    if (request!==comparisonRequest) return;
    const url = result.url+'?task='+selectedTask;
    $('comparison-frame').src = url;
    $('comparison-frame').hidden = false;
    $('comparison-open').href = url;
    $('comparison-json').href = result.json_url;
    $('comparison-open').hidden = $('comparison-json').hidden = false;
    $('comparison-note').textContent = `已载入 ${result.task_count} 条任务。可在报告顶部切换任务；缺失指标留空，效果评价需另行核验。`;
  } catch(error) {
    if (request===comparisonRequest) $('comparison-note').textContent = error.message;
  } finally { if (request===comparisonRequest) $('comparison-refresh').disabled = false; }
}
$('comparison-batch').onchange = () => openComparison($('comparison-batch').value);
$('comparison-refresh').onclick = () => $('comparison-batch').value && openComparison($('comparison-batch').value);
window.addEventListener('message',event=>{
  if (event.origin!==location.origin || event.source!==$('comparison-frame').contentWindow || event.data?.type!=='agent-report-task') return;
  const index=event.data.task;
  const batch=latestBatches.find(b=>b.id===navigation.batchId);
  if (!batch || !Number.isInteger(index) || index<0 || index>=batch.tasks.length) return;
  navigation.tasks[batch.id]=index;
  saveNavigation();
  const link=new URL($('comparison-open').href,location.href);
  link.searchParams.set('task',String(index));
  $('comparison-open').href=link.href;
});

const aliases = {
  prompt:['任务正文','任务内容','任务描述','任务','提示词','问题','prompt','task','question','description'],
  title:['任务名称','任务标题','标题','名称','title','name'],
  id:['任务编号','任务id','编号','id','task_id','taskid'],
  criteria:['验收要求','验收标准','评价标准','criteria','acceptance','acceptance_criteria']
};
function currentSheet() { return workbook?.sheets[Number($('sheet').value)]; }
function populateMapping() {
  const sheet = currentSheet();
  for (const field of Object.keys(aliases)) {
    const options = '<option value="">不映射</option>' + sheet.headers.map((h,i) => `<option value="${i}">${esc(h)}</option>`).join('');
    $('map-'+field).innerHTML = options;
    const match = sheet.headers.findIndex(h => aliases[field].includes(h.trim().toLowerCase()));
    $('map-'+field).value = match >= 0 ? String(match) : '';
  }
  rebuildRows();
}
function rebuildRows() {
  const sheet = currentSheet();
  const mapping = Object.fromEntries(Object.keys(aliases).map(f => [f, $('map-'+f).value === '' ? null : Number($('map-'+f).value)]));
  selected.clear();
  const idCounts = new Map();
  rows = sheet.rows.map((row,index) => {
    const value = field => mapping[field] === null ? '' : String(row.cells[mapping[field]] || '').trim();
    const id = value('id') || `${Number($('sheet').value)+1}-${row.row_number}`;
    const prompt = value('prompt');
    const errors = row.errors.filter(e => Object.values(mapping).includes(e.column)).map(e => e.message);
    if (!prompt) errors.push(mapping.prompt === null ? '请映射任务正文列' : '任务正文为空');
    if (prompt.length > 100000) errors.push('任务正文超过 100000 字符');
    if (value('title').length > 200) errors.push('任务名称超过 200 字符');
    if (value('criteria').length > 10000) errors.push('验收要求超过 10000 字符');
    if (id.length > 120) errors.push('任务编号超过 120 字符');
    idCounts.set(id,(idCounts.get(id)||0)+1);
    return {key:`sheet-${index}`,id,title:value('title') || prompt.slice(0,60) || '未命名任务',prompt,criteria:value('criteria'),
      source:`${filename} / ${sheet.name} / 第 ${row.row_number} 行`.slice(0,240),errors};
  });
  rows.forEach(row => { if (idCounts.get(row.id)>1) row.errors.push('任务编号重复，请修正表格或取消编号映射'); });
  $('search').value = '';
  renderRows();
}
async function importFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.xlsx')) { message('请选择 .xlsx 文件；旧版 .xls 请先另存为 XLSX。'); return; }
  if (file.size > 10*1024*1024) { message('文件不能超过 10 MB。'); return; }
  $('file').disabled = true;
  $('file-title').textContent = '正在读取工作簿…';
  try {
    const parsed = await api('/api/import',file,true);
    workbook = parsed; filename = file.name;
    $('sheet').innerHTML = parsed.sheets.map((s,i) => `<option value="${i}">${esc(s.name)}${s.hidden?'（隐藏工作表）':''} · ${s.rows.length} 行</option>`).join('');
    const firstVisible = parsed.sheets.findIndex(s=>!s.hidden);
    $('sheet').value = String(Math.max(0,firstVisible));
    $('mapping').hidden = false;
    $('file-title').textContent = file.name;
    if (!$('batch-title').value) $('batch-title').value = file.name.replace(/\.xlsx$/i,'').slice(0,120);
    populateMapping();
    message(`已读取 ${parsed.sheets.length} 个工作表。请核对列映射并勾选任务；尚未发布。`,true);
  } catch(error) { message(error.message); $('file-title').textContent = filename || '选择或拖入 XLSX 文件'; }
  finally { $('file').disabled = false; $('file').value = ''; }
}
$('file').onchange = event => importFile(event.target.files[0]);
$('drop-zone').ondragover = event => { event.preventDefault(); $('drop-zone').classList.add('drag'); };
$('drop-zone').ondragleave = () => $('drop-zone').classList.remove('drag');
$('drop-zone').ondrop = event => { event.preventDefault(); $('drop-zone').classList.remove('drag'); importFile(event.dataTransfer.files[0]); };
$('sheet').onchange = populateMapping;
Object.keys(aliases).forEach(field => $('map-'+field).onchange = rebuildRows);
function visibleRows() {
  const q = $('search').value.trim().toLowerCase();
  return rows.filter(row => !q || `${row.id}\n${row.title}\n${row.prompt}`.toLowerCase().includes(q));
}
function renderRows() {
  const visible = visibleRows();
  $('tasks').innerHTML = visible.map(row => `<tr class="${selected.has(row.key)?'selected-row':''}"><td><input type="checkbox" data-select="${esc(row.key)}" aria-label="选择 ${esc(row.title)}" ${selected.has(row.key)?'checked':''} ${row.errors.length?'disabled':''}></td><td><button class="task-open" data-inspect="${esc(row.key)}"><strong>${esc(row.title)}</strong></button><small>${esc(row.id)}</small></td><td><div class="row-preview">${esc(row.prompt || '—')}</div><small>${esc(row.source)}</small></td><td><span class="tag ${row.errors.length?'error':'ok'}" title="${esc(row.errors.join('；'))}">${row.errors.length?'需修正':'可发布'}</span></td></tr>`).join('');
  $('empty-tasks').hidden = rows.length>0;
  $('row-count').textContent = rows.length;
  $('task-note').textContent = rows.length ? `显示 ${visible.length} / ${rows.length} 条，${rows.filter(r=>r.errors.length).length} 条需修正。每批最多发布 100 条。` : '一次最多发布 100 条任务；每个平台为每条任务创建独立会话。';
  const valid = visible.filter(row=>!row.errors.length);
  $('select-all').checked = valid.length>0 && valid.every(row=>selected.has(row.key));
  $('select-all').indeterminate = valid.some(row=>selected.has(row.key)) && !$('select-all').checked;
  updateSummary();
}
function selectVisible(checked) {
  visibleRows().filter(row=>!row.errors.length).forEach(row=>checked ? selected.add(row.key) : selected.delete(row.key));
  renderRows();
}
$('select-visible').onclick = () => selectVisible(true);
$('select-all').onchange = event => selectVisible(event.target.checked);
$('clear-selection').onclick = () => {selected.clear();renderRows();};
$('search').oninput = renderRows;
$('tasks').onchange = event => {
  const key = event.target.dataset.select;
  if (!key) return;
  event.target.checked ? selected.add(key) : selected.delete(key);
  renderRows();
};
$('tasks').onclick = event => {
  const button = event.target.closest('[data-inspect]');
  if (!button) return;
  const row = rows.find(r=>r.key===button.dataset.inspect);
  $('detail-title').textContent = row.title;
  $('detail-content').innerHTML = `<p class="detail-meta">${esc(row.source)}</p>${row.errors.length?`<p class="error-text">${esc(row.errors.join('；'))}</p>`:''}<pre>${esc(row.prompt)}</pre><h3>验收要求</h3><pre>${esc(row.criteria || '未指定；执行完成后仍需核验效果。')}</pre>`;
  $('detail-dialog').showModal();
};
function providers() {
  const result = {}, timeout = Number($('timeout').value);
  for (const p of Object.keys(names)) {
    if (!$('use-'+p).checked) continue;
    result[p] = {timeout_seconds:timeout};
    if (p==='jahead') {
      result[p].module = $('module-jahead').value;
      if (result[p].module==='report') result[p].mode = $('mode-jahead').value;
    } else {
      result[p].model = $('model-'+p).value.trim();
      if (p==='mesh') Object.assign(result[p],meshConnection());
      if (p==='epsilon') Object.assign(result[p],epsilonConnection());
    }
  }
  return result;
}
function updateSummary() {
  const count = selected.size, platformCount = Object.keys(providers()).length;
  $('selected-top').textContent = `已选 ${count} 条`;
  $('publish-summary').textContent = count ? `${count} 条任务 × ${platformCount} 个平台，共 ${count*platformCount} 次执行` : '尚未选择任务';
  $('publish-note').textContent = count>100 ? '超过单批 100 条上限，请减少勾选。' : '新批次 Mesh / Epsilon 各最多并发 10 条，每条独立 session；其余平台逐条执行。';
  $('preview').disabled = count===0 || count>100 || platformCount===0 || !!pendingPayload;
  $('report-mode-label').hidden = $('module-jahead').value!=='report';
  for (const p of ['codex','workbuddy','mesh','epsilon']) $('model-'+p).disabled = !$('use-'+p).checked;
  $('module-jahead').disabled = !$('use-jahead').checked;
  $('mode-jahead').disabled = !$('use-jahead').checked;
}
for (const id of ['use-codex','use-workbuddy','use-jahead','use-mesh','use-epsilon','module-jahead']) $(id).onchange = updateSummary;
function meshConnection() {
  return {base_url:$('url-mesh').value.trim(),company_id:$('company-mesh').value.trim(),project_id:$('project-mesh').value.trim(),agent_id:$('agent-mesh').value.trim()};
}
async function loadMeshModels() {
  const button=$('mesh-models'), previous=$('model-mesh').value;
  button.disabled=true;$('mesh-connection').textContent='正在连接…';
  try {
    const result=await api('/api/mesh/models',meshConnection());
    $('model-mesh').innerHTML='<option value="">服务默认模型</option>'+result.models.map(m=>`<option value="${esc(m.name)}">${esc(m.name)}${m.label&&m.label!==m.name?" · "+esc(m.label):""}</option>`).join('');
    if (previous && !result.models.some(m=>m.name===previous)) $('model-mesh').insertAdjacentHTML('beforeend',`<option value="${esc(previous)}">${esc(previous)}（当前不可用）</option>`);
    $('model-mesh').value=previous;
    $('mesh-connection').textContent=`连接正常 · ${result.models.length} 个模型`;
  } catch(error) {$('mesh-connection').textContent=error.message;}
  finally {button.disabled=false;}
}
$('mesh-models').onclick=loadMeshModels;
function epsilonConnection() {
  return {user_id:$('user-epsilon').value.trim(),base_url:$('url-epsilon').value.trim(),company_id:$('company-epsilon').value.trim(),project_id:$('project-epsilon').value.trim(),agent_id:$('agent-epsilon').value.trim()};
}
async function loadEpsilonModels() {
  const button=$('epsilon-models'), previous=$('model-epsilon').value;
  button.disabled=true;$('epsilon-connection').textContent='正在连接…';
  try {
    const result=await api('/api/epsilon/models',epsilonConnection());
    $('model-epsilon').innerHTML='<option value="">服务默认模型</option>'+result.models.map(m=>`<option value="${esc(m.name)}">${esc(m.name)}${m.label&&m.label!==m.name?" · "+esc(m.label):""}</option>`).join('');
    if (previous && !result.models.some(m=>m.name===previous)) $('model-epsilon').insertAdjacentHTML('beforeend',`<option value="${esc(previous)}">${esc(previous)}（当前不可用）</option>`);
    $('model-epsilon').value=previous;
    $('epsilon-connection').textContent=`连接正常 · ${result.models.length} 个模型`;
  } catch(error) {$('epsilon-connection').textContent=error.message;}
  finally {button.disabled=false;}
}
$('epsilon-models').onclick=loadEpsilonModels;
function preview(payload, recovering=false) {
  previewPayload = payload;
  const ps = Object.keys(payload.providers);
  $('preview-title').textContent = payload.title;
  $('preview-content').innerHTML = `<p><strong>${payload.tasks.length} 条任务 × ${ps.length} 个平台 = ${payload.tasks.length*ps.length} 次执行</strong></p><div class="preview-config">${ps.map(p=>`<span>${names[p]} · ${esc(payload.providers[p].model || (p==='jahead'?payload.providers[p].module+(payload.providers[p].mode?' / '+payload.providers[p].mode:''):'客户端默认模型'))}</span>`).join('')}</div><p class="hint">新批次 Mesh / Epsilon 各最多并发 10 条，其余每个平台 1 条；已创建批次沿用原配置。模型 ID 将传给客户端，实际使用模型以执行日志为准。Codex / WorkBuddy 问答：${payload.interaction_policy.questions==='manual'?'手动':'明确推荐项自动选择'}；工具权限：${payload.interaction_policy.permissions==='manual'?'手动':'批准本任务请求'}。</p>${payload.tasks.map((task,i)=>`<details class="task-preview"><summary>${i+1}. ${esc(task.title)}</summary><pre>${esc(task.prompt)}</pre><p class="hint">验收要求：${esc(task.criteria || '按任务正文核验；不会自动生成质量评分。')}</p></details>`).join('')}`;
  $('confirm-publish').textContent = recovering ? '核查 / 重试本次发布' : '发布所选任务';
  $('confirm-publish').disabled = false;
  $('confirm-note').textContent = recovering ? '复用原请求 ID，已受理的批次不会重复提交。' : '点击发布会真实调用所选 Agent。';
  $('preview-dialog').showModal();
}
$('preview').onclick = () => {
  const tasks = rows.filter(row=>selected.has(row.key)).map(({key,errors,...task})=>task);
  preview({request_id:crypto.randomUUID(),title:$('batch-title').value.trim() || `任务批次 ${new Date().toLocaleString('zh-CN')}`,tasks,providers:providers(),interaction_policy:{questions:$('questions').value,permissions:$('permissions').value}});
};
$('recover-button').onclick = () => pendingPayload && preview(pendingPayload,true);
$('close-preview').onclick = $('cancel-preview').onclick = () => $('preview-dialog').close();
$('confirm-publish').onclick = async () => {
  if (!previewPayload || $('confirm-publish').disabled) return;
  $('confirm-publish').disabled = true;
  pendingPayload = previewPayload;
  try { sessionStorage.setItem('agent-console-pending',JSON.stringify(pendingPayload)); }
  catch {
    pendingPayload = null;
    $('confirm-note').textContent = '浏览器无法保存发布回执，请减少本批任务数量或允许站点存储后重试。尚未发送任务。';
    $('confirm-publish').disabled = false;
    return;
  }
  $('recover').hidden = false;
  try {
    const result = await api('/api/batches',pendingPayload);
    sessionStorage.removeItem('agent-console-pending'); pendingPayload = null;
    $('recover').hidden = true; $('preview-dialog').close();
    selected.clear(); renderRows(); showView(true);
    message(result.reused ? '已找到原批次，没有重复提交。' : '批次已加入队列，将按各平台并发上限执行。',true);
  } catch(error) {
    // Keep the same request body/ID for a network retry; never create a second batch implicitly.
    if (error.status===400) {
      sessionStorage.removeItem('agent-console-pending'); pendingPayload = null;
      $('recover').hidden = true; $('preview-dialog').close();
      message(error.message+'。本次未受理，请修改后重新核对。');
    } else {
      $('confirm-note').textContent = error.message + '。可用相同请求 ID 核查或重试。';
      $('confirm-publish').textContent = '核查 / 重试本次发布';
    }
  } finally { $('confirm-publish').disabled = false; updateSummary(); }
};
$('add-manual').onclick = () => $('manual-dialog').showModal();
$('close-manual').onclick = () => $('manual-dialog').close();
$('manual-form').onsubmit = event => {
  event.preventDefault();
  const prompt = $('manual-prompt').value.trim();
  if (!prompt) return;
  const id = 'manual-'+crypto.randomUUID().slice(0,8);
  rows.push({key:id,id,title:$('manual-title').value.trim() || prompt.slice(0,60),prompt,criteria:$('manual-criteria').value.trim(),source:'网页手动添加',errors:[]});
  $('manual-form').reset(); $('manual-dialog').close(); renderRows();
};
$('close-detail').onclick = () => $('detail-dialog').close();

function jobCell(job, provider) {
  if (!job) return '<span class="batch-empty">—</span>';
  const snap = job.snapshot || {}, metric = snap.metrics || {};
  const model = snap.models?.join(', ') || (provider.model ? `请求：${provider.model}` : '实际模型待记录');
  const note = job.error || job.observation_error || snap.error || snap.model_note || snap.note || '';
  return `<div class="job-cell">${badge(job.status)}<small>${esc(model)}</small>${metric.total_tokens ? `<small>Token ${num(metric.total_tokens.value)} · 工具 ${num(metric.tool_calls)}</small>`:''}${note?`<small class="error-text" title="${esc(note)}">${esc(note.slice(0,110))}</small>`:''}</div>`;
}
function renderBatches(data) {
  const batches = data.batches, jobs = batches.flatMap(b=>b.jobs);
  if (JSON.stringify(latestBatches.map(b=>[b.id,b.title]))!==JSON.stringify(batches.map(b=>[b.id,b.title]))) {
    const old = $('comparison-batch').value || navigation.batchId;
    $('comparison-batch').innerHTML = batches.map(b=>`<option value="${b.id}">${esc(b.title)}</option>`).join('');
    if (batches.some(b=>b.id===old)) $('comparison-batch').value = old;
  }
  latestBatches = batches;
  $('batch-badge').textContent = batches.length;
  $('empty-history').hidden = batches.length>0;
  const stats = [['总执行数',jobs.length],['已完成',jobs.filter(j=>j.status==='completed').length],['执行 / 排队',jobs.filter(j=>['queued','dispatching','running','submitted','answer_observed'].includes(j.status)).length],['需要关注',jobs.filter(j=>!['queued','dispatching','running','submitted','answer_observed','completed'].includes(j.status)).length]];
  $('stats').innerHTML = stats.map(([label,value])=>`<div class="stat">${label}<strong>${value}</strong></div>`).join('');
  $('batches').innerHTML = batches.map(batch => {
    const completed = batch.jobs.filter(j=>j.status==='completed').length;
    const unsubmitted = batch.jobs.filter(j=>j.status==='needs_configuration').length;
    const parallelismText=Object.keys(batch.providers).map(p=>names[p]+' '+(batch.parallelism?.[p]||1)).join(' · ');
    return `<article class="card batch"><div class="batch-head"><div><h2>${esc(batch.title)}</h2><small>${esc(new Date(batch.created_at).toLocaleString('zh-CN'))} · ${batch.tasks.length} 条任务 · 已完成 ${completed} / ${batch.jobs.length} 次${unsubmitted?` · ${unsubmitted} 次连接检查失败、尚未发布`:''}</small><small class="batch-parallelism">并发上限：${esc(parallelismText)}</small></div>${unsubmitted?`<button class="secondary" data-retry="${batch.id}">重试未发布任务</button>`:''}<button class="secondary" data-compare="${batch.id}">指标对比</button><button class="secondary" data-pause="${batch.id}" data-paused="${!batch.paused}" ${batch.jobs.some(j=>j.status==='queued')?'':'disabled'}>${batch.paused?'继续队列':'暂停后续'}</button></div>${batch.queue_note?`<p class="queue-note">${esc(batch.queue_note)}</p>`:''}<div class="table-wrap"><table><thead><tr><th>任务</th>${Object.keys(batch.providers).map(p=>`<th>${names[p]}</th>`).join('')}<th>结果</th></tr></thead><tbody>${batch.tasks.map((task,i)=>`<tr><td><strong>${esc(task.title)}</strong><small>${esc(task.id)}</small></td>${Object.keys(batch.providers).map(p=>`<td>${jobCell(batch.jobs.find(j=>j.task_index===i&&j.platform===p),batch.providers[p])}</td>`).join('')}<td><button class="text-button" data-detail="${batch.id}" data-index="${i}">查看详情</button></td></tr>`).join('')}</tbody></table></div></article>`;
  }).join('');
}
async function refreshBatches() {
  if (polling) return refreshPromise;
  polling = true;
  refreshPromise = api('/api/batches').then(data=>{
    renderBatches(data);
    $('connection').textContent = '已更新 '+new Date().toLocaleTimeString('zh-CN');
  }).catch(()=>{ $('connection').textContent = '连接中断，保留上次结果'; })
    .finally(()=>{ polling = false; });
  return refreshPromise;
}
$('refresh').onclick = refreshBatches;
$('batches').onclick = async event => {
  const pause = event.target.closest('[data-pause]'), detail = event.target.closest('[data-detail]'), retry = event.target.closest('[data-retry]'), comparison = event.target.closest('[data-compare]');
  try {
    if (comparison) {
      await openComparison(comparison.dataset.compare);
    } else if (retry) {
      retry.disabled = true;
      const result = await api(`/api/batches/${retry.dataset.retry}/retry-unsubmitted`,{retry:true});
      message(`已将 ${result.requeued} 次确认未发送的任务重新入队。${result.blocked.length?'另有任务存在发布记录，未自动重试。':''}`,true);
      await refreshBatches();
    } else if (pause) {
      pause.disabled = true;
      await api(`/api/batches/${pause.dataset.pause}/pause`,{paused:pause.dataset.paused==='true'});
      await refreshBatches();
    } else if (detail) {
      const data = await api(`/api/tasks/${detail.dataset.detail}/${detail.dataset.index}`);
      $('detail-title').textContent = data.task.title;
      $('detail-content').innerHTML = `<p class="detail-meta">${esc(data.task.source)}</p><details open><summary>任务正文</summary><pre>${esc(data.task.prompt)}</pre></details><details><summary>验收要求与记录位置</summary><pre>${esc(data.task.criteria || '按任务正文核验；执行完成不等于效果通过。')}\n\n${esc(data.experiment)}</pre></details>${data.jobs.map(job=>{const snap=job.snapshot||{};return `<section class="detail-job"><h3>${names[job.platform]} ${badge(job.status)}</h3><p class="detail-meta">请求模型：${esc(data.providers[job.platform].model || '默认')} · 日志模型：${esc(snap.models?.join(', ') || '尚未记录')}</p>${snap.model_note?`<p class="error-text">${esc(snap.model_note)}</p>`:''}${job.error||snap.error?`<p class="error-text">${esc(job.error||snap.error)}</p>`:''}<pre>${esc(snap.final_answer || '尚无公开回答；可关闭后重新打开详情获取最新内容。')}</pre></section>`}).join('')}`;
      $('detail-dialog').showModal();
    }
  } catch(error) { message(error.message); }
};
renderRows();
showView(navigation.view);
async function restoreNavigation() {
  await refreshBatches();
  if (navigation.view==='comparison' && latestBatches.length) {
    const batchId=latestBatches.some(b=>b.id===navigation.batchId)?navigation.batchId:latestBatches[0].id;
    await openComparison(batchId);
  }
}
restoreNavigation();
setInterval(refreshBatches,3000);
