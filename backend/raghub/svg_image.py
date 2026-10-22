"""Static SVG assets: preserve original bytes and bind visible XML text without execution."""
from __future__ import annotations
import re
from lxml import etree

NS = 'http://www.w3.org/2000/svg'
TAGS = {'svg','g','defs','title','desc','metadata','rect','circle','ellipse','line','polyline',
        'polygon','path','text','tspan','textPath','linearGradient','radialGradient','stop',
        'clipPath','mask','pattern','use','style'}

def read_svg(path):
    raw=path.read_bytes()
    if len(raw)>5*1024*1024 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('SVG declarations or byte budget are unsupported')
    root=etree.fromstring(raw,etree.XMLParser(resolve_entities=False,no_network=True))
    if root.tag!='{'+NS+'}svg':
        raise ValueError('SVG namespace/root is invalid')
    for node in root.iter():
        if not isinstance(node.tag,str):
            continue
        if not node.tag.startswith('{'+NS+'}') or etree.QName(node).localname not in TAGS:
            raise ValueError('SVG contains unsupported active or external content')
        values=list(node.attrib.values())+([node.text or ''] if etree.QName(node).localname=='style' else [])
        for key,value in node.attrib.items():
            name=etree.QName(key).localname.lower()
            if name.startswith('on') or (name=='href' and not value.startswith('#')):
                raise ValueError('SVG event or nonlocal reference is forbidden')
        for value in values:
            if re.search(r'@import|javascript:|expression\s*\(',value,re.I):
                raise ValueError('SVG active style is forbidden')
            for match in re.finditer(r'url\s*\((.*?)\)',value,re.I):
                target=match.group(1).strip().strip(chr(34)+chr(39))
                if not target.startswith('#'):
                    raise ValueError('SVG external resource is forbidden')
    view=root.get('viewBox','').replace(',',' ').split()
    def dimension(name,index):
        value=root.get(name,'')
        if re.fullmatch(r'\d+(?:\.\d+)?(?:px)?',value):
            return float(value.removesuffix('px'))
        if len(view)==4:
            return float(view[index])
        raise ValueError('SVG dimensions are not fixed')
    width,height=dimension('width',2),dimension('height',3)
    if not (0<width<=20000 and 0<height<=20000 and width*height<=40000000):
        raise ValueError('SVG dimensions exceed preview budget')
    rows=[]
    for node in root.iter('{'+NS+'}text'):
        text=''.join(node.itertext()).strip()
        if text:
            rows.append({'text':text,'xml_path':node.getroottree().getpath(node)})
    return raw,rows,(width,height)

