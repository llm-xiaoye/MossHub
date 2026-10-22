"""Preserve mixed repository Markdown tables and referenced Word notes."""
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from lxml import etree
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from raghub.docx_source import note_rows,original_rows
from raghub.parsing.formats import convert,field_values

class StructuredDocumentTest(unittest.TestCase):
    def test_markdown_html_keeps_individual_model_table_cells(self):
        tick=chr(96)
        text='<div align="center"><h1>Model catalogue</h1></div>\n\n| Model | Query instruction |\n| --- | --- |\n| BGE-A | '+tick+'Represent this sentence'+tick+' |\n| BGE-B | 中文检索说明 |\n\n'+tick*3+'python\nprint(1)\n'+tick*3+'\n'
        with tempfile.TemporaryDirectory(prefix='rag-md-html-') as root:
            path=Path(root)/'README.md';path.write_text(text);work=Path(root)/'work';work.mkdir()
            parsed=convert(path,{'format':'md'},{},workdir=work)
            self.assertEqual(len(parsed.document.tables),1)
            table=parsed.document.tables[0]
            self.assertEqual((table.data.num_rows,table.data.num_cols),(3,2))
            self.assertEqual([x.text for x in table.data.table_cells],['Model','Query instruction','BGE-A','Represent this sentence','BGE-B','中文检索说明'])
            self.assertTrue(any(x.text=='print(1)' for x in parsed.document.texts))
            self.assertFalse(parsed.quality)
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            for field,value,_ in field_values(parsed.document.model_dump(mode='json')):
                if value:
                    self.assertEqual(parsed.locations[field]['file_sha256'],digest)
                    self.assertEqual(parsed.locations[field]['kind'],'md')

    def test_referenced_word_notes_keep_original_part_and_text(self):
        with tempfile.TemporaryDirectory(prefix='rag-word-notes-') as root:
            path=Path(root)/'notes.docx';base=io.BytesIO();doc=Document();p=doc.add_paragraph('Body text')
            for kind in ['footnote','endnote']:
                ref=OxmlElement('w:'+kind+'Reference');ref.set(qn('w:id'),'2');p.add_run()._r.append(ref)
            doc.save(base)
            with zipfile.ZipFile(base) as archive:members={x.filename:archive.read(x.filename) for x in archive.infolist()}
            content=etree.fromstring(members['[Content_Types].xml'])
            rels=etree.fromstring(members['word/_rels/document.xml.rels'])
            for index,kind in enumerate(['footnote','endnote']):
                part='word/'+kind+'s.xml'
                members[part]=('<w:'+kind+'s xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:'+kind+' w:id="2"><w:p><w:r><w:t xml:space="preserve"> Original '+kind+' 42 </w:t></w:r></w:p></w:'+kind+'></w:'+kind+'s>').encode()
                etree.SubElement(content,'{http://schemas.openxmlformats.org/package/2006/content-types}Override',PartName='/'+part,ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.'+kind+'s+xml')
                etree.SubElement(rels,'{http://schemas.openxmlformats.org/package/2006/relationships}Relationship',Id='rIdNote'+str(index),Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/'+kind+'s',Target=kind+'s.xml')
            members['[Content_Types].xml']=etree.tostring(content);members['word/_rels/document.xml.rels']=etree.tostring(rels)
            with zipfile.ZipFile(path,'w') as archive:
                for name,data in members.items():archive.writestr(name,data)
            rows=note_rows(Document(path));self.assertEqual(len(rows),2)
            self.assertEqual({x['part'] for x in rows},{'word/footnotes.xml','word/endnotes.xml'})
            self.assertTrue(all(x in original_rows(path) for x in rows))
            work=Path(root)/'work';work.mkdir();parsed=convert(path,{'format':'docx'},{},workdir=work)
            for row in rows:
                matches=[item for item in parsed.document.texts if item.text==row['text'].strip()]
                self.assertEqual(len(matches),1)
                loc=parsed.locations[matches[0].self_ref.removeprefix('#')+'/text']
                self.assertEqual(loc['basis'],'ORIGINAL_REFERENCED_WORD_NOTE')
                self.assertEqual(loc['source_group'][0]['part'],row['part'])

if __name__=='__main__':unittest.main()

