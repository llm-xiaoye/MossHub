export type RecordValue = Record<string, any>;
export class ApiError extends Error {
  constructor(public status:number, public code:string, message:string) { super(message); }
}
export async function api<T = RecordValue>(path:string, options:RequestInit = {}, instance?:string, key?:string, chatKey=false):Promise<T> {
  const headers = new Headers(options.headers);
  if (instance) headers.set("X-RAG-Data-Instance",instance);
  if (key) headers.set(chatKey ? "Idempotency-Key" : "X-Request-ID",key);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type","application/json");
  const response = await fetch(path,{...options,headers,credentials:"same-origin"});
  if (response.status === 204) return undefined as T;
  const value = await response.json().catch(() => null);
  if (!response.ok) {
    const detail=typeof value?.detail==="string" ? value.detail : typeof value?.message==="string" ? value.message : response.status===422 ? "提交的信息不完整或格式不正确。请检查已填写的内容；已保存的资料仍在。" : "这次操作暂未完成。已保存的资料仍在，请稍后读取原状态。";
    throw new ApiError(response.status,typeof value?.code==="string" ? value.code : "HTTP_"+response.status,detail);
  }
  if (value === null || typeof value !== "object") throw new ApiError(502,"INVALID_RESPONSE","后端返回了无法读取的响应。");
  return value as T;
}
export function post<T = RecordValue>(path:string, body:unknown, instance:string, key?:string, chatKey=false) {
  return api<T>(path,{method:"POST",body:JSON.stringify(body)},instance,key,chatKey);
}
export function describeError(error:unknown) {
  if (error instanceof Error) return error.message;
  if(error&&typeof error==="object"){
    const value=error as Record<string,unknown>;
    for(const key of ["message","detail","user_message"]){
      const text=value[key];
      if(typeof text==="string"&&text.trim())return text;
    }
  }
  return typeof error === "string" && error.trim() ? error : "操作未完成。";
}
export function obsidianFailure(error:unknown):Error {
  if(error instanceof ApiError && error.status===404 && error.code==="HTTP_404")
    return new ApiError(404,"OBSIDIAN_ENDPOINT_UNAVAILABLE","本机后端尚未提供这个 Obsidian 功能接口。请更新并重启后端服务后重试；无需重新导入笔记。");
  return error instanceof Error ? error : new Error(describeError(error));
}
export function sourceAccessDenied(error:unknown):boolean {
  if(!(error instanceof ApiError))return false;
  return [401,403,410].includes(error.status)
    || (error.status===404 && error.code!=="HTTP_404")
    || ["DATA_INSTANCE_MISMATCH","RECOVERY_CLOSED"].includes(error.code);
}
export type Submission = {version:1; instance:string; key:string; path:string; body:unknown; phase:"AWAITING_CONFIRMATION"|"ACCEPTED"|"REJECTED"; requestId?:string; createdAt:string};
const pendingPrefix = "raghub.submission.";
export function persistSubmission(path:string,body:unknown,instance:string):Submission {
  const value:Submission = {version:1,instance,key:crypto.randomUUID(),path,body,phase:"AWAITING_CONFIRMATION",createdAt:new Date().toISOString()};
  localStorage.setItem(pendingPrefix+value.key,JSON.stringify(value));
  return value;
}
export function acceptSubmission(value:Submission, requestId:string) {
  const accepted:Submission = {...value,phase:"ACCEPTED",requestId};
  localStorage.setItem(pendingPrefix+value.key,JSON.stringify(accepted)); return accepted;
}
export function rejectSubmission(value:Submission,status:number){
  if(status!==422||value.phase!=="AWAITING_CONFIRMATION")throw new Error("只有明确未受理的格式错误才能标记为已拒绝。");
  const rejected:Submission={...value,phase:"REJECTED"};
  localStorage.setItem(pendingPrefix+value.key,JSON.stringify(rejected));return rejected;
}
export function submissionInventory():{records:Submission[];invalidCount:number} {
  const records:Submission[]=[];let invalidCount=0;
  try { for(let i=0;i<localStorage.length;i++){
    const storageKey=localStorage.key(i);if(!storageKey?.startsWith(pendingPrefix))continue;
    try {const value=JSON.parse(localStorage.getItem(storageKey)??"null");
      const valid=value?.version===1&&typeof value.instance==="string"&&value.instance.length>0&&typeof value.key==="string"&&storageKey===pendingPrefix+value.key&&typeof value.path==="string"&&value.path.startsWith("/api/")&&value.body!==undefined&&typeof value.createdAt==="string"&&(value.phase==="AWAITING_CONFIRMATION"||value.phase==="ACCEPTED"||value.phase==="REJECTED")&&(value.phase!=="ACCEPTED"||typeof value.requestId==="string"&&value.requestId.length>0);
      if(valid)records.push(value);else invalidCount++;
    }catch{invalidCount++;}
  }}catch{invalidCount++;}
  return {records:records.sort((a,b)=>b.createdAt.localeCompare(a.createdAt)),invalidCount};
}
export function savedSubmissions():Submission[]{return submissionInventory().records;}
export function clearConversationSubmissions(id:string,instance:string){
  for(const item of savedSubmissions())if(item.instance===instance&&(item.path==="/api/v1/conversations/"+id+"/questions"||item.path==="/api/v1/conversations"&&item.requestId===id))localStorage.removeItem(pendingPrefix+item.key);
}
export type ConversationDeletion = {version:1;instance:string;kb:string;conversationId:string;title:string;createdAt:string};
const deletionPrefix = "raghub.conversation-deletion.";
function deletionStorageKey(value:Pick<ConversationDeletion,"instance"|"conversationId">){return deletionPrefix+value.instance+"."+value.conversationId;}
export function conversationDeletionInventory():{records:ConversationDeletion[];invalidCount:number}{
  const records:ConversationDeletion[]=[];let invalidCount=0;
  try{for(let i=0;i<localStorage.length;i++){
    const key=localStorage.key(i);if(!key?.startsWith(deletionPrefix))continue;
    try{const value=JSON.parse(localStorage.getItem(key)??"null");
      const valid=value?.version===1&&typeof value.instance==="string"&&value.instance.length>0&&typeof value.kb==="string"&&value.kb.length>0&&typeof value.conversationId==="string"&&value.conversationId.length>0&&typeof value.title==="string"&&typeof value.createdAt==="string"&&Number.isFinite(Date.parse(value.createdAt))&&key===deletionStorageKey(value);
      if(valid)records.push(value);else invalidCount++;
    }catch{invalidCount++;}
  }}catch{invalidCount++;}
  return {records:records.sort((a,b)=>b.createdAt.localeCompare(a.createdAt)),invalidCount};
}
export function persistConversationDeletion(instance:string,kb:string,conversationId:string,title:string):ConversationDeletion{
  const value:ConversationDeletion={version:1,instance,kb,conversationId,title,createdAt:new Date().toISOString()};
  localStorage.setItem(deletionStorageKey(value),JSON.stringify(value));return value;
}
export async function deleteConversation(id:string,instance:string):Promise<"deleted"|"already_absent">{
  if(!id||!instance)throw new Error("会话或数据实例缺失，无法确认删除结果。");
  try{await api("/api/v1/conversations/"+encodeURIComponent(id),{method:"DELETE"},instance);return "deleted";}
  catch(error){if(error instanceof ApiError&&error.status===404&&error.code==="CONVERSATION_NOT_FOUND")return "already_absent";throw error;}
}
export function completeConversationDeletion(value:ConversationDeletion,currentInstance:string){
  if(value.instance!==currentInstance)throw new Error("数据实例已变化，保留删除待收尾记录。");
  clearConversationSubmissions(value.conversationId,value.instance);
  const activeKey="raghub.active."+value.instance+"."+value.kb;
  if(localStorage.getItem(activeKey)===value.conversationId)localStorage.removeItem(activeKey);
  localStorage.removeItem(deletionStorageKey(value));
}
export async function persistentPost<T = RecordValue>(path:string,body:unknown,instance:string,chatKey=false) {
  const saved=persistSubmission(path,body,instance);
  const value=await post<T>(path,body,instance,saved.key,chatKey);
  const id=(value as RecordValue).request_id ?? (value as RecordValue).conversation_id ?? (value as RecordValue).batch_id ?? (value as RecordValue).task_id ?? (value as RecordValue).workflow_id;
  if (id) acceptSubmission(saved,id);
  return value;
}
