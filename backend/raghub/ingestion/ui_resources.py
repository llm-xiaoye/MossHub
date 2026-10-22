"""Exclude only visually reviewed, exact site UI resources; retain every unknown graphic."""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit
from lxml import etree

CONTEXTS = frozenset({"REDIS_COMMAND_GROUP","REDIS_TRY","REDIS_OUTPUT","REDIS_COPY_LINK","REDIS_VISIBILITY","REDIS_COPY","REDIS_HEADING_ANCHOR","REDIS_LANGUAGE_SELECT","FASTAPI_TABS_SCROLL"})
BUTTONS = {
    "REDIS_TRY": ("tryit-button","openTryItCli(this)"),
    "REDIS_OUTPUT": ("cli-output-toggle","toggleCliOutputForCodetabs(this)"),
    "REDIS_COPY_LINK": ("copy-link-button","copyCodeBlockLinkToClipboard(this)"),
    "REDIS_VISIBILITY": ("visibility-button","toggleVisibleLinesForCodetabs(this)"),
    "REDIS_COPY": ("clipboard-button","copyCodeToClipboardForCodetabs(this)"),
}

def validate_rules(rules):
    if not isinstance(rules,list) or len(rules)>32:
        raise ValueError("invalid verified UI rules")
    seen=set()
    for rule in rules:
        if not isinstance(rule,dict) or set(rule)!={"kind","content_sha256","context","review_ref"}:
            raise ValueError("invalid verified UI rule fields")
        digest=rule["content_sha256"]
        if rule["kind"] not in {"svg","css-background"} or rule["context"] not in CONTEXTS:
            raise ValueError("unsupported UI rule kind/context")
        if not isinstance(digest,str) or len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid UI content identity")
        if not isinstance(rule["review_ref"],str) or not rule["review_ref"].strip():
            raise ValueError("missing UI review evidence")
        identity=(rule["kind"],digest,rule["context"])
        if identity in seen:
            raise ValueError("duplicate verified UI rule")
        seen.add(identity)

def classes(node):
    return set((node.get("class") or "").split())

def nearest(node,tag):
    return next((p for p in (node,*node.iterancestors()) if p.tag==tag),None)

def context_matches(doc,node,context,host):
    if context.startswith("REDIS_") and host!="redis.io":
        return False
    if context=="FASTAPI_TABS_SCROLL":
        return (host=="fastapi.tiangolo.com" and node.tag=="div" and
            "tabbed-control" in classes(node) and bool(classes(node)&{"tabbed-control--prev","tabbed-control--next"}) and
            any("tabbed-set" in classes(p) and p.xpath(".//pre") for p in node.iterancestors()))
    if context=="REDIS_LANGUAGE_SELECT":
        return node.tag=="select" and "lang-selector" in classes(node) and bool(node.get("data-codetabs-id")) and bool(node.xpath("./option"))
    if context=="REDIS_HEADING_ANCHOR":
        anchor=nearest(node,"a")
        if anchor is None or "header-link" not in classes(anchor):
            return False
        heading=anchor.getparent()
        target=doc.xpath("//*[@id=$value]",value=(anchor.get("href") or "")[1:])
        return (heading is not None and heading.tag in {"h1","h2","h3","h4","h5","h6"} and
            bool(heading.get("id")) and anchor.get("href")=="#"+heading.get("id") and
            len(target)==1 and target[0] is heading and bool("".join(heading.itertext()).strip()))
    button=nearest(node,"button")
    if button is None:
        return False
    if context=="REDIS_COMMAND_GROUP":
        target=doc.xpath("//*[@id=$value]",value=button.get("aria-controls") or "")
        return ("command-group-toggle" in classes(button) and button.get("onclick")=="toggleCommandGroup(this)" and
            len(target)==1 and any("command-group-foldout" in classes(p) and target[0] in p.iterdescendants() for p in button.iterancestors()))
    expected=BUTTONS.get(context)
    if expected is None or expected[0] not in classes(button) or button.get("onclick")!=expected[1]:
        return False
    group=button.get("data-codetabs-id")
    return bool(group) and any(p.get("id")==group and "codetabs" in classes(p) and p.xpath(".//pre") for p in button.iterancestors())

def partition_ui_resources(document,unsupported,site,final_url):
    rules=site.get("verified_ui_resources",[])
    validate_rules(rules)
    host=urlsplit(final_url).hostname
    kept,excluded=[],[]
    for dependency in unsupported:
        nodes=document.xpath(dependency["node_path"])
        if len(nodes)!=1:
            kept.append(dependency)
            continue
        node=nodes[0]
        if dependency["kind"]=="svg" and node.tag=="svg":
            content=etree.tostring(node,encoding="utf-8",method="xml",with_tail=False)
        elif dependency["kind"]=="css-background":
            content=dependency["source_url"].encode("utf-8")
        else:
            kept.append(dependency)
            continue
        digest=hashlib.sha256(content).hexdigest()
        matches=[r for r in rules if r["kind"]==dependency["kind"] and r["content_sha256"]==digest and context_matches(document,node,r["context"],host)]
        if len(matches)!=1:
            kept.append(dependency)
        else:
            excluded.append({**dependency,"content_sha256":digest,"context":matches[0]["context"],"review_ref":matches[0]["review_ref"],"reason":"VERIFIED_SITE_UI_CONTROL"})
    return kept,excluded
