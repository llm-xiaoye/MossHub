"""One application POST per purpose; bounded upstream bytes precede the SSE parser."""
from __future__ import annotations
import asyncio
import time
from contextlib import asynccontextmanager
from uuid import uuid4
import httpx
from httpx_sse import aconnect_sse
from pydantic_core import from_json
from ..adapters import FlashAdapter
from ..indexing.storage import decode
from ..ingestion.store import BusinessError
from ..model_profiles import canonical, text_sha256

class BoundedCloudClient(httpx.AsyncClient):
    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        async with super().stream(*args, **kwargs) as response:
            original = response.aiter_bytes
            async def bounded_bytes(chunk_size=None):
                total, line, event, previous_cr = 0, 0, 0, False
                async for block in original(chunk_size=4096):
                    total += len(block)
                    if total > 1024 * 1024:
                        raise ValueError("decompressed upstream response exceeds 1 MiB")
                    for byte in block if response.headers.get("content-type", "").split(";")[0].strip() == "text/event-stream" else ():
                        if byte == 10 and previous_cr:
                            previous_cr = False
                            continue
                        event += 1
                        if byte in (10,13):
                            if line == 0:
                                event = 0
                            line = 0
                            previous_cr = byte == 13
                        else:
                            previous_cr = False
                            line += 1
                        if line > 65536 or event > 65536:
                            raise ValueError("upstream SSE line/event/unclosed buffer exceeds 64 KiB")
                    yield block
            response.aiter_bytes = bounded_bytes
            yield response

class Flash:
    def __init__(self, settings, store):
        self.adapter = FlashAdapter.from_settings(settings)
        self.store = store
        self.active = {}
        self.http = BoundedCloudClient(trust_env=False, follow_redirects=False,
                                      transport=httpx.AsyncHTTPTransport(retries=0))
    def bind_response(self, request_id, response):
        handle = {"response":response,"close_task":None,"read_completed":False}
        original_close = response.aclose
        async def observed_close():
            if handle["close_task"] is None:
                handle["close_task"] = asyncio.create_task(original_close(),name="rag-cloud-close-"+request_id)
            await asyncio.shield(handle["close_task"])
        response.aclose = observed_close
        self.active[request_id] = handle
        return handle

    @staticmethod
    def closed(handle):
        task = handle["close_task"] if handle is not None else None
        return (handle is not None and task is not None and task.done()
                and not task.cancelled() and task.exception() is None and handle["response"].is_closed)

    async def close(self):
        for handle in list(self.active.values()):
            if handle["close_task"] is None:
                await handle["response"].aclose()
        tasks = [v["close_task"] for v in self.active.values() if v["close_task"] is not None]
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)
        await self.http.aclose()

    async def settle(self, request_id):
        handle = self.active.get(request_id)
        if handle is None:
            return []
        if not handle["read_completed"]:
            return ["CLOUD_READ_PENDING"]
        if handle["close_task"] is None:
            try:
                await handle["response"].aclose()
            except Exception:
                return ["CLOUD_HTTP_CLOSE_UNCONFIRMED"]
        if self.closed(handle):
            self.active.pop(request_id,None)
            return []
        return ["CLOUD_HTTP_CLOSE_UNCONFIRMED"]

    async def call(self, instance, request_id, purpose, messages, deadline, trace, preview=None):
        streaming = purpose == "generate"
        budget, output, read_seconds, bound = {
            "understand": (20,1024,15,65536),
            "summary": (45,2048,40,131072),
            "generate": (60,2048,55,131072)}[purpose]
        end = min(deadline, time.monotonic()+budget)
        payload = {"model":"deepseek-flash","thinking":{"type":"disabled"},"temperature":0,
                   "messages":messages,"max_tokens":output,"stream":streaming,
                   "response_format":{"type":"json_object"}}
        if streaming:
            payload["stream_options"] = {"include_usage":True}
        record = {"call_id":str(uuid4()),"purpose":purpose,"request":payload,
                  "request_sha256":text_sha256(canonical(payload).decode()),
                  "application_post_attempts":0,"physical_send_count":"NOT_OBSERVED",
                  "actual_model":"NOT_OBSERVED","usage":"NOT_OBSERVED","raw_content":"",
                  "provider_response_id":None,"finish_reason":None,"done_observed":False,
                  "remote_execution_stopped":"NOT_OBSERVED","local_http_closed":False,
                  "started_at_unix":time.time(),"deadline_monotonic":end}
        trace.setdefault("provider_calls",[]).append(record)
        started = time.monotonic()
        observed_response = None
        handle = None
        await self.store.call("provider_begin",instance,request_id,record["call_id"],purpose)
        try:
            await self.store.call("fence",instance,request_id)
            remaining = end-time.monotonic()
            if remaining <= 0:
                raise TimeoutError("cloud stage budget exhausted before send")
            timeout = httpx.Timeout(min(read_seconds,remaining),connect=min(5,remaining),
                                    write=min(5,remaining),pool=min(2,remaining))
            headers = {"Authorization":"Bearer "+self.adapter._api_key(),"Content-Type":"application/json"}
            # SENT denotes a single application attempt; physical transmission is not inferred.
            await self.store.call("provider_sent",instance,request_id,record["call_id"])
            record["application_post_attempts"] = 1
            async with asyncio.timeout(remaining):
                if not streaming:
                    async with self.http.stream("POST",self.adapter.base_url+"/chat/completions",
                                                content=canonical(payload),headers=headers,timeout=timeout) as response:
                        observed_response = response
                        handle = self.bind_response(request_id,response)
                        record["http_status"] = response.status_code
                        raw = bytearray()
                        async for block in response.aiter_bytes():
                            if len(raw)+len(block) > bound:
                                raise ValueError("cloud JSON response exceeds purpose byte limit")
                            raw.extend(block)
                        record["raw_response"] = bytes(raw).decode("utf-8")
                        if response.status_code != 200:
                            raise BusinessError("PROVIDER_HTTP_ERROR","DeepSeek 返回非成功响应，不自动重发。")
                        value = decode(bytes(raw))
                        if type(value) is not dict or type(value.get("choices")) is not list or len(value["choices"]) != 1:
                            raise ValueError("cloud response must have exactly one choice")
                        choice = value["choices"][0]
                        if choice.get("index") != 0 or choice.get("finish_reason") != "stop":
                            raise ValueError("cloud response did not finish normally")
                        message = choice.get("message")
                        if type(message) is not dict or message.get("role") != "assistant" or message.get("tool_calls"):
                            raise ValueError("cloud response is not an assistant content result")
                        content = message.get("content")
                        if type(content) is not str or not content or len(content.encode("utf-8")) > bound:
                            raise ValueError("cloud content is empty or exceeds purpose byte limit")
                        record.update(raw_content=content,actual_model=value.get("model","NOT_OBSERVED"),
                                      usage=value.get("usage") or "NOT_OBSERVED",provider_response_id=value.get("id"),
                                      finish_reason="stop")
                else:
                    chunks, content_bytes, finished = [], 0, False
                    record["raw_sse_events"] = []
                    async with aconnect_sse(self.http,"POST",self.adapter.base_url+"/chat/completions",
                                            content=canonical(payload),headers=headers,timeout=timeout) as events:
                        observed_response = events.response
                        handle = self.bind_response(request_id,events.response)
                        record["http_status"] = events.response.status_code
                        if events.response.status_code != 200:
                            raise BusinessError("PROVIDER_HTTP_ERROR","DeepSeek 流式请求未成功，不自动重发。")
                        async for event in events.aiter_sse():
                            await self.store.call("fence",instance,request_id)
                            record["raw_sse_events"].append(event.data)
                            if event.data == "[DONE]":
                                if not finished:
                                    raise ValueError("DONE arrived before the stop finish reason")
                                record["done_observed"] = True
                                break
                            value = decode(event.data.encode("utf-8"))
                            if type(value) is not dict or type(value.get("choices")) is not list or len(value["choices"]) != 1:
                                raise ValueError("stream must have exactly one choice per content/finish chunk")
                            choice = value["choices"][0]
                            if choice.get("index") != 0 or type(choice.get("delta")) is not dict:
                                raise ValueError("stream choice identity/delta differs")
                            if finished:
                                raise ValueError("content arrived after finish")
                            response_id = value.get("id")
                            if record["provider_response_id"] is not None and response_id != record["provider_response_id"]:
                                raise ValueError("upstream response identity changed")
                            record["provider_response_id"] = response_id
                            record["actual_model"] = value.get("model","NOT_OBSERVED")
                            if value.get("usage") is not None:
                                record["usage"] = value["usage"]
                            delta = choice["delta"]
                            if delta.get("tool_calls") or delta.get("reasoning_content"):
                                raise ValueError("unexpected tool/reasoning output in ordinary JSON mode")
                            part = delta.get("content")
                            if part is not None:
                                if type(part) is not str:
                                    raise ValueError("stream content must be text")
                                content_bytes += len(part.encode("utf-8"))
                                if content_bytes > bound:
                                    raise ValueError("generation content exceeds 128 KiB")
                                chunks.append(part)
                                record["raw_content"] = "".join(chunks)
                                if preview is not None:
                                    try:
                                        partial = from_json(record["raw_content"],allow_partial="trailing-strings",allow_inf_nan=False)
                                    except ValueError:
                                        partial = None
                                    if type(partial) is dict:
                                        paragraphs = partial.get("paragraphs",[])
                                        visible = [p["text"] for p in paragraphs if type(p) is dict and type(p.get("text")) is str] if type(paragraphs) is list else []
                                        limitations = partial.get("limitations",[])
                                        visible += [v for v in limitations if type(v) is str] if type(limitations) is list else []
                                        if any(visible):
                                            await preview("\n\n".join(visible))
                            reason = choice.get("finish_reason")
                            if reason is not None:
                                record["finish_reason"] = reason
                                if reason != "stop":
                                    raise ValueError("stream finish reason is not stop")
                                finished = True
                    if not record["done_observed"] or not record["raw_content"]:
                        raise ValueError("upstream stream ended without complete content and DONE")
            record["local_http_closed"] = self.closed(handle)
            if not record["local_http_closed"]:
                raise BusinessError("CLOUD_HTTP_CLOSE_UNCONFIRMED", "本地 HTTP 关闭尚未确认。")
            result = decode(record["raw_content"].encode("utf-8"))
            if type(result) is not dict:
                raise ValueError("cloud JSON root is not object")
            await self.store.call("provider_end",instance,request_id,record["call_id"],"SUCCEEDED")
            return result
        except BaseException as caught:
            record["error_type"] = type(caught).__name__
            record["local_http_closed"] = self.closed(handle) if observed_response is not None else "NOT_OBSERVED"
            record["result_state"] = "UNKNOWN" if record["application_post_attempts"] else "FAILED"
            await asyncio.shield(self.store.call("provider_end",instance,request_id,record["call_id"],record["result_state"]))
            raise
        finally:
            if handle is not None:
                handle["read_completed"] = True
                record["local_read_completed"] = True
                if self.closed(handle):
                    self.active.pop(request_id,None)
                record["local_http_closed"] = self.closed(handle)
            record["elapsed_seconds"] = time.monotonic()-started
            record["observed_at_unix"] = time.time()
            await asyncio.shield(self.store.call("save_progress",instance,request_id,trace))
