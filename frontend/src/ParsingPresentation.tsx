import {useState} from "react";
import {Alert,Button,Empty,Input,Select,Space,Tag} from "antd";
import {asList,asObject,fieldName,formatNames,issueCopy,labels,locationSummary,readable,reviewChoices,type Data} from "./parsingReviewModel";
import "./parsingReview.css";

export function TechnicalDetails({value,title="技术详情（排查问题时使用）"}:{value:unknown;title?:string}){
  return null;
}
export function ReadableValue({value,depth=0}:{value:unknown;depth?:number}){
  if(value===null||typeof value!=="object")return <span className="parsing-value">{readable(value)}</span>;
  if(depth>=4)return <span className="muted">请查看归档原文。</span>;
  if(Array.isArray(value)){
    if(!value.length)return <span className="muted">无</span>;
    return <div className="parsing-items">{value.slice(0,20).map((v,i)=><div key={i} className="parsing-item"><span className="muted">第 {i+1} 项</span><ReadableValue value={v} depth={depth+1}/></div>)}{value.length>20&&<details><summary>另外 {value.length-20} 项</summary>{value.slice(20).map((v,i)=><div className="parsing-item" key={i}><ReadableValue value={v} depth={depth+1}/></div>)}</details>}</div>;
  }
  return <dl className="parsing-facts">{Object.entries(value as Data).filter(([key])=>!["$schema","self_ref","parent"].includes(key)).map(([key,v])=><div key={key}><dt>{labels[key]??key}</dt><dd><ReadableValue value={v} depth={depth+1}/></dd></div>)}</dl>;
}
const processingRules:Record<string,Array<[string,string]>>={
  md:[
    ["读取范围","读取整篇笔记正文，按标题、段落、列表、表格和代码块解析结构。开头的 YAML 属性单独保存，不混入正文段落。"],
    ["文字整理","统一换行符；普通正文合并连续空格、去掉行首尾多余空格。代码和公式不按普通正文压缩空格。"],
    ["图片","只识别随笔记一起归档的本地图片中的文字。图表表达的含义仍需人工核对；不会联网补下载图片。"],
    ["笔记链接","记录内链和笔记嵌入引用，不自动导入链接指向的其他笔记。"],
  ],
  pdf:[
    ["读取范围","逐页处理整份 PDF，并识别正文与表格结构，记录页码和可核对的位置。"],
    ["文字识别","有可用文字层时先读取原有文字，并按需识别图片区域；没有可用文字层时，对整页进行文字识别（OCR）。"],
    ["需要核对","文字识别和表格转换可能产生疑点。数字、表格及无法可靠定位的内容，在解析结果中按质量规则核对。"],
  ],
  image:[
    ["读取范围","处理图片的所有帧；按照图片中记录的方向校正，透明区域使用白色背景。"],
    ["文字识别","使用 OCR 识别图中文字，不额外增强图片。识别分数低于 0.8 或含数字的文字会标为待核对。"],
    ["识别边界","文字识别结果不等于已经理解图表含义；请对照原图检查。"],
  ],
  html:[
    ["读取范围","读取已归档 HTML 的正文结构，包括段落、列表和表格；本次不重新抓取网页。"],
    ["图片与资源","只处理归档时已登记的图片资源，不联网补下载图片或其他资源，不运行网页脚本。"],
    ["文字整理","统一换行符；普通正文合并连续空格、去掉行首尾多余空格。代码和公式不按普通正文压缩空格。"],
  ],
  docx:[
    ["读取范围","读取 Word 文档正文中的段落和表格，并记录能够核对的原文位置。"],
    ["文字整理","统一换行符；普通正文合并连续空格、去掉行首尾多余空格。受保护的代码等内容不按普通正文压缩空格。"],
    ["资源处理","转换时不自动读取外部链接或下载资源；实际提取范围和质量疑点在解析结果中查看。"],
  ],
  csv:[
    ["读取范围","读取全部记录，第一条记录作为表头；保留单元格原有字符串，不自动把文本转换成数字。"],
    ["文件格式","按 UTF-8 编码读取（兼容 BOM）；逗号分列、双引号包裹字段，按严格 CSV 规则处理。"],
    ["格式检查","空文件、各行列数不一致或超出处理限制时停止解析，不自动猜测补齐。"],
  ],
};
export function ParsingScopeSummary({kind,config,scope,scopeSource}:{kind:string;config:Data;scope:unknown;scopeSource:unknown}){
  const selected=kind==="json"||kind==="xlsx";
  return <div className="stack"><Alert showIcon type="info" message={selected?"请核对本次选中的字段和范围":"本次将怎样处理 "+(formatNames[kind]??kind)} description={selected?"以下范围来自当前原件和你刚才填写的设置。这里只是提交预览，尚未创建解析任务。":"以下是开始解析后会执行的步骤。当前尚未创建任务，也不代表已经完整提取；实际结果请在解析完成后核对。"} />
    {selected&&<div className="parsing-scope"><h3>本次处理范围</h3><ReadableValue value={scope}/><details><summary>查看选用的字段和参数</summary><ReadableValue value={config}/></details></div>}
    {!selected&&<dl className="parsing-facts">
      {(processingRules[kind]??[["读取范围","处理当前已归档文件；实际提取范围请在解析完成后核对。"]]).map(([label,text])=><div key={label}><dt>{label}</dt><dd>{text}</dd></div>)}

      <div><dt>原始资料</dt><dd>只读取已归档的这个版本，不修改原始文件。</dd></div>
      <div><dt>解析之后</dt><dd>先检查解析质量和来源定位；只有符合要求并已发布的资料才能用于问答。正常笔记由连续导入流程自动继续，有疑点时再处理。</dd></div>
    </dl>}
    <TechnicalDetails value={{config,scope,scope_source:scopeSource}} title="技术详情：原始配置与范围依据"/>
  </div>;
}
type ReviewProps={items:Data[];fields:Data[];reviews:Record<string,Data>;disabled:boolean;onReview:(id:string,change:Data)=>void;onSource:()=>void;onFields:()=>void;onOcr:(pointer:string)=>void;edits?:Record<string,string>;onEdit?:(pointer:string,value:string,original:string)=>void};
export function QualityIssues({items,fields,reviews,disabled,onReview,onSource,onFields,onOcr,edits={},onEdit}:ReviewProps){
  if(!items.length)return <Empty description="这一页没有需要处理的质量问题。是否可用于问答，以资料列表的发布状态为准。"/>;
  return <div className="stack">{items.map((item,i)=>{
    const copy=issueCopy(item),options=reviewChoices(item),source=fields.find(f=>f.field_pointer===item.field),decision=reviews[item.issue_id],resolved=Boolean(item.resolution);
    const original=typeof source?.cleaned_text==="string"?source.cleaned_text:undefined;
    const preview=original??(typeof source?.cleaned_text?.preview==="string"?source.cleaned_text.preview:"这段文字暂时无法完整显示，请打开归档原文核对。");
    return <section className="parsing-card" key={item.issue_id??i}>
      <div className="parsing-card-heading"><h3>{copy.title}</h3><Tag color={resolved?"green":item.severity==="ERROR"?"red":"gold"}>{resolved?"已记录核对结论":item.severity==="ERROR"?"需要处理":"待核对"}</Tag></div>
      <p>{copy.description}</p>
      {source?<><h4>需要核对的文字</h4><div className="parsing-source-text">{preview||"（空文字）"}</div><p className="muted">{locationSummary(source.locator)}</p></>:<p className="parsing-help">当前页没有提供对应文字。请打开归档原文核对；暂时无法定位的内容将保持待处理。</p>}
      {source&&original!==undefined&&onEdit&&options.some(o=>o.value==="CORRECTED")&&<label>识别有误时，在这里修改<Input.TextArea aria-label={"问题 "+String(i+1)+" 的修改文字"} autoSize={{minRows:2,maxRows:10}} disabled={disabled} value={edits[source.field_pointer]??original} onChange={event=>onEdit(source.field_pointer,event.target.value,original)}/></label>}
      <div className="parsing-next"><strong>下一步</strong><p>{resolved?"这项已记录人工结论；修改相关文字后，原结论仍会按既有规则重新检查。":copy.next}</p></div>
      <Space wrap><Button onClick={onSource}>打开归档原文</Button>{options.some(o=>o.value==="CORRECTED")&&typeof item.field==="string"&&<Button disabled={disabled} onClick={()=>onOcr(item.field)}>获取这段图片文字的纠错建议</Button>}</Space>
      {options.length>0&&item.issue_id&&<div className="parsing-decision"><label>你的核对结论（尚未提交）<Select aria-label={"问题 "+String(i+1)+" 的核对结论"} disabled={disabled} allowClear placeholder="核对原文后再选择" value={decision?.outcome} options={options} onChange={outcome=>onReview(item.issue_id,{outcome})}/></label>
        <label>核对依据<Input.TextArea aria-label={"问题 "+String(i+1)+" 的核对依据"} disabled={disabled} maxLength={2000} showCount rows={3} placeholder="写明你对照了哪一处，以及确认或修改的依据。没有查明时先不提交。" value={decision?.note??""} onChange={event=>onReview(item.issue_id,{note:event.target.value})}/></label>
        <p className="muted">选择结论不会立即保存。完成后在下方生成差异预览，再确认提交。</p></div>}
      {!resolved&&!options.length&&<Alert showIcon type="warning" message="该笔记暂时无法完成来源定位" description="系统需要修复定位或解析规则。你可以查看归档原文，这份笔记暂时不会用于问答。"/>}
      <TechnicalDetails value={item}/>
    </section>;
  })}</div>;
}
export function ParsedFields({items,edits,onEdit,onSource,disabled}:{items:Data[];edits:Record<string,string>;onEdit:(pointer:string,value:string,original:string)=>void;onSource:()=>void;onOcr:(pointer:string)=>void;disabled:boolean}){
  const [editing,setEditing]=useState<Record<string,boolean>>({});
  if(!items.length)return <Empty description="这一页没有可展示的文字。"/>;
  return <div className="stack">{items.map((item,i)=>{
    const pointer=typeof item.field_pointer==="string"?item.field_pointer:"",original=typeof item.cleaned_text==="string"?item.cleaned_text:"",changed=Object.prototype.hasOwnProperty.call(edits,pointer);
    return <section key={pointer||i} className="parsing-card">
      <div className="parsing-card-heading"><h3>{fieldName(pointer)}</h3>{changed&&<Tag color="gold">文字已修改 · 尚未提交</Tag>}</div>
      <p className="muted">{locationSummary(item.locator)}</p>
      {editing[pointer]?<label>修改后的完整文字<Input.TextArea aria-label={fieldName(pointer)+" 的文字"} rows={5} autoSize={{minRows:4,maxRows:16}} maxLength={100000} disabled={disabled} value={edits[pointer]??original} onChange={e=>onEdit(pointer,e.target.value,original)}/></label>:<div className="parsing-source-text">{(edits[pointer]??original)||"（空文字）"}</div>}
      <Space wrap><Button onClick={onSource}>对照归档原文</Button><Button disabled={disabled||!pointer} onClick={()=>setEditing(old=>({...old,[pointer]:!old[pointer]}))}>{editing[pointer]?"收起编辑":"编辑这段文字"}</Button>{changed&&<Button disabled={disabled} onClick={()=>onEdit(pointer,original,original)}>恢复原文字</Button>}</Space>
      {changed&&<p className="muted">只修改这段文字；其他文字和文档结构保持原样。提交前必须查看差异。</p>}
      {item.original_text!==item.cleaned_text&&<details><summary>查看清洗前的文字</summary><div className="parsing-source-text">{item.original_text}</div></details>}
      <TechnicalDetails value={{field_pointer:pointer,locator:item.locator}} title="技术详情：文字位置与来源记录"/>
    </section>;
  })}</div>;
}
const structuralNames:Record<string,string>={title:"标题",section_header:"小标题",text:"正文",paragraph:"段落",list_item:"列表项",table:"表格",picture:"图片",caption:"图表说明",page_header:"页眉",page_footer:"页脚",code:"代码",formula:"公式"};
export function ParsedStructure({items}:{items:Data[]}){
  const categories=items.filter(item=>["texts","groups","tables","pictures","body","furniture"].includes(item.key));
  return <div className="stack"><Alert showIcon type="info" message="按结构类别查看解析结果" description="下方展示真实解析内容。标题、列表、表格等保留其类别；完整节点关系和未展开内容可在技术详情查看。"/>
    {categories.map(item=><section className="parsing-card" key={item.key}><h3>{labels[item.key]??item.key}</h3>
      {Array.isArray(item.value)?item.value.slice(0,30).map((node:any,i:number)=>{const n=asObject(node);return <div className="parsing-structure-row" key={i}><Tag>{structuralNames[n?.label]??n?.label??"结构项"} {i+1}</Tag>{typeof n?.text==="string"?<span className="parsing-source-text">{n.text}</span>:<span className="muted">{typeof n?.name==="string"?n.name:"详细结构已记录，请展开技术详情查看。"}</span>}</div>}):<ReadableValue value={item.value}/>}
      {Array.isArray(item.value)&&item.value.length>30&&<p className="muted">这里只展开前 30 项，完整内容保留在技术详情。</p>}<TechnicalDetails value={item.value}/></section>)}
    <TechnicalDetails value={items} title="高级：完整的本页解析结构数据"/>
  </div>;
}
export function RevisionSummary({revision}:{revision:Data}){
  const differences=asList(revision.differences),reviews=asList(revision.reviews);
  return <section className="parsing-card stack"><h3>提交前检查本次差异</h3><Alert showIcon type="info" message={"修改 "+differences.length+" 处文字，记录 "+reviews.length+" 条核对结论"} description="这里只生成了预览，还没有采用修订。未解决的问题不会自动变成通过。"/>
    {differences.map((item,i)=><div className="parsing-diff" key={i}><h4>{fieldName(item.pointer)}</h4><div className="parsing-diff-columns"><div><strong>修改前</strong><div className="parsing-source-text">{item.before||"（空）"}</div></div><div><strong>修改后</strong><div className="parsing-source-text">{item.after||"（空）"}</div></div></div></div>)}
    {!differences.length&&reviews.length>0&&<p>文字保持不变，本次只记录你明确填写的核对结论。</p>}
    {reviews.map((review,i)=><div className="parsing-review-summary" key={i}><strong>核对结论 {i+1}：{readable(review.outcome)}</strong><p>{review.note}</p></div>)}
    <TechnicalDetails value={revision} title="技术详情：修订预览与版本绑定"/>
  </section>;
}
