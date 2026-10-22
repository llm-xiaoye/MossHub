export type Data = Record<string, any>;
export const asObject = (value: unknown): Data | null => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Data : null;
export const asList = (value: unknown): any[] => Array.isArray(value) ? value : [];
export const formatNames: Record<string,string> = {txt:"纯文本文件",md:"Markdown 笔记",markdown:"Markdown 笔记",pdf:"PDF 文档",docx:"Word 文档",xlsx:"Excel 工作簿",json:"JSON 数据",csv:"CSV 表格",html:"网页",htm:"网页",png:"图片",jpg:"图片",jpeg:"图片"};
export const labels: Record<string,string> = {
  selected:"处理方式",format:"文件格式",excluded:"未处理内容",included:"处理内容",regions:"选定区域",
  sheet:"工作表",sheets:"工作表",range:"单元格范围",header_rows:"表头行数",title:"标题",unit:"单位",footnotes:"脚注",
  json:"JSON 数据配置",xlsx:"Excel 配置",records_pointer:"记录所在位置",title_pointer:"标题所在位置",text_pointers:"正文所在位置",
  tables:"表格",columns:"列",pointer:"数据位置",name:"名称",value:"内容",count:"数量",rows:"行",
  record_count:"记录数",selected_count:"选中数量",omitted_count:"未处理数量",hidden:"隐藏",state:"状态",
  before:"修改前",after:"修改后",field:"文字位置",outcome:"核对结论",note:"说明",reason:"原因",
  characters:"字符数",texts:"文字",groups:"分组",pictures:"图片",body:"正文结构",furniture:"页眉页脚结构",
  page:"页码",pages:"页",line_start:"起始行",line_end:"结束行",start_line:"起始行",end_line:"结束行",
  row:"行",column:"列",row_index:"行序号",column_index:"列序号",granularity:"定位范围",precision:"定位精度",
  selected_sheets:"选中工作表",excluded_sheets:"未选工作表",excluded_fields:"未选字段",
};
const values: Record<string,string> = {
  EXISTING_FORMAT_DEFAULT:"使用当前格式的默认规则",EXISTING_FORMAT_DEFAULT_CONTRACT:"依据当前格式的默认规则",
  ORIGINAL_INPUT_AND_EXISTING_FORMAT_ADAPTER:"依据当前原件和本次选择",FILE_COARSE:"文件范围，尚未精确定位",
  COARSE:"尚未精确定位",REGION:"原文区域",EXACT:"精确位置",VERIFIED_CORRECT:"对照后确认正确",
  CORRECTED:"已修正对应文字",RESOURCE_EXEMPT:"确认该图片资源无需提取",visible:"可见",hidden:"隐藏",
};
export function readable(value: unknown): string {
  if (value === null || value === undefined) return "未设置";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string") return values[value] ?? formatNames[value] ?? (value === "" ? "根位置 / 空值" : value);
  return String(value);
}
export function fieldName(pointer: unknown): string {
  if (typeof pointer !== "string") return "相关内容";
  const text = /^\/texts\/(\d+)\/text$/.exec(pointer);
  if (text) return "文字片段 " + (Number(text[1]) + 1);
  const cell = /^\/tables\/(\d+)\/data\/table_cells\/(\d+)\/text$/.exec(pointer);
  if (cell) return "表格 " + (Number(cell[1]) + 1) + " · 单元格 " + (Number(cell[2]) + 1);
  return "相关文字";
}
export function reviewChoices(issue: Data): {value:string;label:string}[] {
  if (issue.resolution) return [];
  if (["OCR_NUMERIC_REVIEW","OCR_REVIEW_CANDIDATE"].includes(issue.code)) return [
    {value:"VERIFIED_CORRECT",label:"对照原文后，确认现有文字正确"},
    {value:"CORRECTED",label:"已在对应文字中修正错误"},
  ];
  if (issue.code === "BITMAP_STRUCTURE_REVIEW_REQUIRED") return [
    {value:"RESOURCE_EXEMPT",label:"确认这张图片无需提取内容，并说明依据"},
  ];
  return [];
}
export function issueCopy(issue: Data): {title:string;description:string;next:string} {
  if (issue.code === "WEB_GRAPHICS_NOT_PROCESSED") return {
    title:"网页中还有未处理的图形内容",
    description:"文字已保存，但系统还不能确认网页里的图形、图表或图片内容是否完整。该资料暂不能用于问答。",
    next:"这是网页采集需要完善的问题，不能直接确认通过。你可以查看归档原文，或将完整文章保存为 PDF、Markdown 后导入。",
  };
  if (issue.code === "COARSE_SOURCE_MAPPING") return {
    title:"这段文字的原文位置还不能确定",
    description:asList(issue.candidates).length > 1 ? "系统找到了多个可能位置，暂时没有自动选择；文字相似不能证明它来自哪一处。" : "系统还没有为这段文字找到唯一、可靠的原文位置。",
    next:"这是系统的来源定位问题，需要维护人员修复解析或定位规则。你可以查看归档原文；该笔记暂不发布，不能通过“确认正确”放行，也不需要理解内部编号。",
  };
  if (["OCR_NUMERIC_REVIEW","OCR_REVIEW_CANDIDATE"].includes(issue.code)) return {
    title:issue.code === "OCR_NUMERIC_REVIEW" ? "这处数字需要对照原图核实" : "这段图片识别文字需要核对",
    description:"图片识别可能出现漏字、错字或数字错误。请以归档原图为准。",
    next:"确认文字正确，或在“解析出的文字”中修改对应内容，再填写核对依据并生成预览。",
  };
  if (issue.code === "BITMAP_STRUCTURE_REVIEW_REQUIRED") return {
    title:"这张图片的内容还需要检查",description:"系统尚不能确认图片中的重要文字或图表已被完整处理。",
    next:"打开原图核对；只有确认无需提取时才选择资源豁免并说明依据。重要内容未处理时继续保留待复核。",
  };
  return {title:issue.severity === "ERROR" ? "这项解析问题需要处理" : "这项解析结果需要核对",
    description:typeof issue.message === "string" ? issue.message : typeof issue.reason === "string" ? issue.reason : "系统发现了一项需要核实的内容，尚不能直接确认通过。",
    next:"先查看相关文字并对照原文。可用的处理方式会在下方列出；未提供确认选项的问题不能只靠勾选解除。"};
}
export function locationSummary(value: unknown): string {
  const locator = asObject(value);
  if (!locator) return "尚未提供可展示的原文位置";
  if (locator.granularity === "COARSE" || locator.precision === "FILE_COARSE") return "只定位到文件，具体位置仍需核对";
  const parts: string[] = [];
  if (typeof locator.page === "number") parts.push("第 " + locator.page + " 页");
  if (typeof locator.page_no === "number") parts.push("第 " + locator.page_no + " 页");
  if (typeof locator.sheet === "string") parts.push("工作表 " + locator.sheet);
  const first = locator.line_start ?? locator.start_line;
  const last = locator.line_end ?? locator.end_line;
  if (typeof first === "number") parts.push("第 " + first + (typeof last === "number" && last !== first ? "–" + last : "") + " 行");
  if (typeof locator.cell === "string") parts.push("单元格 " + locator.cell);
  return parts.join(" · ") || "来源位置已记录，可以对照归档原文查看";
}
export function applyTextEdits(native: Data, edits: Record<string,string>): Data {
  const copy: Data = JSON.parse(JSON.stringify(native));
  for (const [pointer,text] of Object.entries(edits)) {
    if (typeof text !== "string" || text.length > 100000 || (!pointer.startsWith("/") || /~(?:[^01]|$)/.test(pointer))) throw new Error("文字修改格式无效。");
    const path = pointer.slice(1).split("/").map(part => part.replace(/~1/g,"/").replace(/~0/g,"~"));
    if (path.at(-1) !== "text" || path.some(part => ["__proto__","prototype","constructor"].includes(part))) throw new Error("只允许修改已有的文字字段。");
    let current: any = copy;
    for (const part of path.slice(0,-1)) {
      if (current === null || typeof current !== "object" || !Object.prototype.hasOwnProperty.call(current,part)) throw new Error("原文字位置已不存在，请重新读取解析结果。");
      current = current[part];
    }
    const leaf = path[path.length-1];
    if (current === null || typeof current !== "object" || !Object.prototype.hasOwnProperty.call(current,leaf) || typeof current[leaf] !== "string") throw new Error("原位置不是可编辑的文字。");
    current[leaf] = text;
  }
  return copy;
}

/** Preserve untouched JSON tokens, including integers larger than Number.MAX_SAFE_INTEGER. */
export function revisionRequestBody(documentId:string,versionId:string,nativeRaw:string,edits:Record<string,string>,reviews:Data[]):string {
  const parsed=JSON.parse(nativeRaw);
  if(!asObject(parsed))throw new Error("完整解析文件必须是一个对象。");
  applyTextEdits(parsed,edits); // Validate edit targets, but never serialize this parsed copy.
  const ranges:{start:number;end:number;pointer:string}[]=[];
  let at=0;
  const ws=()=>{while(/\s/.test(nativeRaw[at]??"")&&at<nativeRaw.length)at++;};
  const stringToken=()=>{
    const start=at;at++;
    while(at<nativeRaw.length){const char=nativeRaw[at++];if(char==="\\"){at++;continue;}if(char==='"')break;}
    return {start,end:at,value:JSON.parse(nativeRaw.slice(start,at)) as string};
  };
  const encode=(path:string[])=>"/"+path.map(part=>part.replace(/~/g,"~0").replace(/\//g,"~1")).join("/");
  const visit=(path:string[])=>{
    ws();const char=nativeRaw[at];
    if(char==='"'){const token=stringToken(),pointer=encode(path);if(Object.prototype.hasOwnProperty.call(edits,pointer))ranges.push({start:token.start,end:token.end,pointer});return;}
    if(char==="{"){
      at++;ws();const keys=new Set<string>();if(nativeRaw[at]==="}"){at++;return;}
      while(at<nativeRaw.length){ws();const key=stringToken().value;if(keys.has(key))throw new Error("解析文件含有重复字段，无法安全准备修订。");keys.add(key);ws();at++;visit([...path,key]);ws();if(nativeRaw[at]==="}"){at++;return;}at++;}
    }else if(char==="["){
      at++;ws();let index=0;if(nativeRaw[at]==="]"){at++;return;}
      while(at<nativeRaw.length){visit([...path,String(index++)]);ws();if(nativeRaw[at]==="]"){at++;return;}at++;}
    }else{while(at<nativeRaw.length&&!/[\s,}\]]/.test(nativeRaw[at]))at++;}
  };
  visit([]);
  if(ranges.length!==Object.keys(edits).length)throw new Error("未能唯一定位所有修改文字，请重新读取解析文件。");
  let output=nativeRaw;
  for(const item of ranges.sort((a,b)=>b.start-a.start))output=output.slice(0,item.start)+JSON.stringify(edits[item.pointer])+output.slice(item.end);
  return '{"document_id":'+JSON.stringify(documentId)+',"base_processing_version_id":'+JSON.stringify(versionId)+',"full_native_json":'+output+',"reviews":'+JSON.stringify(reviews)+'}';
}
