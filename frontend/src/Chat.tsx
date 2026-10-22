import {lazy,useCallback,useEffect,useRef,useState} from "react";
import {createPortal} from "react-dom";
import {ArrowDownOutlined,FileTextOutlined,MenuOutlined,MoreOutlined,PlusOutlined,SendOutlined} from "@ant-design/icons";
import {useChatScroll} from "./useChatScroll";
import BrandIcon from "./BrandIcon";
import {Alert,App,Button,Dropdown,Empty,Input,Space,Spin,Tag,Typography} from "antd";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type {Context} from "./App";
import {api,acceptSubmission,completeConversationDeletion,conversationDeletionInventory,deleteConversation,describeError,persistConversationDeletion,persistSubmission,persistentPost,post,savedSubmissions,submissionInventory,type RecordValue} from "./api";
import DetailsDrawer from "./DetailsDrawer";
import ErrorNotice from "./ErrorNotice";
import LoadBoundary from "./LoadBoundary";
const SourceDrawer=lazy(()=>import("./SourceDrawer"));
const terminal=new Set(["SUCCEEDED","FAILED","CANCELLED","INTERRUPTED"]);
const labels:Record<string,string>={SUCCEEDED:"已完成",FAILED:"失败",CANCELLED:"已取消",INTERRUPTED:"已中断",RUNNING:"处理中",CANCELLING:"正在取消"};
function Answer({result,onSource}:{result:RecordValue;requestId:string;onSource:(value:any)=>void;onDetails:(value:string)=>void}){
  const documents=new Map<string,RecordValue>();
  for(const paragraph of result.paragraphs??[])for(const ref of paragraph.references??[]){
    if(typeof ref.document_id!=="string"){if(ref.deleted)documents.set("deleted",ref);continue;}
    const key=ref.document_id+"|"+(ref.raw_version_id??"");
    if(!documents.has(key))documents.set(key,ref);
  }
  return <div className="answer assistant-message" aria-label="模型回答"><BrandIcon size={28}/><div className="answer-body">
    {result.result_type&&result.result_type!=="facts"&&<Tag>{{facts:"依据资料回答",clarify:"需要补充",abstain:"证据不足"}[result.result_type as string]??result.result_type}</Tag>}
    {(result.paragraphs??[]).map((paragraph:RecordValue,index:number)=><div key={index}><ReactMarkdown remarkPlugins={[remarkGfm]} allowedElements={["p","h1","h2","h3","strong","em","ul","ol","li","blockquote","pre","code","table","thead","tbody","tr","th","td","br","hr","del"]} unwrapDisallowed skipHtml components={{a:({children})=><span>{children}</span>}}>{paragraph.text}</ReactMarkdown></div>)}
    {!(result.paragraphs?.length)&&result.text&&<p style={{whiteSpace:"pre-wrap"}}>{result.text}</p>}
    {result.clarification_question&&<p>{result.clarification_question}</p>}
    {documents.size>0&&<div className="reference-row" aria-label="来源文档">{Array.from(documents.entries()).map(([key,ref],index)=><Button size="small" icon={<FileTextOutlined/>} key={key} disabled={!!ref.deleted} onClick={()=>onSource({document_id:ref.document_id,raw_version_id:ref.raw_version_id})}>{ref.deleted?"资料已删除":ref.display_name??(documents.size===1?"查看来源文档":"查看文档 "+(index+1))}</Button>)}</div>}
    {result.limitations?.length>0&&<Alert type="info" message="回答边界" description={result.limitations.join("；")}/>}
    {result.warnings?.length>0&&<Alert type="warning" message="记忆维护提示" description={result.warnings.map((v:any)=>typeof v==="string"?v:typeof v?.message==="string"?v.message:"记忆整理暂未完成，请稍后继续。").join("；")}/>}
  </div></div>;
}
const progressLabels:Record<string,string>={UNDERSTAND:"正在理解问题",RECALL:"正在检索资料",RERANK:"正在筛选相关内容",ASSEMBLE:"正在整理回答依据",GENERATE:"正在生成回答",VALIDATE:"正在核对引用并保存"};
function questionFailure(value:RecordValue){
  if(value.error?.error_type!=="TimeoutError")return describeError(value.error);
  const names:Record<string,string>={UNDERSTAND:"理解问题",RECALL:"检索资料",RERANK:"资料排序",ASSEMBLE:"整理回答依据",GENERATE:"生成回答",VALIDATE:"核对回答"};
  const progress=value.progress,name=names[progress?.stage]??"本轮处理";
  const count=progress?.stage==="RERANK"&&Number.isInteger(progress.completed)&&Number.isInteger(progress.total)?"（已检查 "+progress.completed+" 段，共 "+progress.total+" 段）":"";
  return name+"超时"+count+"。本轮没有保存正式回答。";
}
function Progress({value}:{value:RecordValue|undefined}){const label=value&&progressLabels[value.stage];if(!label)return null;const counted=Number.isInteger(value.completed)&&Number.isInteger(value.total)&&value.completed>=0&&value.total>=value.completed;return <span role="status" aria-live="polite">{label}{counted?(value?.stage==="RERANK"?" · 已检查 "+value.completed+" 段，共 "+value.total+" 段":" · 已完成 "+value!.completed+" / "+value!.total+" 项"):""}</span>;}
export default function Chat({state,kb,kbName,stale,sidebarSlot,openSidebar,closeSidebar,onQuestionActivity}:Context){
  const {message,modal}=App.useApp();
  const [list,setList]=useState<RecordValue[]>([]),[selected,setSelected]=useState(""),[conversation,setConversation]=useState<RecordValue|null>(null),[current,setCurrent]=useState<RecordValue|null>(null),[question,setQuestion]=useState(""),[error,setError]=useState(""),[recovery,setRecovery]=useState(""),[busy,setBusy]=useState(false),[allowed,setAllowed]=useState<string[]>([]),[detail,setDetail]=useState<string|null>(null),[source,setSource]=useState<any>(null);
  const [pendingQuestion,setPendingQuestion]=useState<{instance:string;conversationId:string;text:string}|null>(null);
  const stream=useRef<EventSource|null>(null),terminalSeen=useRef(new Set<string>()),selectedRef=useRef(selected),followEpoch=useRef(0);
  selectedRef.current=selected;
  const instance=state?.data_instance_id??"";
  useEffect(()=>{if(current?.request_id)onQuestionActivity?.(current.request_id,!terminal.has(current.state));},[current?.request_id,current?.state,onQuestionActivity]);
  const [listStale,setListStale]=useState(true),[conversationStale,setConversationStale]=useState(true);
  const submitting=useRef(false),loadedEarlier=useRef(false);
  const [loadingOlder,setLoadingOlder]=useState(false);
  const scroll=useChatScroll(instance+"|"+kb+"|"+selected,[conversation?.turns?.length??0,conversation?.turns?.[0]?.turn_id??"",current?.request_id??"",current?.draft?.revision??0,current?.state??"",current?.progress?.stage??""].join("|"));
  const [contextMenuId,setContextMenuId]=useState(""),[deleting,setDeleting]=useState("");
  const [deletionRecovery,setDeletionRecovery]=useState(conversationDeletionInventory);
  const scopeRef=useRef({instance,kb}),conversationButtons=useRef(new Map<string,HTMLButtonElement>()),newConversationButton=useRef<HTMLButtonElement|null>(null),deleteDialog=useRef<ReturnType<typeof modal.confirm>|null>(null);
  scopeRef.current={instance,kb};
  useEffect(()=>{setContextMenuId("");deleteDialog.current?.destroy();deleteDialog.current=null;setDeletionRecovery(conversationDeletionInventory());},[instance,kb]);
  const requestOrigin=useRef<{id:string;instance:string}|null>(null);
  function unresolvedSubmissions(){const inventory=submissionInventory();return {invalid:inventory.invalidCount,records:inventory.records.filter(value=>(value.path==="/api/v1/conversations"||value.path.endsWith("/questions"))&&(value.phase==="AWAITING_CONFIRMATION"||value.instance!==instance))};}
  async function confirmNewOperation(){const pending=unresolvedSubmissions();if(!pending.records.length&&!pending.invalid)return true;return new Promise<boolean>(resolve=>modal.confirm({title:"明确发起新的操作？",content:"原提交仍保留，页面不会替它更换实例或重新发送。请先核对原会话；继续将使用一个新的请求键。",okText:"发起新操作",cancelText:"先核对原提交",onOk:()=>{resolve(true);},onCancel:()=>resolve(false)}));}
  const loadList=useCallback(async()=>{if(!instance||!kb)return;try{const result=await api("/api/ui/conversations?kb_id="+kb,{},instance);setList(result.items);const generations=await api("/api/knowledge-bases/"+kb+"/generations");setAllowed(generations.generations.filter((v:RecordValue)=>v.query_allowed).map((v:RecordValue)=>v.generation_id));setListStale(false);}catch(err){setListStale(true);setError(describeError(err));}},[instance,kb]);
  const loadConversation=useCallback(async(id:string,before?:number)=>{
    let value:RecordValue;
    try{value=await api("/api/ui/conversations/"+id+(before?"?before="+before:""),{},instance);setConversationStale(false);}catch(err){setConversationStale(true);throw err;}
    if(selectedRef.current===id){
      if(before){scroll.rememberPrepend();loadedEarlier.current=true;}
      setConversation(old=>{
        if(!old)return value;
        const merge=(first:RecordValue[],second:RecordValue[])=>Array.from(new Map([...first,...second].map(turn=>[turn.turn_id,turn])).values());
        if(before)return {...value,turns:merge(value.turns,old.turns)};
        return loadedEarlier.current?{...value,turns:merge(old.turns,value.turns),older_before:old.older_before}:value;
      });
    }
    return value;
  },[instance,scroll.rememberPrepend]);
  useEffect(()=>{setListStale(true);setConversationStale(true);setSelected("");setConversation(null);setCurrent(null);stream.current?.close();void loadList();const saved=localStorage.getItem("raghub.active."+instance+"."+kb);if(saved)setSelected(saved);},[instance,kb,loadList]);
  useEffect(()=>{
    if(!selected||!instance)return;let active=true;loadedEarlier.current=false;setLoadingOlder(false);setConversation(null);setBusy(true);setConversationStale(true);setError("");setCurrent(null);
    void loadConversation(selected).then(value=>{if(active&&value.active_requests.length)void follow(value.active_requests[0].request_id,instance);}).catch(err=>{if(active)setError(describeError(err));}).finally(()=>{if(active)setBusy(false);});
    localStorage.setItem("raghub.active."+instance+"."+kb,selected);return()=>{active=false;followEpoch.current+=1;stream.current?.close();};
  },[selected,instance,kb,loadConversation]);
  useEffect(()=>{
    if(!instance)return;const pending=unresolvedSubmissions();
    setRecovery(pending.records.length||pending.invalid?"有 "+pending.records.length+" 条问答提交尚未确认或属于另一实例，另有 "+pending.invalid+" 条记录不完整。页面不会自动重新提交。":"");
  },[instance]);
  async function follow(id:string,originalInstance:string){
    const epoch=++followEpoch.current;stream.current?.close();requestOrigin.current={id,instance:originalInstance};
    try{
      const value=await api("/api/ui/question-requests/"+id+"/summary",{},originalInstance);
      if(epoch!==followEpoch.current||selectedRef.current!==value.conversation_id)return;setCurrent(value);
      if(typeof value.question==="string")setPendingQuestion(old=>old?.instance===originalInstance&&old.conversationId===value.conversation_id?null:old);
      if(terminal.has(value.state)){terminalSeen.current.add(id);void loadConversation(value.conversation_id).catch(err=>setError(describeError(err)));return;}
      const events=new EventSource("/api/v1/question-requests/"+id+"/events?data_instance_id="+encodeURIComponent(originalInstance));stream.current=events;
      for(const kind of ["snapshot","done"])events.addEventListener(kind,event=>{
        try{const next=JSON.parse((event as MessageEvent).data);if(epoch!==followEpoch.current||selectedRef.current!==next.conversation_id||terminalSeen.current.has(id))return;setCurrent(next);if(terminal.has(next.state)){terminalSeen.current.add(id);events.close();void loadConversation(next.conversation_id).catch(err=>setError(describeError(err)));}}catch{setError("事件格式无法读取，已停止订阅。");events.close();}
      });
      events.addEventListener("draft",event=>{try{const next=JSON.parse((event as MessageEvent).data);if(epoch===followEpoch.current&&!terminalSeen.current.has(id))setCurrent(old=>old?.request_id===id?{...old,draft:{revision:next.revision,text:next.text,validated:false}}:old);}catch{events.close();setError("草稿事件无法读取。");}});
      events.onerror=()=>{if(epoch!==followEpoch.current){events.close();return;}setError("连接暂时中断。已保留请求身份，点击恢复读取；不会自动重复提交。");events.close();};
    }catch(err){if(epoch!==followEpoch.current)return;setError(describeError(err));setRecovery("无法确认上次操作结果；当前查询失败不代表此前没有执行。");}
  }
  async function create(){
    if(stale||listStale||submitting.current||!await confirmNewOperation())return;if(submitting.current)return;
    submitting.current=true;setBusy(true);setError("");try{const value=await persistentPost("/api/v1/conversations",{kb_id:kb},instance,true);setSelected(value.conversation_id);closeSidebar?.();await loadList();}catch(err){setError(describeError(err));setRecovery("会话创建结果待确认。请先读取原提交，不重复创建。");}finally{submitting.current=false;setBusy(false);}
  }
  async function send(){
    if(stale||listStale||conversationStale||submitting.current||!question.trim()||!selected||!await confirmNewOperation())return;if(submitting.current)return;submitting.current=true;setBusy(true);setError("");
    try{
      const body={question:question.trim()},path="/api/v1/conversations/"+selected+"/questions";
      const saved=persistSubmission(path,body,instance);
      setPendingQuestion({instance,conversationId:selected,text:body.question});
      const value=await post(path,body,instance,saved.key,true);
      acceptSubmission(saved,value.request_id);setQuestion("");setCurrent({...value,question:body.question});setPendingQuestion(null);await follow(value.request_id,saved.instance);
    }catch(err){setError(describeError(err));setRecovery("提交结果待确认。请先读取原提交状态，避免重复操作。");}finally{submitting.current=false;setBusy(false);}
  }
  async function confirmSaved(){
    for(const saved of savedSubmissions().filter(v=>v.phase==="AWAITING_CONFIRMATION"&&v.instance===instance&&(v.path==="/api/v1/conversations"||v.path.endsWith("/questions")))){
      try{const result=await api("/api/ui/submissions/"+saved.key,{},saved.instance);const id=result.request_id??result.conversation_id;acceptSubmission(saved,id);if(result.conversation_id)setSelected(result.conversation_id);if(result.request_id)await follow(result.request_id,saved.instance);}
      catch(err){setRecovery("未能确认原提交："+describeError(err)+" 请核对会话后，由你明确发起新的操作。");return;}
    }const pending=unresolvedSubmissions();setRecovery(pending.records.length||pending.invalid?"仍有属于旧实例或不完整的记录。已停止自动恢复，原记录保留。":"");
  }
  function remove(id:string,title:string){
    if(stale||listStale||!id||!instance||deleteDialog.current)return;
    setContextMenuId("");
    if(selectedRef.current===id&&current&&!terminal.has(current.state)){void message.info("请先停止问答，并等待请求结束后再删除会话。");return;}
    const deletingInstance=instance,deletingKb=kb;
    deleteDialog.current=modal.confirm({
      title:"删除这个会话？",
      content:<><p style={{overflowWrap:"anywhere",marginTop:0}}>{title||"新会话"}</p><p>将永久删除本地聊天记录、记忆摘要、问答与检索详情，以及浏览器中的相关记录。删除后无法恢复。</p></>,
      okText:"删除会话",okButtonProps:{danger:true},cancelText:"保留会话",
      afterClose:()=>{deleteDialog.current=null;(conversationButtons.current.get(id)??newConversationButton.current)?.focus();},
      onOk:async()=>{
        if(scopeRef.current.instance!==deletingInstance||scopeRef.current.kb!==deletingKb){void message.error("知识库或数据实例已变化，请重新选择会话。");throw new Error("Conversation scope changed");}
        setDeleting(id);
        try{
          const pendingDeletion=persistConversationDeletion(deletingInstance,deletingKb,id,title||"新会话");
          setDeletionRecovery(conversationDeletionInventory());
          const outcome=await deleteConversation(id,deletingInstance);
          completeConversationDeletion(pendingDeletion,scopeRef.current.instance);
          setDeletionRecovery(conversationDeletionInventory());
          void message.success(outcome==="already_absent"?"会话已不存在，相关本地记录已清理":"会话及本地记录已删除");
          if(scopeRef.current.kb!==deletingKb)return;
          setList(previous=>previous.filter(item=>item.conversation_id!==id));
          if(selectedRef.current===id){
            followEpoch.current++;stream.current?.close();selectedRef.current="";setSelected("");setConversation(null);setCurrent(null);setDetail(null);setSource(null);setQuestion("");setError("");setBusy(false);
          }
          const pending=unresolvedSubmissions();setRecovery(pending.records.length||pending.invalid?"仍有属于旧实例或不完整的记录。已停止自动恢复，原记录保留。":"");

        }catch(caught){void message.error("删除未完成："+describeError(caught));throw caught;}
        finally{setDeleting("");setDeletionRecovery(conversationDeletionInventory());}
      }
    });
  }
  const active=current&&!terminal.has(current.state),readOnly=conversation&&(conversation.read_only||!allowed.includes(conversation.generation_id));
  const currentInHistory=current?.request_id&&conversation?.turns.some((turn:RecordValue)=>turn.request_id===current.request_id);
  const currentDeletions=deletionRecovery.records.filter(item=>item.instance===instance&&item.kb===kb);
  const otherDeletions=deletionRecovery.records.length-currentDeletions.length;
  const displayList:RecordValue[]=[...currentDeletions.filter(item=>!list.some(row=>row.conversation_id===item.conversationId)).map(item=>({conversation_id:item.conversationId,title:item.title,created_at:item.createdAt,deletion_pending:true,deletion_only:true})),...list.map(item=>({...item,deletion_pending:currentDeletions.some(saved=>saved.conversationId===item.conversation_id)}))];
  const canCreate=!stale&&!listStale&&!busy&&!!kb&&!!instance&&state?.mode==="QUERY"&&!deleting;
  const canSend=!stale&&!listStale&&!conversationStale&&!busy&&!!question.trim()&&!readOnly&&!active&&state?.mode==="QUERY";
  const title=conversation?.title??list.find(item=>item.conversation_id===selected)?.title??(selected?"新会话":"知识问答");
  const dayLabel=(created:string)=>{
    const date=new Date(created);if(!Number.isFinite(date.getTime()))return "较早";
    const today=new Date(),yesterday=new Date();yesterday.setDate(today.getDate()-1);
    const day=(value:Date)=>value.getFullYear()+"-"+value.getMonth()+"-"+value.getDate();
    return day(date)===day(today)?"今天":day(date)===day(yesterday)?"昨天":date.toLocaleDateString("zh-CN",{month:"long",day:"numeric"});
  };
  async function loadEarlier(){
    if(!conversation?.older_before||loadingOlder)return;
    const id=selected;setLoadingOlder(true);
    try{await loadConversation(id,conversation.older_before);}catch(err){setError(describeError(err));}finally{if(selectedRef.current===id)setLoadingOlder(false);}
  }
  const sidebar=<aside className="chat-sidebar" aria-label="会话列表">
    <Button ref={newConversationButton} block type="primary" icon={<PlusOutlined/>} disabled={!canCreate} loading={busy} onClick={()=>void create()}>新建会话</Button>
    <div className="conversation-scroll" tabIndex={0} aria-label="历史会话">
      <ul className="conversation-list">{displayList.map((item,index)=><li key={item.conversation_id}>
        {(index===0||dayLabel(item.created_at)!==dayLabel(displayList[index-1].created_at))&&<div className="conversation-day">{dayLabel(item.created_at)}</div>}
        <Dropdown autoFocus trigger={["contextMenu"]} open={contextMenuId===item.conversation_id} onOpenChange={open=>setContextMenuId(open?item.conversation_id:"")} menu={{items:[{key:"delete",label:"删除会话",danger:true,disabled:!!deleting||(selected===item.conversation_id&&!!active)}],onClick:()=>remove(item.conversation_id,item.title??"新会话")}}>
          <button ref={element=>{if(element)conversationButtons.current.set(item.conversation_id,element);else conversationButtons.current.delete(item.conversation_id);}} className={selected===item.conversation_id?"active":""} aria-label={item.title??"新会话"} aria-current={selected===item.conversation_id?"true":undefined} aria-haspopup="menu" aria-expanded={contextMenuId===item.conversation_id} onClick={()=>{setContextMenuId("");if(item.deletion_only)remove(item.conversation_id,item.title??"新会话");else{setSelected(item.conversation_id);closeSidebar?.();}}} onKeyDown={event=>{if(event.key==="ContextMenu"||(event.shiftKey&&event.key==="F10")){event.preventDefault();setContextMenuId(item.conversation_id);}}}>
            <span className="conversation-title">{item.title??"新会话"}</span>{item.deletion_pending&&<small>删除待收尾{item.deletion_only?" · 点击继续":""}</small>}
          </button>
        </Dropdown>
      </li>)}</ul>
      {displayList.length===0&&<p className="sidebar-empty">新建会话，开始查阅你的笔记。</p>}
    </div>
  </aside>;
  return <section className="chat-page">
    {sidebarSlot&&createPortal(sidebar,sidebarSlot)}
    <header className="chat-header">
      <Button className="mobile-nav-toggle" type="text" aria-label="打开导航" icon={<MenuOutlined/>} onClick={openSidebar}/>
      <div className="chat-heading"><h1 title={title}>{title}</h1><p>{readOnly?"历史会话 · 使用当时的资料版本":state?.mode==="QUERY"?"基于知识库「"+kbName+"」回答":state?.mode==="IMPORT"?"正在处理资料，完成后开放问答":"正在核对系统状态"}</p></div>
      {selected&&<Dropdown trigger={["click"]} menu={{items:[{key:"delete",label:"删除会话",danger:true,disabled:stale||listStale||conversationStale||!!active||!!deleting}],onClick:()=>remove(selected,title)}}><Button className="conversation-actions" type="text" aria-label="会话操作" icon={<MoreOutlined/>}/></Dropdown>}
    </header>
    {(recovery||deletionRecovery.records.length>0||deletionRecovery.invalidCount>0)&&<div className="chat-feedback">
      {recovery&&<Alert showIcon type="warning" message="上次操作需要确认" description={recovery} action={<Button size="small" onClick={()=>void confirmSaved()}>读取原提交</Button>}/>}
      {(deletionRecovery.records.length>0||deletionRecovery.invalidCount>0)&&<Alert showIcon type="warning" message="删除操作待收尾" description={"当前知识库有 "+currentDeletions.length+" 条删除待确认，请右键标注为「删除待收尾」的会话，选择「删除会话」继续。另有 "+otherDeletions+" 条属于其他知识库或数据实例，"+deletionRecovery.invalidCount+" 条记录无法确认归属，均保留。页面不会自动删除。"}/>}
    </div>}
    <div className="chat-body"><Spin spinning={busy&&!conversation}>
      <div className="chat-scroll" ref={scroll.viewportRef} onScroll={scroll.onScroll} tabIndex={0} role="log" aria-live="off" aria-label="聊天记录">
        <div className={"chat-transcript"+(!selected?" empty-transcript":"")} ref={scroll.contentRef}>
          {!selected&&<div className="chat-empty"><BrandIcon size={72}/><h2>从笔记里找到答案</h2><p>选择已有会话，或新建会话开始提问。<br/>回答会附上对应的来源文档。</p><Button type="primary" icon={<PlusOutlined/>} disabled={!canCreate} loading={busy} onClick={()=>void create()}>新建会话</Button></div>}
          {selected&&<>
            {(listStale||conversationStale)&&<Alert type="warning" showIcon message="正在核对会话状态" description="状态确认后才能继续提问或删除。" action={<Button size="small" onClick={()=>{void loadList();void loadConversation(selected).catch(err=>setError(describeError(err)));}}>重新读取会话</Button>}/>}
            {readOnly&&<Alert type="info" message="这段会话使用的旧资料版本已停用，原回答保留。请新建会话，使用现有资料继续提问。"/>}
            {conversation?.older_before&&<div className="earlier-messages"><Button type="text" loading={loadingOlder} disabled={loadingOlder} onClick={()=>void loadEarlier()}>读取更早的对话</Button></div>}
            {conversation?.turns.map((turn:RecordValue)=><article className="chat-turn" key={turn.turn_id}><div className="question" aria-label="你的消息">{turn.user_text}</div>{turn.result?<Answer result={turn.result} requestId={turn.request_id??""} onSource={setSource} onDetails={setDetail}/>:<div className="answer" aria-label="模型回答"><p>{turn.assistant_text}</p></div>}</article>)}
            {conversation?.awaiting&&<Alert type="info" message="请补充一点信息" description={conversation.awaiting.question}/>}
            {pendingQuestion&&pendingQuestion.instance===instance&&pendingQuestion.conversationId===selected&&<article className="chat-turn"><div className="question" aria-label="你的消息">{pendingQuestion.text}</div><div className="status-line" role="status">{busy?"正在发送问题…":"发送结果待确认，请先读取原提交。"}</div></article>}
            {current&&!currentInHistory&&<article className="chat-turn">
              {typeof current.question==="string"&&<div className="question" aria-label="你的消息">{current.question}</div>}
              {!terminal.has(current.state)&&<div className="assistant-message working-answer"><BrandIcon active size={28}/><div className="answer-body"><div className="status-line"><span className="working-dot"/>{current.state==="CANCELLING"?"正在停止本轮回答":<><Progress value={current.progress}/>{!current.progress&&(labels[current.state]??"正在处理问题")}</>}</div><div className="draft"><p>{current.draft?.text||"这一步完成后会自动继续，请稍候。"}</p><small>回答尚在生成，完成检查后自动保存。</small></div></div></div>}
              {terminal.has(current.state)&&!current.result&&!current.error&&<div className="status-line">{labels[current.state]??current.state}</div>}
              {current.result&&<Answer result={current.result} requestId={current.request_id} onSource={setSource} onDetails={setDetail}/>}
              {current.error&&<ErrorNotice stage="本轮回答" error={current.error} summary={questionFailure(current)} retained="此前已保存的对话仍在；本轮未校验的内容不作为正式回答。" next="本轮已停止，不会自动重发。可以根据失败原因调整问题后，再提交一轮新的提问。"/>}
            </article>}
          </>}
          {error&&<ErrorNotice error={error} stage="读取或提交问答" retained="已保存的会话和原请求身份仍保留。" next="先恢复读取原状态；读取失败不能证明原提交未执行。" onRecover={current?()=>void follow(current.request_id,requestOrigin.current?.instance??instance):()=>void confirmSaved()} recoverLabel={current?"恢复读取":"读取原提交"}/>}
        </div>
      </div>
    </Spin>{scroll.showLatest&&<Button className="return-latest" shape="round" icon={<ArrowDownOutlined/>} onClick={scroll.jumpToLatest}>{scroll.hasUnread?"有新内容 · 回到最新":"回到最新"}</Button>}</div>
    {selected&&<div className="composer"><div className="composer-inner">
      {readOnly&&<div className="readonly-action"><span>历史会话只读</span><Button size="small" disabled={!canCreate} onClick={()=>void create()}>新建会话继续</Button></div>}
      <div className="composer-box"><Input.TextArea aria-label="问题" placeholder={readOnly?"历史会话只读，请新建会话继续":"继续问这份资料…"} value={question} onChange={event=>setQuestion(event.target.value)} onKeyDown={event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.nativeEvent.isComposing&&event.keyCode!==229){event.preventDefault();if(!event.repeat)void send();}}} maxLength={2000} autoSize={{minRows:2,maxRows:5}} disabled={stale||listStale||conversationStale||busy||!!readOnly||!!active||state?.mode!=="QUERY"}/>
        <Button className="send-question" type="primary" aria-label="发送问题" icon={<SendOutlined/>} disabled={!canSend} loading={busy} onClick={()=>void send()}>发送</Button>
      </div>
      <div className="composer-meta"><span>Enter 发送 · Shift+Enter 换行{question.length>1800?" · "+question.length+"/2000":""}</span>{active&&<Button size="small" danger disabled={current.state==="CANCELLING"} onClick={async()=>{try{await post("/api/v1/question-requests/"+current.request_id+"/cancel",{},instance);await follow(current.request_id,instance);}catch(err){setError(describeError(err));}}}>停止回答</Button>}</div>
    </div></div>}
    <LoadBoundary label="原文">{source&&<SourceDrawer target={source} instance={instance} onClose={()=>setSource(null)}/>}</LoadBoundary>
  </section>;
}
