import {Component,Suspense,type ReactNode} from "react";
import {Alert,Button,Spin} from "antd";
class LoadErrorBoundary extends Component<{children:ReactNode;label:string},{failed:boolean}>{
  state={failed:false};
  static getDerivedStateFromError(){return {failed:true};}
  render(){return this.state.failed?<Alert showIcon type="error" message={this.props.label+"加载失败"} description="已保存的资料和已接收的任务仍在。刷新页面可重新加载资源，页面不会替你重新提交操作。" action={<Button onClick={()=>window.location.reload()}>刷新页面</Button>}/>:this.props.children;}
}
export default function LoadBoundary({children,label}:{children:ReactNode;label:string}){return <LoadErrorBoundary label={label}><Suspense fallback={<div role="status" aria-live="polite" style={{padding:24}}><Spin/> <span>{label}加载中…</span></div>}>{children}</Suspense></LoadErrorBoundary>;}
