"""Rasterize a passive, self-contained SVG with the already installed browser.

The original SVG stays in the archive. The PNG is an explicitly recorded render,
with scripts, animation and every network request disabled.
"""
from __future__ import annotations
import re
from lxml import etree

POLICY = "PASSIVE_SVG_CHROMIUM_NO_SCRIPT_NO_NETWORK_V1"


class SvgImageError(ValueError):
    pass


def svg_markup(content: bytes) -> str | None:
    if not content.lstrip().startswith(b"<"):
        return None
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True,
                             recover=False, huge_tree=False)
    try:
        root = etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError:
        return None
    if not isinstance(root.tag, str) or etree.QName(root).localname.lower() != "svg":
        return None
    if root.getroottree().docinfo.doctype:
        raise SvgImageError("SVG 含外部实体或文档类型声明，暂不能安全处理。")
    forbidden = {"script", "foreignobject", "iframe", "object", "embed", "audio",
                 "video", "animate", "animatemotion", "animatetransform", "set"}
    for node in root.iter():
        if not isinstance(node.tag, str):
            if isinstance(node, etree._Entity):
                raise SvgImageError("SVG 含实体引用，暂不能安全处理。")
            continue
        name = etree.QName(node).localname.lower()
        if name in forbidden:
            raise SvgImageError("SVG 含脚本、动画或嵌入内容，需另存为静态图片后导入。")
        values = []
        if name == "style":
            values.append(node.text or "")
        for key, value in node.attrib.items():
            local = etree.QName(key).localname.lower()
            if local.startswith("on") or local in {"src", "base"}:
                raise SvgImageError("SVG 含活动属性，需另存为静态图片后导入。")
            if local == "href" and not value.strip().startswith("#"):
                raise SvgImageError("SVG 依赖其他文件，需先保存为完整静态图片后导入。")
            values.append(value)
        for value in values:
            lower = value.lower()
            if "@import" in lower or "@font-face" in lower:
                raise SvgImageError("SVG 含外部样式或字体，需先保存为完整静态图片后导入。")
            for match in re.finditer(r"url\s*\(([^)]*)\)", value, flags=re.IGNORECASE):
                target = match.group(1).strip().strip("\"\x27").strip()
                if not target.startswith("#"):
                    raise SvgImageError("SVG 依赖外部资源，需先保存为完整静态图片后导入。")
    return etree.tostring(root, encoding="unicode")


async def rasterize_svg(markup: str) -> tuple[bytes, dict]:
    from playwright.async_api import async_playwright
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(headless=True)
        try:
            context = await browser.new_context(java_script_enabled=False, offline=True,
                service_workers="block", accept_downloads=False,
                viewport={"width": 1600, "height": 1200}, device_scale_factor=1)
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            network_attempts = []
            page.on("request", lambda request: network_attempts.append(request.url))
            page.set_default_timeout(12000)
            await page.set_content("<html><body style=\"margin:0\">" + markup + "</body></html>",
                                   wait_until="domcontentloaded", timeout=12000)
            node = page.locator("svg").first
            box = await node.bounding_box()
            if box is None or box["width"] <= 0 or box["height"] <= 0:
                raise SvgImageError("SVG 没有可读取的图像区域。")
            width, height = box["width"], box["height"]
            if width > 8192 or height > 8192 or width * height > 20_000_000:
                raise SvgImageError("SVG 尺寸超过图片处理上限。")
            png = await node.screenshot(type="png", animations="disabled", timeout=12000)
            if network_attempts:
                raise SvgImageError("SVG 尝试读取其他资源，需先保存为完整静态图片后导入。")
            return png, {"policy": POLICY, "source_format": "SVG", "output_format": "PNG",
                         "width": width, "height": height, "network": "BLOCKED",
                         "javascript": "DISABLED", "animation": "REJECTED"}
        finally:
            await browser.close()
