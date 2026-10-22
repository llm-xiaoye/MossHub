"""Markdown with embedded HTML, rendered passively and bound to original MD regions."""
from __future__ import annotations
from pathlib import Path
from lxml import html
from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.dollarmath import dollarmath_plugin
from ..ingestion.archive import hash_file,write_closed
from .source_backends import HTML_SOURCE_ATTR

REGION_ATTR='data-raghub-markdown-region'

def convert_markdown(path,source_path,workdir):
    from .formats import native_document,base_location
    from .html_blocks import block_source
    if workdir is None:
        raise ValueError('Markdown with HTML needs its attempt working directory')
    text=path.read_text(encoding='utf-8-sig')
    if REGION_ATTR in text or HTML_SOURCE_ATTR in text:
        raise ValueError('Markdown contains a reserved source observation attribute')
    lines=text.splitlines(keepends=True);offsets=[0]
    for line in lines:offsets.append(offsets[-1]+len(line))
    parser=MarkdownIt('commonmark').enable('table').use(footnote_plugin).use(dollarmath_plugin)
    env={};tokens=parser.parse(text,env);regions={}
    for index,token in enumerate(tokens):
        if token.map and token.type not in {'html_block','inline'}:
            begin,end=token.map
            key='region-'+str(index)
            regions[key]={'line_start':begin+1,'line_end':max(begin+1,end),
                          'source_start':offsets[begin],'source_end':offsets[end]}
            token.attrSet(REGION_ATTR,key)
    rendered=parser.renderer.render(tokens,parser.options,env)
    tree=html.fromstring('<html><body>'+rendered+'</body></html>')
    # Active behavior and remote resources are never executed or fetched.
    for node in tree.xpath('//script|//style'):
        node.drop_tree()
    nodes={};by_xpath={}
    whole={'line_start':1,'line_end':max(1,len(lines)),'source_start':0,'source_end':len(text)}
    for index,node in enumerate(tree.iter()):
        if not isinstance(node.tag,str):continue
        region=whole
        for ancestor in [node,*node.iterancestors()]:
            candidate=ancestor.get(REGION_ATTR)
            if candidate in regions:
                region=regions[candidate];break
        xpath=node.getroottree().getpath(node);key='source-'+str(index)
        nodes[key]={'xpath':xpath,'protected':True,'markdown_region':dict(region),**block_source(node)}
        by_xpath[xpath]=dict(region)
        node.set(HTML_SOURCE_ATTR,key)
    for node in tree.iter():
        if isinstance(node.tag,str):node.attrib.pop(REGION_ATTR,None)
    target=Path(workdir)/'rendered-markdown.html'
    write_closed(target,html.tostring(tree,encoding='utf-8',method='html'))
    digest=hash_file(source_path)[0];rendered_digest=hash_file(target)[0]
    parsed=native_document(target,'html',source_nodes=nodes,source_digest=digest)
    for field,location in list(parsed.locations.items()):
        xpath=location['xpath'];region=by_xpath[xpath]
        parsed.locations[field]=base_location('md',digest,**region,granularity='REGION',
            basis='MARKDOWN_RENDERED_NODE_IN_ORIGINAL_SOURCE_REGION',
            source_group=[{'kind':'md','file_sha256':digest,**region,
                           'rendered_html_sha256':rendered_digest,'rendered_xpath':xpath,
                           'transformation':'MARKDOWN_IT_STATIC_HTML_RENDER_NO_EXECUTION'}])
    parsed.document.name=source_path.name
    parsed.details.update({'format':'md','markdown_renderer':'markdown-it-py with static HTML',
        'rendered_html_sha256':rendered_digest,'original_markdown_sha256':digest,
        'active_content_executed':False,'remote_fetch':False})
    parsed.scope={'selected':'MARKDOWN_BODY_WITH_STATIC_HTML','excluded':'SCRIPT_STYLE_BEHAVIOR_AND_REMOTE_FETCH'}
    return parsed

