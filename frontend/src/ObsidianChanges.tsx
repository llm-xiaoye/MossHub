import {useEffect,useRef,useState} from "react";
import {useLocation,useNavigate} from "react-router-dom";
import {Alert,Button,Drawer,Space,Table,Tag,Typography} from "antd";
import {SyncOutlined} from "@ant-design/icons";
import {ApiError,api,obsidianFailure,type RecordValue} from "./api";
import ErrorNotice from "./ErrorNotice";

type View={scope:string;items:RecordValue[];checkedAt:string};
type Props={instance:string;kb:string;enabled:boolean;mode:string;onUpdate:(item:RecordValue)=>void;compact?:boolean};
const states:Record<string,string>={CHANGED:"有本地修改",MISSING:"来源缺失",UNREADABLE:"暂不可读",NOT_READY:"尚未归档",UNCHANGED:"与归档一致"};
const stages:Record<string,string>={NEW:"已归档 · 待解析",PROCESSING:"解析中",REVIEW_REQUIRED:"待复核",READY:"已解析 · 待建库",FAILED:"处理失败",INTERRUPTED:"处理已中断"};

export default function ObsidianChanges({instance,kb,enabled,mode,onUpdate,compact=false}:Props){
  const navigate=useNavigate(),location=useLocation(),scope=instance+"|"+kb;
  const [expanded,setExpanded]=useState(false);
  const [view,setView]=useState<View|null>(null),[failure,setFailure]=useState<{scope:string;text:Error|string}>({scope:"",text:""}),[checking,setChecking]=useState(false);
  const refresh=useRef<()=>void>(()=>{});
  useEffect(()=>{
    let closed=false,inFlight=false,pendingVisible=false,controller:AbortController|null=null;
    const run=async(afterCurrent=false)=>{
      if(closed||document.hidden||!enabled||!instance||!kb)return;
      if(inFlight){if(afterCurrent)pendingVisible=true;return;}
      inFlight=true;pendingVisible=false;controller=new AbortController();setChecking(true);
      try{
        const rows:RecordValue[]=[],seen=new Set<string>();
        let cursor:string|null=null,checkedAt="";
        do{
          if(closed||document.hidden)return;
          const page:RecordValue=await api<RecordValue>("/api/ui/obsidian/changes?kb_id="+encodeURIComponent(kb)+(cursor?"&cursor="+encodeURIComponent(cursor):""),{signal:controller.signal},instance);
          if(page.kb_id!==kb||!Array.isArray(page.items)||typeof page.checked_at!=="string"||(page.next_cursor!==null&&typeof page.next_cursor!=="string"))throw new Error("笔记检查结果不完整；保留上次结果，请重新检查。");
          for(const item of page.items){
            if(item.kb_id!==kb||typeof item.source_id!=="string"||typeof item.document_id!=="string"||typeof item.relative_path!=="string"||!Array.isArray(item.changes)||typeof item.latest_archive_published!=="boolean"||!["UNCHANGED","CHANGED","MISSING","UNREADABLE","NOT_READY"].includes(item.state)||!["UNCHANGED","CHANGED","MISSING","UNREADABLE","NOT_READY"].includes(item.note_state))throw new Error("笔记来源信息不完整，已停止本轮检查。");
            rows.push(item);
          }
          checkedAt=page.checked_at;cursor=page.next_cursor;
          if(cursor){if(seen.has(cursor))throw new Error("笔记检查分页状态重复，已停止读取。");seen.add(cursor);}
        }while(cursor);
        if(!closed&&!document.hidden){setView({scope,items:rows,checkedAt});setFailure({scope,text:""});}
      }catch(error){
        if(!closed&&!document.hidden&&!(error instanceof DOMException&&error.name==="AbortError"))setFailure({scope,text:obsidianFailure(error)});
      }finally{
        inFlight=false;
        if(!closed){setChecking(false);if(pendingVisible&&!document.hidden){pendingVisible=false;void run();}}
      }
    };
    refresh.current=()=>void run(true);
    const visible=()=>{if(!document.hidden)void run(true);};
    const changed=()=>void run(true);
    void run();
    const timer=setInterval(()=>void run(),60000);
    document.addEventListener("visibilitychange",visible);
    window.addEventListener("raghub:obsidian-refresh",changed);
    return()=>{closed=true;controller?.abort();clearInterval(timer);document.removeEventListener("visibilitychange",visible);window.removeEventListener("raghub:obsidian-refresh",changed);};
  },[instance,kb,enabled,scope,mode,location.pathname]);

  const current=view?.scope===scope?view:null,error=failure.scope===scope?failure.text:"";
  const rows=current?.items??[];
  const affected=rows.filter(item=>item.state!=="UNCHANGED"&&item.state!=="NOT_READY");
  const unpublished=rows.filter(item=>!item.latest_archive_published);
  const actionable=enabled&&!checking&&!error;
  if(!instance||!kb||(!checking&&!error&&current&&rows.length===0))return null;
  const columns=[
    {title:"笔记",dataIndex:"relative_path",key:"path",render:(path:string)=><Typography.Text style={{overflowWrap:"anywhere"}}>{path}</Typography.Text>},
    {title:"本地变化",key:"change",render:(_:unknown,item:RecordValue)=><Space direction="vertical" size={2}><Tag color={item.state==="CHANGED"?"gold":"red"}>{states[item.state]??"需要核对"}</Tag>{item.changes.slice(0,3).map((change:RecordValue,index:number)=><Typography.Text key={index} type="secondary">{change.kind==="IMAGE"?"图片":"笔记"}：{change.relative_path}{change.state==="MISSING"?"（缺失）":change.state==="UNREADABLE"?"（不可读）":""}</Typography.Text>)}{item.changes.length>3&&<Typography.Text type="secondary">另有 {item.changes.length-3} 项，请在更新预览中核对。</Typography.Text>}</Space>},
    {title:"操作",key:"action",render:(_:unknown,item:RecordValue)=><Button size="small" disabled={!actionable||!["UNCHANGED","CHANGED"].includes(item.note_state)||["PENDING","RUNNING"].includes(item.task_status)} onClick={()=>{setExpanded(false);onUpdate(item);}}>更新这篇笔记</Button>}
  ];
  const content=<section aria-label="Obsidian 笔记更新" style={{marginBottom:16}}>
    <Space wrap style={{marginBottom:8}}>
      <Typography.Text strong>Obsidian 笔记</Typography.Text>
      {checking&&<Tag>正在检查登记来源</Tag>}
      {current&&<Typography.Text type="secondary">已登记 {rows.length} 篇 · 最近检查 {new Date(current.checkedAt).toLocaleTimeString()}</Typography.Text>}
      {!enabled&&<Tag color="warning">系统状态待核对</Tag>}
      <Button size="small" loading={checking} disabled={!enabled||checking} onClick={()=>refresh.current()}>立即检查</Button>
    </Space>
    {error&&<ErrorNotice error={error} stage="检查笔记更新" retained="保留上次检查结果；已发布资料和历史回答仍保留，当前检查结果可能已过期。" next={error instanceof ApiError&&error.code==="OBSIDIAN_ENDPOINT_UNAVAILABLE"?"本机后端尚未更新笔记检查功能，请更新并重启服务后再试；无需重新导入笔记。":"请重新检查，状态确认前暂停更新提交。"} onRecover={()=>refresh.current()} recoverLabel="重新检查来源"/>}
    {affected.length>0&&<Alert type="warning" showIcon message={affected.length+" 篇笔记有修改或来源问题"} description={<div>
      <p>回答继续使用已发布版本。点击更新会检查原笔记，有变化时自动继续处理；系统会等待活动问答结束。本地文件消失不会自动删除资料。</p>
      <Table size="small" rowKey="source_id" columns={columns} dataSource={affected} pagination={{pageSize:5,showSizeChanger:false,hideOnSinglePage:true}} scroll={{x:520}}/>
    </div>}/>}
    {unpublished.length>0&&<Alert type="info" showIcon style={{marginTop:8}} message={unpublished.length+" 篇笔记的最新归档尚未发布"} description={<Space direction="vertical" size={4}>
      <Typography.Text>接收或解析完成后，还需要通过复核并发布索引，才能用于新的问答。</Typography.Text>
      {unpublished.slice(0,3).map(item=><Typography.Text key={item.source_id} type="secondary">{item.relative_path} · {stages[item.processing_status]??(item.task_status==="PENDING"?"已接收 · 等待归档":item.task_status==="RUNNING"?"正在归档":"等待处理")}</Typography.Text>)}
      <Button size="small" onClick={()=>navigate("/documents")}>查看资料和任务</Button>
    </Space>}/>}
    {!error&&current&&rows.length>0&&!affected.length&&!unpublished.length&&<Typography.Text type="secondary">登记笔记与归档一致，当前发布版本可用于问答。</Typography.Text>}
  </section>;
  if(!compact)return content;
  const status=error?"笔记检查未完成":checking?"正在检查笔记":!enabled?"笔记状态待核对":!current?"等待检查笔记":affected.length?affected.length+" 篇笔记待处理":unpublished.length?unpublished.length+" 篇笔记尚未发布":"笔记已同步";
  return <><Button type="text" className={"sync-summary"+(error||affected.length||unpublished.length?" needs-attention":"")} icon={<SyncOutlined spin={checking}/>} onClick={()=>setExpanded(true)} aria-label={"查看笔记同步状态："+status}>{status}</Button><Drawer title="笔记同步状态" width={440} open={expanded} onClose={()=>setExpanded(false)}>{content}</Drawer></>;
}
