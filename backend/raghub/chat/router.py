"""Question API and native SSE snapshots; disconnect never starts or cancels a question."""
from __future__ import annotations
import asyncio
import time
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from ..ingestion.store import BusinessError
from ..model_profiles import canonical
from .contracts import ConversationCreate, Question

router = APIRouter(prefix="/api/v1")

def uuid_header(value):
    from ..ingestion.archive import uuid_text
    uuid_text(value)
    from uuid import UUID
    if UUID(value).version != 4 or str(UUID(value)) != value:
        raise BusinessError("INVALID_REQUEST_KEY","请求键必须为标准小写 UUIDv4。",422)
    return value

def public_reference(value):
    if value.get("deleted") is True:
        return {"document_id":value["document_id"],"support_unit_id":value["support_unit_id"],
                "deleted":True,"label":"资料已删除"}
    return {key:value[key] for key in ("evidence_id","support_unit_id","kb_id","generation_id","document_id",
                                     "raw_version_id","processing_version_id","source_record_id")}


def event_packet(value):
    """Large source interval maps are fetched from the detail URL, not repeated in SSE."""
    result = ({k:v for k,v in value["result"].items() if k != "memory_context"} if value["result"] is not None else None)
    if result is not None:
        result = {**result,"paragraphs":[{"text":p["text"],"support_unit_ids":p["support_unit_ids"],
                    "references":[public_reference(r) for r in p["references"]]}
                    for p in result["paragraphs"]]}
    packet = {k:value[k] for k in ("request_id","conversation_id","data_instance_id","state","error","draft")}
    if isinstance(value.get("question"), str):
        packet["question"] = value["question"]
    if value.get("progress") is not None:
        packet["progress"] = value["progress"]
    packet.update(result=result,detail_url="/api/v1/question-requests/"+value["request_id"])
    if len(canonical(packet)) > 256*1024:
        raise BusinessError("SSE_EVENT_TOO_LARGE","下游事件超出256 KiB上限。")
    return packet

@router.post("/conversations",status_code=201)
async def create_conversation(body:ConversationCreate,request:Request,
                              instance:str=Header(alias="X-RAG-Data-Instance"),
                              key:str=Header(alias="Idempotency-Key")):
    return await request.app.state.chat.db.call("create_conversation",instance,uuid_header(key),body.kb_id)

@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id:str,request:Request,instance:str=Header(alias="X-RAG-Data-Instance")):
    return await request.app.state.chat.db.call("conversation",instance,conversation_id)

@router.delete("/conversations/{conversation_id}",status_code=204)
async def delete_conversation(conversation_id:str,request:Request,instance:str=Header(alias="X-RAG-Data-Instance")):
    await request.app.state.chat.delete_conversation(instance,conversation_id)
    return Response(status_code=204)

@router.post("/conversations/{conversation_id}/questions",status_code=202)
async def question(conversation_id:str,body:Question,request:Request,
                   instance:str=Header(alias="X-RAG-Data-Instance"),
                   key:str=Header(alias="Idempotency-Key")):
    return await request.app.state.chat.submit_question(instance,conversation_id,uuid_header(key),body)

@router.get("/question-requests/{request_id}")
async def question_request(request_id:str,request:Request,instance:str=Header(alias="X-RAG-Data-Instance")):
    return await request.app.state.chat.question_snapshot(instance,request_id)

@router.post("/question-requests/{request_id}/cancel")
async def cancel(request_id:str,request:Request,instance:str=Header(alias="X-RAG-Data-Instance")):
    service = request.app.state.chat
    await service.db.call("conversation_for_request",instance,request_id)
    return await service.db.call("cancel",instance,request_id)

async def reserve_subscription(request_id:str,request:Request,
                               instance:str=Query(alias="data_instance_id")):
    """Resolve before FastAPI starts its SSE producer, keeping admission errors HTTP errors."""
    service = request.app.state.chat
    queues = service.subscribers.setdefault(request_id,set())
    if len(queues) >= 2 or service.global_subscribers >= 4:
        if not queues:
            service.subscribers.pop(request_id,None)
        raise BusinessError("SSE_SUBSCRIBERS_BUSY","订阅连接已达上限。",429)
    queue = asyncio.Queue(maxsize=1)
    queues.add(queue)
    service.global_subscribers += 1
    try:
        # Register first; the dependency's request scope lasts through stream teardown.
        snapshot = await service.question_snapshot(instance,request_id)
        first = event_packet(snapshot)
        yield service,queue,snapshot,first
    finally:
        queues.discard(queue)
        service.global_subscribers -= 1
        if not queues:
            service.subscribers.pop(request_id,None)

@router.get("/question-requests/{request_id}/events",response_class=EventSourceResponse)
async def events(request_id:str,instance:str=Query(alias="data_instance_id"),
                 subscription=Depends(reserve_subscription)):
    # Native EventSourceResponse expects the endpoint itself to be an async generator.
    service,queue,snapshot,first = subscription
    last_revision,last_sent = snapshot["draft"]["revision"],time.monotonic()
    last_progress = snapshot.get("progress")
    terminal = {"SUCCEEDED","FAILED","CANCELLED","INTERRUPTED"}
    yield ServerSentEvent(data=first,event="snapshot",id=request_id+":"+str(last_revision),retry=3000)
    while True:
        current = await service.question_snapshot(instance,request_id)
        if current["state"] in terminal:
            yield ServerSentEvent(data=event_packet(current),event="done",
                                  id=request_id+":"+str(current["draft"]["revision"]),retry=3000)
            break
        if current.get("progress") != last_progress:
            yield ServerSentEvent(data=event_packet(current),event="snapshot",
                                  id=request_id+":"+str(current["draft"]["revision"]),retry=3000)
            last_progress = current.get("progress")
        revision = current["draft"]["revision"]
        if revision > last_revision:
            if last_revision > 0:
                await asyncio.sleep(max(0,0.1-(time.monotonic()-last_sent)))
            latest = service.drafts.get(request_id,current["draft"])
            packet = {"request_id":request_id,"revision":latest["revision"],"text":latest["text"],"validated":False}
            if len(canonical(packet)) > 256*1024:
                raise BusinessError("SSE_EVENT_TOO_LARGE","草稿事件超过256 KiB上限。")
            yield ServerSentEvent(data=packet,event="draft",id=request_id+":"+str(latest["revision"]))
            last_revision,last_sent = latest["revision"],time.monotonic()
        try:
            await asyncio.wait_for(queue.get(),timeout=0.5)
        except TimeoutError:
            pass
