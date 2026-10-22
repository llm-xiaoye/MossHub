import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from docling_core.types.doc import DoclingDocument
from raghub.parsing.bundle import review_plan, apply_reviews
from raghub.parsing.formats import Parsed, append_bound_resources
from raghub.parsing.router import RevisionPreview, OCRDecision


class GraphicReviewTest(unittest.TestCase):
    def case(self):
        digest='a'*64
        locator={'kind':'md','file_sha256':'b'*64,'basis':'PROJECT_FORMAT_ADAPTER',
                 'granularity':'REGION','line_start':3,'line_end':3,
                 'resource_sha256':digest,'region':[[20,30],[80,30],[80,45],[20,45]],
                 'transform':{'original_size':[200,100]}}
        base={'texts':[{'text':'BGE','label':'text'}],'tables':[]}
        revised=copy.deepcopy(base);revised['texts'][0]['text']='BGE。图示经核对：Evaluation 包含 MTEB、BEIR。'
        issue={'issue_id':'c'*64,'code':'BITMAP_STRUCTURE_REVIEW_REQUIRED','resource_sha256':digest,'locator':locator}
        supplement=NS(quality=[issue],fields=[NS(field_pointer='/texts/0/text',locator=NS(model_dump=lambda **kw:copy.deepcopy(locator)))],
                      images=[{'resource_sha256':digest,'width':200,'height':100}],
                      raw_version_id='raw',processing_version_id='processing',original_identity={'sha256':'b'*64})
        review={'issue_id':issue['issue_id'],'outcome':'RESOURCE_TRANSCRIBED','field_pointer':'/texts/0/text','note':'用户授权，已对照整张归档原图核对。'}
        return base,revised,supplement,review

    def test_transcription_binds_full_saved_graphic_and_exact_accepted_text(self):
        base,revised,supplement,review=self.case()
        bound=review_plan(base,revised,supplement,'d'*64,[review])[0]['binding']
        self.assertEqual(bound['locator']['region'],[[0,0],[200,0],[200,100],[0,100]])
        self.assertEqual(bound['locator']['basis'],'HUMAN_VERIFIED_RESOURCE_TRANSCRIPTION')
        self.assertEqual(bound['after_text'],revised['texts'][0]['text'])
        self.assertEqual(bound['resource_sha256'],'a'*64)
        self.assertEqual(base['texts'][0]['text'],'BGE')
        self.assertEqual(supplement.fields[0].locator.model_dump()['region'][0],[20,30])

    def test_wrong_graphic_field_is_rejected(self):
        base,revised,supplement,review=self.case();supplement.images[0]['resource_sha256']='e'*64
        with self.assertRaises(ValueError):review_plan(base,revised,supplement,'d'*64,[review])

    def test_unchanged_or_unknown_transcription_field_is_rejected(self):
        base,revised,supplement,review=self.case()
        with self.assertRaises(ValueError):review_plan(base,base,supplement,'d'*64,[review])
        review['field_pointer']='/texts/99/text'
        with self.assertRaises(ValueError):review_plan(base,revised,supplement,'d'*64,[review])

    def test_missing_transcription_field_is_rejected(self):
        base,revised,supplement,review=self.case();review.pop('field_pointer')
        with self.assertRaises(ValueError):review_plan(base,revised,supplement,'d'*64,[review])

    def test_transcription_cannot_clear_system_mapping_issue(self):
        base,revised,supplement,review=self.case();supplement.quality[0]['code']='COARSE_SOURCE_MAPPING'
        with self.assertRaises(ValueError):review_plan(base,revised,supplement,'d'*64,[review])

    def test_transcription_resolution_invalidates_when_bound_text_changes(self):
        base,revised,supplement,review=self.case()
        bound=review_plan(base,revised,supplement,'d'*64,[review])
        quality=apply_reviews(supplement.quality,bound,revised,{'source':'CHATGPT_USER_MESSAGE'})
        self.assertIn('resolution',quality[0])
        self.assertIn('resolution',apply_reviews(quality,[],revised,{} )[0])
        revised['texts'][0]['text']='different'
        invalidated=apply_reviews(quality,[],revised,{})[0]
        self.assertNotIn('resolution',invalidated)
        self.assertEqual(invalidated['review_history'][-1]['invalidated'],'REVIEWED_FIELD_CHANGED')

    def exclusion_case(self):
        base,revised,supplement,review=self.case()
        base["texts"].append({"text":"7.6 t/s","label":"text"})
        revised=copy.deepcopy(base)
        for item in revised["texts"]:item["text"]=""
        source=copy.copy(supplement.fields[0]);source.field_pointer="/texts/1/text"
        supplement.fields.append(source)
        supplement.quality.extend([
            {"issue_id":"e"*64,"code":"OCR_NUMERIC_REVIEW","field":"/texts/1/text"},
            {"issue_id":"f"*64,"code":"COARSE_SOURCE_MAPPING","field":"/texts/1/text"},
            {"issue_id":"0"*64,"code":"OCR_REVIEW_CANDIDATE","field":"/texts/99/text"}])
        review={"issue_id":review["issue_id"],"outcome":"RESOURCE_EXCLUDED","note":"用户明确要求 GIF 只供查看，整段不用于问答。"}
        return base,revised,supplement,review

    def test_resource_exclusion_requires_all_saved_resource_fields_empty(self):
        base,revised,supplement,review=self.exclusion_case()
        revised["texts"][1]["text"]="7.6 t/s"
        with self.assertRaisesRegex(ValueError,"every bound text field"):
            review_plan(base,revised,supplement,"d"*64,[review])

    def test_resource_exclusion_resolves_only_its_ocr_as_scope_not_verification(self):
        base,revised,supplement,review=self.exclusion_case()
        bound=review_plan(base,revised,supplement,"d"*64,[review])
        result=apply_reviews(supplement.quality,bound,revised,{"source":"CHATGPT_USER_MESSAGE"})
        self.assertEqual(result[0]["resolution"]["binding"]["excluded_fields"],["/texts/0/text","/texts/1/text"])
        self.assertEqual(result[1]["resolution"]["outcome"],"RESOURCE_EXCLUDED")
        self.assertEqual(result[1]["resolution"]["binding"]["scope_review_issue_id"],review["issue_id"])
        self.assertNotIn("excluded_fields",result[1]["resolution"]["binding"])
        self.assertNotIn("resolution",result[2])
        self.assertNotIn("resolution",result[3])
        self.assertEqual(base["texts"][1]["text"],"7.6 t/s")
        self.assertEqual(supplement.images[0]["resource_sha256"],"a"*64)
        self.assertNotIn("resolution",supplement.quality[1])

    def test_restored_resource_text_invalidates_entire_exclusion_scope(self):
        base,revised,supplement,review=self.exclusion_case()
        bound=review_plan(base,revised,supplement,"d"*64,[review])
        result=apply_reviews(supplement.quality,bound,revised,{})
        self.assertIn("resolution",apply_reviews(result,[],revised,{})[0])
        revised["texts"][0]["text"]="restored phone content"
        invalidated=apply_reviews(result,[],revised,{})
        self.assertNotIn("resolution",invalidated[0])
        self.assertNotIn("resolution",invalidated[1])

    def test_http_accepts_explicit_resource_scope_exclusion(self):
        from raghub.parsing.router import IssueReview
        record={"issue_id":"a"*64,"outcome":"RESOURCE_EXCLUDED","note":"view only"}
        self.assertEqual(IssueReview.model_validate(record).model_dump(exclude_none=True),record)

    def test_old_review_http_shapes_do_not_gain_null_field_keys(self):
        record={'issue_id':'a'*64,'outcome':'VERIFIED_CORRECT','note':'checked'}
        preview=RevisionPreview.model_validate({'document_id':'072f1a26-3084-4681-83c7-75b6e393490a',
            'base_processing_version_id':'9a2704d6-2b2e-43c6-b35c-850ac9ffa6f6','full_native_json':{},'reviews':[record]})
        self.assertEqual(preview.reviews[0].model_dump(exclude_none=True),record)
        decision=OCRDecision.model_validate({'decision':'ACCEPT','source':'LOCAL_USER_UI','note':'checked','reviews':[record]})
        self.assertEqual(decision.model_dump(exclude_none=True)['reviews'],[record])

    def test_repeated_markdown_image_keeps_every_reference_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);note=root/'original.md';image=root/'icon.svg'
            note.write_text('<img src="./icon.svg">\ntext\n<img src="./icon.svg">',encoding='utf-8')
            image.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10"><text x="1" y="8">Icon</text></svg>')
            digest=hashlib.sha256(image.read_bytes()).hexdigest()
            parsed=Parsed(DoclingDocument(name='repeated'),scope={})
            config={'linked_resources':[{'archive_manifest_path':str(root/'manifest.json'),'archive_manifest_sha256':'a'*64,'member':image.name,'sha256':digest,'reference':'./icon.svg'}]}
            with patch('raghub.ingestion.archive.verify_archive',return_value={'manifest_sha256':'a'*64}):
                append_bound_resources(parsed,note,'md',config,{}, {},workdir=root)
            self.assertEqual([item.text for item in parsed.document.texts],['Icon'])
            loc=parsed.locations['/texts/0/text']
            self.assertEqual((loc['line_start'],loc['line_end']),(1,3))
            self.assertEqual(loc['source_group'][0]['line_numbers'],[1,3])
            self.assertFalse(any(q['code']=='MARKDOWN_IMAGE_LOCATION_AMBIGUOUS' for q in parsed.quality))


if __name__=='__main__':unittest.main()

