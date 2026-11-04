'use strict';
const fs=require('fs'),path=require('path'),crypto=require('crypto');

class InboundSpool {
  constructor(directory){
    this.directory=directory;
    // Recovery remains serialized, but LIVE submits no longer wait behind it.
    this.chain=Promise.resolve();
    this.liveChains=new Map();
    this.liveIds=new Set();
    this.hotChats=new Map();
    fs.mkdirSync(directory,{recursive:true,mode:0o700});
  }

  file(id){
    return path.join(this.directory,crypto.createHash('sha256').update(String(id)).digest('hex')+'.json');
  }

  write(file,job){
    const tmp=file+'.tmp';
    const fd=fs.openSync(tmp,'w',0o600);
    try{
      fs.writeFileSync(fd,JSON.stringify(job));
      fs.fsyncSync(fd);
    }finally{
      fs.closeSync(fd);
    }
    fs.renameSync(tmp,file);
    // Windows does not support opening a directory for fsync; the file itself
    // was flushed above on every platform.
    if(process.platform!=='win32'){
      const dir=fs.openSync(this.directory,'r');
      try{fs.fsyncSync(dir);}finally{fs.closeSync(dir);}
    }
  }

  enqueue(payload,ready=false){
    if(!payload.external_id)throw new Error('Inbound message has no external_id');
    const file=this.file(payload.external_id);
    let job;
    if(fs.existsSync(file))job=JSON.parse(fs.readFileSync(file,'utf8'));
    else job={
      payload,
      createdAt:Date.now(),
      order:process.hrtime.bigint().toString(),
      ready:false,
      attempts:0,
      nextAttemptAt:0,
    };
    if(ready&&!job.result){
      job.payload=payload;
      job.ready=true;
    }
    this.write(file,job);
    return file;
  }

  jobs(){
    return fs.readdirSync(this.directory)
      .filter(n=>n.endsWith('.json'))
      .map(n=>{
        const file=path.join(this.directory,n);
        return {file,job:JSON.parse(fs.readFileSync(file,'utf8'))};
      })
      .sort((a,b)=>a.job.createdAt-b.job.createdAt||a.job.order.localeCompare(b.job.order));
  }

  // Used only by recovery/drain. A slow recovery batch must never hold live input.
  exclusive(fn){
    const result=this.chain.then(fn);
    this.chain=result.catch(()=>{});
    return result;
  }

  // Preserve order for live messages from the SAME chat, while allowing other
  // chats and the recovery worker to proceed independently.
  liveExclusive(chat,fn){
    const key=String(chat||'');
    const previous=this.liveChains.get(key)||Promise.resolve();
    const run=previous.catch(()=>{}).then(fn);
    const tail=run.catch(()=>{});
    this.liveChains.set(key,tail);
    tail.finally(()=>{
      if(this.liveChains.get(key)===tail)this.liveChains.delete(key);
    });
    return run;
  }

  markFailure(file,job,error){
    if(!fs.existsSync(file))return;
    job.attempts=Number(job.attempts||0)+1;
    job.lastError=String(error&&error.message||error).slice(0,300);
    // 3s, 10s, 30s, then 60s. Prevent one bad message from hammering recovery.
    const waits=[3000,10000,30000,60000];
    job.nextAttemptAt=Date.now()+waits[Math.min(job.attempts-1,waits.length-1)];
    this.write(file,job);
  }

  submit(payload,request){
    const file=this.enqueue(payload,true);
    const id=String(payload.external_id||'');
    const chat=String(payload.chat_id||'');
    // While a user is actively writing, let live traffic win over old recovery.
    this.hotChats.set(chat,Date.now()+15000);
    this.liveIds.add(id);

    const run=this.liveExclusive(chat,async()=>{
      let row=null;
      if(fs.existsSync(file)){
        row={file,job:JSON.parse(fs.readFileSync(file,'utf8'))};
      }
      if(!row)return {duplicate:true};
      if(row.job.result)return {duplicate:true}; // drain owns durable reply delivery

      try{
        // IMPORTANT: do NOT check for older files from this chat here.
        // A stale recovery item must never reject a fresh menu/category message.
        const result=await request(payload);
        if(fs.existsSync(file))fs.unlinkSync(file);
        return result;
      }catch(error){
        this.markFailure(file,row.job,error);
        throw error;
      }
    });

    return run.finally(()=>{
      this.liveIds.delete(id);
      this.hotChats.set(chat,Date.now()+5000);
    });
  }

  drain(request,deliver){
    return this.exclusive(async()=>{
      const blocked=new Set();
      let count=0,attempted=0;
      const now=Date.now();

      for(const {file,job} of this.jobs()){
        const chat=String(job.payload&&job.payload.chat_id||'');
        const id=String(job.payload&&job.payload.external_id||'');

        if(blocked.has(chat))continue;

        // Never race recovery against a live submit for the same user/message.
        if(this.liveIds.has(id)||(Number(this.hotChats.get(chat)||0)>Date.now())){
          blocked.add(chat);
          continue;
        }

        if(!job.ready&&Date.now()-job.createdAt<60000){
          blocked.add(chat);
          continue;
        }

        if(Number(job.nextAttemptAt||0)>Date.now()){
          blocked.add(chat);
          continue;
        }

        // Keep recovery intentionally small. Live traffic has priority.
        if(attempted++>=20)break;

        try{
          if(!job.result){
            job.result=await request(job.payload);
            job.attempts=0;
            job.nextAttemptAt=0;
            this.write(file,job);
          }
          await deliver(job.payload,job.result);
          if(fs.existsSync(file))fs.unlinkSync(file);
          count++;
        }catch(error){
          blocked.add(chat);
          this.markFailure(file,job,error);
        }
      }

      // Drop expired hot-chat markers.
      for(const [chat,until] of this.hotChats){
        if(Number(until||0)<=now)this.hotChats.delete(chat);
      }
      return count;
    });
  }
}

module.exports={InboundSpool};
