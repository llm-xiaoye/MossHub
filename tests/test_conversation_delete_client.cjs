"use strict";
// Ordinary client contract checks use actual api.ts and simulated HTTP responses.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const ts = require("../frontend/node_modules/typescript");
const sourcePath = require("node:path").resolve(__dirname, "../frontend/src/api.ts");
const source = fs.readFileSync(sourcePath, "utf8");
const compiled = ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
const INSTANCE = "11111111-1111-4111-8111-111111111111";
const OTHER_INSTANCE = "99999999-9999-4999-8999-999999999999";
const ID = "22222222-2222-4222-8222-222222222222";
const KB = "33333333-3333-4333-8333-333333333333";
const OTHER_ID = "44444444-4444-4444-8444-444444444444";
const pendingPrefix = "raghub.submission.";
const deletionPrefix = "raghub.conversation-deletion.";
function storage(seed=[]){
  const items = new Map(seed);
  return {get length(){return items.size;},key:index=>Array.from(items.keys())[index]??null,getItem:key=>items.get(key)??null,setItem:(key,value)=>items.set(key,String(value)),removeItem:key=>items.delete(key),snapshot:()=>Array.from(items.entries())};
}
function client(localStorage,responder){
  const calls=[];
  const moduleRecord={exports:{}};
  const fetch=async (path,options)=>{calls.push({path,method:options.method,instance:options.headers.get("X-RAG-Data-Instance"),credentials:options.credentials});return responder(path,options);};
  vm.runInNewContext(compiled,{module:moduleRecord,exports:moduleRecord.exports,localStorage,Headers,FormData,fetch},{filename:sourcePath});
  return {api:moduleRecord.exports,calls};
}
function jsonResponse(status,value){return new Response(JSON.stringify(value),{status,headers:{"Content-Type":"application/json"}});}
function submission(key,instance,conversationId,path="/api/v1/conversations/"+conversationId+"/questions"){
  return [pendingPrefix+key,JSON.stringify({version:1,instance,key,path,body:{question:"disposable question"},phase:"AWAITING_CONFIRMATION",requestId:conversationId,createdAt:"2026-10-11T00:00:00.000Z"})];
}
test("successful DELETE carries the original instance and returns deleted",async()=>{
  const local=storage(),loaded=client(local,()=>new Response(null,{status:204}));
  assert.equal(await loaded.api.deleteConversation(ID,INSTANCE),"deleted");
  assert.deepEqual(loaded.calls,[{path:"/api/v1/conversations/"+ID,method:"DELETE",instance:INSTANCE,credentials:"same-origin"}]);
});
test("lost response survives page reload and exact business404 closes only original-instance records",async()=>{
  const local=storage([submission("current-question",INSTANCE,ID),submission("current-create",INSTANCE,ID,"/api/v1/conversations"),submission("other-instance",OTHER_INSTANCE,ID),submission("other-conversation",INSTANCE,OTHER_ID),[pendingPrefix+"invalid","not json"],[deletionPrefix+"invalid","not json"],["raghub.active."+INSTANCE+"."+KB,ID],["raghub.active."+OTHER_INSTANCE+"."+KB,ID]]);
  const first=client(local,()=>{throw new TypeError("simulated deletion response lost");});
  const original=first.api.persistConversationDeletion(INSTANCE,KB,ID,"待收尾的原会话");
  first.api.persistConversationDeletion(OTHER_INSTANCE,KB,ID,"另一实例的同 ID 会话");
  await assert.rejects(first.api.deleteConversation(ID,INSTANCE),/simulated deletion response lost/);
  assert.equal(first.api.conversationDeletionInventory().records.length,2);
  // A fresh module and storage snapshot represent reload; the server list can now be empty.
  const reloadedStorage=storage(local.snapshot());
  const reloaded=client(reloadedStorage,()=>jsonResponse(404,{code:"CONVERSATION_NOT_FOUND",detail:"会话不存在。"}));
  const pending=reloaded.api.conversationDeletionInventory().records.find(value=>value.instance===INSTANCE);
  assert.equal(pending.conversationId,ID);assert.equal(pending.kb,KB);assert.equal(pending.title,original.title);
  assert.equal(await reloaded.api.deleteConversation(pending.conversationId,pending.instance),"already_absent");
  reloaded.api.completeConversationDeletion(pending,INSTANCE);
  assert.equal(reloadedStorage.getItem(pendingPrefix+"current-question"),null);
  assert.equal(reloadedStorage.getItem(pendingPrefix+"current-create"),null);
  assert.equal(reloadedStorage.getItem("raghub.active."+INSTANCE+"."+KB),null);
  for(const key of [pendingPrefix+"other-instance",pendingPrefix+"other-conversation",pendingPrefix+"invalid",deletionPrefix+"invalid","raghub.active."+OTHER_INSTANCE+"."+KB,deletionPrefix+OTHER_INSTANCE+"."+ID])assert.notEqual(reloadedStorage.getItem(key),null);
  assert.equal(reloaded.api.conversationDeletionInventory().records.length,1);
  assert.equal(reloaded.api.conversationDeletionInventory().invalidCount,1);
  assert.equal(reloaded.calls[0].instance,INSTANCE);
});
const rejectedResponses=[
  ["generic JSON404",()=>jsonResponse(404,{detail:"Not Found"})],
  ["other business404",()=>jsonResponse(404,{code:"QUESTION_NOT_FOUND",detail:"请求不存在"})],
  ["non-JSON404",()=>new Response("Not Found",{status:404})],
  ["instance mismatch",()=>jsonResponse(409,{code:"DATA_INSTANCE_CHANGED",detail:"实例已变化"})],
  ["cleanup pending",()=>jsonResponse(409,{code:"CONVERSATION_CLEANUP_PENDING",detail:"本地文件未清理"})],
  ["active request",()=>jsonResponse(409,{code:"CHAT_BUSY",detail:"请求仍在执行"})],
  ["unknown network error",()=>{throw new TypeError("simulated network failure");}]
];
for(const [name,response] of rejectedResponses)test(name+" remains pending and preserves local records",async()=>{
  const local=storage([submission("current-question",INSTANCE,ID),["raghub.active."+INSTANCE+"."+KB,ID]]);
  const loaded=client(local,response);
  loaded.api.persistConversationDeletion(INSTANCE,KB,ID,"原会话");
  await assert.rejects(loaded.api.deleteConversation(ID,INSTANCE));
  assert.equal(loaded.api.conversationDeletionInventory().records.length,1);
  assert.notEqual(local.getItem(pendingPrefix+"current-question"),null);
  assert.equal(local.getItem("raghub.active."+INSTANCE+"."+KB),ID);
});
test("instance changes before completion preserve the original pending deletion and submissions",()=>{
  const local=storage([submission("current-question",INSTANCE,ID),["raghub.active."+INSTANCE+"."+KB,ID]]);
  const loaded=client(local,()=>new Response(null,{status:204}));
  const pending=loaded.api.persistConversationDeletion(INSTANCE,KB,ID,"原会话");
  assert.throws(()=>loaded.api.completeConversationDeletion(pending,OTHER_INSTANCE),/数据实例已变化/);
  assert.equal(loaded.api.conversationDeletionInventory().records.length,1);
  assert.notEqual(local.getItem(pendingPrefix+"current-question"),null);
  assert.equal(local.getItem("raghub.active."+INSTANCE+"."+KB),ID);
  assert.equal(loaded.calls.length,0);
});
test("empty instance is rejected before any fetch",async()=>{
  const loaded=client(storage(),()=>new Response(null,{status:204}));
  await assert.rejects(loaded.api.deleteConversation(ID,""),/数据实例缺失/);
  assert.equal(loaded.calls.length,0);
});

