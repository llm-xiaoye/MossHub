"""Conservative exact alignment; fuzzy scores are diagnostic candidates only."""
from __future__ import annotations
import re
import unicodedata
from collections import Counter
from rapidfuzz.distance import Levenshtein

ENDINGS=("。","！","？","!","?")


def normalized(text,plain):
    text=text.replace("\r\n","\n").replace("\r","\n")
    return "\n".join(re.sub(r"[ \t\u00a0]+"," ",line).strip(" ") for line in unicodedata.normalize("NFC",text).split("\n")) if plain else text


def align(anchors,fields):
    def same_heading_level(anchor,field):
        left_level,right_level=anchor.get("markdown_heading_level"),field.get("markdown_heading_level")
        if left_level is None and right_level is None:
            return True
        return (type(left_level) is int and type(right_level) is int
                and 1<=left_level<=6 and left_level==right_level)

    def same_structure(anchor,field):
        return anchor["kind"]==field["kind"] and same_heading_level(anchor,field)

    def heading_key(node):
        level=node.get("markdown_heading_level")
        if type(level) is int and 1<=level<=6 and node["text"]:
            return level,node["text"]
        return None

    left=[normalized(a["text"],a.get("plain",False)) for a in anchors]
    right=[normalized(f["text"],f.get("plain",False)) for f in fields]
    lc,rc=Counter(left),Counter(right)
    seeds=[]
    for i,text in enumerate(left):
        if not text or lc[text]!=1 or rc[text]!=1:
            continue
        j=right.index(text)
        if same_structure(anchors[i],fields[j]):
            seeds.append(((i,),(j,),"T12_EXACT_UNIQUE"))
    # A complete heading must be unique by both exact text and original level.
    # Docling maps Markdown H1 to title and H2..H6 to section levels 1..5.
    heading_left=Counter(heading_key(a) for a in anchors if heading_key(a) is not None)
    heading_right=Counter(heading_key(f) for f in fields if heading_key(f) is not None)
    for i,anchor in enumerate(anchors):
        key=heading_key(anchor)
        if (key is None or anchor.get("complete_heading_text")!=anchor["text"]
                or heading_left[key]!=1 or heading_right[key]!=1):
            continue
        j=next(j for j,field in enumerate(fields) if heading_key(field)==key)
        if same_structure(anchor,fields[j]) and not any(aa==(i,) and bb==(j,) for aa,bb,_ in seeds):
            seeds.append(((i,),(j,),"T18_EXACT_COMPLETE_MARKDOWN_HEADING_LEVEL"))
    # Reject every crossing pair, including the tempting first occurrence.
    bad=set()
    for n,a in enumerate(seeds):
        for m,b in enumerate(seeds[n+1:],n+1):
            if (a[0][0]-b[0][0])*(a[1][0]-b[1][0])<0:
                bad.update((n,m))
    accepted=[a for n,a in enumerate(seeds) if n not in bad]
    accepted.sort(key=lambda a:a[0][0])
    used_left={i for a,_,_ in accepted for i in a}
    used_right={j for _,b,_ in accepted for j in b}
    def groups(nodes,texts,lo,hi,width,used):
        result=[]
        for start in range(lo,hi-width+1):
            indices=tuple(range(start,start+width))
            if any(i in used for i in indices) or any(not nodes[i].get("plain",False) for i in indices):
                continue
            text="".join(texts[i] for i in indices)
            if not text or len(text)>4000 or (width>1 and not all(texts[i].rstrip("\n").endswith(ENDINGS) for i in indices)):
                continue
            result.append((indices,text))
            # Markdown soft breaks may be emitted as separate inline leaves while
            # Docling renders one plain paragraph with an intervening space.
            # Admit only exact text from the same bound original Markdown block.
            if width>1:
                locations=[nodes[i].get("locator") for i in indices]
                first=locations[0]
                if (isinstance(first,dict) and first.get("kind")=="md"
                        and all(location==first for location in locations)):
                    spaced=" ".join(texts[i] for i in indices)
                    if spaced!=text and len(spaced)<=4000:
                        result.append((indices,spaced))
        return result
    merged=[]
    # bind_native supplies the complete ordered sequences for one source file.
    # Real exact seeds also bound leading/trailing gaps; no-seed files stay
    # diagnostic-only rather than becoming an unconstrained whole-file match.
    windows=[]
    if accepted:
        first,last=accepted[0],accepted[-1]
        windows.append((0,first[0][0],0,first[1][0]))
        windows.extend((before[0][-1]+1,after[0][0],before[1][-1]+1,after[1][0])
                       for before,after in zip(accepted,accepted[1:]))
        windows.append((last[0][-1]+1,len(anchors),last[1][-1]+1,len(fields)))
    for alo,ahi,blo,bhi in windows:
        if ahi-alo>128 or bhi-blo>128:
            continue
        candidates=[]
        # Match the whole source callout with complete contiguous native fields.
        # Only verified neighbour windows and globally unique blocks qualify.
        for i in range(alo,ahi):
            expected=anchors[i].get("complete_callout_text")
            if (i in used_left or not isinstance(expected,str) or not expected
                    or len(expected)>4000 or lc[left[i]]!=1):
                continue
            for start in range(blo,bhi):
                fragments=[]
                for end in range(start,min(bhi,start+32)):
                    if end in used_right or fields[end]["kind"]!="paragraph" or fields[end].get("markdown_heading_level") is not None:
                        break
                    fragments.append(fields[end]["text"])
                    actual=" ".join(fragments)
                    if len(actual)>len(expected):
                        break
                    if len(fragments)>1 and actual==expected:
                        candidates.append(((i,),tuple(range(start,end+1)),"T17_EXACT_COMPLETE_OBSIDIAN_CALLOUT"))
        # Protected atoms may repeat globally, but match only if their exact
        # kind and original text are unique inside verified neighbour bounds.
        for i in range(alo,ahi):
            if i in used_left or not anchors[i]["text"] or len(anchors[i]["text"])>4000:
                continue
            for j in range(blo,bhi):
                if j in used_right or (anchors[i].get("plain",False) and fields[j].get("plain",False)):
                    continue
                if same_structure(anchors[i],fields[j]) and anchors[i]["text"]==fields[j]["text"]:
                    candidates.append(((i,),(j,),"T12_PROTECTED_EXACT_BOUNDED"))
        for aw,bw in ((1,1),(1,2),(1,3),(2,1),(3,1)):
            for aa,at in groups(anchors,left,alo,ahi,aw,used_left):
                for bb,bt in groups(fields,right,blo,bhi,bw,used_right):
                    if at==bt and ((aw,bw)==(1,1) or (all(left[i].rstrip("\n").endswith(ENDINGS) for i in aa) and all(right[j].rstrip("\n").endswith(ENDINGS) for j in bb))):
                        candidates.append((aa,bb,"T12_EXACT_UNIQUE_BOUNDED" if (aw,bw)==(1,1) else "T14_EXACT_NORMALIZED_CONCAT"))
        # A complete original Markdown block can contain any number of soft
        # breaks. Docling also joins list-item continuations into one field.
        # Match the whole bound block exactly; retain protected-field status.
        for start in range(alo,ahi):
            location=anchors[start].get("locator")
            if not isinstance(location,dict) or location.get("kind")!="md":
                continue
            if start>0 and anchors[start-1].get("locator")==location:
                continue
            end=start+1
            while end<ahi and anchors[end].get("locator")==location:
                end+=1
            aa=tuple(range(start,end))
            if len(aa)<2 or (end<len(anchors) and anchors[end].get("locator")==location):
                continue
            complete=anchors[start].get("rendered_block_text")
            source_backed=(isinstance(complete,str) and all(anchors[i].get("rendered_block_text")==complete
                and anchors[i]["kind"] in {"paragraph","code"} for i in aa))
            if not source_backed and any(not anchors[i].get("plain",False) or anchors[i]["kind"]!="paragraph" for i in aa):
                continue
            rendered=complete if source_backed else " ".join(left[i] for i in aa)
            if not rendered or len(rendered)>4000:
                continue
            plain_block=source_backed and all(anchors[i].get("plain",False)
                and anchors[i]["kind"]=="paragraph" for i in aa)
            expected=normalized(rendered,True) if plain_block else rendered
            for j in range(blo,bhi):
                bb=(j,)
                if (fields[j]["kind"]!="paragraph" or len(fields[j]["text"])>4000
                        or not all(same_heading_level(anchors[i],fields[j]) for i in aa)):
                    continue
                actual=fields[j]["text"] if source_backed else right[j]
                exact=rendered==actual
                matched=exact or (plain_block and expected==normalized(actual,True))
                if matched and not any(a==aa and b==bb for a,b,_ in candidates):
                    rule=("T16_EXACT_NORMALIZED_PLAIN_MARKDOWN_BLOCK" if not exact and plain_block else
                        "T15_EXACT_RENDERED_MARKDOWN_BLOCK" if source_backed else
                        "T13_EXACT_COMPLETE_MARKDOWN_BLOCK")
                    candidates.append((aa,bb,rule))
        ac=Counter(i for aa,_,_ in candidates for i in aa)
        bc=Counter(j for _,bb,_ in candidates for j in bb)
        for candidate in candidates:
            aa,bb,_=candidate
            if any(ac[i]!=1 for i in aa) or any(bc[j]!=1 for j in bb):
                continue
            crossing=any((aa[-1]<x[0][0] and not bb[-1]<x[1][0]) or
                         (x[0][-1]<aa[0] and not x[1][-1]<bb[0]) or
                         (set(aa)&set(x[0])) or (set(bb)&set(x[1]))
                         for x in candidates if x!=candidate)
            if not crossing:
                merged.append(candidate)
    accepted.extend(merged)
    used_right={j for _,bb,_ in accepted for j in bb}
    diagnostics=[]
    for j,f in enumerate(fields):
        if j in used_right:
            continue
        previous=[a for a in accepted if a[1][-1]<j]
        following=[a for a in accepted if a[1][0]>j]
        if previous and following:
            lo=max(previous,key=lambda a:a[1][-1])[0][-1]+1
            hi=min(following,key=lambda a:a[1][0])[0][0]
            pool=range(lo,hi) if hi-lo<=128 else ()
            scope="VERIFIED_NEIGHBOUR_WINDOW"
        else:
            pool=range(len(anchors))
            scope="FILE_COARSE"
        ranked=[]
        if 0<len(right[j])<=4000:
            ranked=sorted(({"anchor_id":anchors[i]["id"],"normalized_similarity":Levenshtein.normalized_similarity(left[i],right[j],processor=None)}
                for i in pool if 0<len(left[i])<=4000),key=lambda a:a["normalized_similarity"],reverse=True)[:3]
        diagnostics.append({"field":f["pointer"],"scope":scope,"candidates":ranked,"binding_created":False})
    return accepted,diagnostics
