import {useEffect,useState} from "react";
import {Alert,Drawer,Spin,Tabs} from "antd";
import {api,describeError,type RecordValue} from "./api";
export default function DetailsDrawer({requestId,instance,onClose}:{requestId:string|null;instance:string;onClose:()=>void}){
  const [section,setSection]=useState("memory"),[value,setValue]=useState<RecordValue|null>(null),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  useEffect(()=>{if(!requestId)return;let active=true;setBusy(true);setValue(null);setError("");void api("/api/ui/question-requests/"+requestId+"/details?section="+section,{},instance).then(result=>{if(active)setValue(result);}).catch(err=>{if(active)setError(describeError(err));}).finally(()=>{if(active)setBusy(false);});return()=>{active=false;};},[requestId,instance,section]);
  return <Drawer open={!!requestId} width={760} onClose={onClose} title="本轮详情"><Tabs activeKey={section} onChange={setSection} items={[{key:"memory",label:"记忆"},{key:"retrieval",label:"检索"},{key:"budget",label:"预算与耗时"}]}/>{error&&<Alert type="error" message={error}/>}<Spin spinning={busy}>{value&&<><p className="muted">显示本轮持久记录。界面不会把草稿写入正式历史，也不会重发模型请求。</p><pre className="detail-pre">{JSON.stringify(value.value,null,2)}</pre>{value.omitted_paths.length>0&&<Alert type="info" message="当前视图有界，以下位置未完整展示" description={value.omitted_paths.join("、")}/>}</>}</Spin></Drawer>;
}
