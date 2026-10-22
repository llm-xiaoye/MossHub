"""Source-preservation and actionable reader errors; no provider calls."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from raghub.douyin_worker import _note_analysis
from raghub.ingestion.web import AcquisitionError
from raghub.ingestion.wechat_reader import ReaderVerificationRequired, acquire_wechat_text, reader_content


class EntrySourcePreservationTest(unittest.TestCase):
    def sample(self):
        material = "MCP给AI装手，Skill给AI装脑。读取实时数据时使用MCP，静态规范由Skill提供。"
        body = "> 分层协作。\n\n## 核心要点\n- 两者协作。\n\n## 机制\n视频使用手和脑的比喻。\n\n## 模型补充\n### 补充知识\n未外部核验。\n### 纠错与边界\n类比有边界。\n### 模型判断\n按条件选择。\n\n## 自测\n问：是否替代？答：协作。"
        analysis = {"title":"分工","category":["Agent","工具与技能"],"tags":["MCP","Skill","Agent"],"type":"知识笔记","insight":"分层协作",
            "learning_review":{"learning_goal":"辨析分工","key_claims":[
                {"claim":"对象与比喻对应","evidence":"MCP给AI装手，Skill给AI装脑。","section":"机制"},
                {"claim":"使用条件","evidence":"读取实时数据时使用MCP，静态规范由Skill提供。","section":"模型判断"}],
                "application":"按条件选择","uncertainties":["未外部核验"],"external_checks":[]}}
        return {"knowledge":body,"analysis":analysis}, material

    def test_saved_note_preserves_role_mapping_and_conditions_from_actual_claims(self):
        value, material = self.sample()
        body, analysis = _note_analysis(value, material)
        self.assertIn("> MCP给AI装手，Skill给AI装脑。", body)
        self.assertIn("> 读取实时数据时使用MCP，静态规范由Skill提供。", body)
        self.assertLess(body.index("## 视频原话要点"), body.index("## 模型补充"))
        self.assertIn("尚未人工复审", body)
        self.assertEqual(analysis, value["analysis"])
        self.assertNotIn("视频原话要点", value["knowledge"])

    def test_unverifiable_claim_still_stops_before_saving(self):
        value, material = self.sample()
        value["analysis"]["learning_review"]["key_claims"][0]["evidence"] = "未出现在转写里的内容"
        with self.assertRaisesRegex(ValueError, "not bound"):
            _note_analysis(value, material)

    def test_short_verification_page_has_specific_reason(self):
        raw = json.dumps({"code":200,"data":{"url":"https://mp.weixin.qq.com/s/example","title":"验证","content":"环境异常，完成验证后即可继续访问。"}}, ensure_ascii=False).encode()
        with self.assertRaises(ReaderVerificationRequired):
            reader_content(raw, "https://mp.weixin.qq.com/s/example")

    def test_failed_challenge_retains_response_and_gives_manual_import_advice(self):
        async def run(root):
            raw = json.dumps({"code":200,"data":{"url":"https://mp.weixin.qq.com/s/example","title":"验证","content":"环境异常，完成验证后即可继续访问。"}}, ensure_ascii=False).encode()
            async def transport(request):
                return httpx.Response(200, content=raw, request=request)
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                with patch("raghub.ingestion.web.public_url", new=AsyncMock(side_effect=lambda url, hosts:url)):
                    with self.assertRaises(AcquisitionError) as caught:
                        await acquire_wechat_text({"url":"https://mp.weixin.qq.com/s/example"}, root, time.monotonic()+5,
                            lambda:False, AcquisitionError("WEB_PAGE_VERIFICATION_REQUIRED","验证"), client=client)
            self.assertEqual(caught.exception.code, "WECHAT_TEXT_READER_FAILED")
            self.assertIn("需要完成访问验证", str(caught.exception))
            self.assertIn("PDF", str(caught.exception))
            self.assertFalse(caught.exception.retryable)
            self.assertEqual((root/"reader-response.json").read_bytes(), raw)
            self.assertFalse((root/"original.txt").exists())
        with tempfile.TemporaryDirectory(prefix="rag-entry-source-") as directory:
            asyncio.run(run(Path(directory)))


if __name__ == "__main__":
    unittest.main()
