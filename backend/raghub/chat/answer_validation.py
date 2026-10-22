"""Keep strict answer-schema failures actionable without changing model output."""
from __future__ import annotations

import re

from pydantic import ValidationError

from ..ingestion.store import BusinessError
from .contracts import Answer


def validate_generated_answer(raw, chat):
    try:
        return Answer.model_validate(raw).model_dump()
    except ValidationError as exc:
        chat["answer_validation_failure"] = {
            "error_type": type(exc).__name__,
            "violations": [
                {"location": list(item["loc"]), "type": item["type"], "message": item["msg"]}
                for item in exc.errors(include_input=False, include_url=False)
            ],
            "automatic_repair": False,
            "answer_committed": False,
        }
        raise BusinessError(
            "ANSWER_SCHEMA_INVALID",
            "回答停在最后检查。问题、已发布资料和本轮检索记录已保留，答案未提交为正式结果。"
            "请先查看本轮状态。这是系统整理回答时的问题，无需重新导入资料。可以把多个要点拆开提问，或稍后重新提问。",
        ) from exc

SOURCE_SCOPE_POLICY = "RETRIEVED_FRAGMENT_DIRECT_ABSENCE_GUARD_V1"
_DIRECT_SOURCE_ABSENCE = re.compile(
    r"(?:^|[，,。；;！？!?\n])\s*(?:(?:但|而|不过|然而|因此|所以)\s*)?"
    r"(?:(?:该|这|那)(?:一)?(?:篇|份)(?:笔记|资料|文章|文档)?(?:中|里)?"
    r"|本篇|本文|《[^《》\n]{1,160}》(?:中|里)?)"
    r"\s*(?:未涉及|没有涉及|不涉及|未提及|没有提及|未提到|没有提到|没提到"
    r"|未说明|没有说明|未给出|没有给出|未提供|没有提供|不包含|没有包含"
    r"|未包含|未写|没有写|没写|没有)"
)


def validate_evidence_scope(answer, source_scope, chat):
    """Reject direct whole-source absence claims from fragment-only retrieval.

    This bounded grammar is a scope guard, not a semantic-support judge.
    Quoted wording and negated mentions do not assert source absence directly.
    No answer text or selected support IDs are rewritten.
    """
    violations = []
    if (type(source_scope) is not dict
            or source_scope.get("kind") != "RETRIEVED_FRAGMENTS"
            or source_scope.get("whole_document_coverage") != "NOT_VERIFIED"):
        violations.append({"location": ["source_scope"], "reason": "CURRENT_FRAGMENT_SCOPE_NOT_BOUND"})
    else:
        fields = [(paragraph["text"], ["paragraphs", index, "text"])
                  for index, paragraph in enumerate(answer["paragraphs"])]
        fields += [(text, ["limitations", index]) for index, text in enumerate(answer["limitations"])]
        for text, location in fields:
            for match in _DIRECT_SOURCE_ABSENCE.finditer(text):
                violations.append({"location": location, "span": [match.start(), match.end()],
                                   "matched_scope_phrase": match.group().strip(),
                                   "reason": "WHOLE_SOURCE_ABSENCE_NOT_PROVEN"})
    if violations:
        chat["answer_scope_failure"] = {
            "policy": SOURCE_SCOPE_POLICY, "violations": violations,
            "automatic_repair": False, "answer_committed": False,
            "semantic_support_proven": False,
        }
        raise BusinessError(
            "CITATION_INVALID",
            "回答校验未通过。问题、已发布资料和本轮检索记录已保留，答案未提交为正式结果。"
            "请先查看本轮状态及检索详情；需要继续时，手动发起新提问。",
        )
    return answer


NAMED_CURRENCY_POLICY = "SELECTED_SUPPORT_NAMED_CURRENCY_V1"
_CURRENCY_FORMS = {
    "USD": r"(?:(?<![A-Za-z])(?:USD|U\.?S\.?\s+dollars?|United States dollars?)(?![A-Za-z])|(?<![A-Za-z])US\$|美元|美金)",
    "CNY": r"(?:(?<![A-Za-z])(?:CNY|RMB|Chinese yuan)(?![A-Za-z])|人民币)",
    "HKD": r"(?:(?<![A-Za-z])(?:HKD|Hong Kong dollars?)(?![A-Za-z])|(?<![A-Za-z])HK\$|港元|港币)",
    "CAD": r"(?:(?<![A-Za-z])(?:CAD|Canadian dollars?)(?![A-Za-z])|(?<![A-Za-z])CA\$|加元|加拿大元)",
    "AUD": r"(?:(?<![A-Za-z])(?:AUD|Australian dollars?)(?![A-Za-z])|(?<![A-Za-z])AU\$|澳元|澳大利亚元)",
    "EUR": r"(?:(?<![A-Za-z])EUR(?![A-Za-z])|欧元)",
    "JPY": r"(?:(?<![A-Za-z])(?:JPY|Japanese yen)(?![A-Za-z])|日元)",
    "GBP": r"(?:(?<![A-Za-z])(?:GBP|British pounds?)(?![A-Za-z])|英镑)",
}
_AMOUNT_SCALE = r"(?:百万|千万|十亿|万亿|亿|万|千|million|billion|thousand)?"
_AMOUNT_PREFIX = r"\d[\d,]*(?:\.\d+)?\s*" + _AMOUNT_SCALE + r"\s*"
_UNIT_PREFIX = r"(?:单位|币种|货币|计价币种)\s*(?:[:：]|为|是)\s*" + _AMOUNT_SCALE + r"\s*"


def validate_named_currency_claims(answer, selected_texts, chat):
    """Reject bounded amount/unit claims naming an unsupported currency.

    Only actual paragraph-selected support is eligible. Bare dollar/yen symbols
    do not identify a named currency. Presence is a lexical prerequisite, not a
    proof of semantic attribution, amount correctness, or table relationships.
    No text, IDs, units, or currency symbols are changed.
    """
    violations = []
    for index, paragraph in enumerate(answer["paragraphs"]):
        selected = "\n".join(selected_texts[key] for key in paragraph["support_unit_ids"])
        for currency, forms in _CURRENCY_FORMS.items():
            claim = re.compile(
                r"(?:" + _AMOUNT_PREFIX + r"|" + _UNIT_PREFIX + r")" + forms
                + r"|" + forms + r"\s*[$€£]?\s*\d",
                re.IGNORECASE,
            )
            matches = list(claim.finditer(paragraph["text"]))
            if matches and re.search(forms, selected, re.IGNORECASE) is None:
                violations.append({
                    "location": ["paragraphs", index, "text"],
                    "currency": currency,
                    "amount_or_unit_phrases": [match.group() for match in matches],
                    "reason": "NAMED_CURRENCY_NOT_IN_SELECTED_SUPPORT",
                })
    if violations:
        chat["answer_currency_failure"] = {
            "policy": NAMED_CURRENCY_POLICY,
            "violations": violations,
            "automatic_repair": False,
            "answer_committed": False,
            "semantic_support_proven": False,
        }
        raise BusinessError(
            "CITATION_INVALID",
            "回答校验未通过。问题、已发布资料和本轮检索记录已保留，答案未提交为正式结果。"
            "请先查看本轮状态及检索详情；需要继续时，手动发起新提问。",
        )
    return answer

SECTION_ATTRIBUTION_POLICY = "SELECTED_SUPPORT_EXPLICIT_SECTION_V2"
_SECTION_ATTRIBUTION_TAIL = re.compile(
    r"[\"”」』》）)\]]*\s*(?:一节|章节|部分|所(?:说|述|称|指)"
    r"|中(?:的)?|里|(?:也)?(?:指出|认为|提出|说明|提到|强调|表示|给出|建议|主张))"
)


_SECTION_QUOTE_PAIRS = {"“": "”", "‘": "’", "\u0022": "\u0022", "\u0027": "\u0027", "「": "」", "『": "』", "《": "》"}
_QUOTED_SECTION_ATTRIBUTION = re.compile(
    r"""(?P<open>["\u0027“‘「『《])(?P<title>[^"\u0027“”‘’「」『』《》\n]{1,160})(?P<close>["\u0027”’」』》])\s*(?:一节|章节|部分)"""
)

def validate_section_attributions(answer, groups, chat):
    """Require selected evidence for bounded explicit section attributions.

    Titles come only from backend-bound headings in the actually sent groups.
    This is a metadata prerequisite, not proof of semantic support or authorship.
    It does not infer aliases, rewrite text, or add support IDs.
    """
    violations = []
    unit_sections = {}
    unit_bound_texts = {}
    titles = set()
    for group in groups:
        section_titles = group.get("section_titles")
        if (type(section_titles) is not list
                or any(type(title) is not str or not title for title in section_titles)):
            violations.append({"location": ["evidence", "section_titles"],
                               "reason": "BOUND_SECTION_METADATA_INVALID"})
            continue
        for unit in group["support_units"]:
            key = unit["support_unit_id"]
            if key in unit_sections:
                violations.append({"location": ["evidence", "support_units"],
                                   "reason": "DUPLICATE_SENT_SUPPORT_ID"})
            unit_sections[key] = set(section_titles)
            context, body = group.get("context_text"), unit.get("text")
            if type(context) is str and type(body) is str:
                unit_bound_texts[key] = context + "\n" + body
        titles.update(section_titles)
    for index, paragraph in enumerate(answer["paragraphs"]):
        selected_titles = set()
        for key in paragraph["support_unit_ids"]:
            if key not in unit_sections:
                violations.append({"location": ["paragraphs", index, "support_unit_ids"],
                                   "reason": "SELECTED_SUPPORT_NOT_SENT"})
            else:
                selected_titles.update(unit_sections[key])
        selected_text = "\n".join(unit_bound_texts[key]
                                  for key in paragraph["support_unit_ids"]
                                  if key in unit_bound_texts)
        for match in _QUOTED_SECTION_ATTRIBUTION.finditer(paragraph["text"]):
            if _SECTION_QUOTE_PAIRS[match.group("open")] != match.group("close"):
                continue
            title = match.group("title").strip()
            if title not in selected_titles and title not in selected_text:
                violations.append({
                    "location": ["paragraphs", index, "text"],
                    "section_title": title,
                    "span": [match.start("title"), match.end("title")],
                    "reason": "QUOTED_SECTION_NOT_IN_SELECTED_SUPPORT",
                })
        for title in sorted(titles):
            for match in re.finditer(re.escape(title), paragraph["text"]):
                tail = paragraph["text"][match.end():]
                if _SECTION_ATTRIBUTION_TAIL.match(tail) and title not in selected_titles:
                    violations.append({
                        "location": ["paragraphs", index, "text"],
                        "section_title": title,
                        "span": [match.start(), match.end()],
                        "reason": "ATTRIBUTED_SECTION_NOT_IN_SELECTED_SUPPORT",
                    })
    if violations:
        chat["answer_section_failure"] = {
            "policy": SECTION_ATTRIBUTION_POLICY,
            "violations": violations,
            "automatic_repair": False,
            "answer_committed": False,
            "semantic_support_proven": False,
        }
        raise BusinessError(
            "CITATION_INVALID",
            "回答校验未通过。问题、已发布资料和本轮检索记录已保留，答案未提交为正式结果。"
            "请先查看本轮状态及检索详情；需要继续时，手动发起新提问。",
        )
    return answer
