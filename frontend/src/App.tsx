import {lazy,useEffect,useState,useCallback,useRef} from "react";
import {NavLink,Route,Routes,Navigate,useNavigate,useLocation} from "react-router-dom";
import {Button,Drawer,Select} from "antd";
import {CloseOutlined,CommentOutlined,FileTextOutlined,MenuOutlined} from "@ant-design/icons";
import {api,post,describeError,type RecordValue} from "./api";
import LoadBoundary from "./LoadBoundary";
import ErrorNotice from "./ErrorNotice";
import BrandIcon from "./BrandIcon";
const Documents=lazy(()=>import("./Documents"));
const Chat=lazy(()=>import("./Chat"));
const ObsidianChanges=lazy(()=>import("./ObsidianChanges"));
export type Context={state:RecordValue|null;kb:string;kbName:string;setKb:(value:string)=>void;refresh:()=>Promise<void>;stale:boolean;obsidianUpdate?:RecordValue|null;clearObsidianUpdate?:()=>void;sidebarSlot?:HTMLElement|null;openSidebar?:()=>void;closeSidebar?:()=>void;onQuestionActivity?:(requestId:string,active:boolean)=>void};
export default function App(){
  const navigate=useNavigate(),location=useLocation(),isChat=location.pathname==="/chat";
  const [questionActivity,setQuestionActivity]=useState<{id:string;instance:string}|null>(null);
  const onQuestionActivity=useCallback((id:string,active:boolean)=>{setQuestionActivity(previous=>active?{id,instance:stateRef.current}:previous?.id===id?null:previous);},[]);
  const stateRef=useRef("");
  const [obsidianUpdate,setObsidianUpdate]=useState<RecordValue|null>(null);
  const clearObsidianUpdate=useCallback(()=>setObsidianUpdate(null),[]);
  const [state,setState]=useState<RecordValue|null>(null),[kbs,setKbs]=useState<RecordValue[]>([]),[kb,setKb]=useState(""),[error,setError]=useState(""),[loading,setLoading]=useState(false),[stale,setStale]=useState(true);
  const [sidebarSlot,setSidebarSlot]=useState<HTMLElement|null>(null),[sidebarOpen,setSidebarOpen]=useState(false);
  const [narrow,setNarrow]=useState(()=>window.matchMedia("(max-width:860px)").matches);
  const closeSidebar=useCallback(()=>setSidebarOpen(false),[]),openSidebar=useCallback(()=>setSidebarOpen(true),[]);
  useEffect(()=>{const media=window.matchMedia("(max-width:860px)");const changed=()=>{setNarrow(media.matches);setSidebarOpen(false);};media.addEventListener("change",changed);return()=>media.removeEventListener("change",changed);},[]);
  useEffect(()=>setSidebarOpen(false),[location.pathname]);
  const refresh=useCallback(async()=>{
    try{
      const current=await api("/api/state");setState(current);
      const list=await api("/api/ui/knowledge-bases");
      setKbs(list.items);setKb(old=>list.items.some((item:RecordValue)=>item.kb_id===old)?old:(list.items[0]?.kb_id??""));setError("");setStale(false);
    }catch(err){setStale(true);setError(describeError(err));}
  },[]);
  useEffect(()=>{void refresh();const timer=setInterval(()=>{if(!document.hidden)void refresh();},5000);return()=>clearInterval(timer);},[refresh]);
  stateRef.current=state?.data_instance_id??"";
  useEffect(()=>{setObsidianUpdate(null);setQuestionActivity(null);},[state?.data_instance_id]);
  useEffect(()=>{setObsidianUpdate(null);},[kb]);
  useEffect(()=>{
    if(!questionActivity)return;
    let stopped=false,pending=false;
    const check=async()=>{
      if(pending||document.hidden)return;
      pending=true;
      try{
        const request=await api("/api/ui/question-requests/"+questionActivity.id+"/summary",{},questionActivity.instance);
        if(!stopped&&["SUCCEEDED","FAILED","CANCELLED","INTERRUPTED"].includes(request.state))
          setQuestionActivity(previous=>previous?.id===questionActivity.id?null:previous);
      }catch{/* Preserve the last confirmed activity until the request state can be read. */}
      finally{pending=false;}
    };
    const timer=setInterval(()=>void check(),5000);
    return()=>{stopped=true;clearInterval(timer);};
  },[questionActivity]);
  const importing=Boolean(!stale&&state?.import_workflow&&!["NEEDS_ATTENTION","PAUSED","FAILED","COMPLETE","PARTIAL_COMPLETE","CANCELLED"].includes(state.import_workflow.state));
  const processing=Boolean(questionActivity||importing);
  async function startQuestions(){
    if(!state||stale||loading)return;
    const flow=state.import_workflow;
    if(flow&&!["PAUSED","FAILED"].includes(flow.state)){setError("正在处理资料。请等待完成，或先处理资料页中的问题；不需要手动切换工作模式。");return;}
    setLoading(true);
    try{if(state.mode!=="QUERY")await post("/api/mode",{mode:"QUERY",kb_id:kb},state.data_instance_id);await refresh();navigate("/chat");closeSidebar();}
    catch(err){setError(describeError(err));}finally{setLoading(false);}
  }
  const context={state,kb,kbName:kbs.find(item=>item.kb_id===kb)?.name??kb,setKb:(value:string)=>{setKb(value);closeSidebar();},refresh,stale,obsidianUpdate,clearObsidianUpdate,sidebarSlot,openSidebar,closeSidebar,onQuestionActivity};
  const sidebar=<div className="sidebar-shell">
    <div className="sidebar-brand"><NavLink className="brand" to="/documents" aria-label="Moss Hub 首页"><BrandIcon active={processing}/><span>Moss Hub</span></NavLink>{narrow&&<Button type="text" aria-label="关闭导航" icon={<CloseOutlined/>} onClick={closeSidebar}/>}</div>
    <nav className="primary-nav" aria-label="主要导航">
      <NavLink to="/chat" onClick={event=>{event.preventDefault();void startQuestions();}} aria-disabled={stale||loading}><CommentOutlined/><span>知识问答</span></NavLink>
      <NavLink to="/documents" onClick={closeSidebar}><FileTextOutlined/><span>资料管理</span></NavLink>
    </nav>
    <div className="sidebar-library"><label htmlFor="current-library">当前知识库</label><Select id="current-library" aria-label="当前知识库" value={kb||undefined} placeholder="暂无知识库" options={kbs.map(item=>({value:item.kb_id,label:item.name}))} onChange={context.setKb}/></div>
    <div className="sidebar-slot" ref={setSidebarSlot}>{!isChat&&<div className="sidebar-guide"><strong>让笔记成为回答依据</strong><p>导入资料并发布后，即可在知识问答中使用。</p><Button block disabled={stale||!state||!kb||state.mode==="DRAINING"||!!state.import_workflow&&!["PAUSED","FAILED"].includes(state.import_workflow.state)} loading={loading} onClick={()=>void startQuestions()}>开始提问</Button></div>}</div>
    <div className="sidebar-footer">
      <div className="local-connection"><span className={"connection-dot"+(stale?" warning":"")}/>{stale?(state?"连接状态待核对":"正在连接本机"):"已连接本机"}</div>
      {state&&kb&&<LoadBoundary label="笔记更新"><ObsidianChanges key={state.data_instance_id+"|"+kb} compact instance={state.data_instance_id} kb={kb} enabled={!stale&&state.mode!=="DRAINING"} mode={state.mode} onUpdate={item=>{setObsidianUpdate({...item,data_instance_id:state.data_instance_id});closeSidebar();navigate("/documents");}}/></LoadBoundary>}
    </div>
  </div>;
  return <div className={"app-shell"+(narrow?" narrow":"")}>
    {narrow?<Drawer className="navigation-drawer" width={280} placement="left" open={sidebarOpen} onClose={closeSidebar} closable={false} forceRender styles={{body:{padding:0}}}>{sidebar}</Drawer>:<aside className="app-sidebar" aria-label="主导航与会话">{sidebar}</aside>}
    <main className={"app-main"+(isChat?" chat-main":" documents-main")}>
      {narrow&&!isChat&&<div className="mobile-appbar"><Button type="text" aria-label="打开导航" icon={<MenuOutlined/>} onClick={openSidebar}/><strong>Moss Hub</strong></div>}
      {error&&<div className="app-error"><ErrorNotice error={error} stage="读取系统状态" retained="保留上次成功的页面；当前状态可能已过期。" next="请刷新核对，确认前暂停新提交。" onRecover={()=>void refresh()} recoverLabel="刷新核对状态"/></div>}
      <div className={"route-content"+(isChat?" route-chat":" route-documents")}><LoadBoundary label="页面"><Routes><Route path="/documents" element={<Documents {...context}/>}/><Route path="/chat" element={<Chat {...context}/>}/><Route path="*" element={<Navigate to="/documents" replace/>}/></Routes></LoadBoundary></div>
    </main>
  </div>;
}
