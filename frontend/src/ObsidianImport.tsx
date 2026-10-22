import {useEffect,useRef,useState} from "react";
import {Alert,Button,Checkbox,Empty,Input,Pagination,Progress,Space,Spin,Table,Tag,Tree,Typography} from "antd";
import type {DataNode} from "antd/es/tree";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {ApiError,api,post,describeError,persistSubmission,acceptSubmission,savedSubmissions,type RecordValue,type Submission} from "./api";
import {splitObsidianFiles} from "./obsidianBatches";

type Entry={relative_path:string;filename:string;kind:string;eligible:boolean;skip_reason:string|null;size_bytes:number;signature:string};
type Listing={items:Entry[];total:number;limit:number;offset:number};
type Bucket={submission:Submission;phase:"PLANNED"|"SENT"|"ACCEPTED";batchId?:string};
type Plan={version:1;instance:string;kb:string;documentId?:string|null;previewId:string;previewSha:string;buckets:Bucket[];createdAt:string};
const base="/api/ui/obsidian", MiB=1024*1024;
const reasons:Record<string,string>={SYMLINK:"符号链接不导入",EXCLUDED_DIRECTORY:"应用配置目录跳过",SYSTEM_FILE:"系统文件跳过",TEMPORARY_FILE:"临时文件跳过",UNSUPPORTED_EXTENSION:"暂不支持的格式",NON_REGULAR:"非普通文件",FILE_TOO_LARGE:"超过50 MiB"};
const selectedRecord=(entries:Entry[])=>entries.map(({relative_path,signature})=>({relative_path,signature}));
const planKey=(instance:string,kb:string,documentId:string|null=null)=>"raghub.obsidian.latest."+instance+"."+kb+(documentId?"."+documentId:"");
function persistPlan(plan:Plan){
  const value=JSON.stringify(plan);
  localStorage.setItem("raghub.obsidian.plan."+plan.previewId,value);
  localStorage.setItem(planKey(plan.instance,plan.kb,plan.documentId??null),value);
}
function readPlan(instance:string,kb:string,documentId:string|null=null):Plan|null{
  const raw=localStorage.getItem(planKey(instance,kb,documentId));if(raw===null)return null;
  const value=JSON.parse(raw);
  if(value?.version!==1||value.instance!==instance||value.kb!==kb||(value.documentId??null)!==documentId||typeof value.previewId!=="string"||typeof value.previewSha!=="string"||!Array.isArray(value.buckets)||!value.buckets.length)throw new Error("上次 Obsidian 提交记录不可读；保留记录，停止恢复。");
  for(const bucket of value.buckets)if(!["PLANNED","SENT","ACCEPTED"].includes(bucket.phase)||bucket.submission?.instance!==instance||bucket.submission?.path!=="/api/import/batches"||bucket.submission?.body?.kb_id!==kb||bucket.submission?.body?.preview_id!==value.previewId||bucket.submission?.body?.preview_sha256!==value.previewSha||typeof bucket.submission?.key!=="string")throw new Error("上次批次身份不一致，停止恢复。");
  if(documentId&&value.buckets.some((bucket:Bucket)=>{const items=(bucket.submission.body as RecordValue).items;return !Array.isArray(items)||items.length!==1||items[0].document_id!==documentId;}))throw new Error("原更新计划不属于当前文档，停止恢复。");
  return value;
}

export default function ObsidianImport({active,instance,kb,kbName,importing,documentId=null,documentName="",initialPath="",onAccepted}:{active:boolean;instance:string;kb:string;kbName:string;importing:boolean;documentId?:string|null;documentName?:string;initialPath?:string;onAccepted:()=>void}){
  const [folderFocus,setFolderFocus]=useState("");
  const [listing,setListing]=useState<Record<string,Listing>>({}),[expanded,setExpanded]=useState<string[]>([]),[folderSets,setFolderSets]=useState<Record<string,string[]>>({});
  const [query,setQuery]=useState(""),[search,setSearch]=useState(""),[searchPage,setSearchPage]=useState(1),[searchResult,setSearchResult]=useState<Listing|null>(null);
  const [selected,setSelected]=useState<Record<string,Entry>>({}),[changes,setChanges]=useState<Record<string,string>>({}),[dependencies,setDependencies]=useState<Entry[]>([]);
  const [note,setNote]=useState(""),[noteOffset,setNoteOffset]=useState(0),[markdown,setMarkdown]=useState<RecordValue|null>(null);
  const [markdownLoading,setMarkdownLoading]=useState(false),[markdownError,setMarkdownError]=useState(""),[noteStamp,setNoteStamp]=useState(""),[markdownRetry,setMarkdownRetry]=useState(0);
  const [preview,setPreview]=useState<RecordValue|null>(null),[confirmed,setConfirmed]=useState(false),[plan,setPlan]=useState<Plan|null>(null);
  const [workflowRequest,setWorkflowRequest]=useState<Submission|null>(null),[workflowAbsent,setWorkflowAbsent]=useState(false);
  useEffect(()=>{
    setWorkflowAbsent(false);
    setWorkflowRequest(savedSubmissions().find(item=>item.instance===instance&&item.path===base+"/workflows"&&(item.body as RecordValue)?.kb_id===kb&&item.phase==="AWAITING_CONFIRMATION")??null);
  },[instance,kb,active]);
  const [error,setError]=useState(""),[stale,setStale]=useState(""),[busy,setBusy]=useState(false),[loading,setLoading]=useState(true),[stamp,setStamp]=useState("");
  const [progress,setProgress]=useState({accepted:0,total:0,batch:0,batches:0}),[batchStates,setBatchStates]=useState<RecordValue[]>([]);
  const [inspectedPlan,setInspectedPlan]=useState(""),[resumeConfirmed,setResumeConfirmed]=useState(false),[originalPreview,setOriginalPreview]=useState<RecordValue|null>(null),[jobStates,setJobStates]=useState<Record<string,string>>({});
  const generation=useRef(0),selectionRevision=useRef(0),flight=useRef(false),actionFlight=useRef(false),pending=useRef(false),refreshRef=useRef<()=>Promise<void>>(async()=>{}),context=useRef({active,instance,kb,importing,documentId});
  const listingRef=useRef(listing),planRef=useRef(plan),initialSelection=useRef("");
  const acceptedPreview=useRef<string|null>(null);
  const noteMetadataSignature=[...(search?(searchResult?.items??[]):[]),...Object.values(listing).flatMap(value=>value.items)].find(row=>row.relative_path===note)?.signature??"";
  const previewRef=useRef({note,signature:""});
  previewRef.current={note,signature:markdown?.relative_path===note&&typeof markdown.signature==="string"?markdown.signature:noteMetadataSignature};
  listingRef.current=listing;planRef.current=plan;
  context.current={active,instance,kb,importing,documentId};
  const invalidate=()=>{selectionRevision.current++;setPreview(null);setConfirmed(false);setDependencies([]);setChanges({});};
  useEffect(()=>{const timer=setTimeout(()=>{setSearch(query.trim());setSearchPage(1);},400);return()=>clearTimeout(timer);},[query]);
  useEffect(()=>{
    setListing({});setExpanded([]);setFolderFocus("");
    selectionRevision.current++;setSelected({});setFolderSets({});setPreview(null);setConfirmed(false);setDependencies([]);setChanges({});setMarkdown(null);setNote("");setNoteStamp("");setMarkdownError("");setMarkdownLoading(false);setMarkdownRetry(0);setProgress({accepted:0,total:0,batch:0,batches:0});setBatchStates([]);
    setInspectedPlan("");setResumeConfirmed(false);setOriginalPreview(null);setJobStates({});
    try{setPlan(readPlan(instance,kb,documentId));setError("");}catch(err){setPlan(null);setError(describeError(err));}
  },[instance,kb,documentId]);

  useEffect(()=>{
    let cancelled=false;
    if(!active){initialSelection.current="";return;}
    if(!initialPath)return;
    const key=instance+"|"+kb+"|"+documentId+"|"+initialPath;
    if(initialSelection.current===key)return;
    void post(base+"/selection",{paths:[initialPath],directories:[]},instance).then(result=>{
      if(cancelled||!context.current.active||context.current.instance!==instance||context.current.kb!==kb||context.current.documentId!==documentId)return;
      if(!Array.isArray(result.entries)||result.entries.length!==1)throw new Error("登记笔记当前不可选；归档资料仍保留，请先核对本地文件和路径。");
      const entry=result.entries[0] as Entry;
      if(entry.relative_path!==initialPath||entry.kind!=="REGULAR"||!entry.eligible||typeof entry.signature!=="string")throw new Error("登记笔记的来源状态已改变；保留归档，请重新核对来源。");
      initialSelection.current=key;invalidate();setSelected({[entry.relative_path]:entry});
      const parts=initialPath.split("/").slice(0,-1),parents=parts.map((_,index)=>parts.slice(0,index+1).join("/"));
      setExpanded(old=>Array.from(new Set([...old,...parents])));
      setQuery("");setSearch("");setSearchPage(1);setNote(initialPath);setNoteOffset(0);setError("");
    }).catch(error=>{if(!cancelled&&context.current.active&&context.current.instance===instance&&context.current.kb===kb&&context.current.documentId===documentId)setError(describeError(error));});
    return()=>{cancelled=true;};
  },[active,instance,kb,documentId,initialPath]);

  useEffect(()=>{
    const own=++generation.current;
    const refresh=async()=>{
      if(!active||document.hidden||!instance)return;
      if(flight.current){pending.current=true;return;}
      flight.current=true;pending.current=false;
      const revision=selectionRevision.current;
      try{
        if(search){
          const result=await api<Listing>(base+"/tree?search="+encodeURIComponent(search)+"&offset="+(searchPage-1)*100,{},instance);
          if(generation.current===own)setSearchResult(result);
        }else{
          const next:Record<string,Listing>={},unavailable:string[]=[];
          const under=(path:string,parent:string)=>path===parent||path.startsWith(parent+"/");
          for(const path of ["",...expanded]){
            if(unavailable.some(parent=>under(path,parent)))continue;
            const prior=listingRef.current[path],pageCount=Math.max(1,Math.ceil((prior?.items.length??0)/100));
            let combined:Entry[]=[],last:Listing|null=null;
            try{
              for(let page=0;page<pageCount;page++){
                last=await api<Listing>(base+"/tree?path="+encodeURIComponent(path)+"&offset="+page*100,{},instance);
                combined=combined.concat(last.items);
              }
              if(last)next[path]={...last,items:combined,offset:0};
            }catch(err){
              if(path&&err instanceof ApiError&&["OBSIDIAN_DIRECTORY_MISSING","OBSIDIAN_DIRECTORY_INVALID"].includes(err.code))unavailable.push(path);
              else throw err;
            }
          }
          if(generation.current===own){
            setListing(old=>Object.fromEntries(Object.entries({...old,...next}).filter(([path])=>!unavailable.some(parent=>under(path,parent)))));
            if(unavailable.length){
              setExpanded(old=>old.filter(path=>!unavailable.some(parent=>under(path,parent))));
              const affected=[...Object.values(selected),...dependencies].filter(row=>unavailable.some(parent=>under(row.relative_path,parent)));
              if(affected.length){setChanges(old=>({...old,...Object.fromEntries(affected.map(row=>[row.relative_path,"MISSING"]))}));setConfirmed(false);}
            }
          }
        }
        const selectionTracked=[...Object.values(selected),...dependencies.filter(dep=>!selected[dep.relative_path])];
        const tracked=selectedRecord(selectionTracked),selectedPaths=new Set(selectionTracked.map(row=>row.relative_path)),currentNote=previewRef.current;
        if(currentNote.note&&currentNote.signature&&!tracked.some(row=>row.relative_path===currentNote.note))tracked.push({relative_path:currentNote.note,signature:currentNote.signature});
        if(tracked.length){
          const result=await post(base+"/check",{entries:tracked},instance);
          if(generation.current===own&&selectionRevision.current===revision){
            const changed:Record<string,string>={};
            for(const item of result.items){
              if(selectedPaths.has(item.relative_path)&&item.status!=="UNCHANGED")changed[item.relative_path]=item.status;
              if(item.relative_path===currentNote.note&&previewRef.current.note===currentNote.note)setNoteStamp(currentNote.note+":"+(typeof item.current_signature==="string"?item.current_signature:item.status));
            }
            setChanges(changed);if(Object.keys(changed).length)setConfirmed(false);
          }
        }
        const currentPlan=planRef.current;
        if(currentPlan&&currentPlan.instance===instance&&currentPlan.kb===kb){
          const jobs=await api("/api/ui/tasks?kb_id="+kb,{},instance);
          if(generation.current===own)setJobStates(Object.fromEntries(jobs.items.map((job:RecordValue)=>[job.task_id,job.status])));
        }
        if(generation.current===own){setStale("");setStamp(new Date().toLocaleTimeString());setLoading(false);}
      }catch(err){if(generation.current===own){setStale(describeError(err));setLoading(false);}}
      finally{flight.current=false;if(pending.current&&!document.hidden&&context.current.active)void refreshRef.current();}
    };
    refreshRef.current=refresh;void refresh();
    const timer=setInterval(()=>void refresh(),3000);
    const visible=()=>{if(!document.hidden)void refreshRef.current();};
    document.addEventListener("visibilitychange",visible);
    return()=>{generation.current++;clearInterval(timer);document.removeEventListener("visibilitychange",visible);};
  },[active,instance,kb,documentId,expanded,search,searchPage,selected,dependencies]);

  useEffect(()=>{
    let cancelled=false;
    if(!active||!note){setMarkdown(null);setMarkdownLoading(false);setMarkdownError("");return;}
    const current=()=>!cancelled&&context.current.active&&context.current.instance===instance&&context.current.kb===kb&&previewRef.current.note===note;
    setMarkdown(null);setMarkdownError("");setMarkdownLoading(true);
    void api(base+"/markdown?path="+encodeURIComponent(note)+"&offset="+noteOffset,{},instance).then(result=>{
      if(!current())return;
      const lastOffset=Math.floor(Math.max(0,result.total_characters-1)/8000)*8000;
      if(noteOffset>lastOffset){setNoteOffset(lastOffset);return;}
      setMarkdown(result);setMarkdownLoading(false);
    }).catch(err=>{if(current()){setMarkdownError(describeError(err));setMarkdownLoading(false);}});
    return()=>{cancelled=true;};
  },[active,instance,kb,note,noteOffset,noteMetadataSignature,noteStamp,markdownRetry]);

  async function action(work:()=>Promise<void>){
    if(actionFlight.current)return;actionFlight.current=true;setBusy(true);setError("");
    try{await work();}catch(err){setError(describeError(err));}finally{actionFlight.current=false;setBusy(false);}
  }
  function changeFiles(next:Record<string,Entry>){invalidate();setSelected(next);}
  async function selectDirectory(path:string){
    if(documentId){setError("更新资料时请选择一个文件。");return;}
    await action(async()=>{
      const revision=selectionRevision.current,own=generation.current;
      const rows=await post(base+"/selection",{directories:[path],paths:[]},instance);
      if(generation.current!==own||selectionRevision.current!==revision)return;
      const old=folderSets[path],remove=!!old?.length&&old.every(key=>!!selected[key]);
      const next={...selected};
      if(remove)for(const key of old)delete next[key];
      else for(const row of rows.entries)next[row.relative_path]=row;
      setFolderSets(value=>({...value,[path]:rows.entries.map((row:Entry)=>row.relative_path)}));changeFiles(next);
      if(rows.skipped.length)setError("该目录跳过 "+rows.skipped.length+" 项（配置、符号链接、临时文件或不支持格式）；所选列表已固定，后续新增文件不会自动加入。");
    });
  }
  function toggleFile(row:Entry){if(documentId){changeFiles(selected[row.relative_path]?{}:{[row.relative_path]:row});return;}const next={...selected};if(next[row.relative_path])delete next[row.relative_path];else next[row.relative_path]=row;changeFiles(next);}
  async function more(path:string){
    await action(async()=>{
      const own=generation.current,prior=listing[path];if(!prior)return;
      const result=await api<Listing>(base+"/tree?path="+encodeURIComponent(path)+"&offset="+prior.items.length,{},instance);
      if(own===generation.current)setListing(old=>({...old,[path]:{...result,items:prior.items.concat(result.items),offset:0}}));
    });
  }
  async function recheck(){
    await action(async()=>{
      const own=generation.current,revision=selectionRevision.current;
      const result=await post(base+"/selection",{paths:Object.keys(selected),directories:[]},instance);
      if(own!==generation.current||revision!==selectionRevision.current)return;
      changeFiles(Object.fromEntries(result.entries.map((entry:Entry)=>[entry.relative_path,entry])));
    });
  }
  async function preparePreview(){
    await action(async()=>{
      const revision=selectionRevision.current,originalKb=kb;
      if(documentId&&Object.keys(selected).length!==1)throw new Error("更新资料时只能选择一个文件。");
      const result=await post(base+"/preview",{kb_id:kb,document_id:documentId,entries:selectedRecord(Object.values(selected))},instance);
      if(context.current.kb!==originalKb||selectionRevision.current!==revision||!context.current.active)return;
      splitObsidianFiles(result.manifest.files,result.item_bytes);
      setPreview(result);setDependencies(result.dependency_entries);setConfirmed(false);setPlan(null);
    });
  }
  async function continuePlan(current:Plan){
    const snapshot={...current,buckets:current.buckets.map(bucket=>({...bucket}))};
    const validContext=()=>{if(!context.current.active||context.current.instance!==snapshot.instance||context.current.kb!==snapshot.kb||context.current.documentId!==(snapshot.documentId??null)||!context.current.importing)throw new Error("已停止新派发；请回到原知识库和 IMPORT 模式后手动读取原批次。");};
    let accepted=0,total=snapshot.buckets.reduce((sum,bucket)=>sum+(bucket.submission.body as RecordValue).items.length,0);
    const states:RecordValue[]=[];
    for(let index=0;index<snapshot.buckets.length;index++){
      validContext();const bucket=snapshot.buckets[index],saved=bucket.submission;
      let batch:RecordValue|null=null;
      try{batch=await api("/api/import/batches/by-request/"+saved.key,{},snapshot.instance);}
      catch(err){if(!(err instanceof ApiError&&err.status===404&&err.code==="REQUEST_NOT_FOUND"))throw err;}
      if(!batch){
        validContext();bucket.phase="SENT";persistPlan(snapshot);setPlan({...snapshot});
        batch=await post(saved.path,saved.body,snapshot.instance,saved.key);
      }
      if(!batch)throw new Error("尚未取得原批次回执，停止接收文件。");
      if(batch.data_instance_id!==snapshot.instance||batch.kb_id!==snapshot.kb||batch.request_key!==saved.key||batch.preview_id!==snapshot.previewId)throw new Error("原批次身份改变，停止派发。");
      if(snapshot.documentId&&(batch.items.length!==1||batch.items[0].metadata.document_id!==snapshot.documentId))throw new Error("更新批次的目标文档改变，停止派发。");
      bucket.phase="ACCEPTED";bucket.batchId=batch.batch_id;bucket.submission=acceptSubmission(saved,batch.batch_id);persistPlan(snapshot);validContext();setPlan({...snapshot});
      for(const item of batch.items){
        validContext();
        if(item.state!=="ACCEPTED"){
          if(item.state!=="AWAITING_UPLOAD")throw new Error(item.metadata.relative_path+" 状态为 "+item.state+"；保留原记录，不能自动重传。");
          await post(base+"/batches/"+batch.batch_id+"/items/"+item.item_id+"/receive",{},snapshot.instance);
        }
        validContext();accepted++;setProgress({accepted,total,batch:index+1,batches:snapshot.buckets.length});
      }
      const latest=await api("/api/import/batches/"+batch.batch_id,{},snapshot.instance);validContext();states.push(latest);setBatchStates([...states]);
    }
    validContext();persistPlan(snapshot);onAccepted();
  }
  async function submitWorkflow(saved:Submission){
    const result=await post(saved.path,saved.body,saved.instance,saved.key);
    if(result.data_instance_id!==saved.instance||result.kb_id!==(saved.body as RecordValue).kb_id||result.preview_id!==(saved.body as RecordValue).preview_id)throw new Error("受理回执与原提交范围不一致，请保留记录并核对。");
    acceptedPreview.current=result.preview_id;
    acceptSubmission(saved,result.workflow_id);setWorkflowRequest(null);setWorkflowAbsent(false);onAccepted();
    window.dispatchEvent(new Event("raghub:workflow-refresh"));
  }
  async function inspectWorkflow(){
    await action(async()=>{
      const saved=workflowRequest;if(!saved)return;
      const result=await api(base+"/workflows?kb_id="+encodeURIComponent(kb)+"&request_key="+saved.key,{},saved.instance);
      if(result.workflow){
        if(result.workflow.data_instance_id!==saved.instance||result.workflow.kb_id!==kb||result.workflow.preview_id!==(saved.body as RecordValue).preview_id)throw new Error("原处理记录身份无法核对，已停止。");
        acceptSubmission(saved,result.workflow.workflow_id);setWorkflowRequest(null);setWorkflowAbsent(false);onAccepted();
        window.dispatchEvent(new Event("raghub:workflow-refresh"));
      }else setWorkflowAbsent(true);
    });
  }
  async function confirmAndImport(){
    await action(async()=>{
      if(workflowRequest)throw new Error("已有提交结果需要核对，请先读取原处理状态。");
      if(planRef.current?.buckets.some(bucket=>bucket.phase==="SENT"))throw new Error("旧版提交结果尚未核对，请先读取原批次状态，不重复导入。");
      if(!context.current.active||!preview||acceptedPreview.current===preview.preview_id)return;
      if(!preview||!confirmed||Object.keys(changes).length)throw new Error("请先核对目标知识库、所选笔记和图片。");
      const saved=persistSubmission(base+"/workflows",{kb_id:kb,preview_id:preview.preview_id,preview_sha256:preview.preview_sha256,confirmed:true},instance);
      setWorkflowRequest(saved);setWorkflowAbsent(false);
      await submitWorkflow(saved);
    });
  }
  async function inspectPlan(){
    await action(async()=>{
      const original=readPlan(instance,kb,documentId);if(!original)throw new Error("没有此知识库的原提交记录。");
      const recorded=await api("/api/import/directory/previews/"+original.previewId,{},instance);
      if(recorded.preview_id!==original.previewId||recorded.manifest.kb_id!==kb||recorded.preview_sha256!==original.previewSha||(original.documentId&&(recorded.manifest.files.length!==1||recorded.manifest.files[0].document_id!==original.documentId)))throw new Error("原预览身份不一致。");
      const states:RecordValue[]=[];
      for(const bucket of original.buckets){
        try{const result=await api("/api/import/batches/by-request/"+bucket.submission.key,{},instance);
          if(result.data_instance_id!==instance||result.kb_id!==kb||result.request_key!==bucket.submission.key)throw new Error("原批次身份不一致。");states.push(result);}
        catch(err){if(err instanceof ApiError&&err.status===404&&err.code==="REQUEST_NOT_FOUND")states.push({items:[],not_received:true});else throw err;}
      }
      if(context.current.instance!==original.instance||context.current.kb!==original.kb||!context.current.active)return;
      original.buckets.forEach((bucket,index)=>{const state=states[index],items=(bucket.submission.body as RecordValue).items;if(!state.not_received&&state.items.length===items.length&&state.items.every((item:RecordValue)=>item.state==="ACCEPTED"))bucket.phase="ACCEPTED";});
      persistPlan(original);
      setOriginalPreview(recorded);setInspectedPlan(original.previewId);setResumeConfirmed(false);
      setPlan(original);setBatchStates(states);
      setProgress({accepted:states.reduce((sum,state)=>sum+state.items.filter((item:RecordValue)=>item.state==="ACCEPTED").length,0),
        total:original.buckets.reduce((sum,bucket)=>sum+(bucket.submission.body as RecordValue).items.length,0),batch:0,batches:original.buckets.length});
    });
  }

  const entries=Object.values(selected),changed=Object.keys(changes),allRows=Object.values(listing).flatMap(value=>value.items);
  const nodes=(path:string):DataNode[]=>{
    const value=listing[path];if(!value)return [];
    const result:DataNode[]=value.items.map(row=>({key:row.relative_path,title:<span className="obsidian-node" title={"知识库 / "+row.relative_path.split("/").join(" / ")}><span>{row.filename}</span>{row.kind==="DIRECTORY"&&<small>{"知识库 / "+row.relative_path.split("/").join(" / ")}</small>}{row.skip_reason&&<small>{reasons[row.skip_reason]??row.skip_reason}</small>}</span>,
      isLeaf:row.kind!=="DIRECTORY",checkable:!documentId||row.kind!=="DIRECTORY",disabled:!row.eligible,children:row.kind==="DIRECTORY"?nodes(row.relative_path):undefined}));
    if(value.items.length<value.total)result.push({key:"more:"+path,isLeaf:true,checkable:false,selectable:false,title:<Button size="small" onClick={()=>void more(path)}>显示后100项（已显示 {value.items.length}/{value.total}）</Button>});
    return result;
  };
  const folderKeys=Object.entries(folderSets).filter(([,paths])=>paths.length&&paths.every(path=>!!selected[path])).map(([path])=>path);
  const partialFolders=Object.entries(folderSets).filter(([,paths])=>paths.some(path=>!!selected[path])&&!paths.every(path=>!!selected[path])).map(([path])=>path);
  const groups=preview?splitObsidianFiles(preview.manifest.files,preview.item_bytes):[];
  const columns=[{title:"资料与相对路径",dataIndex:"relative_path",render:(path:string,row:Entry)=><Button type="link" className="obsidian-path" onClick={()=>{setNote(path);setNoteOffset(0);}} disabled={!path.toLowerCase().endsWith(".md")}>{row.filename}<small>{path}</small></Button>},
    {title:"大小",dataIndex:"size_bytes",width:95,render:(size:number)=>(size/1024).toFixed(1)+" KiB"},
    {title:"状态",width:135,render:(_:unknown,row:Entry)=><>{changes[row.relative_path]&&<Tag color="gold">{changes[row.relative_path]==="MISSING"?"已消失":"已改变"}</Tag>}<Button size="small" disabled={busy} onClick={()=>toggleFile(row)}>取消选择</Button></>}];
  return <div className="stack obsidian-import">
    <Alert type="info" showIcon message={documentId?"更新现有资料："+documentName:"导入目标："+kbName} description={documentId?"请选择一份文件作为当前资料的新原件；选择另一份会替换当前选择。文档身份保留，原件与本地图片经确认后一起归档；Obsidian 原库保持只读。":"浏览自动刷新；只有选择并确认后才导入。目录勾选包含当时全部可导入子目录，搜索不改变已选集合。Obsidian 原库保持只读。"}/>
    {!importing&&<Alert type="warning" showIcon message="可以先浏览并核对笔记。确认后系统会等待当前问答结束，再自动开始处理。"/>}
    {error&&<Alert type="error" role="alert" showIcon message="操作未完成" description={error} closable onClose={()=>setError("")}/>}
    {stale&&<Alert type="warning" showIcon message="视图可能已过期，已保留上次成功内容" description={stale}/>}
    <div className="obsidian-layout" style={{display:preview?"none":undefined}}>
      <section className="obsidian-browser" aria-label="Obsidian 文件树">
        <Input.Search aria-label="搜索 Obsidian 文件名和相对路径" placeholder="搜索文件名或相对路径" value={query} onChange={event=>setQuery(event.target.value)} allowClear/>
        <Space wrap className="obsidian-tools">{!documentId&&<Button size="small" disabled={busy} onClick={()=>void selectDirectory("")}>选择知识库全部文件</Button>}{stale&&<Button size="small" onClick={()=>void refreshRef.current()}>重新读取文件树</Button>}</Space>
        <p className="obsidian-current-folder" aria-label="当前查看的 Obsidian 位置">当前查看：知识库{folderFocus?" / "+folderFocus.split("/").join(" / "):""}</p>
        <p className="muted">文字 PDF、Markdown、HTML、DOCX、XLSX、CSV/JSON 和图片；跳过原因显示在文件名旁。</p>
        {loading?<Spin aria-label="正在读取文件树"/>:search?<><Table size="small" rowKey="relative_path" dataSource={searchResult?.items??[]} pagination={false} showHeader={false} columns={[{render:(_:unknown,row:Entry)=><Space><Checkbox disabled={!row.eligible||row.kind!=="REGULAR"||busy} checked={!!selected[row.relative_path]} aria-label={"选择 "+row.relative_path} onChange={()=>toggleFile(row)}/><Button type="link" onClick={()=>{if(row.kind==="DIRECTORY"){setSearch("");setQuery("");setExpanded(old=>Array.from(new Set([...old,...row.relative_path.split("/").map((_,i,array)=>array.slice(0,i+1).join("/"))])));}else{setNote(row.relative_path);setNoteOffset(0);}}}>{row.filename}<small className="obsidian-relative">{row.relative_path}{row.skip_reason?" · "+(reasons[row.skip_reason]??row.skip_reason):""}</small></Button></Space>}]} /><Pagination size="small" pageSize={100} current={searchPage} total={searchResult?.total??0} showSizeChanger={false} onChange={setSearchPage}/></>:
          <Tree checkable checkStrictly blockNode expandedKeys={expanded} onExpand={(keys,info)=>{setExpanded(keys.map(String));if(info.expanded)setFolderFocus(String(info.node.key));else if(folderFocus===info.node.key||folderFocus.startsWith(String(info.node.key)+"/"))setFolderFocus(String(info.node.key).split("/").slice(0,-1).join("/"));}} checkedKeys={{checked:[...Object.keys(selected),...folderKeys],halfChecked:partialFolders}} selectedKeys={note?[note]:[]} treeData={nodes("")} loadData={async()=>{await refreshRef.current();}}
            onCheck={(_keys,info)=>{if(busy)return;const row=allRows.find(row=>row.relative_path===info.node.key);if(row?.kind==="DIRECTORY")void selectDirectory(row.relative_path);else if(row)toggleFile(row);}}
            onSelect={(_keys,info)=>{const path=String(info.node.key);if(!info.node.isLeaf)setFolderFocus(path);else if(path.toLowerCase().endsWith(".md")){setNote(path);setNoteOffset(0);setFolderFocus(path.split("/").slice(0,-1).join("/"));}}}/>}
        <span className="status-line">{stamp?"上次读取 "+stamp:"尚未读取"} · 页面隐藏时暂停自动刷新</span>
      </section>
      <section className="obsidian-preview" aria-label="只读 Markdown 预览"><h3>只读预览</h3>{note?<><Typography.Text className="obsidian-relative">{note}</Typography.Text>{markdown&&markdown.relative_path===note?<><p className="muted">字符 {markdown.total_characters===0?0:markdown.offset+1}–{markdown.end} / {markdown.total_characters} · 长文按段展示</p><div className="answer obsidian-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml allowedElements={["p","strong","em","del","ul","ol","li","blockquote","pre","code","table","thead","tbody","tr","td","th","h1","h2","h3","h4","hr","br","a","img"]} components={{a:({children})=><span>{children}</span>,img:({alt})=><span className="muted">［图片：{alt||"本地引用，确认时核对依赖"}］</span>}}>{markdown.text}</ReactMarkdown></div><Space><Button disabled={noteOffset===0} onClick={()=>setNoteOffset(Math.max(0,noteOffset-8000))}>上一段</Button><Button disabled={markdown.end>=markdown.total_characters} onClick={()=>setNoteOffset(noteOffset+8000)}>下一段</Button></Space></>:markdownLoading?<Spin aria-label="正在读取 Markdown"/>:markdownError?<Alert type="error" showIcon message="预览读取失败" description={markdownError} action={<Button onClick={()=>setMarkdownRetry(value=>value+1)}>重试预览</Button>}/>:<Empty description="等待预览读取"/>}</>:<Empty description="选择 Markdown 文件查看；不会执行 HTML 或加载外部图片。"/>}</section>
    </div>
    <div className="obsidian-selection" style={{display:preview?"none":undefined}}><h3>已选 {entries.length} 份资料 · {(entries.reduce((sum,row)=>sum+row.size_bytes,0)/MiB).toFixed(2)} MiB</h3>
      <Space wrap><Button disabled={!entries.length||busy} onClick={()=>changeFiles({})}>清空选择</Button>{changed.length>0&&<Button disabled={!entries.length||busy} onClick={()=>void recheck()}>重新核对变化的文件</Button>}</Space>
      {changed.length>0&&<Alert type="warning" showIcon message="已选资料或图片已改变／消失，需要重新核对" description={changed.join("；")}/>}
      <Table size="small" rowKey="relative_path" dataSource={entries} columns={columns} pagination={{pageSize:8,showSizeChanger:false}} locale={{emptyText:"未选择资料，自动刷新不会导入文件"}}/>
      <Button disabled={!entries.length||busy||!!changed.length||!!workflowRequest} loading={busy} onClick={()=>void preparePreview()}>{documentId?"核对本次更新":"下一步：核对导入内容"}</Button>
    </div>
    {preview&&<div className="surface stack"><h3>{documentId?"确认更新资料":"确认选择导入"}</h3><p>目标：{kbName} · {preview.file_count} 份 · {groups.length} 批 · 每批最多50份、500 MiB，单文件50 MiB。图片副本计入批次预算。</p>
      <Button disabled={busy||!!workflowRequest||acceptedPreview.current===preview.preview_id} onClick={()=>{setPreview(null);setConfirmed(false);}}>返回选择资料</Button>
      <Table size="small" rowKey="relative_path" dataSource={preview.manifest.files} columns={[{title:"本次导入的资料",dataIndex:"relative_path"}]} pagination={{pageSize:5,showSizeChanger:false}}/>
      <Table size="small" rowKey="relative_path" dataSource={preview.dependency_entries} columns={[{title:"所选 Markdown 需要的本地图片",dataIndex:"relative_path"},{title:"大小",dataIndex:"size_bytes",render:(size:number)=>(size/1024).toFixed(1)+" KiB"}]} pagination={{pageSize:5,showSizeChanger:false}} locale={{emptyText:"没有本地图片依赖"}}/>
      <Checkbox checked={confirmed} disabled={!!changed.length||busy} onChange={event=>setConfirmed(event.target.checked)}>{documentId?"我已核对更新目标、所选文件与图片依赖":"我已核对目标知识库、所选文件与图片依赖"}</Checkbox>
      <Button type="primary" disabled={!active||acceptedPreview.current===preview.preview_id||!confirmed||!!changed.length||busy||!!workflowRequest||Boolean(plan?.buckets.some(bucket=>bucket.phase==="SENT"))} loading={busy} onClick={()=>void confirmAndImport()}>{documentId?"更新并用于问答":"导入并用于问答 · "+kbName}</Button>
    </div>}
    {workflowRequest&&<Alert type="warning" showIcon message={workflowAbsent?"已核对：原提交尚未受理":"这次提交的结果需要核对"} description={workflowAbsent?"清单与原提交键已保留。你可以手动继续这一次提交，不会创建第二份记录。":"请先读取原处理状态；在确认是否受理前，不重复导入。已接收的资料保留。"} action={<Space wrap><Button disabled={busy} onClick={()=>void inspectWorkflow()}>读取原处理状态</Button>{workflowAbsent&&<Button disabled={busy} onClick={()=>void action(()=>submitWorkflow(workflowRequest))}>继续原提交</Button>}</Space>}/>}
    {plan?.buckets.some(bucket=>bucket.phase==="SENT")&&<Alert type="warning" showIcon message="旧版手动提交还有结果待核对" description="请先读取原批次状态，确认是否已接收；核对前停止新导入，不更换请求重新提交。" action={<Button disabled={busy} onClick={()=>void inspectPlan()}>读取原批次状态</Button>}/>}

  </div>;
}
