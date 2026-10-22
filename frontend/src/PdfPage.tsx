import {useEffect,useRef,useState} from "react";
import {Alert} from "antd";
import {describeError,type RecordValue} from "./api";
import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
pdfjs.GlobalWorkerOptions.workerSrc=workerUrl;
export default function PdfPage({url,page,locator}:{url:string;page:number;locator:RecordValue|null}){
  const canvas=useRef<HTMLCanvasElement>(null),[error,setError]=useState(""),[region,setRegion]=useState<number[]|null>(null),[actualPage,setActualPage]=useState(page),[pageCount,setPageCount]=useState<number|null>(null);
  useEffect(()=>{
    let cancelled=false;setError("");setRegion(null);const task=pdfjs.getDocument({url});
    void task.promise.then(async doc=>{
      const resolvedPage=Math.min(Math.max(page,1),doc.numPages);const p=await doc.getPage(resolvedPage);if(cancelled)return;setActualPage(resolvedPage);setPageCount(doc.numPages);
      const viewport=p.getViewport({scale:1.15}),element=canvas.current;
      if(!element||cancelled)return;element.width=viewport.width;element.height=viewport.height;
      const ctx=element.getContext("2d");if(!ctx)throw new Error("无法创建 PDF 画布");
      await p.render({canvas:element,canvasContext:ctx,viewport}).promise;
      const box=locator?.bbox;const coordinates=box&&[box.l,box.t,box.r,box.b];
      if(locator?.page===resolvedPage&&coordinates?.every((v:unknown)=>typeof v==="number"&&Number.isFinite(v))&&["BOTTOMLEFT","TOPLEFT"].includes(box.coord_origin)){
        const native=box.coord_origin==="TOPLEFT"?[box.l,p.view[3]-box.t,box.r,p.view[3]-box.b]:coordinates;
        const rect=viewport.convertToViewportRectangle(native);setRegion([Math.min(rect[0],rect[2])/viewport.width*100,Math.min(rect[1],rect[3])/viewport.height*100,Math.abs(rect[2]-rect[0])/viewport.width*100,Math.abs(rect[3]-rect[1])/viewport.height*100]);
      }else setRegion(null);
    }).catch(err=>{if(!cancelled)setError(describeError(err));});
    return()=>{cancelled=true;void task.destroy();};
  },[url,page,locator]);
  return <>{error&&<Alert type="error" message={error}/>}<span className="muted">第 {actualPage} 页{pageCount?" / 共 "+pageCount+" 页":""}</span><div className="pdf-wrap"><canvas ref={canvas} aria-label={"PDF 第"+actualPage+"页"}/>{region&&<div className="source-region" style={{left:region[0]+"%",top:region[1]+"%",width:region[2]+"%",height:region[3]+"%"}}/>}</div></>;
}
