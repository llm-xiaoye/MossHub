"""Crawl4AI rendered snapshots, bounded public resources and offline derivations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata
import ipaddress
import json
import random
import socket
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote_to_bytes, urljoin, urlsplit

import httpx
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from crawl4ai.browser_manager import BrowserManager
from lxml import html as lxml_html
from PIL import Image

from .archive import json_hash, write_closed, json_bytes, verify_snapshot_image, parse_captured_html
from .contracts import MIB, url_source_key
from .ui_resources import partition_ui_resources, validate_rules


PUBLIC_BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


class AcquisitionError(RuntimeError):
    def __init__(self, code, message, *, details=None, retryable=False):
        super().__init__(message)
        self.code, self.details, self.retryable = code, details or {}, retryable


def load_policy(path: Path) -> dict:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or set(value) != {"schema_version", "sites"} or value["schema_version"] != 1:
        raise ValueError("invalid site policy")
    if not isinstance(value["sites"], dict) or not value["sites"]:
        raise ValueError("site policy must explicitly name public hosts")
    for hostname, site in value["sites"].items():
        if hostname != hostname.lower() or "*" in hostname or not isinstance(site, dict):
            raise ValueError("wildcard or malformed site policy")
        required = {"page_hosts", "render_hosts", "image_hosts", "body_selector", "wait_for"}
        if not required <= set(site) or set(site) - required - {"verified_ui_resources"}:
            raise ValueError("unknown site policy field")
        validate_rules(site.get("verified_ui_resources", []))
        for role in ("page_hosts", "render_hosts", "image_hosts"):
            hosts = site[role]
            if not isinstance(hosts, list) or not hosts or len(hosts) != len(set(hosts)):
                raise ValueError("each URL class requires explicit unique hosts")
            if any(not isinstance(host, str) or host != host.lower() or "*" in host or "/" in host for host in hosts):
                raise ValueError("only exact DNS hostnames are allowed")
    return value


async def public_url(value: str, hosts: list[str], *, phase=None, dns_scope=None) -> str:
    normalized = url_source_key(value)
    parsed = urlsplit(normalized)
    host = parsed.hostname
    if host not in hosts or host in {"localhost", "localhost.localdomain"}:
        raise AcquisitionError("URL_HOST_NOT_ALLOWED", "该域名未列入当前站点的明确白名单。", details={"url": value})
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise AcquisitionError("URL_LITERAL_ADDRESS", "不接受直接 IP 地址。")
    wait_id = None
    if phase is not None:
        phase["dns_wait_sequence"] += 1
        wait_id = phase["dns_wait_sequence"]
        phase["active_dns_waits"][wait_id] = {"wait_id": wait_id, "phase": "URL_DNS_CHECK",
                                               "scope": dns_scope, "url": normalized, "host": host}
    try:
        records = await asyncio.to_thread(socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                          0, socket.SOCK_STREAM)
    except asyncio.CancelledError:
        if phase is not None:
            phase["cancelled_dns_waits"].append(phase["active_dns_waits"][wait_id].copy())
        raise
    finally:
        if phase is not None:
            phase["active_dns_waits"].pop(wait_id)
    addresses = sorted({item[4][0] for item in records})
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise AcquisitionError("URL_NONPUBLIC_DNS", "域名解析包含非公网地址。", details={"host": host, "addresses": addresses})
    return normalized



def dns_wait_observation(phase: dict) -> dict:
    # This observes the Python await, never termination of getaddrinfo threads.
    return {"dns_thread_exit": "NOT_OBSERVED",
            "dns_wait_cancelled": True if phase["cancelled_dns_waits"] else "NOT_OBSERVED",
            "active_dns_waits": list(phase["active_dns_waits"].values()),
            "cancelled_dns_waits": list(phase["cancelled_dns_waits"]),
            "actual_phase_scope": "ACQUISITION_OPERATION"}


async def resolve_navigation(value: str, site: dict, deadline: float, cancelled, *, phase=None) -> tuple[str, list[dict]]:
    """Resolve checked HTTP redirects before browser navigation.

    Each request is a GET with redirects disabled. A later browser redirect is
    rejected rather than followed through an unchecked browser transport.
    """
    chain = []
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=60,
                                 headers={"User-Agent":PUBLIC_BROWSER_UA}) as client:
        for ordinal in range(7):
            if cancelled():
                raise AcquisitionError("CANCELLED", "任务已取消。")
            value = await public_url(value, site["page_hosts"], phase=phase, dns_scope="NAVIGATION_RESOLUTION")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcquisitionError("WEB_TOTAL_TIMEOUT", "网页归档超过总时间预算。")
            async with asyncio.timeout(min(60, remaining)):
                async with client.stream("GET", value, headers={"Accept": "text/html,application/xhtml+xml"}) as response:
                    observation = {"url": value, "status": response.status_code,
                                   "response_headers": dict(response.headers)}
                    chain.append(observation)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or ordinal == 6:
                            raise AcquisitionError("PAGE_REDIRECT_LIMIT", "网页跳转缺少地址或超过 6 次。",
                                                   details={"chain": chain})
                        value = urljoin(value, location)
                        if urlsplit(value).hostname == "mp.weixin.qq.com" and "captcha" in urlsplit(value).path.lower():
                            raise AcquisitionError("WEB_PAGE_VERIFICATION_REQUIRED", "微信要求网页验证，尚未取得文章正文。请打开原链接，保存为 PDF 或 Markdown 后从本地文件导入。", details={"chain":chain})
                        continue
                    if response.status_code != 200:
                        raise AcquisitionError("PAGE_HTTP_STATUS", "网页 HTTP 状态不是 200。",
                                               details={"chain": chain},
                                               retryable=response.status_code in {429, 500, 502, 503, 504})
                    return value, chain
    raise AcquisitionError("PAGE_REDIRECT_LIMIT", "网页跳转没有形成成功终点。", details={"chain": chain})

class ArchiveBrowserManager(BrowserManager):
    """Dedicated context settings for this project's pinned Crawl4AI adapter.

    Service workers are disabled before navigation so they cannot bypass routes.
    There is no shared profile, proxy, authentication state or persistent context.
    This application policy is not an arbitrary-web sandbox guarantee.
    """

    async def create_browser_context(self, crawlerRunConfig=None):
        if self.browser is None or self.config.use_persistent_context or self.config.proxy or self.config.proxy_config:
            raise RuntimeError("archive browser requires a fresh dedicated context")
        return await self.browser.new_context(
            viewport={"width": 1280, "height": 800}, user_agent=self.config.user_agent,
            device_scale_factor=1, accept_downloads=False, ignore_https_errors=False,
            java_script_enabled=True, service_workers="block", storage_state=None,
        )


# Fixed project-owned DOM inspection; no caller-provided JavaScript is executed.
DOM_RESOURCES = r"""(selector) => {
  const xpath = node => {
    if (node === document.documentElement) return "/html";
    const name = node.tagName.toLowerCase();
    const peers = Array.from(node.parentElement.children).filter(x => x.tagName === node.tagName);
    return xpath(node.parentElement) + "/" + name + "[" + (peers.indexOf(node) + 1) + "]";
  };
  let body = null;
  if (selector) {
    const nodes = document.querySelectorAll(selector);
    if (nodes.length !== 1) throw new Error("configured body selector is not unique");
    body = nodes[0];
  } else {
    const articles = document.querySelectorAll("article"), mains = document.querySelectorAll("main");
    body = articles.length === 1 ? articles[0] : mains.length === 1 ? mains[0] : document.body;
  }
  // Aside can contain factual figures and supplementary body content.
  const excluded = node => !!node.closest("nav,header,footer");
  const images = Array.from(body.querySelectorAll("img")).filter(node => !excluded(node)).map(node => {
    const rect = node.getBoundingClientRect();
    const source = node.getAttribute("src"), current = node.currentSrc || "";
    const picture = node.parentElement && node.parentElement.tagName === "PICTURE" ? node.parentElement : null;
    const pictureSources = picture ? Array.from(picture.children).filter(child => child.tagName === "SOURCE")
      .map(child => ({srcset:child.getAttribute("srcset"),media:child.getAttribute("media"),
        type:child.getAttribute("type"),sizes:child.getAttribute("sizes")})) : [];
    return {node_path:xpath(node),attribute:"src",source_url:source,
      selected_url:current || (source ? node.src : ""),current_src:current,srcset:node.getAttribute("srcset"),
      picture_sources:pictureSources,
      alt:node.getAttribute("alt") || "",region:{x:rect.x,y:rect.y,width:rect.width,height:rect.height}};
  });
  const unsupported = [];
  for (const node of body.querySelectorAll("svg,canvas,iframe,video,object,embed")) {
    if (!excluded(node)) unsupported.push({node_path:xpath(node),kind:node.tagName.toLowerCase(),
      source_url:node.getAttribute("src") || node.getAttribute("data") || ""});
  }
  for (const node of body.querySelectorAll("*")) {
    if (!excluded(node) && getComputedStyle(node).backgroundImage !== "none")
      unsupported.push({node_path:xpath(node),kind:"css-background",source_url:getComputedStyle(node).backgroundImage});
  }
  const attachments = Array.from(body.querySelectorAll("a[href]")).filter(node=>!excluded(node))
    .map(node=>({node_path:xpath(node),url:node.href,text:node.textContent || ""}))
    .filter(item=>/\.(pdf|docx?|xlsx?)(?:[?#]|$)/i.test(item.url));
  // No await or second browser call separates these DOM observations.
  const doctype = document.doctype ? new XMLSerializer().serializeToString(document.doctype) + "\n" : "";
  return {captured_html:doctype + document.documentElement.outerHTML,document_url:document.URL,
    body_xpath:xpath(body),base_url:document.baseURI,images,unsupported,attachments};
}"""


def image_bytes(content: bytes) -> str:
    from PIL import UnidentifiedImageError
    if len(content) > 20 * MIB:
        raise AcquisitionError("IMAGE_BYTE_LIMIT", "图片超过 20 MiB。")
    try:
        with Image.open(BytesIO(content)) as image:
            if getattr(image, "is_animated", False):
                raise AcquisitionError("ANIMATED_IMAGE_UNSUPPORTED", "动画图片需复核，不自动抽取一帧冒充原图。")
            if image.width * image.height > 20_000_000:
                raise AcquisitionError("IMAGE_PIXEL_LIMIT", "图片超过 2,000 万像素。")
            formats = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "BMP": ".bmp", "TIFF": ".tiff"}
            if image.format not in formats:
                raise AcquisitionError("IMAGE_FORMAT_UNSUPPORTED", "正文图片格式暂不支持。", details={"format": image.format})
            image.verify()
            return formats[image.format]
    
    except UnidentifiedImageError as exc:
        raise AcquisitionError("IMAGE_UNREADABLE", "网页图片无法识别；已停止导入，请将该图片保存为 PNG 或 JPEG 后再导入。") from exc

async def download_image(client, value, site, deadline, target, *, phase=None, allow_svg=False):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AcquisitionError("WEB_TOTAL_TIMEOUT", "网页归档超过总时间预算。")
    async with asyncio.timeout(min(20, remaining)):
        if value.startswith("data:"):
            header, delimiter, body = value.partition(",")
            if not delimiter or not header.lower().startswith("data:image/") or len(body) > 28 * MIB:
                raise AcquisitionError("DATA_IMAGE_INVALID", "内嵌图片格式或体积不合格。")
            content = base64.b64decode(body, validate=True) if header.lower().endswith(";base64") else unquote_to_bytes(body)
            redirects, headers = [], {"source": "data-uri"}
        else:
            url = await public_url(value, site["image_hosts"], phase=phase, dns_scope="RESOURCE_ACQUISITION")
            redirects = []
            for hop in range(6):
                async with client.stream("GET", url, follow_redirects=False) as response:
                    redirects.append({"url": url, "status": response.status_code})
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or hop == 5:
                            raise AcquisitionError("IMAGE_REDIRECT_LIMIT", "图片重定向未闭合。")
                        url = await public_url(urljoin(url, location), site["image_hosts"], phase=phase, dns_scope="RESOURCE_ACQUISITION")
                        continue
                    if response.status_code != 200:
                        raise AcquisitionError("IMAGE_HTTP_STATUS", "必要图片未完整获取。",
                                               details={"redirects": redirects},
                                               retryable=response.status_code in {429, 500, 502, 503, 504})
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 20 * MIB:
                            raise AcquisitionError("IMAGE_BYTE_LIMIT", "图片超过 20 MiB。")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    headers = {"content_type": response.headers.get("content-type"),
                               "etag": response.headers.get("etag"), "last_modified": response.headers.get("last-modified")}
                    break
            else:
                raise AcquisitionError("IMAGE_REDIRECT_LIMIT", "图片重定向未闭合。")
        vector_source = None
        from .svg_render import svg_markup, rasterize_svg, SvgImageError
        try:
            markup = svg_markup(content) if allow_svg else None
            if markup is not None:
                original = content
                original_digest = hashlib.sha256(original).hexdigest()
                if phase is not None:
                    phase["name"] = "SVG_IMAGE_RENDER"
                content, rendering = await rasterize_svg(markup)
                source_path = target / (original_digest + ".svg")
                if not source_path.exists():
                    write_closed(source_path, original)
                vector_source = {"local_path": "resources/" + source_path.name,
                    "sha256": original_digest, "size_bytes": len(original), "rendering": rendering}
        except SvgImageError as exc:
            raise AcquisitionError("SVG_IMAGE_UNSUPPORTED", str(exc)) from exc
        suffix = image_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        path = target / (digest + suffix)
        if not path.exists():
            write_closed(path, content)
        observation = {"sha256": digest, "size_bytes": len(content), "redirects": redirects, "response_headers": headers}
        if vector_source is not None:
            observation["vector_source"] = vector_source
        return path, observation


async def _acquire_web(payload: dict, directory: Path, deadline: float, cancelled, phase: dict) -> tuple[dict, dict, dict]:
    config = payload["acquisition_config"]
    if (config.get("schema_version") != 1 or set(config) not in ({"schema_version", "policy", "profile", "component_versions"},
            {"schema_version", "policy", "profile", "component_versions", "reader_fallback"})
            or config.get("reader_fallback") not in (None, "jina_text_wechat_v1")):
        raise ValueError("unknown acquisition configuration")
    site = config["policy"]["sites"].get(urlsplit(payload["url"]).hostname)
    if site is None:
        raise AcquisitionError("URL_HOST_NOT_ALLOWED", "该网址尚未配置明确的站点策略。")
    phase["name"] = "NAVIGATION_RESOLUTION"
    navigation_url, navigation_chain = await resolve_navigation(payload["url"], site, deadline, cancelled, phase=phase)
    phase["name"] = "PREPARE_CAPTURE"
    resources = directory / "resources"
    resources.mkdir()
    state = {"blocked_requests": [], "document_requests": [], "scroll": {"enabled": config["profile"] == "bounded_scroll"},
             "dom": None, "captured": None}
    browser = BrowserConfig(browser_type="chromium", browser_mode="dedicated", headless=True,
                            viewport_width=1280, viewport_height=800, ignore_https_errors=False,
                            use_persistent_context=False, accept_downloads=False, verbose=False, user_agent=PUBLIC_BROWSER_UA)

    async def on_context(page, context, **_kwargs):
        async def guard(route, request):
            try:
                if cancelled():
                    await route.abort()
                    return
                if request.is_navigation_request() and request.frame != page.main_frame:
                    raise AcquisitionError("FRAME_NAVIGATION_UNSUPPORTED", "不采集嵌入页面。")
                hosts = site["page_hosts"] if request.is_navigation_request() else site["render_hosts"]
                await public_url(request.url, hosts, phase=phase, dns_scope="BROWSER_RENDER_REQUEST")
                if request.is_navigation_request():
                    state["document_requests"].append(request.url)
                if request.method not in {"GET", "HEAD"}:
                    raise AcquisitionError("RENDER_METHOD_NOT_ALLOWED", "网页归档只允许 GET/HEAD 渲染请求。")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AcquisitionError("WEB_TOTAL_TIMEOUT", "网页归档超过总时间预算。")
                response = await route.fetch(max_redirects=0, max_retries=0,
                                             timeout=max(1, min(60000, int(remaining * 1000))))
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if location:
                        await public_url(urljoin(request.url, location), hosts, phase=phase, dns_scope="BROWSER_RENDER_REDIRECT")
                    raise AcquisitionError("RENDER_REDIRECT_NOT_FOLLOWED", "渲染请求出现新的跳转，已阻止跟随并需复核。",
                                           details={"url": request.url, "location": location, "status": response.status})
                await route.fulfill(response=response)
            except Exception as exc:
                state["blocked_requests"].append({"url": request.url, "type": request.resource_type,
                                                  "error_type": type(exc).__name__, "code": getattr(exc, "code", None)})
                await route.abort()
        await context.route("**/*", guard)
        await context.route_web_socket("**/*", lambda route: route.close())
        return page

    async def before_html(page, **_kwargs):
        if urlsplit(navigation_url).hostname == "mp.weixin.qq.com":
            images=page.locator("#js_content img")
            count=await images.count()
            if count>100:
                raise AcquisitionError("IMAGE_COUNT_LIMIT","文章图片超过100张，请分开保存后导入。")
            # Use the site's normal lazy loading, then bind actual currentSrc.
            for ordinal in range(count):
                if cancelled():
                    raise AcquisitionError("CANCELLED","任务已取消。")
                await images.nth(ordinal).scroll_into_view_if_needed(timeout=max(1,min(10000,int((deadline-time.monotonic())*1000))))
            if count:
                await page.wait_for_function(
                    "() => Array.from(document.querySelectorAll('#js_content img')).every(img => img.currentSrc && img.complete && img.naturalWidth > 0)",
                    timeout=max(1,min(20000,int((deadline-time.monotonic())*1000))))
            await page.evaluate("window.scrollTo(0,0)")
        if config["profile"] == "bounded_scroll":
            initial = await page.evaluate("document.documentElement.scrollHeight")
            bottom = False
            for ordinal in range(10):
                if cancelled():
                    raise AcquisitionError("CANCELLED", "任务已取消。")
                await page.evaluate("window.scrollBy(0,800)")
                await asyncio.sleep(0.5)
                bottom = await page.evaluate("window.scrollY+window.innerHeight>=document.documentElement.scrollHeight-1")
                if bottom:
                    break
            final = await page.evaluate("document.documentElement.scrollHeight")
            await page.evaluate("window.scrollTo(0,0)")
            state["scroll"].update(steps=ordinal + 1, reached_bottom=bottom, content_height_changed=initial != final)
        return page

    async def capture(page, html, **_kwargs):
        # Crawl4AI's html argument is an earlier snapshot. Archive the project
        # snapshot and its resource bindings from one synchronous evaluation.
        snapshot = await page.evaluate(DOM_RESOURCES, site["body_selector"])
        raw = snapshot.pop("captured_html").encode("utf-8")
        if len(raw) > 10 * MIB:
            raise AcquisitionError("HTML_BYTE_LIMIT", "HTML 快照超过 10 MiB。")
        write_closed(directory / "captured.html", raw)
        state["captured"], state["dom"] = raw, snapshot
        state["final_url"] = snapshot["document_url"]
        return page

    crawler = AsyncWebCrawler(config=browser, base_directory=str(directory / "crawler-cache"))
    crawler.crawler_strategy.browser_manager = ArchiveBrowserManager(browser_config=browser, logger=crawler.crawler_strategy.logger)
    crawler.crawler_strategy.set_hook("on_page_context_created", on_context)
    crawler.crawler_strategy.set_hook("before_retrieve_html", before_html)
    crawler.crawler_strategy.set_hook("before_return_html", capture)
    try:
        phase["name"] = "BROWSER_CAPTURE"
        async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
            async with crawler:
                run = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, wait_until="domcontentloaded",
                                       page_timeout=max(1, min(60000, int((deadline-time.monotonic())*1000))),
                                       delay_before_return_html=1, scan_full_page=False,
                                       wait_for=site["wait_for"], wait_for_timeout=60000, max_retries=0,
                                       process_iframes=False, screenshot=False, pdf=False, verbose=False,
                                       prefetch=True)
                result = await crawler.arun(navigation_url, config=run)
            if not result.success or result.status_code != 200:
                raise AcquisitionError("WEB_CAPTURE_FAILED", "网页快照未成功获取。",
                                       details={"status": result.status_code, "error": result.error_message},
                                       retryable=result.status_code in {429, 500, 502, 503, 504})
            if state.get("final_url") != navigation_url:
                raise AcquisitionError("PAGE_FINAL_URL_CHANGED", "最终页面地址与已核验导航终点不一致。",
                                       details={"expected": navigation_url, "observed": state.get("final_url")})
            if state["captured"] is None or state["dom"] is None:
                raise AcquisitionError("SNAPSHOT_BINDING_FAILED", "渲染快照与资源观察未绑定。")
            if state["scroll"].get("reached_bottom") is False:
                raise AcquisitionError("DYNAMIC_CAPTURE_INCOMPLETE", "滚动达到上限仍未到底，请复核动态内容。")
            phase["name"] = "RESOURCE_ACQUISITION"
            dom = state["dom"]
            if len(dom["images"]) > 100:
                raise AcquisitionError("IMAGE_COUNT_LIMIT", "正文图片超过 100 张。")
            members = {"captured.html": directory / "captured.html"}
            bindings, image_observations, saved_bytes = [], [], 0
            async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False,
                                         headers={"User-Agent":PUBLIC_BROWSER_UA}) as client:
                for image in dom["images"]:
                    if cancelled():
                        raise AcquisitionError("CANCELLED", "任务已取消。")
                    selected = image["selected_url"]
                    if not selected:
                        raise AcquisitionError("IMAGE_SOURCE_MISSING", "正文图片没有实际选中资源。", details=image)
                    if not image["current_src"] and (image["srcset"] is not None
                            or any(item["srcset"] is not None for item in image["picture_sources"])):
                        raise AcquisitionError("IMAGE_SELECTION_NOT_READY", "响应式图片在捕获时没有实际选中资源。", details=image)
                    saved, observation = await download_image(client, selected, site, deadline, resources, phase=phase,
                        allow_svg=config.get("component_versions", {}).get("raghub_web_adapter") in {"6", "7"})
                    vector_source = observation.get("vector_source")
                    saved_bytes += observation["size_bytes"] + (vector_source["size_bytes"] if vector_source else 0)
                    if saved_bytes > 100 * MIB:
                        raise AcquisitionError("IMAGE_TOTAL_LIMIT", "正文图片累计超过 100 MiB。")
                    local = "resources/" + saved.name
                    members[local] = saved
                    if vector_source:
                        members[vector_source["local_path"]] = resources / Path(vector_source["local_path"]).name
                    bindings.append({key: image[key] for key in ("node_path", "attribute", "source_url", "selected_url",
                                     "srcset", "current_src", "picture_sources")} | {"local_path": local})
                    image_observations.append(image | observation | {"local_path": local})
            bindings.sort(key=lambda item: (item["node_path"], item["attribute"], item["selected_url"]))
            if len({(item["node_path"], item["attribute"]) for item in bindings}) != len(bindings):
                raise AcquisitionError("RESOURCE_BINDING_DUPLICATE", "同一 DOM 图片引用重复。")
            write_closed(directory / "resource-map.json", json_bytes({"schema_version": 2, "bindings": bindings,
                         "base_url": dom["base_url"], "snapshot_method": "SINGLE_SYNCHRONOUS_DOM_EVALUATION"}))
            members["resource-map.json"] = directory / "resource-map.json"
            phase["name"] = "OFFLINE_DERIVATION"
            document = parse_captured_html(state["captured"])
            unsupported, verified_ui = (dom["unsupported"], [])
            if config.get("component_versions", {}).get("raghub_web_adapter") == "7":
                unsupported, verified_ui = partition_ui_resources(document, dom["unsupported"], site, state["final_url"])
            node_map = []
            selected_nodes = set()
            for binding in bindings:
                nodes = document.xpath(binding["node_path"])
                if len(nodes) != 1 or nodes[0].tag != "img":
                    raise AcquisitionError("RESOURCE_XPATH_MISMATCH", "快照中的图片节点与 DOM 观察不一致。", details=binding)
                node = nodes[0]
                verify_snapshot_image(node, binding, dom["base_url"])
                selected_nodes.add(node)
                node.set("src", binding["local_path"])
                node.attrib.pop("srcset", None)
                node.attrib.pop("data-src", None)
                node_map.append({"captured_xpath": binding["node_path"], "local_path": binding["local_path"], "node": node})
            for node in list(document.xpath("//base|//script|//link|//source|//img")):
                if node.tag != "img" or node not in selected_nodes:
                    node.drop_tree()
            tree = document.getroottree()
            nodes = [{key: value for key, value in item.items() if key != "node"} |
                     {"local_xpath": tree.getpath(item["node"])} for item in node_map]
            local = lxml_html.tostring(document, encoding="utf-8", method="html")
            write_closed(directory / "local.html", local)
            write_closed(directory / "node-map.json", json_bytes({"schema_version": 1, "nodes": nodes,
                         "captured_sha256": hashlib.sha256(state["captured"]).hexdigest(),
                         "local_sha256": hashlib.sha256(local).hexdigest()}))
            observations = {
                "input_kind": "WEB_RENDERED_SNAPSHOT", "url": payload["url"], "final_url": state["final_url"],
                "snapshot_method": "SINGLE_SYNCHRONOUS_DOM_EVALUATION", "snapshot_member": "captured.html",
                "crawl4ai_html_relation": "SEPARATE_EARLIER_SNAPSHOT_NOT_ARCHIVED",
                "navigation_chain": navigation_chain, "navigation_resolution_method": "GET_STREAM_REDIRECTS_DISABLED",
                "browser_redirect_policy": "FETCH_MAX_REDIRECTS_ZERO_NEW_REDIRECT_REJECTED",
                "document_requests": state["document_requests"], "http_status": result.status_code,
                "response_headers": result.response_headers, "captured_at": time.time(),
                "configuration": config, "configuration_sha256": json_hash(config), "scroll": state["scroll"],
                "blocked_requests": state["blocked_requests"], "body_xpath": dom["body_xpath"],
                "base_url": dom["base_url"], "image_observations": image_observations,
                "unsupported_dependencies": unsupported, "resource_review_required": bool(unsupported),
                "verified_ui_dependencies": verified_ui,
                "attachments": dom["attachments"], "attachment_policy": "SEPARATE_USER_SELECTION_NO_RECURSION",
                "format_validation": "PENDING_FORMAT_PARSER", "provenance": payload["metadata"]["provenance"],
            }
            write_closed(directory / "source-observation.json", json_bytes(observations))
            return members, observations, {"local.html": directory / "local.html", "node-map.json": directory / "node-map.json"}
    except BaseException as exc:
        failure = {"error_type": type(exc).__name__, "code": getattr(exc, "code", None),
                   "details": getattr(exc, "details", {}), "blocked_requests": state["blocked_requests"],
                   "document_requests": state["document_requests"], "scroll": state["scroll"],
                   "actual_phase": phase["name"], **dns_wait_observation(phase)}
        write_closed(directory / "acquisition-failure.json", json_bytes(failure))
        raise


async def acquire_web(payload: dict, directory: Path, deadline: float, cancelled) -> tuple[dict, dict, dict]:
    """The single deadline includes the first DNS wait and browser setup."""
    phase = {"name": "CONFIGURATION", "dns_wait_sequence": 0, "active_dns_waits": {}, "cancelled_dns_waits": []}
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AcquisitionError("WEB_TOTAL_TIMEOUT", "网页获取预算已过期，未开始抓取。")
        async with asyncio.timeout(remaining):
            config = payload["acquisition_config"]
            fallback = (config.get("reader_fallback") == "jina_text_wechat_v1"
                        and urlsplit(payload["url"]).hostname == "mp.weixin.qq.com")
            direct_deadline = deadline - 45 if fallback and remaining > 45 else deadline
            try:
                return await _acquire_web(payload, directory, direct_deadline, cancelled, phase)
            except (AcquisitionError, httpx.HTTPError, TimeoutError) as direct_error:
                allowed_codes = {"WEB_PAGE_VERIFICATION_REQUIRED", "PAGE_HTTP_STATUS", "WEB_CAPTURE_FAILED",
                                 "IMAGE_SOURCE_MISSING", "IMAGE_SELECTION_NOT_READY", "IMAGE_HTTP_STATUS",
                                 "WEB_TOTAL_TIMEOUT", "DYNAMIC_CAPTURE_INCOMPLETE"}
                eligible = (isinstance(direct_error, (httpx.HTTPError, TimeoutError))
                            or getattr(direct_error, "code", None) in allowed_codes)
                if not fallback or not eligible or cancelled():
                    raise
                from .wechat_reader import acquire_wechat_text
                phase["name"] = "WECHAT_TEXT_READER"
                return await acquire_wechat_text(payload, directory, deadline, cancelled, direct_error)
    except BaseException as exc:
        failure_path = directory / "acquisition-failure.json"
        if not failure_path.exists():
            write_closed(failure_path, json_bytes({
                "error_type": type(exc).__name__, "code": getattr(exc, "code", None),
                "actual_phase": phase["name"], "details": getattr(exc, "details", {}),
                **dns_wait_observation(phase),
            }))
        raise

def acquisition_config(policy: dict, profile: str) -> dict:
    return {"schema_version": 1, "policy": policy, "profile": profile, "reader_fallback": "jina_text_wechat_v1",
            "component_versions": {name: importlib.metadata.version(name) for name in ("crawl4ai", "playwright", "httpx", "lxml", "pillow")} | {"raghub_web_adapter": "7"}}
