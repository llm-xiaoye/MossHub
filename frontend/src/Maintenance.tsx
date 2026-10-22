import {useState} from "react";
import {Alert,App,Button,Collapse,Input,Select,Space,Table} from "antd";
import {api,describeError,type RecordValue} from "./api";
function quote(value:string){return "\u0027"+value.replace(/\u0027/g,"\u0027\u0022\u0027\u0022\u0027")+"\u0027";}
function command(home:string,python:string,module:string,args:string[]=[]){
  const argv=["/usr/bin/env","RAG_HOME="+home,"PYTHONDONTWRITEBYTECODE=1","PYTHONPATH="+home+"/project/backend",python,"-B","-m",module,...args];
  if(argv.some(value=>!value||/[\u0000\r\n]/.test(value)))return "";
  return argv.map(quote).join(" ");
}
export default function Maintenance({instance}:{instance:string}){
  const {message}=App.useApp();
  const [value,setValue]=useState<RecordValue|null>(null),[error,setError]=useState(""),[log,setLog]=useState<RecordValue|null>(null);
  const [backup,setBackup]=useState(""),[restore,setRestore]=useState(""),[task,setTask]=useState(""),[attempt,setAttempt]=useState("");
  const refresh=()=>void api("/api/ui/maintenance",{},instance).then(result=>{setValue(result);setError("");}).catch(err=>setError(describeError(err)));
  async function copy(text:string){try{if(!text)throw new Error("路径或参数无效。");await navigator.clipboard.writeText(text);void message.success("命令已复制，请在本机终端执行。");}catch(err){setError(describeError(err));}}
  const manager=(action:string)=>value?command(value.home,value.python,"raghub.manager",[action]):"";
  const backupCommand=value?command(value.home,value.python,"raghub.maintenance",["backup"]):"";
  const restoreCommand=value&&backup.startsWith("/")&&restore.startsWith("/")?command(value.home,value.python,"raghub.maintenance",["restore","--backup",backup,"--target",restore]):"";
  const verifyCommand=restore.startsWith("/")?command(restore,restore+"/runtime/python/bin/python","raghub.maintenance",["verify"]):"";
  const resumeCommand=restore.startsWith("/")&&task&&attempt?command(restore,restore+"/runtime/python/bin/python","raghub.maintenance",["resume","--task",task,"--expected-attempt",attempt]):"";
  return <div className="maintenance"><Collapse onChange={keys=>{if(keys.length&&!value)refresh();}} items={[{key:"maintenance",label:"维护 · 日志、启停与备份恢复",children:<div className="stack">
    {error&&<Alert type="error" message={error}/>}<Button onClick={refresh}>读取服务状态</Button>
    {value&&<><Table rowKey="name" size="small" dataSource={value.services} columns={[{title:"服务",dataIndex:"name"},{title:"PID",dataIndex:"pid"},{title:"实际进程",dataIndex:"running",render:(running:boolean)=>running?"运行中":"未运行"}]} pagination={false}/>
    <Space wrap><Select aria-label="日志文件" placeholder="按需读取日志" options={value.logs.map((name:string)=>({value:name,label:name}))} style={{minWidth:220}} onChange={name=>void api("/api/ui/logs/"+encodeURIComponent(name),{},instance).then(setLog).catch(err=>setError(describeError(err)))}/><Button onClick={()=>void copy(manager("start"))}>复制统一启动命令</Button><Button onClick={()=>void copy(manager("stop"))}>复制统一停止命令</Button></Space>
    {log&&<><p className="muted">{log.view}</p><pre className="detail-pre">{log.lines.join("\n")}</pre></>}
    <Alert type="info" message="停止 API 后页面连接会关闭；启动时使用同一安装内 Python。" description={<><code>{manager("start")}</code><br/><code>{manager("stop")}</code></>}/>
    <h2>一致备份与独立恢复</h2><p className="muted">先统一停止服务。备份保存 SQLite、原件、产物与 Milvus 数据；恢复写入全新目录，保留原根并隔离旧任务。收费请求结果未知时不重放。</p>
    <Button onClick={()=>void copy(backupCommand)}>复制备份命令</Button><code>{backupCommand}</code>
    <Input aria-label="备份目录" placeholder="备份返回的实际目录绝对路径" value={backup} onChange={event=>setBackup(event.target.value)}/>
    <Input aria-label="独立恢复目录" placeholder="全新且不存在的恢复目录绝对路径" value={restore} onChange={event=>setRestore(event.target.value)}/>
    <Space wrap><Button disabled={!restoreCommand} onClick={()=>void copy(restoreCommand)}>复制独立恢复命令</Button><Button disabled={!verifyCommand} onClick={()=>void copy(verifyCommand)}>复制恢复核验命令</Button></Space>
    <p className="muted">核验成功后，只由你选择可恢复的旧文档任务。恢复不自动派发，OCR 不自动复活。</p>
    <Input aria-label="旧文档任务编号" placeholder="旧文档 task_id" value={task} onChange={event=>setTask(event.target.value.trim())}/>
    <Input aria-label="旧尝试编号" placeholder="该任务原 current_attempt_id" value={attempt} onChange={event=>setAttempt(event.target.value.trim())}/>
    <Button disabled={!resumeCommand} onClick={()=>void copy(resumeCommand)}>复制手动恢复任务命令</Button>
    </>}</div>}]} /></div>;
}
