"""Format adapters: Docling plus source-bound XLSX/CSV/business JSON/image adapters."""
from __future__ import annotations

import csv
import io
import json
import math
import re
import time
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from docling_core.types.doc import (DoclingDocument, DocItemLabel, TableData, TableCell,
                                    ProvenanceItem, BoundingBox, Size, ImageRef)
from PIL import Image, ImageOps, ImageSequence

from ..ingestion.archive import hash_file, parse_captured_html
from ..parsing_assets import rapid_params

MAX_FIELD = 100000
MAX_RECORDS = 10000


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key: " + key)
        result[key] = value
    return result


class Number:
    def __init__(self, literal, kind):
        self.literal, self.kind = literal, kind


def scalar(value):
    if isinstance(value, Number):
        return value.literal
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    raise ValueError("selected business field is not a scalar")


def pointer(root, path):
    if path == "":
        return root
    if not path.startswith("/"):
        raise ValueError("JSON pointer must use RFC6901")
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(root, list):
            if not part.isdigit() or (part != "0" and part.startswith("0")):
                raise ValueError("invalid array pointer")
            root = root[int(part)]
        elif isinstance(root, dict):
            root = root[part]
        else:
            raise ValueError("pointer traverses a scalar")
    return root


def escaped(key):
    return str(key).replace("~", "~0").replace("/", "~1")


def field_values(native):
    for i, item in enumerate(native["texts"]):
        yield "/texts/" + str(i) + "/text", item["text"], item["label"]
    for i, table in enumerate(native["tables"]):
        for j, cell in enumerate(table["data"]["table_cells"]):
            yield "/tables/" + str(i) + "/data/table_cells/" + str(j) + "/text", cell["text"], "cell"


class Parsed:
    def __init__(self, document, *, scope, details=None):
        self.document, self.scope = document, scope
        self.details, self.locations, self.quality, self.images = details or {}, {}, [], []

    def issue(self, severity, code, **details):
        self.quality.append({"severity": severity, "code": code, **details})

    def text(self, text, label, locator):
        if len(text) > MAX_FIELD:
            raise ValueError("selected field exceeds 100000 characters")
        item = self.document.add_text(label=label, text=text)
        self.locations[item.self_ref.replace("#", "") + "/text"] = locator
        return item

    def table(self, rows, locations, *, header_rows=1, spans=None):
        if not rows or not rows[0] or max(map(len, rows)) > 256 or len(rows) > MAX_RECORDS:
            raise ValueError("invalid or over-budget table")
        columns = max(map(len, rows))
        cells = []
        span_by_cell = spans or {}
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                if text is None:
                    continue
                if len(text) > MAX_FIELD:
                    raise ValueError("table field exceeds limit")
                rs, cs = span_by_cell.get((r, c), (1, 1))
                cells.append(TableCell(text=text, row_span=rs, col_span=cs,
                    start_row_offset_idx=r, end_row_offset_idx=r + rs,
                    start_col_offset_idx=c, end_col_offset_idx=c + cs,
                    column_header=r < header_rows))
        item = self.document.add_table(data=TableData(num_rows=len(rows), num_cols=columns, table_cells=cells))
        for j, cell in enumerate(cells):
            key = (cell.start_row_offset_idx, cell.start_col_offset_idx)
            self.locations[item.self_ref.replace("#", "") + "/data/table_cells/" + str(j) + "/text"] = locations[key]
        return item


def new_doc(path):
    return DoclingDocument(name=path.name)


def base_location(kind, digest, **values):
    return {"kind": kind, "file_sha256": digest, "granularity": "EXACT",
            "basis": "PROJECT_FORMAT_ADAPTER", **values}


def text_document(path):
    raw=path.read_bytes()
    text=raw.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("纯文本文件不能包含空字节。")
    lines=text.splitlines()
    if not lines or len(lines)>MAX_RECORDS:
        raise ValueError("纯文本须包含1至10000行。")
    digest=hash_file(path)[0]
    parsed=Parsed(new_doc(path),scope={"selected":"ALL_NONEMPTY_TEXT_LINES","physical_lines":len(lines),
                  "excluded":"EMPTY_LINES"},details={"encoding":"UTF8_SIG","plain_text":True})
    for number,line in enumerate(lines,1):
        if line.strip():
            parsed.text(line,DocItemLabel.TEXT,base_location("text",digest,line_start=number,line_end=number))
    if not parsed.locations:
        raise ValueError("纯文本中没有可用文字。")
    return parsed



def csv_document(path, config):
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    stream = io.StringIO(text, newline="")
    csv.field_size_limit(MAX_FIELD)
    reader = csv.reader(stream, delimiter=",", quotechar="\"", doublequote=True, strict=True)
    rows, starts = [], []
    previous = 0
    for record, row in enumerate(reader, 1):
        if record > MAX_RECORDS or len(row) > 256:
            raise ValueError("CSV record/column limit")
        rows.append(row)
        starts.append((previous + 1, reader.line_num))
        previous = reader.line_num
    if not rows or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("CSV is empty or has inconsistent record widths")
    digest = hash_file(path)[0]
    p = Parsed(new_doc(path), scope={"selected": "ALL_LOGICAL_RECORDS", "record_count": len(rows),
                                    "header_record": 1 if config.get("header", True) else None,
                                    "excluded": [], "physical_lines": reader.line_num},
               details={"dialect": "UTF8_SIG_COMMA_DOUBLE_QUOTE_STRICT", "string_values_preserved": True})
    locations = {(r, c): base_location("csv", digest, record=r + 1, column=c + 1,
                    physical_line_start=starts[r][0], physical_line_end=starts[r][1])
                 for r, row in enumerate(rows) for c in range(len(row))}
    p.table(rows, locations, header_rows=1 if config.get("header", True) else 0)
    return p


def validate_selection_config(kind, config):
    """Check a manual selection before a processing version is created."""
    if type(config) is not dict:
        raise ValueError("解析配置必须为对象。")
    if kind == "json":
        if set(config) != {"json"} or type(config["json"]) is not dict:
            raise ValueError("业务 JSON 必须明确配置记录、标题和正文字段。")
        selected = config["json"]
        required = {"records_pointer", "title_pointer", "text_pointers"}
        if not required <= set(selected) or set(selected) - required - {"tables"}:
            raise ValueError("JSON 配置字段缺失或包含未知字段。")
        pointers = [selected["records_pointer"], selected["title_pointer"]]
        if type(selected["text_pointers"]) is not list or len(selected["text_pointers"]) > 256:
            raise ValueError("正文位置必须为最多256项的 Pointer 数组。")
        pointers.extend(selected["text_pointers"])
        tables = selected.get("tables", [])
        if type(tables) is not list or len(tables) > 100:
            raise ValueError("JSON 表格配置必须为最多100项的数组。")
        for table in tables:
            if type(table) is not dict or set(table) != {"pointer", "columns"}:
                raise ValueError("JSON 表格只接受 pointer 与 columns。")
            columns = table["columns"]
            if type(columns) is not list or not 1 <= len(columns) <= 256 or any(type(key) is not str or not key for key in columns):
                raise ValueError("JSON 表格列必须为非空原始字段名数组。")
            pointers.append(table["pointer"])
        if any(type(item) is not str or (item != "" and not item.startswith("/")) for item in pointers):
            raise ValueError("JSON Pointer 必须为空根位置或以 / 开头的字符串。")
    elif kind == "xlsx":
        if set(config) != {"xlsx"} or type(config["xlsx"]) is not dict:
            raise ValueError("XLSX 必须明确选择实际工作表和表格范围。")
        selected = config["xlsx"]
        if set(selected) - {"regions", "value_copy_of"} or type(selected.get("regions")) is not list or not 1 <= len(selected["regions"]) <= 100:
            raise ValueError("XLSX 必须配置1至100个 regions，不自动改为全工作簿。")
        for region in selected["regions"]:
            if (type(region) is not dict or not {"sheet", "range"} <= set(region)
                    or set(region) - {"sheet", "range", "header_rows", "title", "unit", "footnotes"}
                    or any(type(region[key]) is not str or not region[key] for key in ("sheet", "range"))):
                raise ValueError("每个 XLSX 范围必须包含实际 sheet 与 A1 range。")
            if type(region.get("header_rows", 1)) is not int or region.get("header_rows", 1) < 0:
                raise ValueError("header_rows 必须为范围开头的非负表头行数。")
            for role in ("title", "unit", "footnotes"):
                if type(region.get(role, [])) is not list or any(type(ref) is not str or not ref for ref in region.get(role, [])):
                    raise ValueError("标题、单位和脚注只能填写原件 A1 范围数组。")
        reference = selected.get("value_copy_of")
        if reference is not None and (type(reference) is not dict or set(reference) != {"raw_version_id", "source", "note"}
                or reference["source"] not in {"LOCAL_USER_CLI", "LOCAL_USER_UI", "CHATGPT_USER_MESSAGE"}
                or type(reference["raw_version_id"]) is not str or not reference["raw_version_id"]
                or type(reference["note"]) is not str or not reference["note"].strip() or len(reference["note"]) > 2000):
            raise ValueError("仅值副本必须明确选择原件并填写人工重算核验说明。")


def json_document(path, config):
    raw = path.read_bytes()
    if len(raw) > 10 * 1024 * 1024:
        raise ValueError("business JSON exceeds 10 MiB")
    obj = json.loads(raw, object_pairs_hook=no_duplicates,
                     parse_int=lambda s: Number(s, "integer"),
                     parse_float=lambda s: Number(s, "number"),
                     parse_constant=lambda s: (_ for _ in ()).throw(ValueError("non-standard JSON number")))
    def depth(value, n=0):
        if n > 32:
            raise ValueError("business JSON depth exceeds 32")
        for child in value.values() if isinstance(value, dict) else value if isinstance(value, list) else ():
            depth(child, n + 1)
    depth(obj)
    required = {"records_pointer", "title_pointer", "text_pointers"}
    if not required <= set(config) or set(config) - required - {"tables"}:
        raise ValueError("business JSON requires an explicit field selection")
    records = pointer(obj, config["records_pointer"])
    if not isinstance(records, list) or not 0 < len(records) <= MAX_RECORDS:
        raise ValueError("selected records must be a nonempty bounded array")
    digest = hash_file(path)[0]
    selected = []
    p = Parsed(new_doc(path), scope={"selected": selected, "excluded": [], "record_count": len(records)},
               details={"numbers_preserve_literal_and_type": True})
    for i, record in enumerate(records):
        prefix = config["records_pointer"] + "/" + str(i)
        for local in [config["title_pointer"], *config["text_pointers"]]:
            target = prefix + local
            value = pointer(record, local)
            selected.append({"pointer": target, "type": value.kind if isinstance(value, Number) else type(value).__name__})
            p.text(scalar(value), DocItemLabel.TITLE if local == config["title_pointer"] else DocItemLabel.TEXT,
                   base_location("json", digest, pointer=target))
        for table in config.get("tables", []):
            if set(table) != {"pointer", "columns"} or not table["columns"]:
                raise ValueError("invalid JSON table selection")
            items = pointer(record, table["pointer"])
            if not isinstance(items, list) or len(items) > MAX_RECORDS:
                raise ValueError("selected JSON table is not a bounded array")
            rows = [list(table["columns"])]
            locations = {(0, c): base_location("json", digest, pointer=prefix + table["pointer"])
                         for c in range(len(table["columns"]))}
            for r, item in enumerate(items, 1):
                rows.append([scalar(pointer(item, "/" + escaped(key))) for key in table["columns"]])
                for c, key in enumerate(table["columns"]):
                    target = prefix + table["pointer"] + "/" + str(r - 1) + "/" + escaped(key)
                    selected.append({"pointer": target, "type": type(item[key]).__name__})
                    locations[r, c] = base_location("json", digest, pointer=target)
            p.table(rows, locations)
    selected_set = {item["pointer"] for item in selected}
    def leaves(value, path=""):
        if isinstance(value, dict):
            for k, v in value.items():
                yield from leaves(v, path + "/" + escaped(k))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                yield from leaves(v, path + "/" + str(i))
        else:
            yield path
    p.scope["excluded"] = [{"pointer": name, "reason": "OUTSIDE_USER_SELECTED_FIELDS"}
                           for name in leaves(obj) if name not in selected_set]
    return p


def display_cell(cell):
    value, fmt = cell.value, cell.number_format
    if value is None:
        return "", True
    if cell.data_type == "f":
        return value, False
    if isinstance(value, (str, bool)):
        return str(value), True
    if hasattr(value, "strftime"):
        if fmt == "yyyy-mm-dd":
            return value.strftime("%Y-%m-%d"), True
        if fmt == "yyyy-mm-dd hh:mm:ss":
            return value.strftime("%Y-%m-%d %H:%M:%S"), True
        return str(value), False
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError("nonfinite XLSX number")
        number = Decimal(str(value))
        if fmt == "General":
            return str(value), True
        if fmt in {"0", "0.00", "0%", "0.00%"}:
            percent = fmt.endswith("%")
            decimals = 2 if "." in fmt else 0
            shown = (number * (100 if percent else 1)).quantize(Decimal("0.01" if decimals else "1"), rounding=ROUND_HALF_UP)
            return format(shown, "." + str(decimals) + "f") + ("%" if percent else ""), True
        if re.fullmatch("0{2,}", fmt) and number == number.to_integral_value() and number >= 0:
            return str(int(number)).zfill(len(fmt)), True
    return str(value), False



def xlsx_sources(path):
    """Read formula kinds, ranges, external references and raw scalar literals."""
    from zipfile import ZipFile
    from hashlib import sha256
    from lxml import etree
    import posixpath
    ns={"m":"http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r":"http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    parser=etree.XMLParser(resolve_entities=False,no_network=True,recover=False,huge_tree=False)
    result={"sheets":{},"external_links":[]}
    with ZipFile(path) as package:
        if sum(info.file_size for info in package.infolist())>128*1024*1024:
            raise ValueError("XLSX expanded XML exceeds 128 MiB")
        workbook=etree.fromstring(package.read("xl/workbook.xml"),parser)
        relationships=etree.fromstring(package.read("xl/_rels/workbook.xml.rels"),parser)
        targets={node.get("Id"):node for node in relationships}
        for node in workbook.findall("m:sheets/m:sheet",ns):
            relation=targets[node.get("{"+ns["r"]+"}id")]
            target=relation.get("Target")
            part=posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/"+target)
            if not part.startswith("xl/") or part not in package.namelist():
                raise ValueError("XLSX sheet relation is outside its archive")
            sheet={"part":part,"state":node.get("state","visible"),"type":relation.get("Type"),
                   "cells":{},"formulas":[]}
            root=etree.fromstring(package.read(part),parser)
            for cell in root.findall(".//m:sheetData/m:row/m:c",ns):
                coordinate=cell.get("r")
                value=cell.find("m:v",ns)
                formula=cell.find("m:f",ns)
                sheet["cells"][coordinate]={"raw_value":value.text if value is not None else None,
                    "xml_type":cell.get("t","n"),"formula":formula.text if formula is not None else None,
                    "formula_attributes":dict(formula.attrib) if formula is not None else None}
                if formula is not None:
                    sheet["formulas"].append({"cell":coordinate,"text":formula.text,"attributes":dict(formula.attrib)})
            result["sheets"][node.get("name")]=sheet
        for name in sorted(package.namelist()):
            if name.startswith("xl/externalLinks/") and name.endswith(".xml"):
                result["external_links"].append({"part":name,"sha256":sha256(package.read(name)).hexdigest()})
            if name.startswith("xl/externalLinks/") and name.endswith(".rels"):
                root=etree.fromstring(package.read(name),parser)
                result["external_links"].extend({"part":name,"relationship":dict(node.attrib)} for node in root)
    return result


def formula_cells(source, sheet_name):
    from openpyxl.utils.cell import range_boundaries
    affected=set()
    for formula in source["sheets"][sheet_name]["formulas"]:
        attrs=formula["attributes"]
        reference=attrs.get("ref",formula["cell"])
        c0,r0,c1,r1=range_boundaries(reference)
        if None in (c0,r0,c1,r1) or r1-r0+1>MAX_RECORDS or c1-c0+1>256:
            raise ValueError("XLSX formula influence range is unresolved or over budget")
        affected.update((r,c) for r in range(r0,r1+1) for c in range(c0,c1+1))
    return affected


def verify_value_copy(book, source, origin):
    from openpyxl import load_workbook
    from ..ingestion.archive import verify_archive
    archive=verify_archive(Path(origin["archive_manifest_path"]))
    if archive["manifest_sha256"]!=origin["archive_manifest_sha256"]:
        raise ValueError("value-copy original archive changed")
    primary=[item for item in archive["files"] if item["path"].startswith("original.")]
    if len(primary)!=1:
        raise ValueError("value-copy original is not one workbook")
    original=Path(archive["manifest_path"]).parent/primary[0]["path"]
    old_source=xlsx_sources(original)
    if any(sheet["formulas"] for sheet in source["sheets"].values()):
        raise ValueError("declared only-values copy still contains formulas")
    parent=load_workbook(original,data_only=False,read_only=False,keep_links=False)
    if any(sheet.max_row>MAX_RECORDS or sheet.max_column>256 for sheet in parent.worksheets):
        raise ValueError("value-copy origin dimensions exceed declared scan budget")
    try:
        if book.sheetnames!=parent.sheetnames or book.epoch!=parent.epoch:
            raise ValueError("value copy changed workbook sheet order or epoch")
        checked=0
        for old in parent.worksheets:
            current=book[old.title]
            if (old.max_row!=current.max_row or old.max_column!=current.max_column or old.sheet_state!=current.sheet_state
                    or sorted(map(str,old.merged_cells.ranges))!=sorted(map(str,current.merged_cells.ranges))
                    or [(t.name,t.ref) for t in old.tables.values()]!=[(t.name,t.ref) for t in current.tables.values()]):
                raise ValueError("value copy changed the source grid or table relationships")
            affected=formula_cells(old_source,old.title)
            for row in old:
                for cell in row:
                    after=current[cell.coordinate]
                    if after.number_format!=cell.number_format:
                        raise ValueError("value copy changed source number format")
                    if (cell.row,cell.column) in affected:
                        if after.data_type in {"f","e"} or after.value is None:
                            raise ValueError("value copy has unresolved/error/empty formula results")
                        checked+=1
                    elif type(cell.value) is not type(after.value) or cell.value!=after.value:
                        raise ValueError("value copy changed a non-formula source cell")
        if checked==0:
            raise ValueError("declared value-copy source has no formula effects to verify")
        return {"status":"USER_CONFIRMED_RECALCULATED_VALUES","mechanical_scope":"FORMULAS_REMOVED_GRID_AND_NON_FORMULA_VALUES_PRESERVED",
                "verified_formula_effect_cells":checked,"original_archive":archive,"user_statement":origin["user_statement"],
                "numeric_correctness":"USER_VERIFICATION_NOT_AUTOMATIC_RECALCULATION"}
    finally:
        parent.close()


def xlsx_document(path, config):
    from openpyxl import load_workbook
    from openpyxl.utils.cell import range_boundaries
    book = load_workbook(path, data_only=False, read_only=False,keep_links=False)
    cached = load_workbook(path, data_only=True, read_only=False,keep_links=False)
    xml_source=xlsx_sources(path)
    digest = hash_file(path)[0]
    p = Parsed(new_doc(path), scope={"selected": [], "excluded": [], "sheets": []},
               details={"epoch": str(book.epoch), "formulas": [], "merged_ranges": [],
                        "number_cache_is_trusted":False,"ooxml":xml_source,"cells":[]})
    if any(sheet.max_row>MAX_RECORDS or sheet.max_column>256 for sheet in book.worksheets):
        raise ValueError("XLSX worksheet dimensions exceed declared scan budget")
    if config.get("value_copy_origin"):
        p.details["value_copy"]=verify_value_copy(book,xml_source,config["value_copy_origin"])
    for chart_sheet in book.chartsheets:
        p.scope["sheets"].append({"name":chart_sheet.title,"state":chart_sheet.sheet_state,"type":"CHARTSHEET"})
        p.scope["excluded"].append({"sheet":chart_sheet.title,"reason":"CHARTSHEET_HAS_NO_CELL_GRID"})
    explicit = config.get("regions", [])
    explicit_mode=bool(explicit)
    by_sheet = {}
    for item in explicit:
        if set(item) - {"sheet", "range", "header_rows", "title", "unit", "footnotes"} or not {"sheet", "range"} <= set(item):
            raise ValueError("XLSX region requires sheet and A1 range")
        by_sheet.setdefault(item["sheet"], []).append(item)
    if set(by_sheet) - {sheet.title for sheet in book.worksheets}:
        raise ValueError("selected XLSX sheet does not exist")
    for sheet in book.worksheets:
        p.scope["sheets"].append({"name": sheet.title, "state": sheet.sheet_state,
                                 "hidden_rows": [i for i, dim in sheet.row_dimensions.items() if dim.hidden],
                                 "hidden_columns": [i for i, dim in sheet.column_dimensions.items() if dim.hidden]})
        if sheet.max_row>MAX_RECORDS or sheet.max_column>256:
            raise ValueError("XLSX worksheet dimensions exceed declared scan budget")
        affected=formula_cells(xml_source,sheet.title)
        for formula in xml_source["sheets"][sheet.title]["formulas"]:
            cell=sheet[formula["cell"]]
            expanded=getattr(cell.value,"text",cell.value)
            p.details["formulas"].append({"sheet":sheet.title,"cell":cell.coordinate,
                "original_formula_text":formula["text"],"formula_attributes":formula["attributes"],
                "openpyxl_expression":expanded,"cached_value_observed":str(cached[sheet.title][cell.coordinate].value),
                "numeric_use":"UNVERIFIED_NOT_ALLOWED"})
        regions = by_sheet.get(sheet.title,[]) if explicit_mode else None
        if regions is None:
            regions = [{"sheet": sheet.title, "range": table.ref, "header_rows": table.headerRowCount or 0}
                       for table in sheet.tables.values()] if sheet.sheet_state == "visible" else []
        if not regions:
            p.scope["excluded"].append({"sheet": sheet.title, "range": sheet.calculate_dimension(),
                                        "reason": "NO_EXPLICIT_OR_VISIBLE_FORMAL_TABLE"})
        selected_cells = set()
        for region in regions:
            c0, r0, c1, r1 = range_boundaries(region["range"])
            if (None in (c0,r0,c1,r1) or not (1<=r0<=r1<=sheet.max_row and 1<=c0<=c1<=sheet.max_column)
                or r1-r0+1>MAX_RECORDS or c1-c0+1>256):
                raise ValueError("invalid XLSX selection dimensions")
            spans, covered = {}, set()
            for merged in sheet.merged_cells.ranges:
                mc0, mr0, mc1, mr1 = merged.bounds
                if not (mc1 < c0 or mc0 > c1 or mr1 < r0 or mr0 > r1):
                    if not (c0 <= mc0 <= mc1 <= c1 and r0 <= mr0 <= mr1 <= r1):
                        raise ValueError("selected region cuts a merged range")
                    spans[mr0-r0, mc0-c0] = (mr1-mr0+1, mc1-mc0+1)
                    covered.update((r,c) for r in range(mr0,mr1+1) for c in range(mc0,mc1+1) if (r,c)!=(mr0,mc0))
                    p.details["merged_ranges"].append({"sheet": sheet.title, "range": str(merged)})
            if type(region.get("header_rows",1)) is not int or not 0<=region.get("header_rows",1)<=r1-r0+1:
                raise ValueError("XLSX header_rows is outside its selected table")
            if any((r,c) in selected_cells for r in range(r0,r1+1) for c in range(c0,c1+1)):
                raise ValueError("XLSX selected table regions overlap")
            rows, locations = [], {}
            for r in range(r0, r1 + 1):
                values = []
                for c in range(c0, c1 + 1):
                    selected_cells.add((r,c))
                    cell = sheet.cell(r,c)
                    if (r,c) in covered:
                        values.append(None)
                        continue
                    if (r,c) in affected and cell.data_type!="f":
                        text,resolved="[未核验公式影响单元格 "+cell.coordinate+"]",False
                    elif cell.data_type=="f" and not isinstance(cell.value,str):
                        text,resolved=str(getattr(cell.value,"text",xml_source["sheets"][sheet.title]["cells"].get(cell.coordinate,{}).get("formula") or "[公式]")),False
                    else:
                        text,resolved=display_cell(cell)
                    p.details["cells"].append({"sheet":sheet.title,"cell":cell.coordinate,"data_type":cell.data_type,
                        "python_type":type(cell.value).__name__,"value":str(getattr(cell.value,"text",cell.value)) if cell.data_type!="f" or isinstance(cell.value,str) else xml_source["sheets"][sheet.title]["cells"].get(cell.coordinate),"number_format":cell.number_format,
                        "ooxml":xml_source["sheets"][sheet.title]["cells"].get(cell.coordinate),"display":text,
                        "formula_effect":(r,c) in affected})
                    values.append(text)
                    locations[r-r0,c-c0] = base_location("xlsx", digest, sheet=sheet.title,
                                                          cell_range=cell.coordinate)
                    if not resolved:
                        p.issue("WARN", "FORMULA_TEXT_ONLY" if cell.data_type=="f" else "FORMULA_EFFECT_UNVERIFIED" if (r,c) in affected else "DISPLAY_UNRESOLVED",
                                sheet=sheet.title, cell=cell.coordinate, number_format=cell.number_format)
                rows.append(values)
            table=p.table(rows, locations, header_rows=region.get("header_rows",1), spans=spans)
            attached={}
            for role in ("title","unit","footnotes"):
                ranges=region.get(role,[])
                if not isinstance(ranges,list) or any(not isinstance(ref,str) for ref in ranges):
                    raise ValueError("XLSX context must list original A1 ranges, never free text")
                attached[role]=[]
                for ref in ranges:
                    cc0,rr0,cc1,rr1=range_boundaries(ref)
                    if None in (cc0,rr0,cc1,rr1) or not (1<=rr0<=rr1<=sheet.max_row and 1<=cc0<=cc1<=sheet.max_column) or (rr1-rr0+1)*(cc1-cc0+1)>256:
                        raise ValueError("invalid or over-budget XLSX context range")
                    for context_row in sheet.iter_rows(min_row=rr0,max_row=rr1,min_col=cc0,max_col=cc1):
                        for context_cell in context_row:
                            selected_cells.add((context_cell.row,context_cell.column))
                            if context_cell.value is None:
                                continue
                            if (context_cell.row,context_cell.column) in affected:
                                text,resolved="[未核验公式上下文 "+context_cell.coordinate+"]",False
                            else:
                                text,resolved=display_cell(context_cell)
                            if not resolved:
                                p.issue("WARN","CONTEXT_DISPLAY_UNRESOLVED",sheet=sheet.title,cell=context_cell.coordinate)
                            item=p.text(text,DocItemLabel.FOOTNOTE if role=="footnotes" else DocItemLabel.CAPTION,
                                base_location("xlsx",digest,sheet=sheet.title,cell_range=context_cell.coordinate))
                            (table.footnotes if role=="footnotes" else table.captions).append(item.get_ref())
                            attached[role].append({"range":ref,"cell":context_cell.coordinate,"field":item.self_ref.replace("#","")+"/text"})
            p.scope["selected"].append({"sheet":sheet.title,"range":region["range"],"context":attached,
                                        "selection_mode":"EXPLICIT" if explicit_mode else "VISIBLE_FORMAL_TABLE"})
        excluded = [cell.coordinate for row in sheet for cell in row
                    if cell.value is not None and (cell.row,cell.column) not in selected_cells]
        if excluded:
            p.scope["excluded"].append({"sheet":sheet.title,"cells":excluded,"reason":"OUTSIDE_SELECTED_REGIONS"})
    book.close()
    cached.close()
    if not p.document.tables:
        raise ValueError("XLSX needs an explicit table region when no visible formal table exists")
    return p


def svg_document(path):
    from ..svg_image import read_svg
    _raw, rows, (width,height)=read_svg(path)
    digest=hash_file(path)[0]
    p=Parsed(new_doc(path),scope={"selected":"VISIBLE_STATIC_SVG_TEXT","excluded":"NON_TEXT_GRAPHIC_MEANING_NOT_INFERRED"},
             details={"text_basis":"ORIGINAL_SVG_XML","active_content_executed":False})
    p.document.add_page(1,Size(width=width,height=height))
    polygon=[[0.0,0.0],[width,0.0],[width,height],[0.0,height]]
    for row in rows:
        item=p.document.add_text(label=DocItemLabel.TEXT,text=row["text"],
            prov=ProvenanceItem(page_no=1,bbox=BoundingBox(l=0,r=width,t=0,b=height,coord_origin="TOPLEFT"),charspan=(0,len(row["text"]))))
        p.locations[item.self_ref.removeprefix("#")+"/text"]=base_location("image",digest,frame=1,region=polygon,
            transform={"svg_xml_path":row["xml_path"],"coordinates":"WHOLE_SVG_CANVAS_XML_TEXT_BOUND"},
            granularity="REGION",basis="ORIGINAL_STATIC_SVG_TEXT_NODE")
    p.images=[{"page":1,"width":width,"height":height,"resource_sha256":digest,"transform":{"source":"STATIC_SVG_XML"}}]
    return p


def image_document(path, lock, *, cache_dir=None):
    if path.suffix.lower()==".svg":
        return svg_document(path)
    import numpy as np
    from ..ingestion.archive import copy_closed
    digest=hash_file(path)[0]
    p=Parsed(new_doc(path),scope={"selected":"ALL_FRAMES_AND_PIXELS","excluded":[]},
             details={"preprocessing":"EXIF_TRANSPOSE_WHITE_ALPHA_RGB","enhancement":False})
    animated=path.suffix.lower()==".gif"
    seen={};pixel_count=0;rendered_bytes=0;tile_count=0
    began=time.monotonic()
    def white_rgb(image):
        rgba=image.convert("RGBA");background=Image.new("RGBA",rgba.size,"white")
        background.alpha_composite(rgba)
        return background.convert("RGB")
    def starts(length):
        values=list(range(0,max(1,length-1536+1),1408))
        if values[-1]+1536<length:values.append(max(0,length-1536))
        return values
    with Image.open(path) as original:
        frame_count=getattr(original,"n_frames",1)
        pixels=original.width*original.height
        if animated and (frame_count>500 or pixels*frame_count>1500000000):
            raise ValueError("GIF exceeds 500 frames or 1.5 billion decoded pixels")
        large=not animated and frame_count==1 and pixels>20000000
        if not animated and pixels*frame_count>(120000000 if large else 20000000):
            raise ValueError("bitmap exceeds decoded-pixel budget")
        p.details.update(source_frame_count=frame_count,embedded_frame_images=not (animated or large),tiled_ocr=large)
        engine=rapid_engine(lock)
        for index,frame in enumerate(ImageSequence.Iterator(original),1):
            orientation=frame.getexif().get(274,1)
            source_size=list(frame.size)
            pixel_count+=frame.width*frame.height
            if large:
                if orientation!=1:raise ValueError("large bitmap must have canonical EXIF orientation before tiled OCR")
                width,height=frame.size
                tiles=[(x,y,min(x+1536,width),min(y+1536,height)) for y in starts(height) for x in starts(width)]
                rgb=None;image_ref=None
            else:
                rgb=white_rgb(ImageOps.exif_transpose(frame.copy()))
                width,height=rgb.size;tiles=[(0,0,width,height)]
                image_ref=None if animated else ImageRef.from_pil(rgb,dpi=72)
            p.document.add_page(index,Size(width=width,height=height),image_ref)
            transform={"exif_orientation":orientation,"original_size":source_size,"rendered_size":[width,height],
                       "coordinates":"ORIGINAL_PIXELS_WITH_TILE_OFFSET" if large else "RENDERED_TOP_LEFT_PIXELS_WITH_EXIF_INVERSE",
                       "inverse_matrix":exif_inverse(orientation,*source_size)}
            for tile in tiles:
                if time.monotonic()-began>1800:raise TimeoutError("bitmap OCR exceeded 1800 seconds")
                tile_count+=1
                tile_rgb=white_rgb(frame.crop(tile)) if large else rgb
                result=engine(np.asarray(tile_rgb),use_det=True,use_cls=True,use_rec=True)
                for box,text,score in zip(result.boxes if result.boxes is not None else [],result.txts or (),result.scores or (),strict=True):
                    polygon=[[float(point[0])+tile[0],float(point[1])+tile[1]] for point in box]
                    xs,ys=[point[0] for point in polygon],[point[1] for point in polygon]
                    key=(text,tuple(round(v/8) for v in (min(xs),min(ys),max(xs),max(ys))))
                    observation={"kind":"image","file_sha256":digest,"frame":index,"region":polygon,"tile_box":list(tile)}
                    if (animated or large) and key in seen:
                        p.locations[seen[key]]["source_group"].append(observation)
                        continue
                    item=p.document.add_text(label=DocItemLabel.TEXT,text=text,
                        prov=ProvenanceItem(page_no=index,bbox=BoundingBox(l=min(xs),r=max(xs),t=min(ys),b=max(ys),coord_origin="TOPLEFT"),charspan=(0,len(text))))
                    field=item.self_ref.removeprefix("#")+"/text"
                    p.locations[field]=base_location("image",digest,frame=index,region=polygon,
                        transform={**transform,"tile_box":list(tile)},granularity="REGION")
                    if animated or large:
                        seen[key]=field;p.locations[field]["source_group"]=[observation]
                    if score<.8 or re.search("[0-9]",text):
                        p.issue("WARN","OCR_REVIEW_CANDIDATE",field=field,confidence=float(score),text=text)
            record={"page":index,"width":width,"height":height,"transform":transform}
            if (animated or large) and cache_dir is not None:
                if large:
                    frame_path=Path(cache_dir)/("bitmap-"+digest[:16]+path.suffix.lower())
                    copy_closed(path,frame_path,digest,50*1024*1024)
                else:
                    frame_path=Path(cache_dir)/("gif-"+digest[:16]+"-"+str(index)+".png")
                    with frame_path.open("xb") as target:rgb.save(target,format="PNG")
                frame_sha,frame_bytes=hash_file(frame_path);rendered_bytes+=frame_bytes
                if rendered_bytes>500*1024*1024:raise ValueError("bitmap frame cache exceeds 500 MiB")
                record.update(path=frame_path.name,sha256=frame_sha)
            p.images.append(record)
    p.details.update(decoded_frame_count=len(p.images),decoded_pixels=pixel_count,ocr_tile_count=tile_count)
    return p


def pdf_document(path, lock):
    import pypdfium2 as pdfium
    from docling.datamodel.base_models import InputFormat, ConversionStatus
    from docling.datamodel.pipeline_options import PdfPipelineOptions,RapidOcrOptions,TableFormerMode
    from docling.document_converter import DocumentConverter,PdfFormatOption
    from docling_core.types.doc import DoclingDocument
    digest = hash_file(path)[0]
    pdf = pdfium.PdfDocument(path)
    policies = []
    for page_index in range(len(pdf)):
        page = pdf[page_index]
        textpage = page.get_textpage()
        text = textpage.get_text_range()
        usable = len(re.sub(r"\s+","",text)) >= 10 and text.count("\ufffd") <= max(1,len(text)*.01)
        policies.append({"page":page_index+1,"text_layer_usable":usable,"ocr":"REGIONS" if usable else "FULL_PAGE"})
        textpage.close()
        page.close()
    pdf.close()
    if not policies:
        raise ValueError("PDF has no pages")
    start = time.monotonic()
    documents = []
    for policy in policies:
        remaining = 1800 - (time.monotonic()-start)
        if remaining <= 0:
            raise TimeoutError("PARSE_TIMEOUT")
        options = PdfPipelineOptions(artifacts_path=Path(lock["root"]),document_timeout=remaining,
                                     generate_page_images=True,images_scale=2,generate_parsed_pages=True)
        options.accelerator_options.device = "cpu"
        options.accelerator_options.num_threads = 4
        options.table_structure_options.mode = TableFormerMode.ACCURATE
        options.table_structure_options.do_cell_matching = True
        options.ocr_options = RapidOcrOptions(backend="onnxruntime",force_full_page_ocr=not policy["text_layer_usable"],
            det_model_path=str(Path(lock["root"])/lock["ocr"]["det"]["path"]),
            cls_model_path=str(Path(lock["root"])/lock["ocr"]["cls"]["path"]),
            rec_model_path=str(Path(lock["root"])/lock["ocr"]["rec"]["path"]),rapidocr_params=rapid_params(lock))
        from .source_backends import pdf_pipeline, verify_pdf_text_bindings
        provenance_observations={}
        converter = DocumentConverter(format_options={InputFormat.PDF:PdfFormatOption(
            pipeline_cls=pdf_pipeline(digest,policy["page"],provenance_observations),pipeline_options=options)})
        conversion = converter.convert(path,page_range=(policy["page"],policy["page"]))
        if conversion.status != ConversionStatus.SUCCESS or conversion.errors:
            raise RuntimeError("PDF conversion incomplete: " + str(conversion.status) + " " + str(conversion.errors))
        if len(conversion.document.pages) != 1:
            raise RuntimeError("PDF per-page conversion did not produce exactly one page")
        policy["native_text_bindings_verified"]=verify_pdf_text_bindings(conversion.document,provenance_observations)
        policy["provenance_transforms"]=provenance_observations.get("transforms",[])
        policy["formula_enrichment_enabled"]=options.do_formula_enrichment
        policy["ocr_cells"]=[]
        for converted_page in conversion.pages:
            if converted_page.size is None:
                raise RuntimeError("PDF converted page has no coordinate size")
            for cell in converted_page.cells:
                if cell.from_ocr:
                    bbox=cell.rect.to_bounding_box().to_top_left_origin(page_height=converted_page.size.height)
                    policy["ocr_cells"].append({"text":cell.text,"confidence":cell.confidence,
                                                "bbox":bbox.model_dump(mode="json")})
        if (not policy["text_layer_usable"] and not policy["ocr_cells"]
            and any(text for _,text,_ in field_values(conversion.document.model_dump(mode="json")))):
            raise RuntimeError("PDF OCR text has no retained source cells")
        documents.append(conversion.document)
    document = DoclingDocument.concatenate(documents)
    if sorted(document.pages) != list(range(1,len(policies)+1)):
        raise RuntimeError("PDF final page coverage mismatch")
    p = Parsed(document,scope={"selected_pages":list(range(1,len(policies)+1)),"excluded":[]},
               details={"page_ocr_policy":policies,"table_mode":"ACCURATE_CELL_MATCHING","timeout_seconds":1800})
    for field,text,label in field_values(document.model_dump(mode="json")):
        parts = field.split("/")
        item = document.texts[int(parts[2])] if parts[1]=="texts" else document.tables[int(parts[2])]
        if item.prov:
            regions=[]
            for prov in item.prov:
                box=prov.bbox
                if parts[1]=="tables" and len(item.prov)==1:
                    cell_box=getattr(item.data.table_cells[int(parts[5])],"bbox",None)
                    if cell_box is not None:
                        box=cell_box
                region={"kind":"pdf","file_sha256":digest,"page":prov.page_no,
                        "bbox":box.model_dump(mode="json")}
                if parts[1]=="texts":
                    start,end=prov.charspan
                    if not 0<=start<=end<=len(text):
                        raise RuntimeError("PDF provenance charspan outside native field")
                    region.update({"charspan":[start,end],"text_basis":"AUTOMATIC_NATIVE_FIELD_TEXT",
                                   "index_unit":"PYTHON_UNICODE_CODE_POINT","interval_semantics":"ZERO_BASED_HALF_OPEN"})
                regions.append(region)
            pages={region["page"] for region in regions}
            locator=base_location("pdf",digest,page=next(iter(pages)) if len(pages)==1 else None,
                bbox=regions[0]["bbox"] if len(regions)==1 else None,source_group=regions)
            locator["granularity"]="REGION"
            locator["basis"]="COMPONENT_PROVENANCE_SOURCE_GROUP"
            p.locations[field]=locator
            if parts[1]=="texts":
                uncovered=[i for i in range(len(text)) if not any(a["charspan"][0]<=i<a["charspan"][1] for a in regions)]
                if uncovered:
                    p.details.setdefault("provenance_unlocated_characters",[]).append({
                        "field":field,"indices":uncovered,"characters":"".join(text[i] for i in uncovered),
                        "classification":"WHITESPACE_ONLY" if all(text[i].isspace() for i in uncovered) else "UNRESOLVED_TEXT"})
                    if any(not text[i].isspace() for i in uncovered):
                        p.issue("WARN","PDF_PROVENANCE_TEXT_COVERAGE",field=field)
        if "\ufffd" in text:
            p.issue("WARN","REPLACEMENT_CHARACTER",field=field)
        ocr_cells=[]
        for prov in item.prov:
            item_box=prov.bbox.to_top_left_origin(page_height=document.pages[prov.page_no].size.height)
            if parts[1]=="tables":
                cell_box=getattr(item.data.table_cells[int(parts[5])],"bbox",None)
                if cell_box is not None:
                    item_box=cell_box.to_top_left_origin(page_height=document.pages[prov.page_no].size.height)
            for cell in policies[prov.page_no-1]["ocr_cells"]:
                bbox=cell["bbox"]
                if max(item_box.l,bbox["l"])<min(item_box.r,bbox["r"]) and max(item_box.t,bbox["t"])<min(item_box.b,bbox["b"]):
                    ocr_cells.append(cell)
        if ocr_cells and re.search("[0-9]",text):
            p.issue("WARN","OCR_NUMERIC_REVIEW",field=field,text=text,
                    ocr_basis="ACTUAL_FROM_OCR_CELLS_OVERLAPPING_FIELD_REGION",
                    pages=sorted({prov.page_no for prov in item.prov}))
        if any(cell["confidence"]<.90 for cell in ocr_cells):
            p.issue("WARN","OCR_REVIEW_CANDIDATE",field=field,text=text,
                    confidence=min(cell["confidence"] for cell in ocr_cells))
    p.images = [{"page":n,"width":page.size.width,"height":page.size.height,"dpi":144} for n,page in document.pages.items()]
    return p


def native_document(path, kind, source_nodes=None, source_digest=None):
    from docling.datamodel.base_models import InputFormat,ConversionStatus
    from docling.document_converter import DocumentConverter,WordFormatOption,MarkdownFormatOption,HTMLFormatOption
    from docling.datamodel.backend_options import MsWordBackendOptions,MarkdownBackendOptions,HTMLBackendOptions
    from .source_backends import html_backend,word_backend
    observations={}
    digest=source_digest or hash_file(path)[0]
    html_options={"backend_options":HTMLBackendOptions(enable_local_fetch=False,enable_remote_fetch=False,fetch_images=False,render_page=False)}
    html_options["backend"]=html_backend(source_nodes or {},digest,observations)
    mapping = {"md":(InputFormat.MD,MarkdownFormatOption(backend_options=MarkdownBackendOptions(enable_local_fetch=False,enable_remote_fetch=False,fetch_images=False))),
               "html":(InputFormat.HTML,HTMLFormatOption(**html_options)),
               "docx":(InputFormat.DOCX,WordFormatOption(backend=word_backend(digest,observations),backend_options=MsWordBackendOptions(enable_local_fetch=False,enable_remote_fetch=False)))}
    fmt,option = mapping[kind]
    result = DocumentConverter(format_options={fmt:option}).convert(path)
    if result.status != ConversionStatus.SUCCESS or result.errors:
        raise RuntimeError("native conversion incomplete: " + str(result.status))
    p=Parsed(result.document,scope={"selected":"DOCUMENT_BODY","excluded":[]},
             details={"remote_fetch":False,"local_component_fetch":False,"format":kind,
                      "external_resource_policy":"EXPLICIT_BACKEND_FETCH_DISABLED_PROJECT_BOUND_ARCHIVES_ONLY"})
    if kind == "html":
        p.details["hyperlink_policy"] = "ARCHIVE_ONLY_LINK_TARGETS_TEXT_PRESERVED"
    protected=[]
    for field,text,_label in field_values(result.document.model_dump(mode="json")):
        observed=observations.get(field)
        parts=field.split("/")
        node=result.document.texts[int(parts[2])] if parts[1]=="texts" else result.document.tables[int(parts[2])].data.table_cells[int(parts[5])]
        if (observed is None or observed["text"]!=text or observed["node"] is not node
            or observations.get("_document") is not result.document):
            continue
        p.locations[field]=observed["locator"]
        if observed["protected"]:
            protected.append(field)
    p.details["protected_fields"]=protected
    p.details["direct_source_node_count"]=len(p.locations)
    return p

def raw_anchors(path,kind,digest,body_xpath=None):
    result = []
    if kind=="md":
        from markdown_it import MarkdownIt
        from mdit_py_plugins.front_matter import front_matter_plugin
        from mdit_py_plugins.footnote import footnote_plugin
        from mdit_py_plugins.dollarmath import dollarmath_plugin
        parser = MarkdownIt("commonmark").enable("table").use(front_matter_plugin).use(footnote_plugin).use(dollarmath_plugin)
        source = path.read_text(encoding="utf-8-sig")
        lines = source.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1]+len(line))
        tokens = parser.parse(source)
        in_table=False
        quote_depth=0
        heading_regions={}
        for i,token in enumerate(tokens):
            if token.type=="blockquote_open":
                quote_depth+=1
            elif token.type=="blockquote_close":
                quote_depth-=1
            if token.type=="table_open":
                in_table=True
            elif token.type=="table_close":
                in_table=False
            if token.map and token.type in {"inline","fence","code_block"} and token.content:
                start,end=token.map
                locator=base_location("md",digest,line_start=start+1,line_end=end,
                                      source_start=offsets[start],source_end=offsets[end])
                label = "code" if token.type in {"fence","code_block"} else "text"
                visible = token.content
                children=token.children or []
                if label == "code":
                    visible = token.content.strip()
                    locator={**locator,"granularity":"REGION",
                             "basis":"ORIGINAL_MARKDOWN_CODE_BLOCK_REGION"}
                if token.type == "inline":
                    visible = "".join(child.content if child.type not in {"softbreak","hardbreak"} else "\n" for child in children if child.type not in {"image","html_inline"})
                if token.type=="inline" and i>0 and tokens[i-1].type=="heading_open" and tokens[i-1].tag in {"h1","h2","h3","h4","h5","h6"}:
                    heading_regions[(offsets[start],offsets[end])] = {
                        "level":int(tokens[i-1].tag[1:]),
                        "complete_text":visible if children and all(child.type=="text" for child in children) else None,
                    }
                plain=token.type=="inline" and i>0 and tokens[i-1].type=="paragraph_open" and not any(child.type in {"code_inline","math_inline","html_inline","image"} for child in children)
                if in_table and token.type=="inline":
                    if not any(child.type in {"image","html_inline","math_inline"} for child in children):
                        result.append({"id":str(i),"text":visible.strip(),"kind":"cell","plain":False,
                                       "locator":{**locator,"granularity":"REGION","basis":"ORIGINAL_MARKDOWN_TABLE_CELL_IN_ROW"}})
                    continue
                # Bind a complete simple callout, including its marker and body.
                # Native emphasis/link fragments must match the whole block in order.
                callout=re.fullmatch(r"\[!([A-Za-z0-9_-]+)\]([+-]?)[ \t]*([^\n]*)(?:\n(.*))?",token.content,re.S) if token.type=="inline" else None
                simple_quote=(quote_depth==1 and i>=2 and i+2<len(tokens)
                    and tokens[i-2].type=="blockquote_open"
                    and tokens[i-2].map==token.map
                    and tokens[i+1].type=="paragraph_close"
                    and tokens[i+2].type=="blockquote_close")
                unsupported_callout=any(child.type in {"image","html_inline","code_inline","math_inline"} for child in children)
                if callout and simple_quote and not unsupported_callout:
                    complete="".join(" " if child.type in {"softbreak","hardbreak"} else child.content for child in children).strip()
                    raw_lines=[re.sub(r"^ {0,3}>[ \t]?", "", line.rstrip("\r\n")) for line in lines[start:end]]
                    original_body="\n".join(raw_lines[1:])
                    result.append({"id":"callout-"+str(i),"text":complete,"kind":"text","plain":False,
                        "locator":{**locator,"granularity":"REGION","basis":"ORIGINAL_COMPLETE_OBSIDIAN_CALLOUT"},
                        "complete_callout_text":complete,
                        "callout":{"type":callout.group(1),"fold":callout.group(2),
                            "title":callout.group(3),"body":original_body}})
                    continue
                leaf=[child for child in children if child.type in {"text","code_inline","math_inline","image"} and child.content.strip()]
                if token.type=="inline" and len(leaf)>1:
                    unsupported=any(child.type in {"image","html_inline"} or
                        (child.type=="math_inline" and child.markup!="$") for child in children)
                    # Preserve literal delimiters, including currency parsed as inline math.
                    # Binding still requires equality with the complete native block.
                    rendered=None if unsupported else "".join(
                        " " if child.type in {"softbreak","hardbreak"} else
                        child.markup+child.content+child.markup if child.type=="math_inline" else
                        child.content for child in children).strip()
                    for child_index,child in enumerate(leaf):
                        fragment=child.content if child.type=="code_inline" else child.content.strip()
                        child_kind="code" if child.type=="code_inline" else "text"
                        child_location={**locator,"granularity":"REGION",
                                        "basis":"ORIGINAL_MARKDOWN_INLINE_NODE_IN_BLOCK"}
                        result.append({"id":str(i)+"-"+str(child_index),"text":fragment,"kind":child_kind,
                                       "plain":plain and child.type=="text","locator":child_location,
                                       "rendered_block_text":rendered})
                elif token.type=="inline" and len(leaf)==1 and leaf[0].type=="image":
                    result.append({"id":str(i),"text":leaf[0].content.strip(),"kind":"text","plain":False,
                                   "locator":{**locator,"granularity":"REGION","basis":"ORIGINAL_MARKDOWN_IMAGE_ALT"}})
                else:
                    if token.type=="inline" and len(leaf)==1 and leaf[0].type=="code_inline":
                        label="code"
                    result.append({"id":str(i),"text":visible,"kind":label,"plain":plain,"locator":locator})
            elif token.map and token.type in {"table_open","math_block","html_block"}:
                result.append({"id":"barrier-"+str(i),"text":"","kind":"barrier","plain":False,"locator":None})
        # Keep original heading identity on every inline fragment. Complete plain
        # headings can additionally disambiguate repeated text by exact level.
        for anchor in result:
            location=anchor.get("locator")
            if not isinstance(location,dict):
                continue
            heading=heading_regions.get((location.get("source_start"),location.get("source_end")))
            if heading is not None:
                anchor["markdown_heading_level"]=heading["level"]
                if heading["complete_text"] is not None and anchor["text"]==heading["complete_text"]:
                    anchor["complete_heading_text"]=heading["complete_text"]
    elif kind=="html":
        tree=parse_captured_html(path.read_bytes())
        selected = tree.xpath(body_xpath) if body_xpath else [tree]
        if len(selected)!=1:
            raise ValueError("HTML body selection is not unique")
        for node in selected[0].xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6|.//p|.//li|.//pre|.//td|.//th|.//caption"):
            if node.xpath("ancestor::script|ancestor::style|ancestor::nav|ancestor::footer"):
                continue
            text="".join(node.itertext())
            if text.strip():
                result.append({"id":tree.getroottree().getpath(node),"text":text,
                               "kind":"cell" if node.tag in {"td","th"} else "code" if node.tag=="pre" else "text",
                               "plain":node.tag=="p" and not node.xpath(".//code|.//math|.//img"),
                               "locator":base_location("html",digest,xpath=tree.getroottree().getpath(node))})
    elif kind=="docx":
        from docx import Document
        document=Document(path)
        namespace={"w":"http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        for node in document.element.body.xpath(".//w:p"):
            text="".join(node.xpath(".//w:t/text()"))
            if text:
                xpath=node.getroottree().getpath(node)
                result.append({"id":xpath,"text":text,"kind":"cell" if node.xpath("ancestor::w:tc") else "text",
                               "plain":not node.xpath("ancestor::w:tc|.//w:pPr/w:pStyle") and not any(n.tag.endswith("}oMath") for n in node.iter()),
                               "locator":base_location("docx",digest,xml_path=xpath)})
        # Referenced ordinary notes are retained and bound by the Word backend.
        # Tracked changes and comments still require a separately resolved copy.
        unsupported=document.element.body.xpath(".//w:ins|.//w:del|.//w:commentReference")
        if unsupported:
            raise ValueError("DOCX contains unsupported revisions/comments")
    return result


def bind_native(p,path,kind,digest,body_xpath=None):
    import copy
    from .alignment import align
    anchors=raw_anchors(path,kind,digest,body_xpath)
    for anchor in anchors:
        if anchor["kind"]=="text":
            anchor["kind"]="paragraph"
    fields=[]
    for item,_level in p.document.iterate_items():
        ref=item.self_ref.replace("#","")
        if ref.startswith("/texts/"):
            text=item.text
            label=item.label.value
            # Docling retains empty list containers for nested Markdown lists.
            # Their children carry the content; the container is not a text field.
            if label=="list_item" and not text.strip() and item.children:
                continue
            field={"pointer":ref+"/text","text":text,
                "kind":"code" if label=="code" else "paragraph",
                "plain":label in {"text","paragraph"} and "formatting" in type(item).model_fields and not getattr(item,"formatting",None) and not any(c in text for c in ("\u0060","$"))}
            if kind=="md" and label=="title":
                field["markdown_heading_level"]=1
            elif kind=="md" and label=="section_header":
                native_level=getattr(item,"level",None)
                if type(native_level) is int and 1<=native_level<=5:
                    field["markdown_heading_level"]=native_level+1
            fields.append(field)
        elif ref.startswith("/tables/"):
            for j,cell in enumerate(item.data.table_cells):
                # Empty cells retain their grid entry but have no text to align.
                if cell.text == "":
                    continue
                fields.append({"pointer":ref+"/data/table_cells/"+str(j)+"/text","text":cell.text,"kind":"cell","plain":False})
    accepted,diagnostics=align(anchors,fields)
    protected=set(p.details.get("protected_fields",[])) | {field["pointer"] for field in fields if not field["plain"]}
    matched=set(p.locations)
    for aa,bb,rule in accepted:
        source_group=[anchors[i]["locator"] for i in aa]
        for j in bb:
            if fields[j]["pointer"] in p.locations:
                continue
            matched.add(fields[j]["pointer"])
            if any(not anchors[i]["plain"] for i in aa):
                protected.add(fields[j]["pointer"])
            locator=copy.deepcopy(source_group[0])
            locator["basis"]=rule
            if len(aa)>1 or len(bb)>1:
                locator["source_group"]=source_group
                locator["granularity"]="REGION"
                locator["basis"]=rule if rule=="T17_EXACT_COMPLETE_OBSIDIAN_CALLOUT" else "T14_EXACT_GROUP_NO_INTRA_GROUP_CHARACTER_ALIGNMENT"
            p.locations[fields[j]["pointer"]]=locator
    diagnostics=[d for d in diagnostics if d["field"] not in p.locations]
    for diagnostic in diagnostics:
        p.issue("WARN","COARSE_SOURCE_MAPPING",**diagnostic)
    if any(not anchor["plain"] and anchor["kind"]!="barrier" for anchor in anchors):
        protected.update(field["pointer"] for field in fields if field["pointer"] not in matched)
    p.details["protected_fields"]=sorted(protected)
    p.details["cleanup_protection_basis"]="ORIGINAL_STRUCTURE_OR_CONSERVATIVE_UNMATCHED_FIELD"
    p.details["source_anchor_count"]=len(anchors)
    p.details["obsidian_callout_regions"]=[{**anchor["callout"],"locator":anchor["locator"],
        "field_pointers":[fields[j]["pointer"] for aa,bb,_rule in accepted if i in aa for j in bb],
        "complete_binding":any(i in aa for aa,_bb,_rule in accepted)}
        for i,anchor in enumerate(anchors) if "complete_callout_text" in anchor]
    p.details["source_alignment"]={"rules":["T12_EXACT_UNIQUE","T13_EXACT_COMPLETE_MARKDOWN_BLOCK",
                                   "T14_EXACT_NORMALIZED_CONCAT","T15_EXACT_RENDERED_MARKDOWN_BLOCK",
                                   "T16_EXACT_NORMALIZED_PLAIN_MARKDOWN_BLOCK","T17_EXACT_COMPLETE_OBSIDIAN_CALLOUT",
                                   "T18_EXACT_COMPLETE_MARKDOWN_HEADING_LEVEL"],
                                   "accepted_rules":sorted({rule for _aa,_bb,rule in accepted}),
                                   "accepted_groups":len(accepted),"diagnostic_only":len(diagnostics)}
    return p

def convert(path,config,lock,workdir=None,resource_observations=None):
    suffix=path.suffix.lower()
    kind=config.get("format") or {".md":"md",".markdown":"md",".txt":"txt",".html":"html",".htm":"html",".docx":"docx",
        ".xlsx":"xlsx",".csv":"csv",".json":"json",".pdf":"pdf",".png":"image",".jpg":"image",
        ".jpeg":"image",".webp":"image",".bmp":"image",".tif":"image",".tiff":"image",".gif":"image",".svg":"image"}.get(suffix)
    if kind not in {"md","txt","html","docx","xlsx","csv","json","pdf","image"}:
        raise ValueError("unsupported input format")
    if kind=="txt":
        return text_document(path)
    if kind=="pdf":
        return pdf_document(path,lock)
    if kind=="image":
        return image_document(path,lock,cache_dir=workdir)
    if kind=="xlsx":
        return xlsx_document(path,config.get("xlsx",{}))
    if kind=="csv":
        return csv_document(path,config.get("csv",{}))
    if kind=="json":
        return json_document(path,config.get("json",{}))
    body_xpath=config.get("html",{}).get("body_xpath") if kind=="html" else None
    input_path=path
    metadata={}
    source_nodes=None
    if kind=="html":
        from lxml import html
        from ..ingestion.archive import write_closed
        tree=parse_captured_html(path.read_bytes())
        body=tree.xpath(body_xpath) if body_xpath else [tree]
        if len(body)!=1:
            raise ValueError("HTML selected body XPath is not unique")
        if workdir is None:
            raise ValueError("scoped HTML conversion needs its attempt directory")
        from .source_backends import HTML_SOURCE_ATTR
        source_nodes={}
        for index,node in enumerate(body[0].iter()):
            if not isinstance(node.tag,str):
                continue
            if HTML_SOURCE_ATTR in node.attrib:
                raise ValueError("HTML contains reserved source observation attribute")
            key="source-"+str(index)
            source_nodes[key]={"xpath":node.getroottree().getpath(node),
                "protected":node.tag in {"code","pre","math"} or bool(node.xpath(".//code|.//pre|.//math|.//img"))}
            from .html_blocks import block_source
            source_nodes[key].update(block_source(node))
            node.set(HTML_SOURCE_ATTR,key)
        input_path=workdir/"selected-body.html"
        write_closed(input_path,html.tostring(body[0],encoding="utf-8",method="html"))
        metadata={"selected_xpath":body_xpath,"excluded":"NODES_OUTSIDE_BOUND_BODY_XPATH" if body_xpath else [],
                  "source_annotation":"PROJECT_GENERATED_IDS_BOUND_TO_ORIGINAL_ARCHIVED_XPATHS"}
    if kind=="md":
        import yaml
        from ..ingestion.archive import write_closed
        text=path.read_text(encoding="utf-8-sig")
        if text.startswith("---\n") or text.startswith("---\r\n"):
            lines=text.splitlines(keepends=True)
            closing=next((i for i,line in enumerate(lines[1:],1) if line.strip()=="---"),None)
            if closing is None:
                raise ValueError("unterminated Markdown front matter")
            class Loader(yaml.SafeLoader):
                pass
            # Obsidian dates are metadata text. Disable only this loader's
            # timestamp coercion so cached metadata remains JSON serializable.
            Loader.yaml_implicit_resolvers = {
                key: [(tag, rule) for tag, rule in rules if tag != "tag:yaml.org,2002:timestamp"]
                for key, rules in Loader.yaml_implicit_resolvers.items()
            }
            def unique_mapping(loader,node):
                return no_duplicates([(loader.construct_object(k),loader.construct_object(v)) for k,v in node.value])
            Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,unique_mapping)
            front=yaml.load("".join(lines[1:closing]),Loader=Loader)
            if front is not None and not isinstance(front,dict):
                raise ValueError("Markdown front matter must be a metadata object")
            if workdir is None:
                raise ValueError("front-matter Markdown conversion needs its attempt directory")
            input_path=workdir/"selected-body.md"
            write_closed(input_path,("".join("\n" for _ in lines[:closing+1])+"".join(lines[closing+1:])).encode("utf-8"))
            metadata["front_matter"]=front
        metadata["wiki_links"]=re.findall(r"!?\[\[([^\]]+)\]\]",text)
        metadata["callouts"]=re.findall(r"^>\s*\[!([^\]]+)\]",text,re.M)
        metadata["note_embeds"]="REFERENCES_ONLY_NO_RECURSION"
    if kind=="md" and re.search(r"</?(?:div|table|p|h[1-6]|details|summary|a|span|img)\b",text,re.I):
        from .markdown_html import convert_markdown
        p=convert_markdown(input_path,path,workdir)
    else:
        p=native_document(input_path,kind,source_nodes=source_nodes,source_digest=hash_file(path)[0])
    p.details.update(metadata)
    if body_xpath:
        p.scope={"selected":body_xpath,"excluded":"NODES_OUTSIDE_BOUND_BODY_XPATH"}
    p=bind_native(p,path,kind,hash_file(path)[0],body_xpath)
    append_bound_resources(p,path,kind,config,lock,resource_observations or {},workdir=workdir)
    return p


_RAPID_ENGINES={}


def rapid_engine(lock):
    from rapidocr import RapidOCR
    key=lock["lock_sha256"]
    if key not in _RAPID_ENGINES:
        _RAPID_ENGINES[key]=RapidOCR(params=rapid_params(lock))
    return _RAPID_ENGINES[key]


def exif_inverse(orientation,width,height):
    matrices={
        1:[[1,0,0],[0,1,0],[0,0,1]],
        2:[[-1,0,width],[0,1,0],[0,0,1]],
        3:[[-1,0,width],[0,-1,height],[0,0,1]],
        4:[[1,0,0],[0,-1,height],[0,0,1]],
        5:[[0,1,0],[1,0,0],[0,0,1]],
        6:[[0,1,0],[-1,0,height],[0,0,1]],
        7:[[0,-1,width],[-1,0,height],[0,0,1]],
        8:[[0,-1,width],[1,0,0],[0,0,1]],
    }
    if orientation not in matrices:
        raise ValueError("unsupported EXIF orientation")
    return matrices[orientation]


def append_bitmap(p,image_path,locator,lock,*,graphic_review=True,workdir=None):
    child=image_document(image_path,lock,cache_dir=workdir)
    first=max(p.document.pages,default=0)
    old_to_new={}
    for n,page in child.document.pages.items():
        new_page=first+n
        p.document.add_page(new_page,page.size,page.image)
        old_to_new[n]=new_page
    for i,item in enumerate(child.document.texts):
        old_key="/texts/"+str(i)+"/text"
        source=child.locations[old_key]
        prov=item.prov[0].model_copy(update={"page_no":old_to_new[item.prov[0].page_no]})
        added=p.document.add_text(label=DocItemLabel.TEXT,text=item.text,prov=prov)
        key=added.self_ref.replace("#","")+"/text"
        composite={**locator,"resource_sha256":source["file_sha256"],"region":source["region"],
                   "transform":{**source["transform"],**locator.get("transform",{}),
                                "source_frame":source["frame"],"source_frame_group":source.get("source_group",[])},"granularity":"REGION"}
        p.locations[key]=composite
        for issue in child.quality:
            if issue.get("field")==old_key:
                p.quality.append({**issue,"field":key})
    for record in child.images:
        p.images.append({**record,"page":old_to_new[record["page"]],"resource_sha256":hash_file(image_path)[0],
                         "source_locator":locator})
    if graphic_review:
        p.issue("WARN","BITMAP_STRUCTURE_REVIEW_REQUIRED",locator=locator,resource_sha256=hash_file(image_path)[0],
                reason="OCR text is extracted; non-text graphic meaning is not inferred.")
    return len(child.document.texts)


def append_bound_resources(p,path,kind,config,lock,observations,*,workdir=None):
    digest=hash_file(path)[0]
    coverage=[]
    if kind=="html":
        for image in observations.get("image_observations",[]):
            relative=image["local_path"]
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("unsafe archived image path")
            resource=path.parent/relative
            if hash_file(resource)[0]!=image["sha256"]:
                raise ValueError("HTML image differs from captured resource")
            loc=base_location("html",digest,xpath=image["node_path"],
                              transform={"dom_region":image["region"],"dom_coordinates":"CAPTURED_CSS_PIXELS"})
            count=append_bitmap(p,resource,loc,lock,workdir=workdir)
            coverage.append({"xpath":image["node_path"],"resource_sha256":image["sha256"],
                             "ocr_text_count":count,"graphic_semantics":"REVIEW_REQUIRED"})
    elif kind=="md":
        from ..ingestion.archive import verify_archive
        lines=path.read_text(encoding="utf-8-sig").splitlines()
        for resource in config.get("linked_resources",[]):
            archive=verify_archive(Path(resource["archive_manifest_path"]))
            if archive["manifest_sha256"]!=resource["archive_manifest_sha256"]:
                raise ValueError("linked Markdown archive changed")
            image_path=Path(resource["archive_manifest_path"]).parent/resource["member"]
            if hash_file(image_path)[0]!=resource["sha256"]:
                raise ValueError("Markdown linked image changed")
            from html import unescape
            locations=[i+1 for i,line in enumerate(lines) if resource["reference"] in unescape(line)]
            if not locations:
                p.issue("WARN","MARKDOWN_IMAGE_LOCATION_AMBIGUOUS",reference=resource["reference"])
                continue
            loc=base_location("md",digest,line_start=locations[0],line_end=locations[-1])
            if len(locations)>1:
                loc["source_group"]=[{"policy":"IDENTICAL_ARCHIVED_IMAGE_REPEATED_REFERENCES_V1",
                    "file_sha256":digest,"resource_sha256":resource["sha256"],
                    "reference":resource["reference"],"line_numbers":locations}]
            count=append_bitmap(p,image_path,loc,lock,workdir=workdir)
            coverage.append({"reference":resource["reference"],"resource_sha256":resource["sha256"],
                             "ocr_text_count":count,"graphic_semantics":"REVIEW_REQUIRED"})
    if coverage:
        p.details["bitmap_coverage"]=coverage
