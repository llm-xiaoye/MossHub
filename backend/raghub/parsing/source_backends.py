"""Source-node observations collected at the installed Docling backend hooks."""
from __future__ import annotations

import copy
from contextlib import contextmanager


HTML_SOURCE_ATTR = "data-raghub-source-node"


def html_backend(source_nodes, digest, observations):
    from docling.backend.html_backend import HTMLDocumentBackend

    class BoundHTMLBackend(HTMLDocumentBackend):
        @contextmanager
        def _use_hyperlink(self, tag):
            # Navigation remains passive in archived originals. Resolving an href
            # against the local archive can reject legitimate ../ website links.
            previous = self.hyperlink
            self.hyperlink = None
            observations["_hyperlink_policy"] = "ARCHIVE_ONLY_LINK_TARGETS_TEXT_PRESERVED"
            try:
                yield None
            finally:
                self.hyperlink = previous

        def convert(self):
            document=super().convert()
            observations["_document"]=document
            return document

        def _record(self, doc, start_text, start_table, tag):
            node = tag
            while node is not None:
                key = node.get(HTML_SOURCE_ATTR) if hasattr(node, "get") else None
                if key in source_nodes:
                    break
                node = getattr(node, "parent", None)
            if node is None:
                return
            source = source_nodes[key]
            locator = {"kind":"html","file_sha256":digest,"xpath":source["xpath"],
                       "granularity":"REGION","basis":"DOCLING_BACKEND_SOURCE_NODE"}
            from .html_blocks import closure_proof
            new_items = [(item.self_ref.removeprefix("#") + "/text", item.text)
                         for item in doc.texts[start_text:] if item.text]
            proof = closure_proof(source, digest, new_items)
            if proof is not None:
                locator["basis"] = "DOCLING_BACKEND_COMPLETE_INLINE_BLOCK"
                locator["source_group"] = [proof]
            for item in doc.texts[start_text:]:
                field = item.self_ref.removeprefix("#") + "/text"
                item_locator = copy.deepcopy(locator)
                if not item.text:
                    item_locator["basis"] = "DOCLING_BACKEND_SOURCE_NODE"
                    item_locator.pop("source_group", None)
                observations.setdefault(field, {"locator":item_locator,
                    "text":item.text,"node":item,"protected":source["protected"]})
            for table in doc.tables[start_table:]:
                for index, cell in enumerate(table.data.table_cells):
                    field = table.self_ref.removeprefix("#") + "/data/table_cells/" + str(index) + "/text"
                    observations.setdefault(field, {"locator":copy.deepcopy(locator),
                        "text":cell.text,"node":cell,"protected":True})

        def _walk(self, element, doc):
            before = len(doc.texts), len(doc.tables)
            result = super()._walk(element, doc)
            self._record(doc, *before, element)
            return result

        def _handle_block(self, tag, doc):
            before = len(doc.texts), len(doc.tables)
            result = super()._handle_block(tag, doc)
            self._record(doc, *before, tag)
            return result

        def _add_list_item_with_content(self, tag, doc, *args, **kwargs):
            before = len(doc.texts), len(doc.tables)
            result = super()._add_list_item_with_content(tag, doc, *args, **kwargs)
            self._record(doc, *before, tag)
            return result

    return BoundHTMLBackend


def word_backend(digest, observations):
    from docling.backend.msword_backend import MsWordDocumentBackend
    from docx.table import Table, _Cell
    from docx.oxml.simpletypes import ST_Merge
    from docx.oxml.ns import qn

    class BoundWordBackend(MsWordDocumentBackend):
        def _source_part(self, element):
            root=element.getroottree().getroot()
            matches=[part for part in self.docx_obj.part.package.parts
                     if getattr(part,"element",None) is root]
            if len(matches)!=1:
                return None
            return str(matches[0].partname).lstrip("/")

        def convert(self):
            document=super().convert()
            from ..docx_source import note_rows
            from docling_core.types.doc import DocItemLabel
            for row in note_rows(self.docx_obj):
                text=row["text"].strip()
                matches=[item for item in document.texts
                         if item.label==DocItemLabel.FOOTNOTE and item.text==text]
                if len(matches)!=1:
                    continue
                item=matches[0]
                locator={"kind":"docx","file_sha256":digest,"xml_path":row["xml_path"],
                         "granularity":"REGION","basis":"ORIGINAL_REFERENCED_WORD_NOTE",
                         "source_group":[{"kind":"docx","file_sha256":digest,"part":row["part"],
                                          "xml_path":row["xml_path"],"note_id":row["note_id"],
                                          "original_text":row["text"],"normalization":"BOUNDARY_WHITESPACE_TRIM"}]}
                observations[item.self_ref.removeprefix("#")+"/text"]={
                    "locator":locator,"text":item.text,"node":item,"protected":True}
            observations["_document"]=document
            return document

        def _handle_text_elements(self, element, doc, skip_empty_text=False):
            before = {item.self_ref:item.text for item in doc.texts}
            result = super()._handle_text_elements(element, doc, skip_empty_text)
            part_name=self._source_part(element)
            if part_name is None:
                for item in doc.texts:
                    if before.get(item.self_ref)!=item.text:
                        observations.pop(item.self_ref.removeprefix("#")+"/text",None)
                return result
            locator = {"kind":"docx","file_sha256":digest,
                       "xml_path":element.getroottree().getpath(element),
                       "granularity":"REGION","basis":"DOCLING_BACKEND_XML_NODE",
                       "source_group":[{"kind":"docx","file_sha256":digest,"part":part_name,
                                        "xml_path":element.getroottree().getpath(element)}]}
            for item in doc.texts:
                if before.get(item.self_ref) == item.text:
                    continue
                field = item.self_ref.removeprefix("#") + "/text"
                previous = observations.get(field)
                current = copy.deepcopy(locator)
                if item.self_ref in before:
                    if previous is None or not item.text.startswith(before[item.self_ref]):
                        observations.pop(field,None)
                        continue
                    group = previous["locator"].get("source_group") or [previous["locator"]]
                    current["source_group"] = copy.deepcopy(group) + copy.deepcopy(locator["source_group"])
                    current["basis"] = "DOCLING_BACKEND_XML_SOURCE_GROUP"
                observations[field] = {"locator":current,"text":item.text,"node":item,"protected":True}
            return result

        def _handle_tables(self, element, doc):
            source_table = Table(element, self.docx_obj)
            part_name=self._source_part(element)
            root=element.getroottree().getroot()
            single_layout_row=(root.tag in {qn("w:hdr"),qn("w:ftr")}
                               and len(source_table.rows)==1
                               and not source_table.rows[0]._tr.xpath("./w:trPr/w:tblHeader"))
            expected = {}
            open_cells = {}
            for row_index, row in enumerate(source_table.rows):
                column = row.grid_cols_before
                for tc in self._row_cells(row._tr):
                    if column >= len(source_table.columns):
                        break
                    if tc.vMerge == ST_Merge.CONTINUE and column in open_cells:
                        entry = open_cells[column]
                        entry["width_consistent"] = entry["width_consistent"] and entry["col_span"]==tc.grid_span
                        entry["row_span"] += 1
                        entry["source_group"].append({"xml_path":tc.getroottree().getpath(tc)})
                    else:
                        entry = {"text":_Cell(tc,source_table).text,
                                 "xml_path":tc.getroottree().getpath(tc),
                                 "row_span":1,"col_span":tc.grid_span,"width_consistent":True,
                                 "source_group":[{"xml_path":tc.getroottree().getpath(tc)}]}
                        expected[(row_index,column)] = entry
                        open_cells[column] = entry
                    column += tc.grid_span
            result = super()._handle_tables(element, doc)
            if part_name is None:
                return result
            for ref in result:
                table = ref.resolve(doc)
                if not hasattr(table, "data") or not hasattr(table.data, "table_cells"):
                    continue
                if (table.data.num_rows,table.data.num_cols) != (len(source_table.rows),len(source_table.columns)):
                    continue
                bound_count=0
                for index, cell in enumerate(table.data.table_cells):
                    position = cell.start_row_offset_idx,cell.start_col_offset_idx
                    source = expected.get(position)
                    if source is None or not source["width_consistent"] or (cell.text,cell.row_span,cell.col_span) != (source["text"],source["row_span"],source["col_span"]):
                        continue
                    locator = {"kind":"docx","file_sha256":digest,"xml_path":source["xml_path"],
                               "granularity":"REGION","basis":"DOCLING_BACKEND_XML_TABLE_GRID",
                               "source_group":[{"kind":"docx","file_sha256":digest,"part":part_name,**part} for part in source["source_group"]]}
                    field = table.self_ref.removeprefix("#") + "/data/table_cells/" + str(index) + "/text"
                    observations[field] = {"locator":locator,"text":cell.text,"node":cell,"protected":True}
                    bound_count+=1
                cells=table.data.table_cells
                if single_layout_row and cells and bound_count==len(cells) and all(cell.column_header for cell in cells):
                    # A fully bound one-row header/footer layout is retained as
                    # ordinary content. Body tables and explicit tblHeader are
                    # deliberately outside this rule.
                    for cell in cells:
                        cell.column_header=False
                    observations.setdefault("_docx_layout_tables",[]).append({
                        "table_ref":table.self_ref,"part":part_name,"cell_count":len(cells),
                        "rule":"BOUND_HEADER_FOOTER_SINGLE_ROW_WITHOUT_TBLHEADER"})
            return result

    return BoundWordBackend


def pdf_reading_order(digest, source_page, observations, *, restore_formulas=False):
    """Bind spans at the installed component's explicit text transformations."""
    import hashlib
    from docling_core.types.doc import DocItemLabel, ProvenanceItem
    from docling.models.postprocessing.list_marker_processor import ListItemMarkerProcessor
    from docling.models.stages.reading_order.readingorder_model import ReadingOrderModel

    def text_digest(text):
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def snapshot(item):
        return [part.model_dump(mode='json') for part in item.prov]

    def mapped(parts, length, left, right):
        if not 0<=left<=right<=length:
            raise RuntimeError('PDF native substring transform outside source text')
        retained=[]; excluded=[]
        for part in parts:
            start,end=part['charspan']
            if type(start) is not int or type(end) is not int or not 0<=start<=end<=length:
                raise RuntimeError('PDF pre-transform provenance outside its text basis')
            a,b=max(start,left),min(end,right)
            if a<b:
                retained.append(ProvenanceItem.model_validate({**part,'charspan':[a-left,b-left]}))
            else:
                excluded.append(part)
        return retained,excluded

    def record(item, operation, before, after, **details):
        observations.setdefault('transforms',[]).append({
            'operation':operation,'file_sha256':digest,'source_page':source_page,
            'item_ref_before_concatenation':item.self_ref,
            'before_text_length':len(before),'after_text_length':len(after),
            'before_text_sha256':text_digest(before),'after_text_sha256':text_digest(after),
            'text_hash_encoding':'UTF-8','span_index_unit':'PYTHON_UNICODE_CODE_POINT',
            'bbox_precision':'COMPONENT_REGION',**details})

    class BoundListMarker(ListItemMarkerProcessor):
        def process_list_item(self, item):
            before,original=item.text,item.orig
            parts=snapshot(item)
            match=self._find_match(text=original,patterns=self._compiled_bullet_item_patterns)
            if match is None:
                match=self._find_match(text=original,patterns=self._compiled_numbered_item_patterns)
            result=super().process_list_item(item)
            if item.text!=before:
                if match is None or before!=original or item.text!=original[match.start(2):match.end(2)]:
                    raise RuntimeError('PDF list transformation differs from the bound component substring')
                item.prov,excluded=mapped(parts,len(before),match.start(2),match.end(2))
                record(item,'EXACT_LIST_MARKER_SUBSTRING',before,item.text,
                       original_text=original,kept_input_interval=[match.start(2),match.end(2)],
                       before_provenance=parts,after_provenance=snapshot(item),
                       non_text_provenance=excluded)
            return result

    class BoundReadingOrder(ReadingOrderModel):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.list_item_processor=BoundListMarker()

        def _handle_text_element(self,element,**kwargs):
            item,current_list=super()._handle_text_element(element,**kwargs)
            if restore_formulas and item.label==DocItemLabel.FORMULA and item.text=='' and item.orig:
                parts=snapshot(item)
                mapped(parts,len(item.orig),0,len(item.orig))
                before=item.text
                item.text=item.orig
                record(item,'EMPTY_FORMULA_FROM_COMPONENT_ORIG',before,item.text,
                       original_component_text=before,source_native_field=item.self_ref+'/orig',
                       target_native_field=item.self_ref+'/text',original_text=item.orig,
                       before_provenance=parts,after_provenance=snapshot(item),
                       basis='ORIGINAL_EXTRACTED_TEXT_NOT_FORMULA_ENRICHMENT')
            return item,current_list

        def _merge_elements(self,element,merged_elem,new_item,page_height):
            before=new_item.text
            incoming=merged_elem.text
            parts=snapshot(new_item)
            mapped(parts,len(before),0,len(before))
            continuation=incoming.lstrip()
            deleted=before.endswith('\u00ad') or (before.endswith('-') and continuation and continuation[0].islower())
            expected=before[:-1]+incoming if deleted else before+' '+incoming
            super()._merge_elements(element,merged_elem,new_item,page_height)
            produced=snapshot(new_item)
            if new_item.text!=expected or len(produced)!=len(parts)+1 or produced[:-1]!=parts:
                raise RuntimeError('PDF merge differs from the bound component transformation')
            left=len(before)-1 if deleted else len(before)+1
            retained,excluded=mapped(parts,len(before),0,len(before)-1 if deleted else len(before))
            continuation_part={**produced[-1],'charspan':[left,left+len(incoming)]}
            if incoming:
                retained.append(ProvenanceItem.model_validate(continuation_part))
            else:
                excluded.append(produced[-1])
            new_item.prov=retained
            record(new_item,'DELETE_TRAILING_HYPHEN_AND_CONCAT' if deleted else 'SPACE_AND_CONCAT',
                   before,new_item.text,incoming_text=incoming,deleted_text=before[-1:] if deleted else '',
                   before_provenance=parts,component_postmerge_provenance=produced,after_provenance=snapshot(new_item),
                   non_text_provenance=excluded,continuation_output_interval=[left,left+len(incoming)])

        def __call__(self,conv_res):
            document=super().__call__(conv_res)
            observations['text_bindings']={item.self_ref:{'text':item.text,'orig':item.orig,'provenance':snapshot(item)}
                                           for item in document.texts}
            return document

    return BoundReadingOrder


def pdf_pipeline(digest, source_page, observations):
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

    class BoundPdfPipeline(StandardPdfPipeline):
        def __init__(self,pipeline_options):
            super().__init__(pipeline_options)
            model=pdf_reading_order(digest,source_page,observations,
                                    restore_formulas=not self.pipeline_options.do_formula_enrichment)
            self.reading_order_model=model(options=self.reading_order_model.options)

    return BoundPdfPipeline


def verify_pdf_text_bindings(document, observations):
    expected=observations.get('text_bindings')
    actual={item.self_ref:{'text':item.text,'orig':item.orig,
                           'provenance':[part.model_dump(mode='json') for part in item.prov]} for item in document.texts}
    if not isinstance(expected,dict) or actual!=expected:
        raise RuntimeError('PDF native text changed after its provenance transformation')
    return len(actual)
