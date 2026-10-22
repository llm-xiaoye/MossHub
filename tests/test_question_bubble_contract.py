"""The question bubble must survive every status-only snapshot and reconnect."""
import unittest
from raghub.chat.router import event_packet

class QuestionBubbleContractTest(unittest.TestCase):
    def test_user_question_is_in_every_snapshot_including_terminal_states(self):
        question="什么是 Skill 的渐进加载？\n请按资料分步骤解释。"
        for state in ("ACCEPTED","RUNNING","CANCELLING","SUCCEEDED","FAILED","CANCELLED","INTERRUPTED"):
            with self.subTest(state=state):
                value={"request_id":"r1","conversation_id":"c1","data_instance_id":"i1",
                       "state":state,"question":question,"error":None,
                       "draft":{"revision":0,"text":"","validated":False},"result":None,
                       "progress":{"stage":"RERANK","completed":4,"total":40}}
                packet=event_packet(value)
                self.assertEqual(packet["question"],question)
                self.assertEqual(packet["request_id"],"r1")
                self.assertEqual(packet["conversation_id"],"c1")
                self.assertEqual(packet["progress"],value["progress"])

    def test_full_question_is_retained_at_input_limit(self):
        value={"request_id":"r2","conversation_id":"c2","data_instance_id":"i1",
               "state":"RUNNING","question":"问"*2000,"error":None,"draft":{},"result":None}
        self.assertEqual(event_packet(value)["question"],value["question"])

    def test_result_only_history_carrier_stays_compatible(self):
        from raghub.ui import light_result
        result={"text":"历史回答","paragraphs":[{"text":"历史回答","support_unit_ids":[],"references":[]}],
                "memory_context":{"private":"not repeated"}}
        packet=light_result(result)
        self.assertEqual(packet["paragraphs"][0]["text"],"历史回答")
        self.assertNotIn("memory_context",packet)

    def test_legacy_carrier_without_question_does_not_invent_one(self):
        packet=event_packet({"request_id":"","conversation_id":"","data_instance_id":"",
                            "state":"SUCCEEDED","error":None,"draft":{},"result":None})
        self.assertNotIn("question",packet)
