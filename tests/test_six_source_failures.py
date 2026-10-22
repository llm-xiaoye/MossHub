"""Observed acquisition failures: preserve the security boundary and useful state."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from raghub.douyin_intake import receipt
from raghub.ingestion.web import AcquisitionError
from raghub.ingestion.wechat_reader import acquire_wechat_text

class SourceFailureTest(unittest.TestCase):
    def test_reader_dns_policy_rejection_preserves_cause_and_sends_no_http(self):
        async def scenario(root, cause):
            sent=[]
            async def transport(request):
                sent.append(request)
                raise AssertionError('DNS-rejected request must never be sent')
            async def gate(url, hosts):
                if hosts==['r.jina.ai']:raise cause
                return url
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                with patch('raghub.ingestion.web.public_url',new=AsyncMock(side_effect=gate)):
                    with self.assertRaises(AcquisitionError) as caught:
                        await acquire_wechat_text({'url':'https://mp.weixin.qq.com/s/test'},root,time.monotonic()+10,
                            lambda:False,AcquisitionError('WEB_PAGE_VERIFICATION_REQUIRED','verification'),client=client)
            error=caught.exception
            self.assertEqual(error.code,'WECHAT_TEXT_READER_FAILED')
            self.assertFalse(error.retryable)
            self.assertFalse(error.details['reader_http_sent'])
            self.assertEqual(error.details['reader_stage'],'PUBLIC_ADDRESS_CHECK')
            self.assertIn('PDF',str(error));self.assertIn('保留',str(error));self.assertIn('DNS',str(error))
            self.assertEqual(sent,[])
            self.assertFalse((root/'original.txt').exists())
            return error
        with tempfile.TemporaryDirectory() as d:
            cause=AcquisitionError('URL_NONPUBLIC_DNS','reject',details={'host':'r.jina.ai','addresses':['2001::6810:fc37']})
            error=asyncio.run(scenario(Path(d),cause))
            self.assertEqual(error.details['reader_code'],cause.code)
            self.assertEqual(error.details['reader_details'],cause.details)
        with tempfile.TemporaryDirectory() as d:
            error=asyncio.run(scenario(Path(d),OSError('name resolution failed')))
            self.assertEqual(error.details['reader_error_type'],'OSError')

    def test_failed_video_does_not_still_claim_to_be_processing(self):
        db=Mock();db.execute.return_value.fetchone.return_value=None
        row={'status':'FAILED','stage':'DY_PREPARING','last_error_json':json.dumps({'code':'DY_MATERIAL_UNAVAILABLE','message':'完整音轨未取得；记录保留'})}
        with patch('raghub.douyin_intake._payload',return_value=(row,{'kb_id':'kb','url':'https://v.douyin.com/test/'})):
            value=receipt(db,Mock(),'instance','task',Path('/unused'))
        self.assertEqual(value['status'],'FAILED');self.assertEqual(value['progress'],'处理已停止')
        self.assertFalse(value['saved']);self.assertFalse(value['result_unknown'])
        self.assertIn('音轨',value['message'])

if __name__=='__main__':unittest.main()
