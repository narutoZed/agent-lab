import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';

test('Codex client answers recommended option and accepts permission over stdio',()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'arc-rpc-'));
  const fake=path.join(root,'server.mjs');
  fs.writeFileSync(fake,`import readline from 'node:readline';
const out=o=>console.log(JSON.stringify(o));
readline.createInterface({input:process.stdin}).on('line',line=>{const m=JSON.parse(line);
if(m.method==='initialize')out({id:m.id,result:{}});
if(m.method==='thread/start')out({id:m.id,result:{thread:{id:'owned'}}});
if(m.method==='turn/start'){
out({id:m.id,result:{turn:{id:'turn'}}});
out({method:'turn/started',params:{threadId:'owned',turn:{id:'turn'}}});
out({id:'question',method:'item/tool/requestUserInput',params:{threadId:'owned',turnId:'turn',questions:[{id:'q',options:[{label:'A (Recommended)'},{label:'B'}]}]}});
}
if(m.id==='question'&&!m.method){
if(m.result?.answers?.q?.answers?.[0]!=='A (Recommended)')process.exit(9);
out({id:'permission',method:'item/commandExecution/requestApproval',params:{threadId:'owned',turnId:'turn',availableDecisions:['accept','decline']}});
}
if(m.id==='permission'&&!m.method){
if(m.result?.decision!=='accept')process.exit(8);
out({method:'item/completed',params:{threadId:'owned',item:{type:'agentMessage',id:'answer',phase:'final_answer',text:'OK'}}});
out({method:'turn/completed',params:{threadId:'owned',turn:{id:'turn',status:'completed'}}});
setTimeout(()=>process.exit(0),30);
}});`);
  try{
    const runner=fileURLToPath(new URL('../scripts/codex-interactive.mjs',import.meta.url));
    const p=spawnSync(process.execPath,[runner,'--server-argv',JSON.stringify([process.execPath,fake]),'--workspace',root,'--output',path.join(root,'answer.txt'),'--timeout','5'],{input:'task',encoding:'utf8',timeout:10000});
    assert.equal(p.status,0,p.stderr+'\n'+p.stdout);
    const events=p.stdout.trim().split('\n').map(JSON.parse);
    assert.equal(events.filter(e=>e.type==='interaction.resolved').length,2);
    assert.ok(events.some(e=>e.type==='turn.completed'));
    assert.equal(fs.readFileSync(path.join(root,'answer.txt'),'utf8'),'OK');
  }finally{fs.rmSync(root,{recursive:true,force:true})}
});
