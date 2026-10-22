import type {RecordValue} from "./api";
export const FILE_BYTES=50*1024*1024, BATCH_BYTES=500*1024*1024, BATCH_FILES=50;

/** Each entry stays with its complete image closure; no path occurs twice. */
export function splitObsidianFiles(files:RecordValue[],weights:Record<string,number>):RecordValue[][] {
  if(!files.length)throw new Error("请选择资料。");
  const paths=new Set<string>(),batches:RecordValue[][]=[];let batch:RecordValue[]=[],bytes=0;
  for(const file of files){
    const path=file.relative_path,weight=weights[path]??file.size_bytes;
    if(typeof path!=="string"||paths.has(path))throw new Error("选择清单含有重复或无效路径。");
    if(!Number.isSafeInteger(file.size_bytes)||file.size_bytes<0||file.size_bytes>FILE_BYTES)throw new Error("单文件超过50 MiB或大小无效："+path);
    if(!Number.isSafeInteger(weight)||weight<file.size_bytes||weight>BATCH_BYTES)throw new Error("资料连同图片超过500 MiB或预算无效："+path);
    if(batch.length===BATCH_FILES||bytes+weight>BATCH_BYTES){batches.push(batch);batch=[];bytes=0;}
    batch.push(file);bytes+=weight;paths.add(path);
  }
  if(batch.length)batches.push(batch);
  return batches;
}
