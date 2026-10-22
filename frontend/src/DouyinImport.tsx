import {useEffect,useRef,useState} from "react";
import {Alert,Button,Input,Typography} from "antd";
import {ApiError,api,post,persistSubmission,acceptSubmission,rejectSubmission,savedSubmissions,type RecordValue,type Submission} from "./api";

const path="/api/ui/douyin/jobs";
const terminal=(value:RecordValue|null)=>Boolean(value&&["SUCCEEDED","FAILED","CANCELLED","INTERRUPTED"].includes(value.status));

export default function DouyinImport({active,instance,kb,kbName,importing,beforeSubmit,onPreview}:{active:boolean;instance:string;kb:string;kbName:string;importing:boolean;beforeSubmit:()=>Promise<void>;onPreview:(path:string)=>void}){
  const preparing=useRef(false);
  const [text,setText]=useState(""),[task,setTask]=useState<RecordValue|null>(null);
  const [pending,setPending]=useState<Submission|null>(null),[absent,setAbsent]=useState(false);
  const [busy,setBusy]=useState(false),[error,setError]=useState(""),[stale,setStale]=useState(false);
  const context=useRef({active,instance,kb});context.current={active,instance,kb};
  const revision=useRef(0),reading=useRef(false),sending=useRef(false);
  const current=(own:number)=>own===revision.current&&context.current.active&&context.current.instance===instance&&context.current.kb===kb;
  function adopt(value:RecordValue,own:number){
    if(typeof value?.task_id!=="string"||value.kb_id!==kb||typeof value.saved!=="boolean")throw new Error("无法核对本次视频处理状态。");
    if(current(own)){setTask(value);setStale(false);setError("");}
  }
  useEffect(()=>{
    revision.current++;setTask(null);setPending(null);setError("");setStale(false);setAbsent(false);setBusy(false);
    if(!active||!instance||!kb)return;
    const records=savedSubmissions().filter(s=>s.instance===instance&&s.path===path&&(s.body as RecordValue)?.kb_id===kb);
    const record=records.find(s=>s.phase==="AWAITING_CONFIRMATION")??records[0];
    setText(typeof (record?.body as RecordValue)?.share_text==="string"?(record!.body as RecordValue).share_text:"");
    if(record?.phase==="AWAITING_CONFIRMATION")setPending(record);
    else if(record?.phase==="ACCEPTED"&&record.requestId)setTask({task_id:record.requestId,kb_id:kb,status:"PENDING",saved:false,progress:"正在查看处理进度"});
    else if(record?.phase==="REJECTED")setError("上次链接没有受理。请修改链接后再提交；没有重复创建任务。");
  },[active,instance,kb]);

  async function readOriginal(){
    if(reading.current||!active||!instance||!kb)return;
    const own=revision.current;reading.current=true;
    try{
      if(pending){
        const value=await api<RecordValue>("/api/ui/douyin/by-request/"+encodeURIComponent(pending.key)+"?kb_id="+encodeURIComponent(kb),{},instance);
        if(value.task===null){if(current(own)){setAbsent(true);setStale(false);setError("");}return;}
        if(!value.task||typeof value.task.task_id!=="string")throw new Error("原状态暂时无法核对。");
        acceptSubmission(pending,value.task.task_id);
        if(current(own)){setPending(null);setAbsent(false);adopt(value.task,own);}
      }else if(task?.task_id){
        const value=await api<RecordValue>(path+"/"+encodeURIComponent(task.task_id)+"?kb_id="+encodeURIComponent(kb),{},instance);
        adopt(value,own);
      }
    }catch(failure){
      if(current(own)){setStale(true);setError(failure instanceof ApiError?failure.message:"暂时无法连接本机服务。系统会自动查询处理进度，恢复连接后继续显示结果，无需重复提交。");}
    }finally{reading.current=false;}
  }
  useEffect(()=>{
    if(!active||!instance||!kb||(!pending&&!task?.task_id))return;
    const refresh=()=>{if(!document.hidden)void readOriginal();};
    refresh();
    const timer=window.setInterval(()=>{if(pending||!terminal(task)||task?.result_unknown||stale)refresh();},2500);
    document.addEventListener("visibilitychange",refresh);
    return()=>{window.clearInterval(timer);document.removeEventListener("visibilitychange",refresh);};
  },[active,instance,kb,pending?.key,task?.task_id,task?.status,task?.result_unknown,stale]);

  async function send(saved:Submission){
    if(sending.current)return;
    sending.current=true;setBusy(true);setError("");setAbsent(false);
    const own=++revision.current;
    try{
      const value=await post<RecordValue>(saved.path,saved.body,saved.instance,saved.key);
      if(typeof value.task_id!=="string"||value.kb_id!==kb)throw new Error("任务受理结果尚未确认。");
      acceptSubmission(saved,value.task_id);
      if(current(own)){setPending(null);adopt(value,own);}
    }catch(failure){
      if(failure instanceof ApiError&&failure.status===422){
        rejectSubmission(saved,422);
        if(current(own)){
          // A pending status read may finish after this known rejection.
          // It must not clear the error or adopt an obsolete submission.
          revision.current++;setPending(null);setAbsent(false);setError(failure.message);setBusy(false);
        }
      }else if(current(own)){
        setPending(saved);setError(failure instanceof ApiError?failure.message:"暂时无法确认提交结果。系统正在自动查询，确认前不会重复提交。");
      }
    }finally{sending.current=false;if(current(own))setBusy(false);}
  }
  async function submit(){
    if(preparing.current||!text.trim()||!importing||pending||busy||task?.result_unknown||task&&!terminal(task))return;
    const own=revision.current;preparing.current=true;setBusy(true);setError("");
    try{
      await beforeSubmit();if(!current(own))return;
      const saved=persistSubmission(path,{kb_id:kb,share_text:text.trim()},instance);
      setPending(saved);setTask(null);await send(saved);
    }catch(failure){if(current(own))setError(failure instanceof Error?failure.message:"暂时无法确认是否可提交，原输入保留。");}
    finally{preparing.current=false;if(current(own))setBusy(false);}
  }
  return <div className="stack">
    <Alert showIcon type="info" message="先保存为 Obsidian 笔记，再确认用于问答" description={"视频提取语音讲解；图文逐页识别图片文字，并保留全部原图。图文的图示含义尚未核对，请在预览时对照原图。一次只处理你粘贴的一条作品，已有笔记直接复用，不覆盖正文。保存完成后，你再预览并确认加入“"+kbName+"”。"}/>
    <Typography.Paragraph type="secondary">视频语音：Whisper Small（MLX 版） · 图文文字：Apple Vision OCR（本机识别）</Typography.Paragraph>
    <label htmlFor="dy-share">抖音作品链接或分享口令</label>
    <Input.TextArea id="dy-share" aria-label="抖音作品链接或分享口令" rows={3} maxLength={8192} placeholder="粘贴一条抖音视频、图文链接或完整分享口令" value={text} disabled={Boolean(pending)||busy||Boolean(task?.result_unknown)||Boolean(task&&!terminal(task))} onChange={event=>setText(event.target.value)}/>
    {error&&<Alert showIcon type="warning" message={stale?"状态暂未更新":"本次处理尚未完成"} description={error}/>}
    {pending?<Alert showIcon type="warning" message={absent?"这条视频尚未开始处理":"正在确认提交结果"} description={absent?"已核对：系统没有受理这次提交。你可以继续保存，仍使用同一个请求，不会重复创建任务。":"系统正在自动查询本次提交是否已受理，请稍候；确认前不会重复提交。"} action={absent&&<Button disabled={busy||!importing} onClick={()=>void send(pending)}>继续保存</Button>}/>:
      <Button type="primary" loading={busy} disabled={!text.trim()||!importing||Boolean(task?.result_unknown)||Boolean(task&&!terminal(task))} onClick={submit}>保存到 Obsidian</Button>}
    {task&&<section className="surface" aria-label="抖音笔记处理结果" aria-live="polite">
      <Typography.Title level={4}>{task.saved?(task.written?"已保存到 Obsidian":"已找到已有的 Obsidian 笔记"):task.progress||"正在查看处理进度"}</Typography.Title>
      {stale&&<Typography.Paragraph>暂时无法更新进度，正在自动重连。下面保留上次查询的结果。</Typography.Paragraph>}
      {task.title&&<Typography.Paragraph strong>{task.title}</Typography.Paragraph>}
      {task.saved&&task.recognition_engine==="Apple Vision OCR"&&<Typography.Paragraph>已使用 Apple Vision OCR 识别 {task.image_count} 页图片文字，全部原图随笔记保存；请在预览时核对。</Typography.Paragraph>}
      {task.message&&<Typography.Paragraph>{task.message}</Typography.Paragraph>}
      {task.note_changed&&<Alert type="warning" message="笔记保存后有修改，预览时请核对当前内容。"/>}
      {Array.isArray(task.warnings)&&task.warnings.map((warning:string,index:number)=><Typography.Paragraph key={index}>{warning}</Typography.Paragraph>)}
      {task.saved&&task.preview_available&&typeof task.relative_path==="string"&&<Button type="primary" disabled={stale} onClick={()=>onPreview(task.relative_path)}>预览笔记并核对入库</Button>}
      {!task.saved&&!terminal(task)&&<Typography.Paragraph>正在处理本次作品。已保存的阶段会保留，请不要重复点击；关闭窗口后任务仍可继续。</Typography.Paragraph>}
    </section>}
  </div>;
}
