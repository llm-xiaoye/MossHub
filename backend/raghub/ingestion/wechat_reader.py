"""Bounded WeChat text fallback; never presents reader output as original HTML."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .archive import json_bytes, write_closed
from .contracts import MIB, url_source_key

LIMITATION = "仅导入文字副本：图片和版式可能不全。回答只依据本次保存的文字；需要核对图片或表格时，请保存原文章为 PDF 后导入。"
MAX_BYTES = 2 * MIB


class ReaderVerificationRequired(ValueError):
    """The reader returned a challenge page rather than article content."""


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("reader response has duplicate fields")
        value[key] = item
    return value


def reader_content(raw: bytes, source_url: str) -> tuple[str, str]:
    if len(raw) > MAX_BYTES:
        raise ValueError("备用文字超过 2 MiB，请保存文章为 PDF 或 Markdown 后导入。")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or value.get("code") != 200:
        raise ValueError("备用读取器未返回成功正文。")
    data = value.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("url"), str):
        raise ValueError("备用读取器没有返回可核对的来源。")
    if url_source_key(data["url"]) != url_source_key(source_url):
        raise ValueError("备用文字的来源与提交链接不同，已停止导入。")
    text, title = data.get("content"), data.get("title", "")
    if not isinstance(text, str) or not isinstance(title, str):
        raise ValueError("备用读取器返回的文字格式不正确。")
    text = text.strip()
    if any(marker in text.lower() for marker in (
            "环境异常", "访问过于频繁", "完成验证后即可继续访问", "verify you are human",
            "captcha verification", "enable javascript and cookies to continue")):
        raise ReaderVerificationRequired("备用通道仍然返回验证页面，没有取得文章正文。")
    if len(text) < 200 or len(text.splitlines()) > 10000 or "\x00" in text:
        raise ValueError("备用正文过短或不完整，请保存文章为 PDF 或 Markdown 后导入。")
    return title.strip()[:500], text + "\n"


async def acquire_wechat_text(payload: dict, directory: Path, deadline: float, cancelled,
                             direct_error: Exception, *, client=None):
    # Import here to avoid a module cycle; all requests keep the public-DNS gate.
    from .web import AcquisitionError, public_url
    source = url_source_key(payload["url"])
    parsed = urlsplit(source)
    if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com" or parsed.path not in {"/s", "/s/"} and not parsed.path.startswith("/s/"):
        raise AcquisitionError("WECHAT_READER_SOURCE_REJECTED", "备用入口只读取本次提交的公开微信文章。")
    await public_url(source, ["mp.weixin.qq.com"])
    endpoint = "https://r.jina.ai/" + source
    try:
        await public_url(endpoint, ["r.jina.ai"])
    except (AcquisitionError, OSError) as exc:
        # A fallback DNS rejection is still fail-closed, with an actionable
        # explanation instead of leaking an unrelated generic URL error.
        raise AcquisitionError("WECHAT_TEXT_READER_FAILED",
            "微信直连未取得正文，备用文字通道的网络地址核对也未通过。任务记录已保留，没有导入正文；"
            "请检查本机网络、代理或 DNS 设置，或将文章保存为 PDF / Markdown 后从本地文件导入。",
            details={"direct_code": getattr(direct_error, "code", None),
                     "reader_stage": "PUBLIC_ADDRESS_CHECK", "reader_http_sent": False,
                     "reader_error_type": type(exc).__name__,
                     "reader_code": getattr(exc, "code", None),
                     "reader_details": getattr(exc, "details", {})}, retryable=False) from exc
    if cancelled():
        raise AcquisitionError("CANCELLED", "任务已取消。")
    remaining = min(45.0, deadline - time.monotonic())
    if remaining <= 0:
        raise AcquisitionError("WEB_TOTAL_TIMEOUT", "网页获取已超时。已保留任务记录，请保存文章为 PDF 或 Markdown 后导入。")
    own_client = client is None
    client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                       timeout=httpx.Timeout(remaining, connect=min(10, remaining)))
    try:
        async with asyncio.timeout(remaining):
            async with client.stream("GET", endpoint, headers={"Accept": "application/json",
                    "X-Timeout": str(max(1, int(remaining - 2))), "X-Retain-Images": "none"}) as response:
                raw, size = [], 0
                async for chunk in response.aiter_bytes():
                    if cancelled():
                        raise AcquisitionError("CANCELLED", "任务已取消。")
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("备用文字超过 2 MiB。")
                    raw.append(chunk)
                content = b"".join(raw)
                # Persist the actual response before classifying or parsing it.
                write_closed(directory / "reader-response.json", content)
                if response.status_code != 200:
                    raise ValueError("备用读取器未取得正文（HTTP " + str(response.status_code) + "）。")
                title, text = reader_content(content, source)
        write_closed(directory / "original.txt", text.encode("utf-8"))
        observation = {
            "input_kind": "WEB_TEXT_READER", "url": source, "final_url": source,
            "title": title, "reader_url": endpoint, "reader": "jina_reader",
            "snapshot_method": "READER_TEXT_COPY_NO_ORIGINAL_HTML",
            "captured_at": time.time(), "http_status": response.status_code,
            "reader_response_sha256": hashlib.sha256(content).hexdigest(),
            "direct_failure": {"type": type(direct_error).__name__,
                               "code": getattr(direct_error, "code", None)},
            "content_completeness": "TEXT_ONLY_IMAGES_AND_LAYOUT_UNVERIFIED",
            "limitations": [LIMITATION], "resource_review_required": False,
            "provenance": payload["metadata"]["provenance"],
            "configuration": payload["acquisition_config"],
        }
        write_closed(directory / "source-observation.json", json_bytes(observation))
        return {name: directory / name for name in
                ("original.txt", "reader-response.json", "source-observation.json")}, observation, {}
    except AcquisitionError:
        raise
    except (ValueError, httpx.HTTPError, TimeoutError) as exc:
        message = ("微信文章需要完成访问验证，直连和备用读取均未取得正文。任务记录已保留，未导入验证页面；"
                   "请在浏览器打开文章，保存为 PDF 或 Markdown 后从本地文件导入。"
                   if isinstance(exc, ReaderVerificationRequired) else
                   "微信直连和备用文字读取均未完成。已保留任务记录；请在浏览器打开文章，保存为 PDF 或 Markdown 后从本地文件导入。")
        raise AcquisitionError("WECHAT_TEXT_READER_FAILED",
            message,
            details={"direct_code": getattr(direct_error, "code", None),
                     "reader_error_type": type(exc).__name__, "reader_error": str(exc)},
            retryable=False) from exc
    finally:
        if own_client:
            await client.aclose()

