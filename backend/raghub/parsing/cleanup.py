"""Trace cleanup operations without guessing positions from text similarity."""
from __future__ import annotations
import unicodedata as ud

HORIZONTAL = {" ", "\t", "\u00a0"}


def clean(text: str, *, plain: bool) -> tuple[str, list[dict], list[dict]]:
    units = [{"char":c,"origins":[[i,i+1]],"operations":[]} for i,c in enumerate(text)]
    changes = []
    def merged(char, consumed, operation):
        origins = sorted({tuple(span) for u in consumed for span in u["origins"]})
        joined = []
        for a,b in origins:
            if joined and a <= joined[-1][1]:
                joined[-1][1] = max(joined[-1][1],b)
            else:
                joined.append([a,b])
        operations = sorted({v for u in consumed for v in u["operations"]} | {operation})
        return {"char":char,"origins":joined,"operations":operations}
    result=[]
    i=0
    while i<len(units):
        u=units[i]
        if u["char"]=="\r":
            width=2 if i+1<len(units) and units[i+1]["char"]=="\n" else 1
            result.append(merged("\n",units[i:i+width],"LINE_ENDING"))
            i+=width
        else:
            result.append(u)
            i+=1
    units=result
    if plain:
        expected=ud.normalize("NFC","".join(u["char"] for u in units))
        decomposed=[{**u,"char":c} for u in units for c in ud.normalize("NFD",u["char"])]
        ordered,pending=[],[]
        for u in decomposed:
            if ud.combining(u["char"])==0:
                ordered.extend(sorted(pending,key=lambda v:ud.combining(v["char"])))
                pending=[]
                ordered.append(u)
            else:
                pending.append(u)
        ordered.extend(sorted(pending,key=lambda v:ud.combining(v["char"])))
        composed,starter,last=[],None,0
        for u in ordered:
            cc=ud.combining(u["char"])
            if starter is not None and (last==0 or last<cc):
                pair=ud.normalize("NFC",composed[starter]["char"]+u["char"])
                if len(pair)==1:
                    composed[starter]=merged(pair,[composed[starter],u],"NFC")
                    continue
            if cc==0:
                starter=len(composed)
            composed.append(u)
            last=cc
        if "".join(u["char"] for u in composed)!=expected:
            raise ValueError("NFC trace does not reproduce Unicode normalization")
        units=composed
        result=[]
        i=0
        while i<len(units):
            if units[i]["char"] not in HORIZONTAL:
                result.append(units[i]);i+=1;continue
            stop=i+1
            while stop<len(units) and units[stop]["char"] in HORIZONTAL:
                stop+=1
            run=units[i:stop]
            result.append(run[0] if len(run)==1 and run[0]["char"]==" " else merged(" ",run,"HORIZONTAL_SPACE"))
            i=stop
        units=result
        result=[]
        start=0
        for stop in range(len(units)+1):
            if stop<len(units) and units[stop]["char"]!="\n":
                continue
            left,right=start,stop
            while left<right and units[left]["char"]==" ":
                left+=1
            while right>left and units[right-1]["char"]==" ":
                right-=1
            removed=units[start:left]+units[right:stop]
            if removed:
                changes.append({"operation":"LINE_EDGE_SPACE_REMOVED",
                    "input_intervals":[span for u in removed for span in u["origins"]],"output_intervals":[]})
            result.extend(units[left:right])
            if stop<len(units):
                result.append(units[stop])
            start=stop+1
        units=result
    mappings=[]
    for i,u in enumerate(units):
        origins=u["origins"]
        mappings.append({"output_start":i,"output_end":i+1,
            "input_start":min(s[0] for s in origins),"input_end":max(s[1] for s in origins),
            "input_intervals":origins,"precision":"TRANSFORM_TRACE",
            "operation":"+".join(u["operations"]) or "IDENTITY"})
    return "".join(u["char"] for u in units),mappings,changes


def manual(text: str, original: str):
    normalized=text
    mappings=[] if not normalized else [{"output_start":0,"output_end":len(normalized),
        "input_start":0,"input_end":len(original),"input_intervals":[[0,len(original)]],
        "precision":"FIELD_REFERENCE","operation":"MANUAL_REPLACEMENT"}]
    return normalized,mappings,[{"operation":"MANUAL_REPLACEMENT",
        "input_intervals":[[0,len(original)]],"output_intervals":[[0,len(normalized)]]}]
