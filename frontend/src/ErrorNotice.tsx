import {Alert,Button} from "antd";
import {ApiError,describeError} from "./api";
type Props={error:unknown;stage:string;retained?:string;next?:string;onRecover?:()=>void;recoverLabel?:string;unknownSubmission?:boolean;summary?:string};
export default function ErrorNotice({error,stage,retained="上次成功读取的内容仍保留。",next="先刷新状态，确认完成情况后再继续。",onRecover,recoverLabel="读取原状态",unknownSubmission=false,summary}:Props){
  const detail=describeError(error),code=error instanceof ApiError?error.code:undefined;
  return <Alert showIcon type="error" message={stage+"暂未完成"} description={<><p style={{margin:"0 0 8px"}}>{summary??detail}</p><p style={{margin:"0 0 8px"}}>{retained} {unknownSubmission?"本次提交是否已接收尚未确认，请先读取原状态；不要重复提交。":next}</p><></></>} action={onRecover&&<Button size="small" onClick={onRecover}>{recoverLabel}</Button>}/>;
}
