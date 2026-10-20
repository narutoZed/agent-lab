/** Versioned Jahead module routing. The desktop owns authentication. */
import {CDP,endpoint,delay} from './desktop_cdp.mjs';
export const MODULES={
 report:{label:'报告',project:'6a436e5d0b40a02275e7428a',entry:'/assets/index-8hWVE1So.js',entries:['/assets/index-vlalPs-Y.js','/assets/index-8hWVE1So.js']},
 create:{label:'创造',project:'6a488768744492da48072c35',entry:'/v1.1.7/assets/mf-entry-bootstrap-0-d14f6939.js',api:'/v1.1.7/assets/skillSettingDrawerBridge-6UEMKBhJ.js',export:'q'},
 search:{label:'搜索',project:'6a325695f5a59d4b3ecb7b37',entry:'/assets/index-DFrUAfWX.js',api:'/assets/http-DXWt_8ha.js',export:'t'},
 developer:{label:'开发',route:'/developer',api:'https://ai.jahead.com/ai-coding/v1.1.6/assets/index-dmQY2qMr.js'},
 experts:{label:'专家',project:'6a9519690eb701309e811338',entry:'/assets/index-CjXnmARF.js',authStorage:'tqai_backend_token'},
 skills:{label:'技能',project:'6a38efbd49324b04173881d1',entry:'/v1.1.5/assets/mf-entry-bootstrap-0-43cd290b.js',api:'/v1.1.5/assets/skillSettingDrawerBridge-LXP0Ax5s.js',export:'q'},
 knowledge:{label:'知识',project:'6a3a32854da14ea6f1bd14b9',entry:'/v0.1.26/assets/index-_CWZ4S5v.js',api:'/v0.1.26/assets/index-_CWZ4S5v.js',export:'h'},
 hardware:{label:'硬件',project:'6a3a5aec5ef298ff975a66f7',entry:'/v0.1.19/assets/index-qFENhxuc.js',authStorage:'tqai_backend_token'}
};
export const moduleName=n=>({'创作':'create','创造':'create','creation':'create','开发':'developer','development':'developer','报告':'report','搜索':'search','专家':'experts','技能':'skills','知识':'knowledge','硬件':'hardware'}[n]||n||'report');
export const originFor=c=>`https://proj-prod-${c.project}.site.jahead.com`;
export async function listTargets(port){const r=await fetch(endpoint(port)+'/json/list',{signal:AbortSignal.timeout(3000),redirect:'error'});if(!r.ok)throw Error('调试端口不可用');return r.json();}
export async function connectHost(port){const ts=(await listTargets(port)).filter(t=>t.type==='page'&&/\/app\.asar\/out\/renderer\/index\.html(?:[#?]|$)/.test(t.url||''));if(ts.length!==1)throw Error('需要唯一即先桌面主窗口');const c=await CDP.connect(ts[0].webSocketDebuggerUrl,port);if(!await c.evaluate("document.title==='即先 AI' && typeof api?.debugNavigateRoute==='function' && Boolean(api?.runtimePlatform)")){c.close();throw Error('非即先桌面');}return c;}
export async function connectModule(opts,open=true){
 const name=moduleName(opts.module),config=MODULES[name];if(!config)throw Error('未知即先模块 '+name);
 const host=await connectHost(opts.port);
 try{
  if(open){const route=config.route||'/external?url='+encodeURIComponent(originFor(config));await host.evaluate(`api.debugNavigateRoute(${JSON.stringify(route)})`);}
  if(name==='developer'){
   for(let i=0;i<40;i++){
    try{const observed=await host.evaluate(`performance.getEntriesByType('resource').some(e=>e.name.split('?')[0]===${JSON.stringify(config.api)})`);if(!observed){await delay(250);continue;}const info=await host.evaluate(`(async()=>{const m=await import(${JSON.stringify(config.api)});const c=m.N();return {ready:!!c.companyId&&!!c.userId,mock:m.R}})()`);if(info.mock)throw Error('拒绝 mock 开发环境');if(info.ready)return{client:host,module:name,config,profile:'developer-1.1.6'};}catch(e){if(e.message.includes('mock'))throw e;}
    await delay(250);
   }throw Error('开发模块身份初始化未完成');
  }
  for(let i=0;i<40;i++){
   const ts=(await listTargets(opts.port)).filter(t=>{try{return ['iframe','page'].includes(t.type)&&new URL(t.url).origin===originFor(config)&&(!opts.target||opts.target===t.id)}catch{return false}});
   if(ts.length>1)throw Error('模块窗口不唯一，请 --target 指定');
   if(ts.length===1){const client=await CDP.connect(ts[0].webSocketDebuggerUrl,opts.port);try{const entry=await client.evaluate(`(()=>{if(!document.getElementById('root')?.childElementCount)return null;const allowed=${JSON.stringify(config.entries||[config.entry])};return [...document.querySelectorAll('script[src]')].map(s=>new URL(s.src,location.href).pathname).find(p=>allowed.includes(p))||null})()`);if(entry){host.close();return{client,module:name,config:{...config,entry},targetId:ts[0].id,profile:entry};}}catch{}client.close();}
   if(!open)break;await delay(250);
  }throw Error('模块尚未加载或前端版本不匹配：'+name);
 }catch(e){host.close();throw e;}
}
