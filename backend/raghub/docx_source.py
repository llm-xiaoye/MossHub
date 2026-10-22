"""Read Word body, header and footer text without executing document content."""
from pathlib import Path
from docx import Document
from docx.oxml.ns import qn


def note_rows(document):
    """Read referenced footnotes/endnotes from their original package XML."""
    from lxml import etree
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    rows = []
    for kind, section in (("footnote", "脚注"), ("endnote", "尾注")):
        references = document.element.body.xpath(".//w:" + kind + "Reference")
        wanted = {node.get(qn("w:id")) for node in references}
        if not wanted:
            continue
        name = "word/" + kind + "s.xml"
        parts = [part for part in document.part.package.parts
                 if str(part.partname).lstrip("/") == name]
        if len(parts) != 1:
            raise ValueError("DOCX note references have no unique package part")
        root = etree.fromstring(parts[0].blob, etree.XMLParser(resolve_entities=False, no_network=True))
        notes = {}
        for note in root.findall("w:" + kind, namespace):
            note_id = note.get(qn("w:id"))
            if note_id in notes:
                raise ValueError("DOCX note IDs are not unique")
            notes[note_id] = note
        for note_id in wanted:
            if note_id not in notes or notes[note_id].get(qn("w:type")) not in {None, "normal"}:
                raise ValueError("DOCX note reference does not bind an ordinary note")
        for note_id, note in notes.items():
            if note_id not in wanted:
                continue
            for paragraph in note.iter(qn("w:p")):
                text = "".join(node.text or "" if node.tag == qn("w:t") else
                               "\t" if node.tag == qn("w:tab") else "\n"
                               for node in paragraph.iter()
                               if node.tag in {qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr")})
                if text.strip():
                    rows.append({"part": name, "section": section, "note_id": note_id,
                                 "xml_path": paragraph.getroottree().getpath(paragraph), "text": text})
    return rows


def original_rows(path: Path):
    document = Document(path)
    sections = {qn("w:document"): "正文", qn("w:hdr"): "页眉", qn("w:ftr"): "页脚"}
    parts = []
    for part in document.part.package.parts:
        root = getattr(part, "element", None)
        if root is not None and root.tag in sections:
            parts.append((str(part.partname).lstrip("/"), root))
    parts.sort(key=lambda item: (item[0] != "word/document.xml", item[0]))
    rows = []
    for name, root in parts:
        for paragraph in root.iter(qn("w:p")):
            text = []
            for node in paragraph.iter():
                # A text box can nest paragraphs; each XML paragraph is shown once.
                owner = next(node.iterancestors(qn("w:p")), None)
                if owner is not paragraph:
                    continue
                if node.tag == qn("w:t"):
                    text.append(node.text or "")
                elif node.tag == qn("w:tab"):
                    text.append("\t")
                elif node.tag in {qn("w:br"), qn("w:cr")}:
                    text.append("\n")
            value = "".join(text)
            if value.strip():
                rows.append({"part": name, "section": sections[root.tag],
                             "xml_path": paragraph.getroottree().getpath(paragraph), "text": value})
    return rows + note_rows(document)

