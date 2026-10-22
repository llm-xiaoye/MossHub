"""T187 soft review hints over each paragraph's selected support excerpts only."""
from __future__ import annotations
import re
import unicodedata

NUMBERS = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:,\d{3})*(?:\.\d+)?(?![A-Za-z0-9_.])")
UNITS = re.compile(r"GiB|MiB|KiB|GB|MB|KB|MHz|GHz|毫秒|ms|Hz|kg|毫米|mm|厘米|cm|公里|km|字节|秒|分钟|小时|天|周|个月|年|元|个|次|条|%|％")
NEGATIONS = re.compile(r"不支持|不允许|不得|禁止|不能|未提供|未启用|未实现|不包含|不适用")
UNIT_ALIASES = {"毫秒": "ms", "毫米": "mm", "厘米": "cm", "公里": "km", "％": "%"}

def numeric_literals(text):
    normalized = unicodedata.normalize("NFKC", text)
    return {match.group().replace(",", "") for match in NUMBERS.finditer(normalized)}

def unit_literals(text):
    normalized = unicodedata.normalize("NFKC", text)
    return {UNIT_ALIASES.get(match.group(), match.group()) for match in UNITS.finditer(normalized)}

def support_suspicions(paragraphs):
    doubts = []
    for index, paragraph in enumerate(paragraphs):
        selected = paragraph["references"]
        selected_ids = [unit["support_unit_id"] for unit in selected]
        excerpts = "\n".join(unit["excerpt"] for unit in selected)
        common = {"paragraph_index": index, "selected_support_unit_ids": selected_ids,
                  "severity": "REVIEW_HINT", "semantic_support_proven": False}
        for number in sorted(numeric_literals(paragraph["text"]) - numeric_literals(excerpts)):
            doubts.append({**common, "code": "NUMBER_NOT_FOUND_IN_SELECTED_SUPPORT",
                           "answer_fragment": number, "reason": "本段所选原文未检出该数字字面量；计算、换算等仍需人工核对。"})
        for unit in sorted(unit_literals(paragraph["text"]) - unit_literals(excerpts)):
            doubts.append({**common, "code": "UNIT_NOT_FOUND_IN_SELECTED_SUPPORT",
                           "answer_fragment": unit, "reason": "本段所选原文未检出该单位；仅使用明确的字面别名，不验证换算。"})
        if not NEGATIONS.search(paragraph["text"]):
            for source in selected:
                match = NEGATIONS.search(source["excerpt"])
                if match:
                    doubts.append({**common, "code": "NEGATION_MAY_BE_OMITTED",
                                   "answer_fragment": paragraph["text"],
                                   "trigger_support_unit_id": source["support_unit_id"],
                                   "support_fragment": source["excerpt"][max(0, match.start()-24):match.end()+48],
                                   "reason": "所选原文有明确否定，答案未检出保守否定词；只提示可能遗漏限制。"})
    return doubts
