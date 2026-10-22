import {useCallback,useEffect,useRef,useState} from "react";
import {Alert,App,Button,Pagination,Space,Table,Tag} from "antd";
import {Link} from "react-router-dom";
import {api,post,describeError,type RecordValue} from "./api";
import ErrorNotice from "./ErrorNotice";

type Props={instance:string;kb:string;onProblem:(item:RecordValue)=>void;onSettings:(item:RecordValue)=>void;onSource:(item:RecordValue)=>void;onTasks:()=>void;onChange:()=>void};
const completed=new Set(["COMPLETE","PARTIAL_COMPLETE","CANCELLED"]);
export default function ImportProgress({instance,kb,onProblem,onSettings,onSource,onTasks,onChange}:Props){
  const {modal}=App.useApp();
  const [flow,setFlow]=useState<RecordValue|null>(null),[error,setError]=useState(""),[busy,setBusy]=useState(false),[page,setPage]=useState(1);
  const scope=useRef(""),flight=useRef(false),actionFlight=useRef(false),priorState=useRef("");
  const key=instance+"|"+kb;scope.current=key;
  const reload=useCallback(async()=>{
    if(!instance||!kb||flight.current)return;flight.current=true;
    try{
      const current=await api("/api/ui/obsidian/workflows?kb_id="+encodeURIComponent(kb),{},instance);
      let next=current.workflow;
      if(next&&page>1)next=await api("/api/ui/obsidian/workflows/"+next.workflow_id+"?offset="+(page-1)*50,{},instance);
      if(scope.current!==key)return;
      setFlow(next);setError("");
      if(next&&priorState.current!==next.workflow_id+"|"+next.state){priorState.current=next.workflow_id+"|"+next.state;onChange();}
    }catch(err){if(scope.current===key)setError(describeError(err));}finally{flight.current=false;}
  },[instance,kb,key,page,onChange]);
  useEffect(()=>{setFlow(null);setError("");setPage(1);priorState.current="";},[key]);
  useEffect(()=>{
    void reload();const timer=setInterval(()=>{if(!document.hidden)void reload();},2000);
    const visible=()=>{if(!document.hidden)void reload();};window.addEventListener("raghub:workflow-refresh",visible);document.addEventListener("visibilitychange",visible);
    return()=>{clearInterval(timer);window.removeEventListener("raghub:workflow-refresh",visible);document.removeEventListener("visibilitychange",visible);};
  },[reload]);
  async function act(action:string){
    if(!flow||actionFlight.current)return;actionFlight.current=true;setBusy(true);
    try{await post("/api/ui/obsidian/workflows/"+flow.workflow_id+"/actions",{action,...(action==="use-ready"?{ready_sha256:flow.ready_sha256}:{})},instance);await reload();onChange();}
    catch(err){setError(describeError(err));}finally{actionFlight.current=false;setBusy(false);}
  }
  function useReady(){if(!flow)return;modal.confirm({
    title:"先使用已通过的 "+flow.ready_count+" 份资料？",
    content:"只发布这次已通过检查的笔记；其余笔记保留原文和问题，不会绕过检查。库中原先已发布的资料继续保留；更新未通过时，原来可用的版本继续使用。",
    okText:"发布已通过资料",cancelText:"先处理问题",onOk:()=>act("use-ready"),
  });}
  if(!flow&&!error)return null;
  if(flow&&completed.has(flow.state)&&flow.total===0)return null;
  return <section className="surface stack import-progress" aria-label="导入处理进度" aria-busy={busy}>
    <div className="page-heading"><div><h2>{flow?.label??"读取导入进度"}</h2><p className="muted">归档原文 → 解析与质量检查 → 建库发布 → 可用于问答</p></div>
      {flow&&completed.has(flow.state)&&flow.state!=="CANCELLED"&&<Link className="ant-btn ant-btn-primary" to="/chat">开始提问</Link>}
    </div>
    {error&&<ErrorNotice error={error} stage="核对本批处理状态" retained="保留上次成功进度和已保存资料；当前显示可能过期。" next="先读取原状态，不重新导入，也不更换提交记录。" onRecover={()=>void reload()} recoverLabel="核对原处理状态"/>}
    {flow&&<>
      <p role="status" aria-live="polite">本批 {flow.total} 份 · {flow.ready_count} 份检查通过待建库 · {flow.published_count} 份可问答{flow.problem_count>0?" · "+flow.problem_count+" 份需要处理":""}</p>
      {flow.state==="WAITING_IMPORT"&&<Alert showIcon type="info" message="等待当前问答和本地计算结束" description="所选清单已保存。系统会按原规则切换后继续，你不需要再次提交。"/>}
      {flow.state==="WAITING_QUERY"&&<Alert showIcon type="info" message="资料已发布，等待其他后台工作收尾" description="收尾后自动开放问答；不会强行中断正在运行的任务。"/>}
      {flow.state==="NEEDS_ATTENTION"&&<Alert showIcon type="warning" message="请先处理下面的问题，或明确选择只使用已通过资料" description="未通过的笔记不会自动发布。已保存的原文和之前可用的版本保留。"/>}
      {flow.state==="PAUSED"&&<Alert showIcon type="info" message="已暂停后续步骤，已经提交的任务仍会执行" description={flow.build_task_id?"已提交的建库任务仍可能完成发布。请查看原任务；暂停不等于取消。":"已保存结果保留。恢复时先核对原任务，不重新导入。"}/>}
      {["NEEDS_ATTENTION","PAUSED","FAILED"].includes(flow.state)&&<p className="muted">{flow.has_previous_publication?"上次发布的版本仍保留，本次未通过的更新不会替换它。导入期间暂不能提问；如需先使用旧版，可暂停后续步骤，再点击上方“开始提问”。":"这是尚未完成发布的新资料，目前还不能用于问答。原文和已完成结果保留。"}</p>}
      {flow.error&&<ErrorNotice error={flow.error.message} stage={flow.error.code==="INDEX_STOPPED"?"建立索引":"连续处理"} retained="已保存的原文和解析结果保留；已有发布版本继续保留。" next="查看原任务和具体原因。无法确认受理结果时先核对状态，不再点一次导入。"/>}
      <Table size="small" rowKey="ordinal" dataSource={flow.items.filter((item:RecordValue)=>!item.document_id||item.source_available!==false)} pagination={false} scroll={{x:560}} columns={[
        {title:"笔记",render:(_:unknown,item:RecordValue)=><div><strong>{item.name}</strong>{item.reused&&<div className="muted">已存在可用处理结果，本次复用</div>}</div>},
        {title:"进度",render:(_:unknown,item:RecordValue)=><div><Tag color={item.state==="PUBLISHED"?"green":["FAILED","REVIEW_REQUIRED"].includes(item.state)?"gold":"default"}>{item.label}</Tag>{item.issue&&<p>{item.issue.message}</p>}</div>},
        {title:"操作",render:(_:unknown,item:RecordValue)=><Space wrap>
          {item.raw_version_id&&item.source_available!==false&&<Button size="small" onClick={()=>onSource(item)}>查看原文</Button>}
          {item.state==="REVIEW_REQUIRED"&&<Button size="small" onClick={()=>onProblem(item)}>处理问题</Button>}
          {item.state==="FAILED"&&item.issue?.code==="PARSE_CONFIG_INVALID"?<Button size="small" onClick={()=>onSettings(item)}>设置处理范围</Button>:item.state==="FAILED"&&<Button size="small" onClick={onTasks}>查看原任务</Button>}
        </Space>}
      ]}/>
      {flow.total>50&&<Pagination size="small" current={page} pageSize={50} total={flow.total} showSizeChanger={false} onChange={setPage}/>}
      <Space wrap>
        {flow.can_use_ready&&<Button type="primary" disabled={busy||!!error} onClick={useReady}>先使用已通过的 {flow.ready_count} 份资料</Button>}
        {flow.state==="FAILED"&&<Button disabled={busy||!!error} onClick={onTasks}>查看原任务与恢复方式</Button>}
        {["PAUSED","FAILED"].includes(flow.state)&&<Button disabled={busy||!!error} onClick={()=>void act("resume")}>核对原任务后继续</Button>}
        {!completed.has(flow.state)&&flow.state!=="PAUSED"&&<Button disabled={busy||!!error} onClick={()=>void act("pause")}>暂停后续步骤</Button>}
        {["PAUSED","FAILED","NEEDS_ATTENTION"].includes(flow.state)&&<Button disabled={busy||!!error} onClick={()=>modal.confirm({title:"结束本批自动处理？",content:"已接收的任务不会重复提交；系统先确认后台任务结束。已保存的原文和处理结果保留，尚未发布的资料不自动用于问答。",okText:"确认结束",cancelText:"保留进度",onOk:()=>act("finish")})}>结束本批处理</Button>}
      </Space>
      <></>
    </>}
  </section>;
}
