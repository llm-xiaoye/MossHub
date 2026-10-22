"""Bound each Apple Vision page to the requested DY image post, without claiming visual review."""
from __future__ import annotations
import hashlib
import re
from pathlib import Path
from .douyin_intake import object_json
from .ingestion.archive import inside
from .ingestion.store import BusinessError

OCR_NOTICE = "本笔记根据 Apple Vision OCR 提取的图片文字自动整理；原图已保留，图示含义尚未核对。"

def image_material(item, state_item, tool, video_id):
    def fail(message):
        raise BusinessError("DY_IMAGE_EVIDENCE_INVALID", message)
    def read(path):
        if path.stat().st_size > 2 * 1024 * 1024:
            fail("图文识别记录过大，已保留材料，尚未生成笔记。")
        return object_json(path.read_bytes())
    try:
        bundle_path = Path(state_item["bundle"]).resolve(strict=True)
        inside(bundle_path, Path(tool["cache_dir"]))
        bundle = read(bundle_path)
        transcript_path = Path(item["material_file"]).resolve(strict=True)
        raw_bytes = transcript_path.read_bytes()
        if len(raw_bytes) > 2 * 1024 * 1024:
            fail("图文识别记录过大，已保留材料，尚未生成笔记。")
        raw = object_json(raw_bytes)
        if (bundle.get("media_kind") != "image" or str(bundle.get("aweme_id")) != video_id
                or bundle.get("source_url") != "https://www.douyin.com/note/" + video_id
                or raw.get("source_kind") != "image" or str(raw.get("aweme_id")) != video_id
                or raw.get("engine") != "Apple Vision"
                or state_item.get("transcript_sha256") != hashlib.sha256(raw_bytes).hexdigest()):
            fail("图文的作品、识别工具或内容记录不一致，已停止保存。")
        images, pages, files = bundle.get("images"), raw.get("pages"), item.get("image_files")
        count = bundle.get("image_count")
        if (type(count) is not int or not 1 <= count <= 50 or not isinstance(images, list)
                or not isinstance(pages, list) or not isinstance(files, list)
                or len(images) != count or len(pages) != count or len(files) != count):
            fail("没有取得完整的图文页数，已停止保存；不会跳过缺页后生成笔记。")
        total_bytes, texts, empty = 0, [], []
        for number, (image, page, file) in enumerate(zip(images, pages, files), 1):
            path = (bundle_path.parent / image["file"]).resolve(strict=True)
            inside(path, bundle_path.parent)
            if path != Path(file).resolve(strict=True):
                fail("原图顺序与本次图文记录不一致，已停止保存。")
            size = path.stat().st_size
            total_bytes += size
            if size > 50 * 1024 * 1024 or total_bytes > 500 * 1024 * 1024:
                fail("图文图片超过导入大小限制，原材料保留，尚未保存笔记。")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            text = page.get("text")
            if (type(page.get("page")) is not int or page["page"] != number
                    or type(image.get("page")) is not int or image["page"] != number
                    or image.get("sha256") != digest or page.get("sha256") != digest
                    or not isinstance(text, str)):
                fail("有图片或识别文字与原始记录不一致，已停止保存。")
            if not text.strip():
                empty.append(number)
            texts.append("第 " + str(number) + " 页\n" + (text.strip() or "本页未识别到文字，请查看原图。"))
        if len(empty) == count:
            fail("Apple Vision OCR 没有识别到可用文字。原图已保留；本入口无法只根据封面或配乐整理图文知识。")
        material = "\n\n".join(texts)
        caption = raw.get("caption", "")
        if isinstance(caption, str) and caption.strip():
            material = "作品说明（作者提供）：\n" + caption.strip() + "\n\n" + material
        if len(material.encode("utf-8")) > 48000:
            fail("图文文字超过一次自动整理范围；全部原图和识别文字已保留，尚未生成笔记。")
        return material, count, empty
    except BusinessError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BusinessError("DY_IMAGE_EVIDENCE_INVALID", "无法核对全部原图与识别文字，材料保留，尚未生成笔记。") from exc

def bind_ocr_note(body, analysis, page_count):
    """OCR page coverage is mechanical; it must never be labelled reviewed_pages."""
    pages = list(range(1, page_count + 1))
    matches, fence = [], None
    for line in body.splitlines():
        marker = re.match(r"^\s*(\x60{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
        match = re.fullmatch(r"\{\{DY_IMAGE:([1-9][0-9]*)\}\}", line)
        if match and fence is None:
            matches.append(match[1])
    if sorted(int(value) for value in matches) != pages or body.count("{{DY_IMAGE:") != page_count:
        raise ValueError("every source page must appear once outside prose")
    if "reviewed_pages" in analysis or "ocr_pages" in analysis:
        raise ValueError("model cannot self-certify source page coverage")
    analysis = dict(analysis, image_review_mode="ocr_text_only", ocr_pages=pages)
    lines = body.splitlines()
    lines.insert(1, "\n" + OCR_NOTICE + "\n")
    return "\n".join(lines), analysis
