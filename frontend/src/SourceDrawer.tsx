import {lazy,useEffect,useRef,useState} from "react";
import {Alert,Button,Drawer,InputNumber,Pagination,Select,Space,Spin,Table,Tag} from "antd";
import {ApiError,api,describeError,obsidianFailure,sourceAccessDenied,type RecordValue} from "./api";
import LoadBoundary from "./LoadBoundary";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import ErrorNotice from "./ErrorNotice";
const PdfPage=lazy(()=>import("./PdfPage"));
type Target={document_id:string; raw_version_id?:string; request_id?:string; support_unit_id?:string};
export default function SourceDrawer({target,instance,onClose}:{target:Target|null;instance:string;onClose:()=>void}){
  const returnFocus=useRef<HTMLElement|null>(document.activeElement instanceof HTMLElement?document.activeElement:null);
  const sourceContent=useRef<HTMLDivElement|null>(null);
  useEffect(()=>()=>{const element=returnFocus.current;if(!element||element===document.body)return;window.requestAnimationFrame(()=>{if(element.isConnected)element.focus({preventScroll:true});});},[]);
  const scope=JSON.stringify([instance,target?.document_id??"",target?.raw_version_id??"",target?.request_id??"",target?.support_unit_id??""]);
  const [view,setView]=useState<{scope:string;source:RecordValue;reference:RecordValue|null}|null>(null);
  const [failure,setFailure]=useState<{scope:string;text:Error|string}>({scope:"",text:""}),[offset,setOffset]=useState(0),[sheet,setSheet]=useState(0),[page,setPage]=useState(1),[frame,setFrame]=useState(0),[busy,setBusy]=useState(false),[locatorIndex,setLocatorIndex]=useState(0),[retry,setRetry]=useState(0);
  const [native,setNative]=useState<{scope:string;raw:string;value:RecordValue}|null>(null),[nativeFailure,setNativeFailure]=useState<{scope:string;text:Error|string}>({scope:"",text:""}),[nativeNotice,setNativeNotice]=useState({scope:"",text:""}),[nativeBusy,setNativeBusy]=useState(false),[nativeRetry,setNativeRetry]=useState(0);
  const source=view?.scope===scope?view.source:null,reference=view?.scope===scope?view.reference:null,error=failure.scope===scope?failure.text:"";
  const nativeTarget=native?.scope===scope&&native.raw===source?.raw_version_id?native.value:null;
  const nativeError=nativeFailure.scope===scope?nativeFailure.text:"",notice=nativeNotice.scope===scope?nativeNotice.text:"";
  const current=useRef({scope,source,error,busy}),nativeSerial=useRef(0),nativeAbort=useRef<AbortController|null>(null),nativeFlight=useRef(""),locatorDefaultScope=useRef("");
  current.current={scope,source,error,busy};
  useEffect(()=>{setOffset(0);setSheet(0);setPage(1);setFrame(0);setLocatorIndex(0);locatorDefaultScope.current="";},[scope]);
  useEffect(()=>{
    const selected=target;
    if(!selected||!instance){setBusy(false);return;}
    let active=true;const controller=new AbortController();
    setBusy(true);setFailure({scope,text:""});
    void (async()=>{
      let ref:RecordValue|null=null;
      if(Boolean(selected.request_id)!==Boolean(selected.support_unit_id))throw new Error("引用身份不完整，已暂停原文读取。");
      if(selected.request_id&&selected.support_unit_id){
        ref=await api<RecordValue>("/api/ui/question-requests/"+selected.request_id+"/references/"+encodeURIComponent(selected.support_unit_id),{signal:controller.signal},instance);
        if(ref.reference?.document_id!==selected.document_id||!Array.isArray(ref.locators))throw new Error("引用与资料身份不一致，已停止读取。");
      }
      const raw=ref?.reference.raw_version_id??selected.raw_version_id;
      const value=await api("/api/ui/documents/"+selected.document_id+"/source?offset="+offset+"&sheet="+sheet+"&frame="+frame+(raw?"&raw_version_id="+encodeURIComponent(raw):""),{signal:controller.signal},instance);
      if(value.document_id!==selected.document_id||typeof value.raw_version_id!=="string"||(raw&&value.raw_version_id!==raw)||!Array.isArray(value.rows))throw new Error("原文响应与本次版本不一致，已暂停读取。");
      if(active){
        if(locatorDefaultScope.current!==scope){
          setLocatorIndex(Math.max(0,ref?.locators.findIndex((item:RecordValue)=>item.support_role==="BODY")??-1));
          locatorDefaultScope.current=scope;
        }
        setView({scope,source:value,reference:ref});setFailure({scope,text:""});
      }
    })().catch(err=>{
      if(!active||(err instanceof DOMException&&err.name==="AbortError"))return;
      setFailure({scope,text:err instanceof Error?err:new Error(describeError(err))});
      if(err instanceof ApiError&&err.status>=400&&err.status<500&&![408,429].includes(err.status)){setView(null);setNative(null);}
    }).finally(()=>{if(active)setBusy(false);});
    return()=>{active=false;controller.abort();};
  },[scope,instance,offset,sheet,frame,retry]);

  async function checkNative(open=false){
    const ownScope=scope,selected=target,raw=source?.raw_version_id;
    if(!selected||!source||!raw||error||busy)return;
    const key=ownScope+"|"+raw;
    if(open&&nativeFlight.current===key)return;
    nativeFlight.current=key;nativeAbort.current?.abort();
    const controller=new AbortController(),serial=++nativeSerial.current;nativeAbort.current=controller;
    setNativeBusy(true);setNativeFailure({scope:ownScope,text:""});setNativeNotice({scope:ownScope,text:""});
    const stillCurrent=()=>serial===nativeSerial.current&&current.current.scope===ownScope&&current.current.source?.raw_version_id===raw&&!current.current.error&&!current.current.busy;
    try{
      const value=await api("/api/ui/documents/"+selected.document_id+"/obsidian-target?raw_version_id="+encodeURIComponent(raw),{signal:controller.signal},instance);
      if(typeof value.available!=="boolean")throw new Error("本地笔记入口响应不完整，请重新核对。");
      if(!stillCurrent())return;
      setNative({scope:ownScope,raw,value});
      if(!open)return;
      if(!value.available)throw new Error(value.reason??"本地笔记暂不可打开；归档资料仍保留。");
      if(typeof value.uri!=="string")throw new Error("本地打开目标缺失，已停止跳转。");
      const uri=new URL(value.uri);
      if(uri.protocol!=="obsidian:"||uri.hostname!=="open"||uri.searchParams.size!==1||!uri.searchParams.has("path"))throw new Error("本地打开目标不符合只读笔记入口，已停止跳转。");
      window.location.assign(uri.href);
      setNativeNotice({scope:ownScope,text:"已请求在 Obsidian 打开当前笔记。若未打开，请确认 Obsidian 已安装。"});
    }catch(err){
      if(serial!==nativeSerial.current||current.current.scope!==ownScope||(err instanceof DOMException&&err.name==="AbortError"))return;
      setNativeFailure({scope:ownScope,text:obsidianFailure(err)});
      setNative(null);
      if(sourceAccessDenied(err))setView(null);
    }finally{
      if(serial===nativeSerial.current){nativeFlight.current="";setNativeBusy(false);}
    }
  }
  useEffect(()=>{
    if(source?.kind==="md"&&!busy&&!error)void checkNative();
    return()=>{nativeAbort.current?.abort();};
  },[scope,source?.raw_version_id,busy,error,nativeRetry]);
  const locator=reference?.locators?.[locatorIndex]?.locator??null;
  useEffect(()=>{if(locator){const value=locator.page_no??locator.page; if(typeof value==="number")setPage(value);if(typeof locator.line_start==="number")setOffset(Math.floor((locator.line_start-1)/100)*100);if(typeof locator.record==="number")setOffset(Math.floor((locator.record-1)/100)*100);}},[locator]);
  const rows=source?.rows??[],kind=source?.kind,displayOffset=source?.offset??offset;

  const wordBindings=locator?.kind==="docx"?(Array.isArray(locator.source_group)&&locator.source_group.length?locator.source_group:[{part:"word/document.xml",xml_path:locator.xml_path}]):[];
  const rowHighlighted=(row:any,index:number)=>Boolean(
    (typeof locator?.line_start==="number"&&locator.line_start<=displayOffset+index+1&&locator.line_end>=displayOffset+index+1)
    ||(typeof row?.xpath==="string"&&typeof locator?.xpath==="string"
       &&(row.xpath===locator.xpath||locator.xpath.startsWith(row.xpath+"/")))
    ||(typeof row?.part==="string"&&typeof row?.xml_path==="string"&&wordBindings.some((item:RecordValue)=>
      item.part===row.part&&typeof item.xml_path==="string"&&item.xml_path.length>1&&
      (row.xml_path===item.xml_path||row.xml_path.startsWith(item.xml_path+"/")||item.xml_path.startsWith(row.xml_path+"/")))));
  useEffect(()=>{
    if(!reference||busy||error)return;
    const id=window.requestAnimationFrame(()=>sourceContent.current?.querySelector<HTMLElement>(".source-highlight")?.scrollIntoView({block:"center"}));
    return()=>window.cancelAnimationFrame(id);
  },[source,reference,locator,busy,error]);
  const savedImages=source&&source.images?.length>0&&<section className="stack" aria-label="随原文保存的图片"><h3>随原文保存的图片</h3>{source.images.map((image:RecordValue)=><figure className="parsing-card" key={image.preview_url}><figcaption>{image.label}</figcaption><img src={image.preview_url} alt={image.label} style={{maxWidth:"100%",height:"auto",display:"block"}}/><Button href={image.preview_url} target="_blank" rel="noreferrer">查看大图</Button></figure>)}</section>;
  const table=kind==="csv"||kind==="xlsx";
  const cols=table?Array.from({length:Math.max(0,...rows.map((r:unknown[])=>r.length))},(_,i)=>({title:String(i+1),dataIndex:String(i),key:String(i),render:(value:unknown,row:RecordValue)=>{const meta=source?.cell_metadata?.[Number(row.key)]?.[i];return <span title={meta?meta.coordinate:undefined}>{String(value??"")}{meta?.display_status==="DISPLAY_UNRESOLVED"&&<Tag color="warning">格式未解析</Tag>}{meta?.display_status==="FORMULA_TEXT_ONLY"&&<Tag>公式 · 未核验</Tag>}{meta?.display_status==="FORMULA_EFFECT_UNVERIFIED"&&<Tag color="warning">公式影响 · 未核验</Tag>}</span>;}})):[];
  const header=<Space wrap>{source?.display_name??"原文来源"}{locator&&<Tag>{locator.granularity==="EXACT"?"已定位引用位置":locator.granularity==="REGION"?"已定位到原文范围":"查看归档原文"}</Tag>}</Space>;
  return <Drawer open={!!target} onClose={onClose} title={header} width={900}>
    {error&&<ErrorNotice error={error} stage="读取归档原文" retained={source?"保留上次成功原文，当前显示可能已过期；原文操作暂时停用。":"资料记录和历史回答仍保留，当前原文不可访问。"} next="请重新读取原状态；若资料已移除，原文入口保持关闭。" onRecover={()=>setRetry(value=>value+1)} recoverLabel="重新读取原文"/>}
    {nativeError&&<ErrorNotice error={nativeError} stage="核对本地笔记入口" retained="归档资料与历史回答仍保留；当前本地入口尚未确认。" next={nativeError instanceof ApiError&&nativeError.code==="OBSIDIAN_ENDPOINT_UNAVAILABLE"?"本机后端尚未更新此功能，请更新并重启服务后再试；无需重新导入笔记。":"请检查笔记是否仍在原位置。下方归档原文可以继续查看。"} onRecover={()=>setNativeRetry(value=>value+1)} recoverLabel="重新核对入口"/>}
    {nativeTarget?.current_note_differs_from_archive&&<Alert type="warning" showIcon message="当前本地笔记与引用的归档版本不同" description="回答引用仍以归档原文为准；跳转会打开现在的笔记。"/>}
    {notice&&<p role="status" className="muted">{notice}</p>}
    <Spin spinning={busy}><div ref={sourceContent} className="stack">
      {source?.source_notice&&<Alert showIcon type="warning" message="当前查看的是文字副本" description={source.source_notice}/>}
      {source&&<Space wrap>{nativeTarget?.available&&<Button loading={nativeBusy} disabled={busy||!!error||nativeBusy} onClick={()=>void checkNative(true)}>在 Obsidian 打开当前笔记</Button>}{error&&<Tag color="warning">上次成功视图</Tag>}</Space>}
      {reference&&<><Alert type="info" showIcon message="请核对下方高亮原文是否支持这段回答。回答依据是导入时保存的归档版本。"/>{reference.locators.length>1&&<Select value={locatorIndex} onChange={setLocatorIndex} options={reference.locators.map((value:RecordValue,i:number)=>({value:i,label:"引用位置 "+(i+1)}))} aria-label="来源位置"/>}<></></>}
      {!reference&&source&&<></>}
      {!reference&&savedImages}
      {kind==="pdf"&&source&&<><Space><span>页码</span><InputNumber min={1} value={page} onChange={value=>setPage(value??1)}/></Space><LoadBoundary label="PDF 原文"><PdfPage url={source.content_url} page={page} locator={locator}/></LoadBoundary><span className="muted">高亮区域用于帮助对照回答依据。</span></>}
      {["png","jpg","jpeg","gif","webp","bmp","tif","tiff"].includes(kind)&&source&&<>{source.frame_count>1&&<Space><span>图片帧（从0开始）</span><InputNumber min={0} max={source.frame_count-1} value={frame} onChange={value=>setFrame(value??0)}/></Space>}<img src={source.preview_url??source.content_url} alt={source.display_name} style={{maxWidth:"100%"}}/><p className="muted">已展示归档图片的预览。</p></>}
      {table&&source&&<>{source.sheets?.length>1&&<Select aria-label="工作表" value={sheet} options={source.sheets.map((name:string,i:number)=>({value:i,label:name}))} onChange={value=>{setSheet(value);setOffset(0);}}/>}<Table rowKey="key" columns={[{title:"行",dataIndex:"row",fixed:"left",width:60},...cols]} dataSource={rows.map((row:unknown[],i:number)=>({key:i,row:displayOffset+i+1,...Object.fromEntries(row.map((value,j)=>[String(j),value]))}))} pagination={false} size="small" scroll={{x:"max-content"}}/>{kind==="xlsx"&&source.formula_policy&&<Alert message={source.formula_policy} type="info"/>}</>}
      {kind==="md"&&source&&<div className="source-document"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{a:({children})=><span>{children}</span>,img:({alt})=><span className="muted">〔图片：{alt||"见下方归档图片"}〕</span>}}>{rows.filter((row:unknown)=>typeof row==="string").join("\n").replace(displayOffset===0?/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/:/^\b\B/,"")}</ReactMarkdown></div>}
      {!table&&kind!=="pdf"&&kind!=="md"&&rows.map((row:any,i:number)=><div key={displayOffset+i} className={"source-row "+(rowHighlighted(row,i)?"source-highlight":"")}><span className="source-number">{displayOffset+i+1}</span><div className="source-text">{kind==="docx"&&typeof row?.section==="string"&&<span className="muted">{row.section} · </span>}{typeof row==="string"?row:kind==="json"?row.pointer.split("/").filter(Boolean).join(" › ")+"："+(typeof row.value==="object"?"请展开对应内容查看":String(row.value??"空值")):String(row.text??"")}</div></div>)}
      {reference&&savedImages}
      {typeof source?.total==="number"&&<Pagination current={Math.floor(displayOffset/100)+1} pageSize={100} total={source.total} showSizeChanger={false} onChange={value=>setOffset((value-1)*100)}/>}
    </div></Spin>
  </Drawer>;
}
