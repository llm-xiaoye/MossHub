"""Reader policy regressions; synthetic responses are not live import evidence."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from raghub.ingestion.web import AcquisitionError, acquisition_config, acquire_web
from raghub.ingestion.wechat_reader import LIMITATION, acquire_wechat_text, reader_content

URL = "https://mp.weixin.qq.com/s/test-public-article"
TEXT = "这是文章中明确记录的知识与条件。正文只有本次取得的文字，没有完整图片。" * 12


def response_bytes(**changes):
    data = {"url": URL, "title": "公开文章", "content": TEXT}
    data.update(changes)
    return json.dumps({"code": 200, "data": data}, ensure_ascii=False).encode()


class ReaderPolicyTest(unittest.TestCase):
    def test_source_is_exact_and_text_copy_is_not_html(self):
        title, text = reader_content(response_bytes(), URL)
        self.assertEqual(title, "公开文章")
        self.assertEqual(text, TEXT + "\n")
        with self.assertRaisesRegex(ValueError, "来源"):
            reader_content(response_bytes(url=URL + "-another"), URL)

    def test_challenge_short_content_and_duplicate_fields_are_rejected(self):
        for body in ("很短", "完成验证后即可继续访问" * 50, TEXT + "\x00"):
            with self.subTest(body=body[:20]), self.assertRaises(ValueError):
                reader_content(response_bytes(content=body), URL)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            reader_content(b'{"code":200,"code":200,"data":{}}', URL)

    def test_failed_reader_does_not_create_a_primary_original(self):
        async def run(root):
            async def transport(request):
                return httpx.Response(429, content=b"rate limited", request=request)
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                with patch("raghub.ingestion.web.public_url", new=AsyncMock(side_effect=lambda value, hosts: value)):
                    with self.assertRaises(AcquisitionError) as caught:
                        await acquire_wechat_text({"url": URL}, root, time.monotonic()+5,
                            lambda: False, AcquisitionError("WEB_PAGE_VERIFICATION_REQUIRED", "验证码"), client=client)
                self.assertEqual(caught.exception.code, "WECHAT_TEXT_READER_FAILED")
                self.assertFalse(caught.exception.retryable)
                self.assertTrue((root/"reader-response.json").is_file())
                self.assertFalse((root/"original.txt").exists())
        with tempfile.TemporaryDirectory() as value:
            asyncio.run(run(Path(value)))

    def test_saved_reader_bytes_and_limitation_are_bound(self):
        async def run(root):
            seen = []
            async def transport(request):
                seen.append(request)
                return httpx.Response(200, content=response_bytes(), request=request)
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                payload={"url": URL, "metadata":{"provenance":"synthetic-unit-fixture"},
                         "acquisition_config":{"reader_fallback":"jina_text_wechat_v1"}}
                with patch("raghub.ingestion.web.public_url", new=AsyncMock(side_effect=lambda value, hosts: value)):
                    members, obs, derived = await acquire_wechat_text(payload, root, time.monotonic()+5,
                        lambda: False, AcquisitionError("WEB_PAGE_VERIFICATION_REQUIRED","验证码"), client=client)
            self.assertEqual(len(seen), 1)
            self.assertEqual(str(seen[0].url), "https://r.jina.ai/"+URL)
            self.assertNotIn("cookie", seen[0].headers)
            self.assertEqual(set(members), {"original.txt","reader-response.json","source-observation.json"})
            self.assertEqual(derived, {})
            self.assertEqual((root/"original.txt").read_text(), TEXT+"\n")
            self.assertEqual((root/"reader-response.json").read_bytes(), response_bytes())
            self.assertEqual(obs["input_kind"], "WEB_TEXT_READER")
            self.assertEqual(obs["limitations"], [LIMITATION])
            self.assertNotIn("captured.html", members)
        with tempfile.TemporaryDirectory() as value:
            asyncio.run(run(Path(value)))

    def test_old_config_and_other_hosts_do_not_enable_fallback(self):
        async def run(root):
            for url, config in ((URL, {}), ("https://www.sqlite.org/about.html", {"reader_fallback":"jina_text_wechat_v1"})):
                error=AcquisitionError("WEB_PAGE_VERIFICATION_REQUIRED","验证")
                with patch("raghub.ingestion.web._acquire_web", new=AsyncMock(side_effect=error)), patch(
                    "raghub.ingestion.wechat_reader.acquire_wechat_text", new=AsyncMock()) as fallback:
                    with self.assertRaises(AcquisitionError):
                        await acquire_web({"url":url,"acquisition_config":config},root,time.monotonic()+10,lambda:False)
                    fallback.assert_not_called()
        with tempfile.TemporaryDirectory() as value:
            asyncio.run(run(Path(value)))

    def test_security_rejection_and_cancellation_never_use_reader(self):
        async def run(root):
            for code in ("URL_NONPUBLIC_DNS","URL_HOST_NOT_ALLOWED","CANCELLED"):
                with patch("raghub.ingestion.web._acquire_web",new=AsyncMock(side_effect=AcquisitionError(code,code))), patch(
                    "raghub.ingestion.wechat_reader.acquire_wechat_text",new=AsyncMock()) as fallback:
                    with self.assertRaises(AcquisitionError):
                        await acquire_web({"url":URL,"acquisition_config":{"reader_fallback":"jina_text_wechat_v1"}},
                                          root,time.monotonic()+10,lambda:False)
                    fallback.assert_not_called()
        with tempfile.TemporaryDirectory() as value:
            asyncio.run(run(Path(value)))

    def test_verified_direct_failure_uses_one_reader_call(self):
        async def run(root):
            with patch("raghub.ingestion.web._acquire_web",new=AsyncMock(side_effect=AcquisitionError(
                    "WEB_PAGE_VERIFICATION_REQUIRED","验证"))), patch(
                    "raghub.ingestion.wechat_reader.acquire_wechat_text",new=AsyncMock(return_value=({},{"input_kind":"WEB_TEXT_READER"},{}))) as fallback:
                result=await acquire_web({"url":URL,"acquisition_config":{"reader_fallback":"jina_text_wechat_v1"}},
                                         root,time.monotonic()+70,lambda:False)
                self.assertEqual(result[1]["input_kind"],"WEB_TEXT_READER")
                fallback.assert_awaited_once()
        with tempfile.TemporaryDirectory() as value:
            asyncio.run(run(Path(value)))


if __name__ == "__main__":
    unittest.main()

