"""HierarchicalChunker plus source-preserving parent/child and complete-row views."""
from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from importlib.metadata import version

from docling_core.transforms.chunker.hierarchical_chunker import HierarchicalChunker, ChunkingSerializerProvider
from docling_core.types.doc import ContentLayer, DoclingDocument, PictureItem, SectionHeaderItem, TableItem, TextItem, TitleItem

from ..model_profiles import canonical, identity, text_sha256, token_count
from ..parsing.bundle import verify_bundle
from ..parsing.contracts import Supplement
from ..ingestion.archive import hash_file

TABLE_INTRO_POLICY = "MD_ADJACENT_EXPLICIT_UNIT_INTRO_V2"

RULES = {
    "schema_version": 5, "hierarchical_merge_list_items": False,
    "table_intro_context": TABLE_INTRO_POLICY,
    "list_children": "SOURCE_LIST_ITEM_BOUNDARY_PROSE_GROUP_V1",
    "html_inline_blocks": "COMPLETE_DOM_BLOCK_BEFORE_INLINE_CODE_SPLIT_V1",
    "child_tokens": 512, "overlap_tokens": 64, "parent_tokens": 1536,
    "indivisible_tokens": 1024, "table_view": "SOURCE_COLUMN_PATH_WITH_LEADING_CONTEXT_V2",
    "text_utf8_max": 65535,
}


SOURCE_CONTENT_LAYERS = frozenset({ContentLayer.BODY, ContentLayer.FURNITURE})


def source_items(document, *, with_groups=False, root=None):
    return document.iterate_items(
        root=root,
        with_groups=with_groups, traverse_pictures=True,
        included_content_layers=set(SOURCE_CONTENT_LAYERS),
    )


class BoundSourceSerializerProvider(ChunkingSerializerProvider):
    def get_serializer(self, doc):
        serializer = super().get_serializer(doc=doc)
        serializer.params = serializer.params.model_copy(update={
            "traverse_pictures": True, "layers": set(SOURCE_CONTENT_LAYERS),
        })
        return serializer


class ChunkingUnsupported(ValueError):
    code = "CHUNKING_UNSUPPORTED"


@dataclass
class Stream:
    text: str
    pieces: list

    @classmethod
    def empty(cls):
        return cls("", [])

    def construct(self, text):
        if text:
            begin = len(self.text)
            self.text += text
            self.pieces.append({"start": begin, "end": len(self.text), "constructed": True})

    def field(self, field, start=0, end=None, purpose="BODY"):
        end = len(field.cleaned_text) if end is None else end
        if not 0 <= start <= end <= len(field.cleaned_text):
            raise ValueError("field range outside verified source")
        begin = len(self.text)
        self.text += field.cleaned_text[start:end]
        self.pieces.append({
            "start": begin, "end": len(self.text), "constructed": False,
            "field_pointer": field.field_pointer, "cleaned_start": start,
            "cleaned_end": end, "purpose": purpose,
        })

    def append(self, other):
        offset = len(self.text)
        self.text += other.text
        self.pieces.extend({**part, "start": part["start"] + offset,
                            "end": part["end"] + offset} for part in other.pieces)

    def slice(self, start, end):
        result = Stream.empty()
        result.text = self.text[start:end]
        for piece in self.pieces:
            left, right = max(start, piece["start"]), min(end, piece["end"])
            if right <= left:
                if piece["start"] == piece["end"] and (start <= piece["start"] < end or piece["start"] == end == len(self.text)):
                    result.pieces.append({**piece, "start": piece["start"] - start, "end": piece["end"] - start})
                continue
            part = {**piece, "start": left - start, "end": right - start}
            if not piece["constructed"]:
                part["cleaned_start"] = piece["cleaned_start"] + left - piece["start"]
                part["cleaned_end"] = piece["cleaned_start"] + right - piece["start"]
            result.pieces.append(part)
        return result


def joined(streams, delimiter="\n"):
    result = Stream.empty()
    for i, stream in enumerate(streams):
        if i:
            result.construct(delimiter)
        result.append(stream)
    return result


def field_stream(field, purpose="BODY"):
    result = Stream.empty()
    result.field(field, purpose=purpose)
    return result


def retrieval(context, body):
    return joined([context, body]) if context.text else body


def fits(tokenizer, stream, limit):
    return token_count(tokenizer, stream.text) <= limit and len(stream.text.encode("utf-8")) <= 65535


def span_end(tokenizer, text, start, end, limit, context):
    """All returned windows are measured on the complete actual serialized input."""
    if fits(tokenizer, retrieval(context, text.slice(start, end)), limit):
        return end
    left, right, best = start + 1, end - 1, None
    while left <= right:
        middle = (left + right) // 2
        if fits(tokenizer, retrieval(context, text.slice(start, middle)), limit):
            best, left = middle, middle + 1
        else:
            right = middle - 1
    if best is None and fits(tokenizer, retrieval(context, text.slice(start, start + 1)), limit):
        best = start + 1
    if best is None:
        raise ChunkingUnsupported("required context or minimal text exceeds the budget")
    return best


def ordinary_plans(tokenizer, body, context):
    """Parent bodies never contain the child overlap more than once."""
    parent_start = 0
    while parent_start < len(body.text):
        parent_end = span_end(tokenizer, body, parent_start, len(body.text), 1536, context)
        children = []
        covered, previous_start = parent_start, None
        while covered < parent_end:
            start = covered
            if previous_start is not None:
                left, right = previous_start + 1, covered - 1
                while left <= right:
                    middle = (left + right) // 2
                    if token_count(tokenizer, body.text[middle:covered]) <= 64:
                        start, right = middle, middle - 1
                    else:
                        left = middle + 1
            try:
                end = span_end(tokenizer, body, start, parent_end, 512, context)
            except ChunkingUnsupported:
                start = covered
                end = span_end(tokenizer, body, start, parent_end, 512, context)
            if end <= covered:
                start = covered
                end = span_end(tokenizer, body, start, parent_end, 512, context)
            children.append((body.slice(start, end), 512))
            covered, previous_start = end, start
        yield body.slice(parent_start, parent_end), children
        parent_start = parent_end


def structural_plain_plans(tokenizer, streams, context):
    """Keep source list items separate for retrieval, with shared parent context."""
    if not streams:
        return
    if not any(is_list for _stream, is_list in streams):
        yield from ordinary_plans(tokenizer, joined([stream for stream, _flag in streams]), context)
        return

    units, prose = [], []
    for stream, is_list in streams:
        if is_list:
            if prose:
                units.append(joined(prose))
                prose = []
            units.append(stream)
        else:
            prose.append(stream)
    if prose:
        units.append(joined(prose))

    body, ranges, offset = joined(units), [], 0
    for index, unit in enumerate(units):
        if index:
            offset += 1  # joined() inserts this constructed newline.
        ranges.append((offset, offset + len(unit.text)))
        offset += len(unit.text)
    unit_ends = {end for _start, end in ranges}
    parent_start = 0
    while parent_start < len(body.text):
        budget_end = span_end(tokenizer, body, parent_start, len(body.text), 1536, context)
        complete_ends = [end for _start, end in ranges if parent_start < end <= budget_end]
        # A source unit that fits alone moves intact to the next parent.
        # Only an oversized unit uses the existing bounded-window fallback.
        parent_end = complete_ends[-1] if complete_ends else budget_end
        children = []
        for unit_start, unit_end in ranges:
            left, right = max(parent_start, unit_start), min(parent_end, unit_end)
            if left >= right:
                continue
            # Overlap is confined to this unit and this parent window.
            for _unit_parent, unit_children in ordinary_plans(tokenizer, body.slice(left, right), context):
                children.extend(unit_children)
        if not children:
            raise ChunkingUnsupported("structural parent contains no source body")
        yield body.slice(parent_start, parent_end), children
        parent_start = parent_end + (1 if parent_end in unit_ends and parent_end < len(body.text) else 0)


def pack_units(tokenizer, units, context, limit, single_limit, *, delimiter="\n", extended_allowed=None):
    """Return exact unit ownership; text content never determines membership."""
    first = 0
    while first < len(units):
        single = units[first]
        if not fits(tokenizer, retrieval(context, single), limit):
            can_extend = single_limit > limit and (extended_allowed is None or extended_allowed[first])
            if not can_extend or not fits(tokenizer, retrieval(context, single), single_limit):
                raise ChunkingUnsupported("one indivisible source unit exceeds its permitted budget")
            yield first, first + 1, single, single_limit
            first += 1
            continue
        stop, best = first + 1, single
        while stop < len(units):
            candidate = joined(units[first:stop + 1], delimiter)
            if not fits(tokenizer, retrieval(context, candidate), limit):
                break
            best, stop = candidate, stop + 1
        yield first, stop, best, limit
        first = stop


def bound_parts(stream, fields):
    parts = []
    for piece in stream.pieces:
        if piece["constructed"]:
            parts.append(dict(piece))
            continue
        field = fields[piece["field_pointer"]]
        indices = [i for i, trace in enumerate(field.mappings)
                   if trace.output_start < piece["cleaned_end"]
                   and trace.output_end > piece["cleaned_start"]]
        parts.append({
            **piece, "locator": field.locator.model_dump(mode="json"),
            "mapping_indices": indices,
            # Consumers take the original bound intervals, never scale by length.
            "mapping_precision": sorted({field.mappings[i].precision for i in indices}),
        })
    return parts


def heading_context(refs, fields):
    result = Stream.empty()
    if refs:
        result.construct("标题：")
        for i, ref in enumerate(refs):
            if i:
                result.construct(" / ")
            result.field(fields[ref.replace("#", "") + "/text"], purpose="HEADING")
    return result


def reference_fields(item, field_map):
    refs = []
    for role in ("captions", "footnotes"):
        for reference in getattr(item, role, []):
            pointer = reference.cref.replace("#", "") + "/text"
            if pointer not in field_map:
                raise ChunkingUnsupported("caption/footnote has no bound source field")
            refs.append((pointer, role.upper()))
    return refs


def table_units(item, fields):
    """Use real cell spans; duplicate context is labelled and source-bound."""
    base = item.self_ref.replace("#", "") + "/data/table_cells/"
    cells = [(cell, fields[base + str(i) + "/text"]) for i, cell in enumerate(item.data.table_cells)]
    rows, cols = item.data.num_rows, item.data.num_cols
    if rows <= 0 or cols <= 0:
        raise ChunkingUnsupported("table has no rectangular source structure")
    headers = [(cell, field) for cell, field in cells if cell.column_header]
    header_end = max((cell.end_row_offset_idx for cell, _field in headers), default=0)
    if header_end >= rows:
        # A header-only table still has source content. Keep its cells and
        # coordinates without inventing data rows or header/value pairs.
        stream = Stream.empty()
        stream.construct("表格原文（无数据行）：")
        for index, (cell, field) in enumerate(cells):
            if index:
                stream.construct("；")
            stream.construct("第" + str(cell.start_row_offset_idx + 1) + "行，第"
                             + str(cell.start_col_offset_idx + 1) + "列")
            stream.construct("（表头）：" if cell.column_header else "（前置内容）：")
            stream.field(field, purpose="HEADER" if cell.column_header else "TABLE_LEADING_CONTEXT")
            if not field.cleaned_text:
                stream.construct("（空白）")
        return [stream], [(0, rows)], header_end
    # Preserve every cell above the body boundary, including cells the component
    # did not label as headers. Their structural role is leading context; do not
    # alter source flags or silently discard a mixed header row.
    leading = [(cell, field) for cell, field in cells
               if not cell.column_header and cell.end_row_offset_idx <= header_end]
    # A row-spanning merge defines an indivisible row group.
    groups, row_ranges, first = [], [], header_end
    while first < rows:
        end = first + 1
        changed = True
        while changed:
            changed = False
            for cell, _field in cells:
                if not cell.column_header and cell.start_row_offset_idx < end and cell.end_row_offset_idx > first:
                    if cell.end_row_offset_idx > end:
                        end, changed = cell.end_row_offset_idx, True
        streams = []
        for row in range(first, end):
            stream = Stream.empty()
            stream.construct("第" + str(row + 1) + "行：")
            for col in range(cols):
                if col:
                    stream.construct("；")
                path = [(cell, field) for cell, field in headers + leading
                        if cell.start_col_offset_idx <= col < cell.end_col_offset_idx]
                path.sort(key=lambda pair: (pair[0].start_row_offset_idx, pair[0].start_col_offset_idx))
                if path:
                    for i, (context_cell, field) in enumerate(path):
                        if i:
                            stream.construct("／")
                        stream.field(field, purpose="HEADER" if context_cell.column_header else "TABLE_LEADING_CONTEXT")
                else:
                    stream.construct("列" + str(col + 1))
                stream.construct("=")
                found = [(cell, field) for cell, field in cells if not cell.column_header
                         and cell.start_row_offset_idx <= row < cell.end_row_offset_idx
                         and cell.start_col_offset_idx <= col < cell.end_col_offset_idx]
                if len(found) > 1:
                    raise ChunkingUnsupported("overlapping source cells")
                if found:
                    cell, field = found[0]
                    purpose = "MERGE_CONTEXT" if (cell.row_span > 1 or cell.col_span > 1) else "BODY"
                    stream.field(field, purpose=purpose)
                    if not field.cleaned_text:
                        stream.construct("（空白）")
                else:
                    stream.construct("（无原始单元格）")
            streams.append(stream)
        groups.append(joined(streams))
        row_ranges.append((first, end))
        first = end
    return groups, row_ranges, header_end



_EXPLICIT_UNIT_INTRO = re.compile(
    r"(?P<intro>\bThe\s+(?:table\s+below|following\s+table)\b[^\n:]{0,300}"
    r"\(in\s+(?:thousands|millions|billions)\)\s*:)\s*$", re.IGNORECASE,
)


def explicit_unit_intro(field):
    """Keep the full bound field when its terminal statement introduces a scale.

    Earlier qualifications cannot safely be discarded just to shorten context.
    """
    if field.locator.kind != "md":
        return None
    match = _EXPLICIT_UNIT_INTRO.search(field.cleaned_text)
    if match is None:
        return None
    mentions = re.findall(r"\b(?:table\s+below|following\s+table)\b",
                          field.cleaned_text, re.IGNORECASE)
    if len(mentions) != 1:
        return None
    return 0, len(field.cleaned_text)


def explicit_table_intros(document, fields, scopes):
    """Bind only an immediately preceding explicit intro with the same parent/scope.

    Adjacency alone, a nearby unit, or an unsupported source format is insufficient.
    The returned Unicode intervals are copied with their actual field mappings.
    """
    result, previous = {}, None
    for item, _level in source_items(document):
        if (isinstance(item, TableItem) and isinstance(previous, TextItem)
                and not isinstance(previous, (TitleItem, SectionHeaderItem))):
            pointer = previous.self_ref.replace("#", "") + "/text"
            field = fields.get(pointer)
            same_scope = (previous.self_ref in scopes and item.self_ref in scopes
                          and scopes[previous.self_ref] == scopes[item.self_ref])
            previous_parent = getattr(previous.parent, "cref", None)
            current_parent = getattr(item.parent, "cref", None)
            same_parent = (isinstance(previous_parent, str)
                           and previous_parent == current_parent)
            window = explicit_unit_intro(field) if field is not None else None
            if same_scope and same_parent and window is not None:
                result[item.self_ref] = (pointer, *window)
        previous = item
    return result


def complete_html_fields(fields, pointer):
    """Resolve the whole proven DOM block before splitting inline code/text."""
    field = fields[pointer]
    locator = field.locator.model_dump(mode="json")
    if (locator.get("kind") != "html"
            or locator.get("basis") != "DOCLING_BACKEND_COMPLETE_INLINE_BLOCK"):
        return None
    groups = locator.get("source_group")
    if type(groups) is not list or len(groups) != 1 or type(groups[0]) is not dict:
        raise ChunkingUnsupported("complete HTML block proof is missing")
    proof = groups[0]
    if (proof.get("policy") != "HTML_INLINE_BLOCK_CLOSURE_V1"
            or proof.get("xpath") != locator["xpath"]
            or proof.get("file_sha256") != locator["file_sha256"]
            or type(proof.get("members")) is not list or not proof["members"]):
        raise ChunkingUnsupported("complete HTML block identity is invalid")
    pointers, normalized = [], ""
    for member in proof["members"]:
        key = member["field_pointer"]
        if key not in fields or key in pointers:
            raise ChunkingUnsupported("complete HTML block member is missing or duplicated")
        value = fields[key]
        if value.locator.model_dump(mode="json") != locator:
            raise ChunkingUnsupported("complete HTML block source changed")
        text = re.sub(r"\s+", " ", value.cleaned_text).strip()
        gap = member["start"] - len(normalized)
        if not text or gap not in {0, 1} or member["end"]-member["start"] != len(text):
            raise ChunkingUnsupported("complete HTML block has a gap or changed field")
        normalized += " " * gap + text
        pointers.append(key)
    if (len(normalized) != proof["normalized_text_length"]
            or text_sha256(normalized) != proof["normalized_text_sha256"]):
        raise ChunkingUnsupported("complete HTML block text changed")
    return pointers

def plan_chunks(bundle_path, tokenizer, profile):
    verified = verify_bundle(Path(bundle_path))
    if verified["status"] != "READY":
        raise ChunkingUnsupported("processing version is still REVIEW_REQUIRED")
    root = Path(verified["manifest_path"]).parent
    supplement = Supplement.model_validate_json((root / "supplement.json").read_bytes())
    document = DoclingDocument.model_validate_json((root / "cleaned.native.json").read_bytes())
    fields = {field.field_pointer: field for field in supplement.fields}
    # The installed HierarchicalChunker supplies doc_items. Its deprecated
    # merge_list_items option is not relied on to separate project source atoms.
    hierarchical = list(HierarchicalChunker(
        merge_list_items=False, serializer_provider=BoundSourceSerializerProvider(),
    ).chunk(document))
    scopes, heading_by_level = {}, {}
    for item, _level in source_items(document, with_groups=True):
        if isinstance(item, (TitleItem, SectionHeaderItem)):
            level = item.level if isinstance(item, SectionHeaderItem) else 0
            for old in list(heading_by_level):
                if old >= level:
                    del heading_by_level[old]
            heading_by_level[level] = item.self_ref
        scopes[item.self_ref] = tuple(heading_by_level[k] for k in sorted(heading_by_level))
    table_intros = explicit_table_intros(document, fields, scopes)
    rules = {**RULES, "source_sha256": hash_file(Path(__file__))[0],
             "docling_core_version": version("docling-core")}
    chunk_set = identity("CHUNK_SET", {
        "processing_version_id": supplement.processing_version_id,
        "bundle_sha256": verified["manifest_sha256"], "rules": rules,
        "embedding_space_id": profile["embedding_space_id"],
    })
    parents, children, sources, seen, context_fields = [], [], [], set(), set()
    plain_scope, plain_context, plain_streams = None, Stream.empty(), []

    def emit(parent_body, child_bodies, context, scope, kind):
        parent_text = retrieval(context, parent_body)
        parent_parts = bound_parts(parent_text, fields)
        parent_id = identity("PARENT", {"chunk_set_id": chunk_set, "ordinal": len(parents),
                                        "text_sha256": text_sha256(parent_text.text), "parts": parent_parts})
        parent_source_id = identity("SOURCE_RECORD", {"processing_version_id": supplement.processing_version_id,
                                                      "text_sha256": text_sha256(parent_text.text), "parts": parent_parts})
        sources.append({"source_record_id": parent_source_id, "text_sha256": text_sha256(parent_text.text), "parts": parent_parts})
        parent = {
            "parent_id": parent_id, "chunk_set_id": chunk_set,
            "processing_version_id": supplement.processing_version_id,
            "raw_version_id": supplement.raw_version_id, "content_kind": kind,
            "heading_scope": list(scope), "body": parent_body.text, "text": parent_text.text,
            "token_count": token_count(tokenizer, parent_text.text),
            "source_record_id": parent_source_id, "child_ids": [],
        }
        for body, limit in child_bodies:
            stream = retrieval(context, body)
            parts = bound_parts(stream, fields)
            chunk_id = identity("CHILD", {
                "chunk_set_id": chunk_set, "parent_id": parent_id, "ordinal": len(children),
                "text_sha256": text_sha256(stream.text), "parts": parts,
            })
            source_id = identity("SOURCE_RECORD", {
                "processing_version_id": supplement.processing_version_id, "text_sha256": text_sha256(stream.text), "parts": parts,
            })
            sources.append({"source_record_id": source_id, "text_sha256": text_sha256(stream.text), "parts": parts})
            children.append({
                "chunk_id": chunk_id, "parent_id": parent_id, "chunk_set_id": chunk_set,
                "processing_version_id": supplement.processing_version_id,
                "raw_version_id": supplement.raw_version_id,
                "embedding_space_id": profile["embedding_space_id"],
                "source_record_id": source_id, "content_kind": kind,
                "source_format": fields[next(p["field_pointer"] for p in parts if not p["constructed"])].locator.kind,
                "text": stream.text, "text_sha256": text_sha256(stream.text),
                "token_count": token_count(tokenizer, stream.text), "token_limit": limit,
                "input_kind": "document_extended" if limit == 1024 else "document_standard",
                "chunk_ordinal": len(children),
                "equivalence_key": identity("CONSERVATIVE_EQUIVALENCE", {
                    "processing_version_id": supplement.processing_version_id,
                    "heading_scope": list(scope), "content_kind": kind, "text": stream.text,
                }),
            })
            parent["child_ids"].append(chunk_id)
        parents.append(parent)

    def flush_plain():
        nonlocal plain_streams
        if plain_streams:
            for parent, child_bodies in structural_plain_plans(tokenizer, plain_streams, plain_context):
                emit(parent, child_bodies, plain_context, plain_scope, "text")
        plain_streams = []

    html_native_order = [item.self_ref.removeprefix("#") + "/text"
                         for item, _ in source_items(document)
                         if isinstance(item, TextItem)
                         and item.self_ref.removeprefix("#") + "/text" in fields
                         and fields[item.self_ref.removeprefix("#") + "/text"].cleaned_text]

    for chunk in hierarchical:
        # Docling serializes a rich list item as one span. Its actual text may
        # live in inline-group descendants, which must keep their own bindings.
        expanded = (descendant for item in chunk.meta.doc_items
                    for descendant, _ in source_items(document, root=item))
        for item in expanded:
            if item.self_ref in seen or isinstance(item, (TitleItem, SectionHeaderItem)):
                continue
            if isinstance(item, PictureItem) and item.self_ref.replace("#", "") + "/text" not in fields:
                # The container supplies captions; its bound text children are
                # emitted separately by the same complete source traversal.
                continue
            seen.add(item.self_ref)
            scope = scopes[item.self_ref]
            context = heading_context(scope, fields)
            context_fields.update(part["field_pointer"] for part in context.pieces if not part["constructed"])
            if isinstance(item, TableItem):
                flush_plain()
                if item.self_ref in table_intros:
                    pointer, start, end = table_intros[item.self_ref]
                    extra = Stream.empty()
                    extra.field(fields[pointer], start, end, purpose="BODY")
                    context = joined([context, extra]) if context.text else extra
                    context_fields.add(pointer)
                for pointer, purpose in reference_fields(item, fields):
                    extra = field_stream(fields[pointer], purpose)
                    context = joined([context, extra]) if context.text else extra
                    context_fields.add(pointer)
                units, row_ranges, _header_end = table_units(item, fields)
                for first, stop, parent_body, _limit in pack_units(tokenizer, units, context, 1536, 1536):
                    owned = units[first:stop]
                    allowed = [end - begin == 1 for begin, end in row_ranges[first:stop]]
                    child_bodies = [(body, limit) for _a, _b, body, limit in
                                    pack_units(tokenizer, owned, context, 512, 1024, extended_allowed=allowed)]
                    emit(parent_body, child_bodies, context, scope, "table")
                plain_scope = None
                continue
            pointer = item.self_ref.replace("#", "") + "/text"
            if pointer not in fields:
                raise ChunkingUnsupported("structural item has no bound source field")
            field = fields[pointer]
            if not field.cleaned_text:
                continue
            html_members = complete_html_fields(fields, pointer)
            if html_members is not None:
                if pointer != html_members[0]:
                    raise ChunkingUnsupported("HTML block starts after a missing member")
                at = html_native_order.index(pointer)
                if html_native_order[at:at+len(html_members)] != html_members:
                    raise ChunkingUnsupported("HTML block members are not in native source order")
                refs = [key.removesuffix("/text").replace("/", "#/", 1)
                        for key in html_members]
                if any(scopes[ref] != scope for ref in refs):
                    raise ChunkingUnsupported("HTML block crosses section conditions")
                if any(ref in seen for ref in refs[1:]):
                    raise ChunkingUnsupported("HTML block member already consumed")
                flush_plain()
                whole = joined([field_stream(fields[key]) for key in html_members])
                child_bodies = [(body, limit) for _a, _b, body, limit in
                                pack_units(tokenizer, [whole], context, 512, 1024)]
                emit(whole, child_bodies, context, scope, "text")
                seen.update(refs)
                plain_scope = None
                continue
            kind = getattr(item.label, "value", str(item.label))
            if kind in {"code", "formula"}:
                flush_plain()
                if kind == "code":
                    ends = [i + 1 for i, char in enumerate(field.cleaned_text) if char == "\n"]
                    if not ends or ends[-1] != len(field.cleaned_text):
                        ends.append(len(field.cleaned_text))
                    units, start = [], 0
                    full = field_stream(field)
                    for end in ends:
                        units.append(full.slice(start, end))
                        start = end
                else:
                    units = [field_stream(field)]
                delimiter = "" if kind == "code" else "\n"
                for first, stop, parent_body, _limit in pack_units(tokenizer, units, context, 1536, 1536, delimiter=delimiter):
                    owned = units[first:stop]
                    child_bodies = [(body, limit) for _a, _b, body, limit in
                                    pack_units(tokenizer, owned, context, 512, 1024, delimiter=delimiter)]
                    emit(parent_body, child_bodies, context, scope, kind)
                plain_scope = None
            else:
                if plain_scope != scope:
                    flush_plain()
                    plain_scope, plain_context = scope, context
                plain_streams.append((field_stream(field), kind == "list_item"))
    flush_plain()
    consumed = {piece["field_pointer"] for source in sources for piece in source["parts"] if not piece["constructed"]}
    # Titles are context, never independent support units.
    title_fields = {item.self_ref.replace("#", "") + "/text" for item, _ in source_items(document)
                    if isinstance(item, (TitleItem, SectionHeaderItem))}
    missing = [pointer for pointer, field in fields.items()
               if field.cleaned_text and pointer not in consumed and pointer not in title_fields]
    if missing or not children:
        raise ChunkingUnsupported("unmapped content fields: " + repr(missing))
    unique_sources = {source["source_record_id"]: source for source in sources}
    return {
        "schema": "raghub.chunk-set.v1", "chunk_set_id": chunk_set,
        "bundle_path": verified["manifest_path"], "bundle_sha256": verified["manifest_sha256"],
        "processing_version_id": supplement.processing_version_id, "raw_version_id": supplement.raw_version_id,
        "embedding_space_id": profile["embedding_space_id"], "rules": rules,
        "parents": parents, "children": children, "sources": list(unique_sources.values()),
        "coverage": {"nonempty_source_fields": sum(bool(f.cleaned_text) for f in fields.values()),
                     "consumed_fields": sorted(consumed), "title_only_fields": sorted(title_fields - consumed),
                     "unmapped_fields": missing},
    }
