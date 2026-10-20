/** Shared atomic receipt writer. No dependency on any desktop product. */
import fs from 'node:fs';import path from 'node:path';import crypto from 'node:crypto';
export function atomicJson(file,value,indent=2){
 fs.mkdirSync(path.dirname(file),{recursive:true});const tmp=file+'.'+crypto.randomUUID()+'.tmp';
 try{fs.writeFileSync(tmp,JSON.stringify(value,null,indent),{encoding:'utf8',mode:0o600});fs.renameSync(tmp,file);}finally{if(fs.existsSync(tmp))fs.unlinkSync(tmp);}
}
