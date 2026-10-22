import {useCallback,useLayoutEffect,useRef,useState} from "react";

export function useChatScroll(scope:string,revision:string){
  const viewportRef=useRef<HTMLDivElement|null>(null),contentRef=useRef<HTMLDivElement|null>(null);
  const following=useRef(true),prepend=useRef<{scope:string;height:number;top:number}|null>(null);
  const [showLatest,setShowLatest]=useState(false),[hasUnread,setHasUnread]=useState(false);
  const jumpToLatest=useCallback(()=>{
    const element=viewportRef.current;if(!element)return;
    following.current=true;element.scrollTop=element.scrollHeight;
    setShowLatest(false);setHasUnread(false);
  },[]);
  const onScroll=useCallback(()=>{
    const element=viewportRef.current;if(!element)return;
    const atBottom=element.scrollHeight-element.clientHeight-element.scrollTop<=72;
    following.current=atBottom;setShowLatest(!atBottom);if(atBottom)setHasUnread(false);
  },[]);
  const rememberPrepend=useCallback(()=>{
    const element=viewportRef.current;if(!element)return;
    prepend.current={scope,height:element.scrollHeight,top:element.scrollTop};
    following.current=false;
  },[scope]);
  useLayoutEffect(()=>{
    following.current=true;prepend.current=null;setShowLatest(false);setHasUnread(false);
  },[scope]);
  useLayoutEffect(()=>{
    const element=viewportRef.current;if(!element)return;
    const anchor=prepend.current;
    if(anchor?.scope===scope){
      element.scrollTop=anchor.top+element.scrollHeight-anchor.height;
      prepend.current=null;onScroll();return;
    }
    if(following.current)jumpToLatest();else setHasUnread(true);
  },[scope,revision,jumpToLatest,onScroll]);
  useLayoutEffect(()=>{
    const content=contentRef.current;if(!content)return;
    const observer=new ResizeObserver(()=>{
      if(following.current&&!prepend.current)jumpToLatest();
    });
    observer.observe(content);
    return()=>observer.disconnect();
  },[scope,jumpToLatest]);
  return {viewportRef,contentRef,onScroll,rememberPrepend,jumpToLatest,showLatest,hasUnread};
}
