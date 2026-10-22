import {lazy,useCallback,useEffect,useRef,useState} from "react";
import {Alert,App,Button,Checkbox,Drawer,Empty,Input,Modal,Select,Space,Spin,Table,Tabs,Tag,Typography,Upload} from "antd";
import type {Context} from "./App";
import {api,acceptSubmission,describeError,persistentPost,post,savedSubmissions,type RecordValue,type Submission} from "./api";
const SourceDrawer=lazy(()=>import("./SourceDrawer"));
const ParsingConfig=lazy(()=>import("./ParsingConfig"));
import Maintenance from "./Maintenance";
import ImportProgress from "./ImportProgress";
import ErrorNotice from "./ErrorNotice";
import LoadBoundary from "./LoadBoundary";
import {revisionRequestBody} from "./parsingReviewModel";
import {QualityIssues,ParsedFields,ParsedStructure,ReadableValue,RevisionSummary,TechnicalDetails} from "./ParsingPresentation";
const ObsidianImport=lazy(()=>import("./ObsidianImport"));
const DouyinImport=lazy(()=>import("./DouyinImport"));
type Picked={file:File; metadata:RecordValue};
const statuses:Record<string,string>={NEW:"待解析",PROCESSING:"解析中",PENDING:"等待处理",RUNNING:"处理中",SUCCEEDED:"已完成",FAILED:"失败",CANCELLED:"已取消",READY:"可建库",REVIEW_REQUIRED:"待复核",INTERRUPTED:"已中断"};
const taskStages:Record<string,string>={PENDING:"等待处理",DY_PLANNING:"正在核对视频",DY_PREPARING:"正在获取视频讲解",DY_ANALYSING:"正在整理学习笔记",DY_SAVING:"正在保存到 Obsidian",DY_DONE:"笔记已保存",ACQUIRING:"正在读取资料",ARCHIVE_CLOSING:"正在保存原文",ARCHIVE_VERIFIED:"原文已保存",PARSING:"正在解析资料",PARSE:"正在解析资料",CLEANING:"正在整理文字",OCR:"正在识别图片文字",CHUNKING:"正在整理检索内容",EMBEDDING:"正在建立索引",INDEXING:"正在建立索引",CHECKING:"正在检查质量",READY:"处理完成",FAILED:"处理停止"};
async function fileHash(file:File){const value=await crypto.subtle.digest("SHA-256",await file.arrayBuffer());return Array.from(new Uint8Array(value),byte=>byte.toString(16).padStart(2,"0")).join("");}
export default function Documents({state,kb,kbName,setKb,refresh,stale,obsidianUpdate,clearObsidianUpdate}:Context){
  const {message,modal}=App.useApp();
  const [listScope,setListScope]=useState(""),[obsidianVisited,setObsidianVisited]=useState(false);
  const instance=state?.data_instance_id??"";
  const flowProcessing=Boolean(state?.import_workflow&&!["PAUSED","FAILED","COMPLETE","PARTIAL_COMPLETE","CANCELLED"].includes(state.import_workflow.state));
  const [docs,setDocs]=useState<RecordValue[]>([]),[tasks,setTasks]=useState<RecordValue[]>([]),[generations,setGenerations]=useState<RecordValue[]>([]),[total,setTotal]=useState(0),[page,setPage]=useState(1),[error,setError]=useState(""),[busy,setBusy]=useState(false),[stamp,setStamp]=useState(""),[activeTab,setActiveTab]=useState("documents");
  const [importOpen,setImportOpen]=useState(false),[files,setFiles]=useState<Picked[]>([]),[root,setRoot]=useState(""),[preview,setPreview]=useState<RecordValue|null>(null),[url,setUrl]=useState(""),[profile,setProfile]=useState("normal"),[updateDoc,setUpdateDoc]=useState<string|null>(null),[importType,setImportType]=useState("obsidian"),[name,setName]=useState("");
  const [source,setSource]=useState<any>(null),[review,setReview]=useState<RecordValue|null>(null),[view,setView]=useState<RecordValue|null>(null),[viewSection,setViewSection]=useState("quality"),[viewOffset,setViewOffset]=useState(0),[revision,setRevision]=useState<RecordValue|null>(null),[revisionFile,setRevisionFile]=useState<RecordValue|null>(null),[decisionNote,setDecisionNote]=useState(""),[verified,setVerified]=useState(false),[reviews,setReviews]=useState<Record<string,RecordValue>>({});
  const [ocrOpen,setOcrOpen]=useState(false),[field,setField]=useState(""),[cropPage,setCropPage]=useState("1"),[ocr,setOcr]=useState<RecordValue|null>(null),[ocrTask,setOcrTask]=useState(""),[modified,setModified]=useState(""),[taskDetail,setTaskDetail]=useState<RecordValue|null>(null),[cleanup,setCleanup]=useState<RecordValue|null>(null);
  const [resumeOpen,setResumeOpen]=useState(false),[resumeBatch,setResumeBatch]=useState<RecordValue|null>(null),[createKbOpen,setCreateKbOpen]=useState(false);
  const [parseDoc,setParseDoc]=useState<RecordValue|null>(null);
  const [deletions,setDeletions]=useState<RecordValue[]>([]);
  const [directoryCandidates,setDirectoryCandidates]=useState<File[]>([]),[directoryServerReceive,setDirectoryServerReceive]=useState(false);
  const [readError,setReadError]=useState(""),[importError,setImportError]=useState("");
  const viewStale=stale||!!readError||(!!kb&&listScope!==kb),importing=!viewStale&&state?.mode==="IMPORT"&&!!state?.intake_open;
  const [reviewFields,setReviewFields]=useState<RecordValue[]>([]),[textEdits,setTextEdits]=useState<Record<string,string>>({}),[viewLoading,setViewLoading]=useState(false),[viewError,setViewError]=useState("");
  const [revisionRaw,setRevisionRaw]=useState("");
  const reviewScopeRef=useRef("");reviewScopeRef.current=[instance,review?.document_id??"",review?.current_processing_version_id??""].join("|");
  const operationRunning=useRef(false);
  const [obsidianInitialPath,setObsidianInitialPath]=useState("");
  const updateScope=useRef({instance,kb});updateScope.current={instance,kb};
  const [updateAbsent,setUpdateAbsent]=useState<Submission|null>(null);
  const pendingUpdates=savedSubmissions().filter(value=>value.path==="/api/ui/obsidian/workflows"&&(value.body as RecordValue)?.kb_id===kb&&value.phase==="AWAITING_CONFIRMATION");
  useEffect(()=>setUpdateAbsent(null),[instance,kb]);
  async function updateNote(row:RecordValue){
    await run(async()=>{
      if(viewStale||flowProcessing||pendingUpdates.length)throw new Error("请先核对当前资料或上次更新的状态，再开始更新。");
      const own={instance,kb},ensureScope=()=>{if(updateScope.current.instance!==own.instance||updateScope.current.kb!==own.kb)throw new Error("知识库已切换，更新已停止；请在原知识库核对状态。");};
      const target=await api("/api/ui/documents/"+row.document_id+"/obsidian-target?fresh=true",{},own.instance);
      ensureScope();
      if(target.document_id!==row.document_id||target.kb_id!==own.kb||!target.available||typeof target.relative_path!=="string"||typeof target.source_id!=="string")throw new Error(target.reason??"该资料没有可更新的 Obsidian 原笔记。");
      if(row.current_raw_version_id&&target.current_raw_version_id!==row.current_raw_version_id)throw new Error("资料版本已改变，请先刷新当前状态。");
      if(target.state==="UNCHANGED"){void message.success("这篇笔记没有变化，已是最新版本。");return;}
      if(target.state!=="CHANGED")throw new Error("原笔记或图片暂时不可读取，之前可用的版本仍保留。");
      const selected=await post("/api/ui/obsidian/selection",{paths:[target.relative_path],directories:[]},own.instance);
      ensureScope();
      if(selected.entries?.length!==1||selected.entries[0].relative_path!==target.relative_path)throw new Error("原笔记范围发生变化，未选择其他文件。");
      const preview=await post("/api/ui/obsidian/preview",{kb_id:own.kb,document_id:row.document_id,expected_raw_version_id:target.current_raw_version_id,registered_source_id:target.source_id,entries:[{relative_path:target.relative_path,signature:selected.entries[0].signature}]},own.instance);
      ensureScope();
      if(preview.manifest?.files?.length!==1||preview.manifest.files[0].document_id!==row.document_id||preview.manifest.files[0].relative_path!==target.relative_path)throw new Error("更新目标无法核对，未继续提交。");
      await persistentPost("/api/ui/obsidian/workflows",{kb_id:own.kb,preview_id:preview.preview_id,preview_sha256:preview.preview_sha256,confirmed:true},own.instance);
      ensureScope();window.dispatchEvent(new Event("raghub:workflow-refresh"));await refresh();
      void message.success("已开始更新这篇笔记，处理完成后会自动用于问答。");
    });
  }
  async function inspectUpdate(){
    await run(async()=>{
      const saved=pendingUpdates[0];if(!saved||saved.instance!==instance)throw new Error("请在原数据实例中核对上次更新。");
      const value=await api("/api/ui/obsidian/workflows?kb_id="+kb+"&request_key="+saved.key,{},saved.instance);
      if(!value.workflow){setUpdateAbsent(saved);return;}
      acceptSubmission(saved,value.workflow.workflow_id);setUpdateAbsent(null);window.dispatchEvent(new Event("raghub:workflow-refresh"));await refresh();
    });
  }
  async function continueUpdate(){
    await run(async()=>{
      const saved=updateAbsent;if(!saved||saved.instance!==instance||(saved.body as RecordValue)?.kb_id!==kb)throw new Error("原更新范围已改变，不能继续提交。");
      const value=await post(saved.path,saved.body,saved.instance,saved.key);acceptSubmission(saved,value.workflow_id);setUpdateAbsent(null);window.dispatchEvent(new Event("raghub:workflow-refresh"));await refresh();
    });
  }
  function beginImport(documentId:string|null){setUpdateDoc(documentId);setImportType("obsidian");setFiles([]);setPreview(null);setResumeBatch(null);setUrl("");setRoot("");setDirectoryCandidates([]);setDirectoryServerReceive(false);setImportError("");setObsidianVisited(true);setObsidianInitialPath("");setImportOpen(true);}
  useEffect(()=>{
    if(!obsidianUpdate||viewStale||obsidianUpdate.kb_id!==kb||obsidianUpdate.data_instance_id!==instance)return;
    void updateNote({document_id:obsidianUpdate.document_id});
    clearObsidianUpdate?.();
  },[obsidianUpdate,clearObsidianUpdate,kb,instance,viewStale]);
  const polling=useRef(false),hasActive=useRef(false),dirInput=useRef<HTMLInputElement>(null);
  const listScopeRef=useRef({kb,page});listScopeRef.current={kb,page};
  const reload=useCallback(async()=>{
    if(!kb||polling.current)return;polling.current=true;
    try{
      const [documents,jobs,indexes,removing]=await Promise.all([api("/api/ui/documents?kb_id="+kb+"&offset="+(page-1)*50),api("/api/ui/tasks?kb_id="+kb),api("/api/knowledge-bases/"+kb+"/generations"),api("/api/knowledge-bases/"+kb+"/deletions",{},instance)]);
      if(listScopeRef.current.kb!==kb||listScopeRef.current.page!==page)return;
      setDocs(documents.items);setTotal(documents.total);setTasks(jobs.items);setGenerations(indexes.generations);setDeletions(removing.items);
      setListScope(kb);hasActive.current=jobs.items.some((item:RecordValue)=>["PENDING","RUNNING"].includes(item.status))||removing.items.some((item:RecordValue)=>item.state!=="FAILED");setStamp(new Date().toLocaleTimeString());setReadError("");
    }catch(err){setReadError(describeError(err));}finally{polling.current=false;}
  },[kb,page]);
  const workflowChanged=useCallback(()=>{void reload();void refresh();},[reload,refresh]);
  useEffect(()=>{void reload();let timer:ReturnType<typeof setTimeout>;const tick=async()=>{if(!document.hidden&&(hasActive.current||listScope!==kb))await reload();timer=setTimeout(tick,readError?5000:2000);};timer=setTimeout(tick,2000);const visible=()=>{if(!document.hidden)void reload();};document.addEventListener("visibilitychange",visible);return()=>{clearTimeout(timer);document.removeEventListener("visibilitychange",visible);};},[reload,readError,listScope,kb]);
  useEffect(()=>{setPage(1);setReview(null);setSource(null);setDocs([]);setTasks([]);setGenerations([]);setTotal(0);setListScope("");setStamp("");setReadError("");},[kb]);
  useEffect(()=>{dirInput.current?.setAttribute("webkitdirectory","");},[importOpen,importType]);
  async function run(work:()=>Promise<unknown>,rethrow=false,scope:"page"|"import"="page"){if(operationRunning.current)return;operationRunning.current=true;const setOperationError=scope==="import"?setImportError:setError;setBusy(true);setOperationError("");try{await work();await reload();}catch(err){setOperationError(scope==="import"&&err instanceof Error&&"code" in err&&typeof err.code==="string"?describeError(err):describeError(err));if(rethrow)throw err;}finally{operationRunning.current=false;setBusy(false);}}
  const batchRecords=savedSubmissions().filter(value=>value.path==="/api/import/batches"&&(value.body as RecordValue)?.kb_id===kb).slice(0,20);
  const fileKey=(item:RecordValue)=>item.relative_path??item.filename;
  async function pick(values:FileList|File[]|null,directory=false){
    if(!values)return;setBusy(true);setImportError("");
    try{
      const expected:RecordValue[]|null=resumeBatch?resumeBatch.items.map((item:RecordValue)=>item.metadata):directory&&!updateDoc?preview?.manifest.files??null:null;
      if(directory&&!expected&&!updateDoc)throw new Error("先扫描目录预览，再选择同一目录中的文件。");
      const candidates=Array.from(values);
      const chosen=directory&&expected?candidates.filter(file=>expected.some(item=>item.relative_path===file.webkitRelativePath.split("/").slice(1).join("/"))):candidates;
      if(updateDoc&&chosen.length!==1)throw new Error("更新资料时只能选择一个文件。");
      if(directory&&updateDoc&&root.split("/").filter(Boolean).at(-1)!==chosen[0].webkitRelativePath.split("/")[0])throw new Error("本机选择的目录名与输入的绝对路径不一致。");
      if(updateDoc){setPreview(null);setDirectoryServerReceive(false);}
      if(chosen.length>50||chosen.some(file=>file.size>50*1024*1024)||chosen.reduce((sum,file)=>sum+file.size,0)>500*1024*1024)throw new Error("每批最多50个文件，单文件50 MiB、整批500 MiB。");
      const items:Picked[]=[];const seen=new Set<string>();
      for(const file of chosen){
        const relative=directory?file.webkitRelativePath.split("/").slice(1).join("/"):null,key=relative??file.name;
        if(seen.has(key))throw new Error("文件路径重复："+key);seen.add(key);
        const hash=await fileHash(file),original=expected?.find(item=>fileKey(item)===key);
        if(expected&&(!original||original.filename!==file.name||original.sha256!==hash||original.size_bytes!==file.size))throw new Error("文件与原清单不一致："+key);
        if(!/\.(pdf|md|txt|html?|docx|xlsx|csv|json|png|jpe?g|webp|bmp|tiff?)$/i.test(file.name))throw new Error("不支持的格式："+file.name);
        items.push({file,metadata:original??{filename:file.name,size_bytes:file.size,sha256:hash,relative_path:relative,document_id:updateDoc,provenance:{nature:"ORIGINAL",origin_url:null,origin_local_path:null,source_revision:null,source_date:null,license:null,derivation:null}}});
      }
      if(expected&&items.length!==expected.length)throw new Error("缺少原清单文件："+expected.filter(item=>!seen.has(fileKey(item))).map(fileKey).join("；"));
      setFiles(items);
    }catch(err){setFiles([]);setImportError(describeError(err));}finally{setBusy(false);}
  }
  async function previewDirectory(){
    await run(async()=>{
      if(updateDoc){
        if(files.length!==1)throw new Error("请先选择目录中的一个文件。");
        const result=await post("/api/ui/directory-update/preview",{kb_id:kb,document_id:updateDoc,absolute_root:root,file:files[0].metadata},instance);
        if(result.manifest.files.length!==1||result.manifest.files[0].document_id!==updateDoc)throw new Error("更新预览的目标文档不一致。");
        setPreview(result);setFiles([{file:files[0].file,metadata:result.manifest.files[0]}]);setDirectoryServerReceive(true);
      }else{
        const scan=await post("/api/ui/directory-scan",{kb_id:kb,absolute_root:root},instance);
        const result=await post("/api/import/directory/preview",scan,instance);setPreview(result);setFiles([]);setDirectoryServerReceive(false);
      }
    },false,"import");
  }
  async function readBatch(saved:Submission){
    await run(async()=>{
      if(saved.instance!==instance)throw new Error("原批次属于另一实例。旧操作可能已执行，不更换实例、不恢复上传。");
      const batch=await api("/api/import/batches/by-request/"+saved.key,{},saved.instance);
      if(batch.data_instance_id!==saved.instance||batch.request_key!==saved.key||batch.kb_id!==(saved.body as RecordValue).kb_id)throw new Error("原批次身份与本地记录不一致，已停止恢复。");
      const originalPreview=batch.preview_id?await api("/api/import/directory/previews/"+batch.preview_id,{},saved.instance):null;
      const serverReceive=!!originalPreview&&originalPreview.manifest.scope==="SELECTION"&&originalPreview.manifest.files.length===1&&!!originalPreview.manifest.files[0].document_id;
      setDirectoryServerReceive(serverReceive);setDirectoryCandidates([]);
      acceptSubmission(saved,batch.batch_id);setUpdateDoc(batch.items.length===1?batch.items[0].metadata.document_id??null:null);setUrl("");setRoot("");setPreview(null);setResumeBatch(batch);setFiles([]);setImportType(batch.preview_id?"directory":"files");setResumeOpen(false);setImportError("");setImportOpen(true);
    });
  }
  const pendingContinuations=savedSubmissions().filter(value=>value.path==="/api/ui/import/continue"&&(value.body as RecordValue)?.kb_id===kb&&value.phase==="AWAITING_CONFIRMATION");
  async function checkImportSlot(){
    const value=await api("/api/ui/import/availability",{},instance);
    if(value.busy===true)throw new Error("另一批资料正在处理或等待处理问题。请先等待原批次完成，或处理原进度；已受理的任务保留，暂不提交新的后续处理。");
    if(value.busy!==false)throw new Error("尚未核对当前处理状态，已暂停新提交。请先读取原状态。");
  }
  async function readContinuation(){
    const saved=pendingContinuations[0];if(!saved||saved.instance!==instance)throw new Error("请回到原数据实例核对处理记录。");
    const result=await api("/api/ui/import/continue/by-request/"+saved.key+"?kb_id="+encodeURIComponent(kb),{},instance);
    let value=result.workflow;
    if(value===null){
      await checkImportSlot();
      value=await post("/api/ui/import/continue",saved.body,saved.instance,saved.key);
    }
    if(!value||typeof value.workflow_id!=="string")throw new Error("原处理状态仍无法确认；保留原提交，不重新导入资料。");
    acceptSubmission(saved,value.workflow_id);window.dispatchEvent(new Event("raghub:workflow-refresh"));await reload();await refresh();
  }
  async function continueTasks(taskIds:string[]){
    if(pendingContinuations.length){await readContinuation();return;}
    await persistentPost("/api/ui/import/continue",{kb_id:kb,task_ids:[...new Set(taskIds)]},instance);
    window.dispatchEvent(new Event("raghub:workflow-refresh"));await refresh();
  }
  async function importFiles(){
    await run(async()=>{
      let batch=resumeBatch;
      if(!batch){
        if(batchRecords.some(value=>value.phase==="AWAITING_CONFIRMATION"&&value.instance===instance))throw new Error("上次文件提交尚未确认，请先读取原提交。查询失败不能证明此前没有执行。");
        await checkImportSlot();
        let directory=preview;
        if(importType==="directory"){if(!directory)throw new Error("请先读取并核对目录预览。");directory=await post("/api/import/directory/previews/"+directory.preview_id+"/confirm",{preview_sha256:directory.preview_sha256,confirmed:true},instance);}
        batch=await persistentPost("/api/import/batches",{kb_id:kb,items:files.map(item=>item.metadata),preview_id:directory?.preview_id??null,preview_sha256:preview?.preview_sha256??null},instance);setResumeBatch(batch);
      }
      if(!batch)throw new Error("尚未确认批次，停止上传。");
      const current=await api("/api/import/batches/"+batch.batch_id,{},instance);
      if(current.data_instance_id!==instance||current.request_key!==batch.request_key)throw new Error("原批次身份改变，停止上传。");
      for(const item of current.items){
        if(item.state==="ACCEPTED")continue;
        if(item.state!=="AWAITING_UPLOAD")throw new Error(fileKey(item.metadata)+" 当前为 "+item.state+"；先读取原状态，不重新传送。");
        if(directoryServerReceive){await post("/api/ui/directory-update/batches/"+batch.batch_id+"/items/"+item.item_id+"/receive",{},instance);continue;}
        const selected=files.find(value=>fileKey(value.metadata)===fileKey(item.metadata));
        if(!selected||selected.metadata.sha256!==item.metadata.sha256||selected.file.size!==item.metadata.size_bytes)throw new Error("请重新选择并核对原文件："+fileKey(item.metadata));
        const body=new FormData();body.append("file",selected.file,selected.file.name);
        await api("/api/import/batches/"+batch.batch_id+"/items/"+item.item_id+"/file",{method:"POST",body},instance);
      }
      const final=await api("/api/import/batches/"+batch.batch_id,{},instance);
      if(!final.items.every((item:RecordValue)=>item.state==="ACCEPTED"))throw new Error("文件接收尚未全部确认。读取原批次后再决定，不自动重传。");
      await continueTasks(final.items.map((item:RecordValue)=>item.task_id));
      setImportOpen(false);setFiles([]);setPreview(null);setResumeBatch(null);setUpdateDoc(null);void message.success("文件已接收，系统会继续解析并建立索引。");
    },false,"import");
  }
  const pendingUrls=savedSubmissions().filter(value=>value.path==="/api/import/urls"&&(value.body as RecordValue)?.kb_id===kb&&value.phase==="AWAITING_CONFIRMATION");
  async function readOriginalUrl(){await run(async()=>{
    const saved=pendingUrls[0];if(!saved||saved.instance!==instance)throw new Error("原提交属于其他数据实例，请核对原实例状态。");
    const value=await api("/api/ui/submissions/"+saved.key,{},saved.instance);
    if(value.kind!=="task"||!value.task_id||value.kb_id!==(saved.body as RecordValue).kb_id)throw new Error("原提交身份无法核对，保留原请求且停止。");
    acceptSubmission(saved,value.task_id);setTaskDetail(await api("/api/tasks/"+value.task_id));
  },false,"import");}
  async function importUrl(){await run(async()=>{if(pendingUrls.length)throw new Error("上次网页提交结果待确认，请先读取原提交；不会自动更换请求键。");await checkImportSlot();const accepted=await persistentPost("/api/import/urls",{kb_id:kb,url,document_id:updateDoc,profile,provenance:{nature:"ORIGINAL",origin_url:url,origin_local_path:null,source_revision:null,source_date:null,license:null,derivation:null}},instance);await continueTasks([accepted.task_id]);setImportOpen(false);setUrl("");setUpdateDoc(null);},false,"import");}
  async function openReview(doc:RecordValue){setReview(doc);setRevision(null);setRevisionFile(null);setRevisionRaw("");setDecisionNote("");setVerified(false);setReviews({});setReviewFields([]);setTextEdits({});setViewError("");setViewSection("quality");setViewOffset(0);}
  function invalidateReview(){setRevision(null);setVerified(false);}
  function changeReview(id:string,change:RecordValue){invalidateReview();setReviews(old=>{const next:Record<string,RecordValue>={...old,[id]:{issue_id:id,...old[id],...change}};if(!next[id].outcome&&!next[id].note)delete next[id];return next;});}
  function editText(pointer:string,value:string,original:string){invalidateReview();setTextEdits(old=>{const next={...old};if(value===original)delete next[pointer];else next[pointer]=value;return next;});}
  function closeReview(){if(busy)return;if(Object.keys(textEdits).length||Object.keys(reviews).length||revisionFile){modal.confirm({title:"关闭前，请确认是否放弃尚未提交的修改",content:"页面里的文字修改和核对结论还没有保存。原资料和已完成的处理结果不会被删除。",okText:"关闭且不提交",cancelText:"继续编辑",onOk:()=>setReview(null)});}else setReview(null);}

  useEffect(()=>{
    if(!review?.current_processing_version_id)return;let active=true;setView(null);setViewError("");setViewLoading(true);
    const current=api("/api/ui/documents/"+review.document_id+"/processing-view?section="+viewSection+"&offset="+viewOffset,{},instance);
    void current.then(value=>{
      if(!active)return;if(!Array.isArray(value.items)||typeof value.total!=="number")throw new Error("解析视图不完整，请重新打开当前资料。");
      setView(value);const rows=viewSection==="fields"?value.items:value.related_fields;
      if(Array.isArray(rows))setReviewFields(old=>{const map=new Map(old.map(item=>[item.field_pointer,item]));for(const item of rows)map.set(item.field_pointer,item);return [...map.values()];});
    }).catch(err=>{if(active)setViewError(describeError(err));}).finally(()=>{if(active)setViewLoading(false);});
    return()=>{active=false;};
  },[review,viewSection,viewOffset,instance]);
  async function previewRevision(){
    if(!review||!importing)return;const scope=reviewScopeRef.current;await run(async()=>{
      const decisions=Object.values(reviews);
      if(decisions.some(item=>!item.outcome||!item.note?.trim()))throw new Error("请为每条已填写的核对记录选择结论，并写明依据；未查明的问题可以暂不填写。");
      if(!revisionFile&&!Object.keys(textEdits).length&&!decisions.length)throw new Error("尚无需要提交的文字修改或核对结论。");
      let nativeRaw=revisionRaw;
      if(!revisionFile){
        const response=await fetch("/api/parsing/versions/"+review.current_processing_version_id+"/native",{headers:{"X-RAG-Data-Instance":instance},credentials:"same-origin"});
        nativeRaw=await response.text();
        if(!response.ok)throw new Error("完整解析数据读取失败，请重新核对当前版本；页面修改仍保留。");
      }
      if(!nativeRaw)throw new Error("完整解析数据不可用，请重新选择修订文件。");
      if(scope!==reviewScopeRef.current)return;
      const body=revisionRequestBody(review.document_id,review.current_processing_version_id,nativeRaw,revisionFile?{}:textEdits,decisions);
      const result=await api("/api/parsing/revisions/preview",{method:"POST",body},instance);
      if(scope===reviewScopeRef.current){setRevision(result);setVerified(false);}
    });
  }
  async function decideRevision(decision:string){if(!revision||!decisionNote.trim())return;await run(async()=>{await persistentPost("/api/parsing/revisions/"+revision.preview_id+"/submit",{decision,source:"LOCAL_USER_UI",note:decisionNote,preview_sha256:revision.preview_sha256},instance);setReview(null);});}
  async function queueOcr(){if(!review)return;await run(async()=>{const result=await persistentPost("/api/parsing/ocr/tasks",{document_id:review.document_id,processing_version_id:review.current_processing_version_id,field_pointer:field,crops:[{page:Number(cropPage),box:null}]},instance);setOcrTask(result.task_id);setOcr(null);});}
  async function readOcr(){await run(async()=>setOcr(await api("/api/parsing/ocr/tasks/"+ocrTask)));}
  async function decideOcr(decision:string){await run(async()=>{await post("/api/parsing/ocr/tasks/"+ocrTask+"/decision",{decision,source:"LOCAL_USER_UI",note:decisionNote,modified_text:decision==="MODIFY_ACCEPT"?modified:null,reviews:decision==="REJECT"?[]:Object.values(reviews)},instance);setOcrOpen(false);});}
  async function removeDoc(doc:RecordValue){modal.confirm({title:"删除 "+doc.display_name+"？",content:"将删除这份资料在 RAG 中保存的原文、解析和检索内容，无法恢复。旧回答保留，引用显示“资料已删除”；受影响的旧会话只读，继续提问需新建会话。本地原始文件不会被修改。",okText:"删除资料",okButtonProps:{danger:true},cancelText:"保留",onOk:()=>run(()=>api("/api/knowledge-bases/"+kb+"/documents/"+doc.document_id,{method:"DELETE"},instance),true)});}
  const pendingReparses=savedSubmissions().filter(value=>value.path==="/api/parsing/tasks"&&(value.body as RecordValue)?.kb_id===kb&&value.phase==="AWAITING_CONFIRMATION");
  function failedDocument(task:RecordValue|null){const row=tasks.find(value=>value.task_id===task?.task_id);return docs.find(value=>value.document_id===row?.document_id&&value.processing_status==="FAILED");}
  function taskProblem(task:RecordValue|null){
    const error=task?.last_error;if(!error)return "";
    if(error.code==="PARSE_FAILED")return "文字读取或整理未通过系统校验。归档原文仍保留；系统修复后，可以重新处理这份原文。";
    if(error.code==="GENERATION_FAILED")return "建立索引时停止；已保存的资料和之前可用的版本仍保留。请核对原任务后继续。";
    if((error.code==="URL_NONPUBLIC_DNS"&&error.details?.host==="r.jina.ai")||(error.code==="WECHAT_TEXT_READER_FAILED"&&error.details?.reader_stage==="PUBLIC_ADDRESS_CHECK"))return "微信直连未取得正文，备用文字通道的网络检查也未通过。原任务记录仍保留，没有导入正文。请检查本机网络或代理设置；也可以把文章保存为 PDF 或 Markdown 后从本地文件导入。";
    if(error.code==="WECHAT_TEXT_READER_FAILED")return "微信直连和备用文字读取都未取得正文。原任务记录仍保留；请在浏览器打开文章，保存为 PDF 或 Markdown 后从本地文件导入。";
    if(error.code==="WEB_PAGE_VERIFICATION_REQUIRED")return "网站要求验证，尚未取得正文。请保存为 PDF 或 Markdown 后导入。";
    if(typeof error.message==="string"&&error.message.length<=240&&/[一-龥]/.test(error.message)&&!["\n","{","}","[","]","Traceback","validation error","http://","https://"].some(value=>error.message.includes(value)))return error.message;
    return "本次处理没有完成。已保存的原文和之前可用的结果仍保留；请读取原状态，再从允许恢复的位置继续。";
  }
  async function readReparse(){
    const saved=pendingReparses[0];if(!saved||saved.instance!==instance)throw new Error("请在原实例中核对上次处理，不能重新提交。");
    await run(async()=>{const value=await api("/api/ui/submissions/"+saved.key,{},saved.instance);if(value.kind!=="task"||!value.task_id||value.kb_id!==kb)throw new Error("原处理结果仍无法核对，保留请求记录，不重新提交。");acceptSubmission(saved,value.task_id);setTaskDetail(await api("/api/tasks/"+value.task_id));window.dispatchEvent(new Event("raghub:workflow-refresh"));},true);
  }
  async function reprocessTask(task:RecordValue){
    await run(async()=>{
      if(pendingReparses.length)throw new Error("上次重新处理的结果待确认，请先读取原提交。");
      const doc=failedDocument(task);if(!doc?.current_raw_version_id||!importing)throw new Error("请先读取当前资料状态；只重新处理已保存且明确失败的原文。");
      const accepted=await persistentPost("/api/parsing/tasks",{kb_id:kb,document_id:doc.document_id,expected_raw_version_id:doc.current_raw_version_id,config:{}},instance);
      setTaskDetail(await api("/api/tasks/"+accepted.task_id));
      const flow=state?.import_workflow;
      if(flow&&!["COMPLETE","PARTIAL_COMPLETE","CANCELLED"].includes(flow.state)){
        window.dispatchEvent(new Event("raghub:workflow-refresh"));await refresh();
      }else await continueTasks([accepted.task_id]);
    },true);
  }
  async function recoverTask(task:RecordValue){modal.confirm({title:"从上次停下的位置继续？",content:"系统会先确认上次处理已停止，再从可恢复的步骤继续。已保存的结果保留；云端结果未知时不会再次发送。",okText:"确认并检查",cancelText:"取消",onOk:()=>run(()=>post("/api/tasks/"+task.task_id+"/recover",{expected_attempt_id:task.current_attempt_id,confirm_stopped:true},instance),true)});}
  const docColumns=[
    {title:"资料",dataIndex:"display_name",render:(value:string,row:RecordValue)=><div><strong>{value}</strong>{row.source_notice&&<div className="muted" style={{fontSize:12}}>文字副本 · 图片和版式可能不全</div>}<></>{!row.current_raw_version_id&&!row.removed_at&&<div className="muted" style={{fontSize:12}}>原文尚未保存，暂时不能查看</div>}</div>},
    {title:"当前状态",render:(_:any,row:RecordValue)=>{
      const archiving=!row.current_raw_version_id&&["PENDING","RUNNING"].includes(row.task_status);
      const published=!row.removed_at&&!!row.has_published_version,currentPublished=published&&!!row.current_version_published;
      const updateLabel=row.processing_status==="READY"?"新版待发布":row.processing_status==="REVIEW_REQUIRED"?"新版待复核":row.processing_status==="FAILED"?"更新失败":row.processing_status==="PROCESSING"?"新版解析中":"新版待解析";
      const label=row.removed_at?"已移除":archiving?(row.task_status==="RUNNING"?"正在归档":"等待归档"):currentPublished?"已发布":published?updateLabel:row.processing_status==="READY"?"已解析 · 待发布":statuses[row.processing_status]??statuses[row.task_status]??"已归档";
      const color=row.removed_at?"default":row.processing_status==="FAILED"||row.task_status==="FAILED"?"red":archiving||row.processing_status==="REVIEW_REQUIRED"?"gold":"green";
      return <div><Tag color={color}>{label}</Tag>{published&&<div className="muted" style={{fontSize:12,marginTop:4}}>{state?.mode==="QUERY"?(currentPublished?"可提问":"问答使用已发布版"):"问答暂停，已发布版保留"}</div>}</div>;
    }},

    {title:"操作",render:(_:any,row:RecordValue)=><Space wrap>
      <span title={!row.current_raw_version_id?"原件归档完成后可查看原文":undefined}><Button size="small" disabled={!row.current_raw_version_id||!!row.removed_at} onClick={()=>setSource({document_id:row.document_id})}>原文</Button></span>
      {row.processing_status==="REVIEW_REQUIRED"&&!row.removed_at&&<Button size="small" onClick={()=>void openReview(row)}>处理问题</Button>}
      {["NEW","READY"].includes(row.processing_status)&&!row.current_version_published&&row.active_prepare_task_id&&!flowProcessing&&<Button size="small" disabled={busy||viewStale} onClick={()=>void run(()=>continueTasks([row.active_prepare_task_id]))}>继续处理</Button>}
      <Space wrap>
      {row.can_update&&<Button size="small" loading={busy} disabled={viewStale||busy||flowProcessing||pendingUpdates.length>0||!!row.removed_at||!row.current_raw_version_id} onClick={()=>void updateNote(row)}>更新</Button>}
      <Button danger size="small" disabled={viewStale||busy||!!row.removed_at} onClick={()=>void removeDoc(row)}>删除</Button>
      </Space>
    </Space>}
  ];
  const taskColumns=[{title:"资料",dataIndex:"display_name",render:(v:string)=>v||"知识库整理"},{title:"工作",dataIndex:"kind",render:(v:string)=>v==="OCR_SUGGESTION"?"图片文字核对":"资料处理"},{title:"进度",dataIndex:"stage",render:(v:string,row:RecordValue)=>row.status==="SUCCEEDED"?"该步骤已完成":["FAILED","CANCELLED","INTERRUPTED"].includes(row.status)?"已停止："+(taskStages[v]??"资料处理").replace("正在",""):taskStages[v]??"正在处理"},{title:"状态",dataIndex:"status",render:(v:string)=><Tag>{statuses[v]??"状态待确认"}</Tag>},{title:"操作",render:(_:any,row:RecordValue)=><Space><Button size="small" onClick={()=>void run(async()=>setTaskDetail(await api("/api/tasks/"+row.task_id)))}>查看进度</Button>{["PENDING","RUNNING"].includes(row.status)?<Button size="small" disabled={viewStale||busy} onClick={()=>void run(()=>post("/api/tasks/"+row.task_id+"/cancel",{},instance))}>取消</Button>:["FAILED","CANCELLED","INTERRUPTED"].includes(row.status)?<Button size="small" disabled={!importing||viewStale||busy||pendingReparses.length>0} onClick={()=>void (failedDocument(row)?reprocessTask(row):recoverTask(row))}>{failedDocument(row)?"重新处理原文":"继续处理"}</Button>:null}</Space>}];
  return <section><div className="page-heading"><div><h1>资料管理</h1><p className="muted">选择笔记并确认一次，系统会连续归档、检查和建库；有问题时再请你处理。</p></div><Space wrap><Button disabled={viewStale} onClick={()=>{setName("");setCreateKbOpen(true);}}>新建知识库</Button><Button type="primary" disabled={!kb||deletions.some(d=>d.state!=="FAILED")} onClick={()=>beginImport(null)}>导入资料</Button></Space></div>
    {pendingUpdates.length>0&&<Alert showIcon type="warning" message={updateAbsent?"原更新暂未受理":"上次导入或更新的结果待核对"} description="已保存原提交记录。先读取原状态，不创建新的更新请求。" action={<Space wrap><Button disabled={busy} onClick={()=>void inspectUpdate()}>读取原状态</Button>{updateAbsent&&<Button disabled={busy} onClick={()=>void continueUpdate()}>继续本次更新</Button>}</Space>}/>}
    {pendingContinuations.length>0&&<Alert showIcon type="warning" message="后续处理待核对" description="原提交记录与已接收任务已保留。先核对状态，再继续后续处理；不要重复导入。" action={<Button loading={busy} onClick={()=>void run(readContinuation)}>核对并继续处理</Button>}/>}
    {deletions.length>0&&<Alert showIcon type={deletions.some(d=>d.state==="FAILED")?"warning":"info"} message={deletions.some(d=>d.state==="FAILED")?"删除已生效，部分副本尚未清理完":"正在删除资料并整理知识库"} description="资料已从列表和检索中排除；旧回答保留，受影响的旧会话只读。本地原始文件不变。" action={deletions.some(d=>d.state==="FAILED")?<Button disabled={busy} onClick={()=>void run(async()=>{for(const d of deletions.filter(d=>d.state==="FAILED"))await post("/api/document-deletions/"+d.document_id+"/resume",{},instance);})}>继续清理</Button>:undefined}/>}
    {readError&&<div style={{marginBottom:16}}><ErrorNotice error={readError} stage="读取资料和任务状态" retained="保留上次成功列表，显示内容可能已过期。" next="先刷新核对；确认前暂停需要实时状态的操作。" onRecover={()=>void reload()} recoverLabel="刷新核对状态"/></div>}
    {error&&<div style={{marginBottom:16}}><ErrorNotice error={error} stage="资料操作" retained="已归档资料和已接收任务仍保留。" next="先刷新列表并查看原任务，再处理尚未完成的步骤。" onRecover={()=>void reload()}/></div>}
    {pendingReparses.length>0&&<Alert showIcon type="warning" message="重新处理的提交结果待确认" description="原请求已保留。先读取上次状态，确认前不再次提交。" action={<Button disabled={busy} onClick={()=>void readReparse()}>读取上次处理状态</Button>}/>}
    {kb&&<ImportProgress instance={instance} kb={kb} onChange={workflowChanged} onTasks={()=>{setActiveTab("tasks");void reload();}} onSource={item=>setSource({document_id:item.document_id,raw_version_id:item.raw_version_id})} onSettings={item=>setParseDoc({document_id:item.document_id,display_name:item.name,current_raw_version_id:item.raw_version_id})} onProblem={item=>void openReview({document_id:item.document_id,display_name:item.name,current_processing_version_id:item.processing_version_id})}/>}
    <></>
    {!kb&&<div className="surface"><Empty description="先创建一个知识库"/><Space style={{marginTop:16}}><Input aria-label="知识库名称" placeholder="例如：AI开发资料" value={name} onChange={event=>setName(event.target.value)}/><Button type="primary" disabled={!name.trim()||viewStale} onClick={()=>void run(async()=>{if(state?.mode!=="IMPORT")await post("/api/mode",{mode:"IMPORT",kb_id:null},instance);const result=await post("/api/knowledge-bases",{name:name.trim()},instance);await refresh();setKb(result.kb_id);})}>创建知识库</Button></Space></div>}
    {kb&&<Spin spinning={busy}><div className="surface">{activeTab==="tasks"?<><Button onClick={()=>setActiveTab("documents")}>返回资料</Button><h2>处理进度</h2><Table rowKey="task_id" columns={taskColumns} dataSource={tasks} pagination={{pageSize:50,showSizeChanger:false}} scroll={{x:600}}/></>:<Table rowKey="document_id" columns={docColumns} dataSource={docs} pagination={{current:page,pageSize:50,total,showSizeChanger:false,onChange:setPage}} scroll={{x:620}} locale={{emptyText:"还没有资料。点击“导入资料”，选择要用于问答的笔记。"}}/>}<div className="status-line" role="status">{viewStale?"状态待核对 · ":""}最近更新 {stamp||"尚未读取"}</div></div></Spin>}
    <Modal open={importOpen} onCancel={()=>setImportOpen(false)} title={resumeBatch?"继续原批次上传":updateDoc?"更新资料":"导入资料"} footer={null} width={importType==="obsidian"?1160:760}>
      <div className="stack">
        {importError&&<ErrorNotice error={importError} stage="资料导入" retained="已经受理的项目仍在后台，已选择的文件保留。" next="先读取上次文件提交或原任务。结果未知时不要再点受理。" onRecover={()=>{if(importType==="url")void readOriginalUrl();else setResumeOpen(true);}}/>} 
        {updateDoc&&<Alert type="info" message={"更新："+(docs.find(item=>item.document_id===updateDoc)?.display_name??updateDoc)} description="请选择这份资料的新版内容，或重新填写它的原网址。更新失败时，原来可用的版本继续保留。" />}
        {resumeBatch&&<Alert type="info" message="继续上次未完成的文件导入" description={directoryServerReceive?"读取原状态后，手动继续接收未受理项；后端核对原目录文件及图片字节，已接收项跳过，失败或结果未知时停止。":"重新选择原批次全部文件；只有核对通过且你点击继续，才上传缺项。已接收项跳过，处理中、失败或结果未知的项停止。"} />}
        <Tabs className="import-type-tabs" activeKey={importType} onChange={value=>{if(value!=="obsidian"&&!importing)void run(async()=>{await post("/api/mode",{mode:"IMPORT",kb_id:null},instance);await refresh();});if(value==="obsidian")setObsidianVisited(true);setImportType(value);setResumeBatch(null);setFiles([]);setPreview(null);setUrl("");setRoot("");setDirectoryCandidates([]);setDirectoryServerReceive(false);}} items={[{key:"obsidian",label:"Obsidian 笔记"},{key:"files",label:"本地文件"},{key:"url",label:"网页 URL"},{key:"dy",label:"抖音视频与图文"}].filter(item=>!updateDoc||item.key!=="dy")}/>
        <div hidden={importType!=="obsidian"}>{obsidianVisited&&<LoadBoundary label="Obsidian 知识库"><ObsidianImport key={updateDoc??"new-import"} active={importOpen&&importType==="obsidian"} instance={instance} kb={kb} kbName={kbName} importing={importing} documentId={updateDoc} documentName={docs.find(item=>item.document_id===updateDoc)?.display_name??updateDoc??""} initialPath={obsidianInitialPath} onAccepted={()=>{setImportOpen(false);void reload();void refresh();window.dispatchEvent(new Event("raghub:obsidian-refresh"));window.dispatchEvent(new Event("raghub:workflow-refresh"));}}/></LoadBoundary>}</div>
        {importType==="obsidian"?null:importType==="dy"?<LoadBoundary label="抖音视频"><DouyinImport active={importOpen&&importType==="dy"} instance={instance} kb={kb} kbName={kbName} importing={importing} beforeSubmit={checkImportSlot} onPreview={relativePath=>{setUpdateDoc(null);setResumeBatch(null);setObsidianInitialPath(relativePath);setObsidianVisited(true);setImportType("obsidian");}}/></LoadBoundary>:importType==="url"?<>
          {pendingUrls.length>0&&<Alert showIcon type="warning" message="网页提交结果待确认" description="原实例和请求键已保留。先读取原任务，确认前暂停新的网页提交。" action={<Button disabled={busy} onClick={()=>void readOriginalUrl()}>读取原提交</Button>}/>}
          <Input aria-label="网页 URL" placeholder="https://…" value={url} onChange={event=>setUrl(event.target.value)}/>
          {url.includes("mp.weixin.qq.com")&&<Alert showIcon type="info" message="优先读取完整文章，受阻时尝试备用文字" description="直连成功时保存文章和图片；直连失败时尝试备用读取器。备用副本可能缺少图片和版式，页面会明确标注。如果仍无法读取，请保存为 PDF 或 Markdown 后从“本地文件”导入。"/>}
          <Button type="primary" loading={busy} disabled={!importing||!url||pendingUrls.length>0} onClick={()=>void importUrl()}>{updateDoc?"更新并用于问答":"导入并用于问答"}</Button>
        </>:<>
          {importType==="directory"&&<>
            {!resumeBatch&&<Input aria-label="本地目录绝对路径" placeholder="本机目录绝对路径，需与选中文件目录一致" value={root} onChange={event=>{setRoot(event.target.value);setPreview(null);setFiles([]);setDirectoryCandidates([]);setDirectoryServerReceive(false);}}/>}
            {updateDoc&&<Alert type="info" message="从目录中选择一份文件更新当前资料" description="先填写目录绝对路径，再选择同一目录和其中一个文件。只更新当前资料；所需本地图片会随原件归档。"/>}
            <input disabled={!!(resumeBatch&&directoryServerReceive)||(!updateDoc&&!resumeBatch&&!preview)} ref={dirInput} aria-label="选择目录文件" type="file" multiple onChange={event=>{
              if(updateDoc&&!resumeBatch){const values=Array.from(event.target.files??[]);setFiles([]);setPreview(null);setDirectoryServerReceive(false);if(values.length>2000){setDirectoryCandidates([]);setImportError("目录中超过2000个文件，请选择较小的子目录。");return;}setDirectoryCandidates(values.filter(file=>/\.(pdf|md|txt|html?|docx|xlsx|csv|json|png|jpe?g|webp|bmp|tiff?)$/i.test(file.name)));setImportError("");}
              else void pick(event.target.files,true);
            }}/>
            {updateDoc&&!resumeBatch&&<Select aria-label="目录中用于更新的文件" placeholder="选择一个文件用于更新" showSearch optionFilterProp="label" value={files[0]?.file.webkitRelativePath} options={directoryCandidates.map(file=>({value:file.webkitRelativePath,label:file.webkitRelativePath.split("/").slice(1).join("/")}))} onChange={value=>{const file=directoryCandidates.find(item=>item.webkitRelativePath===value);if(file)void pick([file],true);}}/>}
          </>}
          {importType==="files"&&<input aria-label="选择本地文件" type="file" multiple={!updateDoc} onChange={event=>void pick(event.target.files)}/>} 
          <Typography.Text type="secondary">{updateDoc?"更新时选择1份资料；单文件50 MiB，连同图片最多500 MiB。":"最多50个文件 · 单个50 MiB · 整批500 MiB。"}文件哈希按真实字节计算。</Typography.Text>
          <Table size="small" rowKey={row=>row.metadata.sha256+row.metadata.filename} columns={[{title:"文件",render:(_:any,row:Picked)=>row.metadata.relative_path??row.file.name},{title:"大小",render:(_:any,row:Picked)=>(row.file.size/1024).toFixed(1)+" KiB"}]} dataSource={files} pagination={false}/>
          {importType==="directory"&&!resumeBatch&&<>
            <Button disabled={!root.startsWith("/")||!importing||!!(updateDoc&&!files.length)} loading={busy} onClick={()=>void previewDirectory()}>{updateDoc?"生成更新预览":"生成目录预览"}</Button>
            {preview&&<Alert message={updateDoc?"请核对更新预览":"请核对目录预览"} description={updateDoc?<div className="stack"><p>将文件 <strong>{preview.manifest.files[0].relative_path}</strong> 作为当前资料的新原件。更新1份资料，保留旧版本。</p><Table size="small" rowKey="relative_path" dataSource={preview.dependency_entries} columns={[{title:"随原件归档的本地图片",dataIndex:"relative_path"},{title:"大小",dataIndex:"size_bytes",render:(size:number)=>(size/1024).toFixed(1)+" KiB"}]} pagination={false} locale={{emptyText:"没有本地图片依赖"}}/><></></div>:<p>共 {preview.manifest?.files?.length??0} 份文件；请核对上方文件名称。随文件一起归档的本地图片会一并处理。</p>} type="info"/>}
          </>}
          <Button type="primary" loading={busy} disabled={(!files.length&&!(resumeBatch&&directoryServerReceive))||!importing||(importType==="directory"&&!preview&&!resumeBatch)} onClick={()=>void importFiles()}>{resumeBatch?"继续接收已核对的缺项":updateDoc?"确认并更新此资料":importType==="directory"?"确认并用于问答":"导入并用于问答"}</Button>
        </>}
      </div>
    </Modal>
    <Drawer open={!!review} onClose={closeReview} closable={!busy} maskClosable={!busy} width={960} title={(review?.display_name??"")+" · 处理问题"}>
      <div className="stack">
        {error&&<Alert showIcon type="error" message="本次操作未完成" description={<><p>{error}</p><p>你的文字修改和核对记录仍保留。请处理具体原因，再重新检查；未确认保存前不会替换当前解析版本。</p></>}/>}
        {!importing&&<Alert type="info" showIcon message="当前仅可查看" description="修改、复核提交和图片文字纠错须在导入模式进行。已填写但未提交的内容不会自动保存。"/>}
        <Space wrap><Button onClick={()=>setSource({document_id:review?.document_id,raw_version_id:review?.current_raw_version_id})}>查看归档原文</Button>{viewSection!=="quality"&&<Button onClick={()=>{setViewSection("quality");setViewOffset(0);}}>返回问题列表</Button>}</Space><p className="muted">只需处理下方列出的问题。来源定位等系统问题暂时不能人工放行，原文仍可查看。</p>
        {viewError&&<Alert showIcon type="error" message="本页解析结果未能读取" description={viewError}/>}
        {viewLoading&&<Spin tip="正在读取当前页解析结果"/>}
        {view&&<>
          {viewSection==="quality"&&<QualityIssues items={view.items} fields={reviewFields} edits={textEdits} onEdit={editText} reviews={reviews} disabled={!importing||busy} onReview={changeReview} onSource={()=>setSource({document_id:review?.document_id,raw_version_id:review?.current_raw_version_id})} onFields={()=>{setViewSection("fields");setViewOffset(0);}} onOcr={pointer=>{setField(pointer);setOcrOpen(true);setOcrTask("");setOcr(null);}}/>}
          {viewSection==="fields"&&<ParsedFields items={view.items} edits={textEdits} disabled={!importing||busy||!!revisionFile} onEdit={editText} onSource={()=>setSource({document_id:review?.document_id,raw_version_id:review?.current_raw_version_id})} onOcr={pointer=>{setField(pointer);setOcrOpen(true);setOcrTask("");setOcr(null);}}/>}
          {viewSection==="native"&&<ParsedStructure items={view.items}/>}
          <Space wrap><Button disabled={viewOffset===0} onClick={()=>setViewOffset(Math.max(0,viewOffset-100))}>上一页</Button><span>{view.total===0?"暂无内容":<>第 {viewOffset+1}–{Math.min(viewOffset+100,view.total)} 项，共 {view.total} 项</>}</span><Button disabled={viewOffset+100>=view.total} onClick={()=>setViewOffset(viewOffset+100)}>下一页</Button></Space>
        </>}
        {(Object.keys(textEdits).length>0||Object.keys(reviews).length>0||!!revision)&&<section className="parsing-card stack parsing-submit-area">
          <h2>检查并提交你的修改</h2><p>你已修改 {Object.keys(textEdits).length} 处文字，填写 {Object.keys(reviews).length} 条核对记录。页面先生成差异预览，再由你确认提交；未解决的问题仍按原规则处理。</p>
          {revisionFile&&<Alert showIcon type="warning" message="本次将使用你上传的完整修订文件" description={<span>页面上的文字编辑仍保留，但不会叠加到上传文件。<Button type="link" onClick={()=>{setRevisionFile(null);setRevisionRaw("");invalidateReview();}}>取消使用上传文件，改用页面编辑</Button></span>}/>}
          <Button type="primary" disabled={!importing||busy||(!revisionFile&&!Object.keys(textEdits).length&&!Object.keys(reviews).length)} loading={busy} onClick={()=>void previewRevision()}>检查本次修改与复核</Button>
          {revision&&<><RevisionSummary revision={revision}/><label>本次提交说明<Input.TextArea aria-label="本次提交说明" placeholder="说明你对照了哪些原文，以及修改或确认了什么。" value={decisionNote} onChange={event=>setDecisionNote(event.target.value)}/></label><Space wrap><Button disabled={!importing||!decisionNote.trim()} type="primary" loading={busy} onClick={()=>void decideRevision("MODIFY_ACCEPT")}>确认保存本次修订</Button><Button disabled={!importing||!decisionNote.trim()} onClick={()=>void decideRevision("REJECT")}>不采用本次修订</Button></Space></>}
        </section>}
        <></>
      </div>
    </Drawer>
    <Modal open={ocrOpen} onCancel={()=>setOcrOpen(false)} title="图片文字纠错建议" footer={null} width={760}><div className="stack"><Alert type="info" message="对照原图检查识字结果" description="此功能仅在导入模式使用。建议不会自动替换文字，必须由你核对；结果未知时不会自动重发。"/><Input aria-label="OCR 原文页码" type="number" min={1} value={cropPage} onChange={event=>setCropPage(event.target.value)}/><Button disabled={!importing||!field||!Number(cropPage)} loading={busy} onClick={()=>void queueOcr()}>发起本次建议</Button>{ocrTask&&<><Tag>任务 {ocrTask.slice(0,8)}</Tag><Button onClick={()=>void readOcr()}>读取建议结果</Button></>}{ocr&&<section className="parsing-card"><h3>图片文字纠错结果</h3><ReadableValue value={ocr.value??ocr}/><TechnicalDetails value={ocr}/></section>}<Input.TextArea aria-label="本人对OCR建议的决定说明" value={decisionNote} placeholder="本人核对说明" onChange={event=>setDecisionNote(event.target.value)}/><Input.TextArea aria-label="修改后完整字段文字" value={modified} placeholder="修改后采纳时填写完整字段文字" onChange={event=>setModified(event.target.value)}/><Space>{["ACCEPT","MODIFY_ACCEPT","REJECT"].map(value=><Button key={value} disabled={!ocr?.result||!decisionNote.trim()||(value==="MODIFY_ACCEPT"&&!modified)} onClick={()=>void decideOcr(value)}>{value==="ACCEPT"?"采纳":value==="MODIFY_ACCEPT"?"修改后采纳":"拒绝"}</Button>)}</Space></div></Modal>
    <Drawer open={!!taskDetail} onClose={()=>setTaskDetail(null)} title="任务进度与错误" width={760}><p>当前停留在：{taskStages[taskDetail?.stage]??"正在核对处理状态"} · {statuses[taskDetail?.status]??"状态待确认"}</p>{taskProblem(taskDetail)&&<Alert showIcon type="warning" message={taskProblem(taskDetail)}/>}<p>已归档原件和已完成的处理阶段仍保留。提交结果未知时先读取原状态，不重新发送。</p><Space wrap><Button disabled={busy} onClick={()=>void run(async()=>setTaskDetail(await api("/api/tasks/"+taskDetail?.task_id)))}>读取原任务状态</Button>{taskDetail?.status==="FAILED"&&failedDocument(taskDetail)&&<Button type="primary" disabled={!importing||viewStale||busy||pendingReparses.length>0} onClick={()=>void reprocessTask(taskDetail)}>重新处理已保存原文</Button>}</Space></Drawer>
    <Modal open={createKbOpen} title="新建知识库" onCancel={()=>setCreateKbOpen(false)} okText="创建" okButtonProps={{disabled:!name.trim()||viewStale}} onOk={()=>run(async()=>{if(state?.mode!=="IMPORT")await post("/api/mode",{mode:"IMPORT",kb_id:null},instance);const result=await post("/api/knowledge-bases",{name:name.trim()},instance);await refresh();setKb(result.kb_id);setCreateKbOpen(false);},true)}><Input aria-label="新知识库名称" value={name} onChange={event=>setName(event.target.value)}/></Modal>
    <Modal open={resumeOpen} title="原文件提交记录" onCancel={()=>setResumeOpen(false)} footer={null}><Alert type="info" message="只读取原请求键，不重新创建批次。旧实例或未找到的结果保持未知。" />{batchRecords.length?batchRecords.map(saved=><div className="surface" key={saved.key}><p>{new Date(saved.createdAt).toLocaleString()} · {saved.key.slice(0,8)} · {saved.instance===instance?"当前实例":"旧实例"}</p><Button disabled={busy||saved.instance!==instance} onClick={()=>void readBatch(saved)}>读取该原提交</Button></div>):<Empty description="本浏览器没有保存的文件批次"/>}</Modal>
    <LoadBoundary label="解析配置与原文">{parseDoc&&<ParsingConfig document={parseDoc} kb={kb} instance={instance} onClose={()=>setParseDoc(null)} onSubmitted={reload} onSource={()=>setSource({document_id:parseDoc.document_id})}/>}{source&&<SourceDrawer target={source} instance={instance} onClose={()=>setSource(null)}/>}</LoadBoundary>
  </section>;
}
