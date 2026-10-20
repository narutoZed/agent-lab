/** Allowlisted resource management; no arbitrary URL or eval command. */
import fs from 'node:fs';import path from 'node:path';
import {connectModule} from './jahead-modules.mjs';
import {atomicJson} from './desktop_fs.mjs';
const ID=x=>{if(typeof x!=='string'||!x.trim()||['.','..'].includes(x)||/[/?#]/.test(x))throw Error('此操作需要有效 --id');return encodeURIComponent(x)};
export function resourceSpec(module,action,id,data={}){
 const base=module==='knowledge'?'/api/v1/knowledge_libraries':module==='hardware'?'/api/v1/hardware/devices':null;
 if(base){if(action==='list')return{method:'GET',url:base,params:data};if(action==='get')return{method:'GET',url:base+'/'+ID(id)};
  if(action==='create'){if(module==='knowledge'&&(!data.title||!Array.isArray(data.scope_user_ids)))throw Error('知识库创建需要 title 与 scope_user_ids 数组');if(module==='hardware'&&!data.imei)throw Error('设备创建需要真实 imei');return{method:'POST',url:base,data};}
  if(action==='update'||action==='delete')return{method:action==='update'?'PUT':'DELETE',url:base+'/'+ID(id),data};
  if(module==='knowledge'&&action==='documents')return{method:'GET',url:base+'/'+ID(id)+'/documents',params:data};
  if(module==='hardware'&&action==='logs')return{method:'GET',url:base+'/'+ID(id)+'/logs',params:data};
 }
 if(module==='experts'){
  if(action==='list')return{method:'GET',url:'/api/v1/experts',params:{page:1,page_size:100,...data}};
  if(action==='get')return{method:'GET',url:'/api/v1/experts/'+ID(id)};
  if(action==='create-assistant')return{targetModule:'create',method:'POST',url:'/api/v1/experts/'+ID(id)+'/create-workflow/start',data};
  throw Error('专家目录由平台维护；可 list/get/create-assistant，创建后的助理用 create 模块管理');
 }
 if(module==='create'){
  const b='/api/v1/assistant';
  if(action==='list')return{method:'GET',url:b+'/list'};
  if(action==='get')return{method:'GET',url:b+'/detail/'+ID(id)};
  if(action==='create')return{method:'POST',url:b+'/create',data};
  if(action==='update')return{method:'PUT',url:b+'/update/'+ID(id),data};
  if(action==='delete')return{method:'DELETE',url:b+'/delete/'+ID(id)};
 }
 if(module==='skills'){
  if(action==='list')return{method:'GET',url:'/api/skills/search',params:{q:'',page:1,page_size:30,...data}};
  if(action==='get')return{method:'GET',url:'/api/skills/'+ID(id)};
  if(action==='delete')return{method:'DELETE',url:'/api/skills/'+ID(id)+'/remove'};
  if(action==='install')return{method:'POST',url:'/api/installed',data};
  if(action==='update')return{method:'PUT',url:'/api/installed/'+ID(id)+'/update',data};
  if(action==='markdown')return{method:'GET',url:'/api/skills/'+ID(id)+'/markdown'};
  if(action==='files')return{method:'GET',url:'/api/skills/'+ID(id)+'/file-tree'};
  if(action==='create'||action==='import')return{method:'POST',url:'/api/skills-import/upload',upload:true};
 }
 throw Error('此模块不支持该管理操作：'+module+'/'+action);
}
export async function resourceRenderer(config,spec,file){
 const un=x=>{if(x?.stat!==undefined&&x.stat!==1||x?.code!==undefined&&![0,200,'0'].includes(x.code)||x?.success===false)throw Error(x.msg||x.message||'管理操作失败');return x?.data??x};
 let r;
 if(config.authStorage){
  const token=localStorage.getItem(config.authStorage);const u=new URL(spec.url,location.origin);for(const[k,v]of Object.entries(spec.params||{}))u.searchParams.set(k,String(v));
  const response=await fetch(u,{method:spec.method,headers:{'Content-Type':'application/json',...(token?{'X-Auth-Token':token}:{})},...(spec.data?{body:JSON.stringify(spec.data)}:{})});if(!response.ok)throw Error('资源接口 HTTP '+response.status);r=await response.json();
 }else{
  const c=(await import(config.api))[config.export];let data=spec.data,headers;
  if(spec.upload){if(!file)throw Error('技能导入需要 --file ZIP');const bytes=Uint8Array.from(atob(file.base64),c=>c.charCodeAt(0));data=new FormData();data.append('file',new File([bytes],file.name,{type:'application/zip'}));headers={'Content-Type':'multipart/form-data'};}
  r=(await c.request({method:spec.method,url:spec.url,params:spec.params,data,headers})).data;
 }
 const result=un(r);
 const clean=v=>Array.isArray(v)?v.map(clean):v&&typeof v==='object'?Object.fromEntries(Object.entries(v).filter(([k])=>!/(token|secret|password|authorization|cookie)/i.test(k)).map(([k,x])=>[k,clean(x)])):v;
 return clean(result);
}
export async function resourceMain(opts){
 const action=opts.action||'list';let data=opts['data-file']?JSON.parse(fs.readFileSync(opts['data-file'],'utf8')):{};
 if(!data||typeof data!=='object'||Array.isArray(data))throw Error('data-file 必须为 JSON 对象');
 const spec=resourceSpec(opts.module,action,opts.id,data);
 if(opts['dry-run']){console.log(JSON.stringify({module:opts.module,action,spec,dry_run:true}));return 0;}
 const c=await connectModule({...opts,module:spec.targetModule||(opts.module==='skills'?'create':opts.module)});
 try{
  let file;if(spec.upload){if(!opts.file)throw Error('需要 --file');const p=path.resolve(opts.file);if(fs.statSync(p).size>20*1024*1024)throw Error('当前 CLI 导入文件限制 20MB');file={name:path.basename(p),base64:fs.readFileSync(p).toString('base64')};}
  const key='resource_'+Date.now();const expression=`(globalThis.__arcRpcKeepAlive??=Object.create(null))[${JSON.stringify(key)}]=(${resourceRenderer.toString()})(${JSON.stringify(c.config)},${JSON.stringify(spec)},${JSON.stringify(file)})`;
  const result=await c.client.evaluate(expression,90000);await c.client.evaluate(`delete globalThis.__arcRpcKeepAlive[${JSON.stringify(key)}]`);
  const value={module:opts.module,action,id:opts.id,result};if(opts.output){atomicJson(path.resolve(opts.output),value);console.log(JSON.stringify({exported:path.resolve(opts.output)}));}else console.log(JSON.stringify(value));return 0;
 }finally{c.client.close()}
}
