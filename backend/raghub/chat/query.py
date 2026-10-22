"""Conservative source intervals and literal conditions for the existing D01 path."""
from __future__ import annotations
import re
from .contracts import Understanding, explicit_control, normalized_question
from ..model_profiles import text_sha256

RULE_VERSION = "D01_INTERVALS_T176_V7"
OLD_OBJECT = r"(?:刚才的问题|上一个问题|这次的问题|当前问题|旧问题|[vV]\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.]+)?\s*的问题)"
COMBINED = re.compile(r"\s*(?P<control>(?:请\s*)?(?:先\s*)?取消\s*" + OLD_OBJECT + r")(?P<link>[，,；;。]\s*)(?P<knowledge>\S.*?)\s*", re.S)
OLD_CONTROL_ACTION = r"(?:(?:取消|撤销|保留|不问)\s*" + OLD_OBJECT + r"|撤销\s*取消)"
SECOND_CONTROL_PREFIX = re.compile(r"^(?:(?:现在|接下来|然后|再)\s*)?(?:请\s*|帮我\s*)?(?:(?:不要|别|不能|不必)\s*)?" + OLD_CONTROL_ACTION)
CONTROL_CLAUSE = re.compile(r"[，,；;。\n]\s*(?:(?:但|同时|另外|再|并且|顺便|然后)\s*)?(?:请\s*|帮我\s*)?(?:(?:不要|别|不能|不必)\s*)?" + OLD_CONTROL_ACTION)
UNCLEAR_PREFIX = re.compile(r"^(?:[“\"‘']\s*|请\s*|帮我\s*)?(?:(?:不要|别|不能|不必)\s*)?" + OLD_CONTROL_ACTION)
TOPIC_SWITCH = re.compile(r"\s*(?:请\s*)?(?:换(?:个)?(?:问题|话题)|换题|改问)\s*[，,；;。:：]\s*\S.*", re.S)
UNITS = r"(?:GiB|MiB|KiB|GB|MB|KB|MHz|GHz|ms|Hz|kg|mm|cm|km|%|％|毫秒|秒|分钟|小时|天|周|月|年|元|个|次|条|字节)"
VERSION = re.compile(r"(?<![A-Za-z0-9_.-])[vV]\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.]+)?(?![A-Za-z0-9_.-])")
MODEL = re.compile(r"(?<![A-Za-z0-9_.-])(?:[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)+|[A-Za-z]{1,8}\d+[A-Za-z0-9.-]*|CPU|GPU|MPS)(?![A-Za-z0-9_.-])")
NUMBER = re.compile(r"(?<![A-Za-z0-9_.-])\d+(?:\.\d+)?(?:\s*" + UNITS + r")?(?![A-Za-z0-9_.-])")
INDEPENDENT_UNIT = re.compile(r"(?<![A-Za-z0-9_.-])(?:GiB|MiB|KiB|GB|MB|KB|GHz|MHz|Hz|ms|kg|mm|cm|km|%|％)(?![A-Za-z0-9_.-])")
CONDITION = re.compile(r"不是|不等于|不使用|不采用|不涉及|不支持|不允许|不得|禁止|不能|不要|不含|未提供|未启用|仅|只在|只有|如果|当.+时|至少|最多|大于|小于|超过|最近|过去|截止|截至")

def knowledge_view(raw):
    normalized = normalized_question(raw)
    control = explicit_control(normalized)
    base = {"rule_version": RULE_VERSION, "raw_message": raw,
            "interval_coordinate_space": "RAW_UNICODE_CODE_POINTS_HALF_OPEN",
            "normalization_operations": ["CRLF_CR_TO_LF", "STRIP_EDGES"],
            "control_removed_intervals": [], "knowledge_intervals": [],
            "control_intent": control, "boundary_reliable": True,
            "resolution_intent_required": "cancel" if control == "cancel" else None}
    if control is not None:
        return {**base, "effective_original_query": None, "ownership_basis": "EXACT_COMPLETE_CONTROL"}
    match = COMBINED.fullmatch(raw)
    if (match and not CONTROL_CLAUSE.search(match.group("knowledge"))
            and not SECOND_CONTROL_PREFIX.search(normalized_question(match.group("knowledge")))):
        start, end = match.span("knowledge")
        cstart = match.start("control")
        return {**base, "effective_original_query": normalized_question(raw[start:end]),
                "control_intent": "cancel",
                "resolution_intent_required": "cancel",
                "control_removed_intervals": [{"start": cstart, "end": start,
                                               "text": raw[cstart:start], "rule": "EXPLICIT_CANCEL_THEN_ONE_NEW_QUERY"}],
                "knowledge_intervals": [{"start": start, "end": end, "text": raw[start:end]}],
                "ownership_basis": "COMPLETE_AFFIRMATIVE_CANCEL_GRAMMAR"}
    if UNCLEAR_PREFIX.search(normalized) or CONTROL_CLAUSE.search(normalized):
        return {**base, "effective_original_query": None, "boundary_reliable": False,
                "ownership_basis": "CONTROL_OR_ACTION_OWNERSHIP_UNRESOLVED"}
    start = len(raw) - len(raw.lstrip())
    end = len(raw.rstrip())
    return {**base, "effective_original_query": normalized,
            "resolution_intent_required": "replace" if TOPIC_SWITCH.fullmatch(raw) else None,
            "knowledge_intervals": [{"start": start, "end": end, "text": raw[start:end]}],
            "ownership_basis": "ONE_UNSPLIT_MESSAGE_WITHOUT_CONTROL_CLAUSE"}

DOCUMENT_REFERENCE_PATTERNS = (
    ("EXPLICIT_BOOK_TITLE", re.compile(r"《(?P<name>[^《》\r\n]{2,160})》")),
    ("EXPLICIT_NAMED_DOCUMENT", re.compile(
        r"(?:按照|根据|切换到|换成|我是指|按)\s*"
        r"(?P<name>[^，,；;。！？?!\r\n《》]{2,100}?)(?:那篇|这篇|笔记)")),
)


def explicit_document_references(text):
    """Preserve current literal resource names, without resolving aliases."""
    found = []
    for basis, pattern in DOCUMENT_REFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group("name")
            name = raw.strip()
            if basis == "EXPLICIT_NAMED_DOCUMENT" and re.search(
                    r"刚才|之前|上述|前面|那份|这份|他的|它的", name):
                continue  # A deictic reference still needs the existing history path.
            start = match.start("name") + len(raw) - len(raw.lstrip())
            end = start + len(name)
            if any(start < item["end"] and item["start"] < end for item in found):
                continue
            found.append({"text": name, "start": start, "end": end, "basis": basis})
    return sorted(found, key=lambda item: (item["start"], item["end"]))


def document_reference_check(original, rewritten):
    references = explicit_document_references(original)
    normalized = re.sub(r"\s+", "", rewritten)
    missing = [item for item in references
               if re.sub(r"\s+", "", item["text"]) not in normalized]
    return {"policy": "EXPLICIT_DOCUMENT_LITERAL_WHITESPACE_ONLY_V1",
            "coordinate_space": "NORMALIZED_EFFECTIVE_QUERY",
            "references": references, "missing": missing,
            "accepted": not missing, "semantic_equivalence_proven": False}


def protected_conditions(text):
    found = []
    occupied = []
    for kind, pattern in (("version", VERSION), ("model", MODEL), ("number_unit", NUMBER),
                          ("independent_unit", INDEPENDENT_UNIT)):
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in occupied):
                continue
            found.append({"kind": kind, "text": match.group(), "start": match.start(), "end": match.end()})
            occupied.append(match.span())
    for match in re.finditer(r"[^，,；;。！？?\n]+", text):
        clause = match.group().strip()
        if CONDITION.search(clause):
            found.append({"kind": "negation_or_condition_clause", "text": clause,
                          "start": match.start(), "end": match.end()})
    return found

def literal_present(literal, text):
    # Only whitespace variation is accepted. Units, case and numbers are not converted.
    pattern = r"\s*".join(re.escape(piece) for piece in literal.split())
    return re.search(r"(?<![A-Za-z0-9_.-])" + pattern + r"(?![A-Za-z0-9_.-])", text) is not None

def condition_check(original, rewritten, history_sources):
    original_items = protected_conditions(original)
    rewritten_items = protected_conditions(rewritten)
    missing = [item for item in original_items if not literal_present(item["text"], rewritten)]
    added = []
    for item in rewritten_items:
        if literal_present(item["text"], original):
            continue
        origins = [source["source_id"] for source in history_sources
                   if literal_present(item["text"], source["text"])]
        if not origins:
            added.append(item)
    document_check = document_reference_check(original, rewritten)
    return {"policy": RULE_VERSION, "coordinate_space": "NORMALIZED_EFFECTIVE_QUERY",
            "original_conditions": original_items, "rewritten_conditions": rewritten_items,
            "provided_history_sources": history_sources, "missing_original_conditions": missing,
            "unsupported_added_conditions": added, "document_reference_check": document_check,
            "accepted": not missing and not added and document_check["accepted"],
            "semantic_equivalence_proven": False}

def rewrite_condition_sources(value, history, awaiting):
    """Collect sources after the understanding IDs and clarification binding validate."""
    resolution = value["clarification_resolution"]
    if resolution is not None and resolution["intent"] == "cancel":
        return []
    sources = []
    for turn in history["turns"]:
        if turn["turn_id"] in value["used_turn_ids"]:
            sources.extend({"source_id":message["message_id"],"kind":"provided_user_message","text":message["text"]}
                           for message in turn["messages"] if message["role"] == "user")
    if history["summary"]:
        sources.extend({"source_id":item["item_id"],"kind":"provided_summary_item",
                        "status":item["status"],"text":item["content"]}
                       for item in history["summary"]["items"] if item["item_id"] in value["used_summary_item_ids"])
    # Replacing a question does not silently inherit its pending conditions.
    if resolution is not None and resolution["intent"] == "complete":
        sources.append({"source_id":awaiting["source_turn_id"],"kind":"bound_awaiting_original",
                        "text":awaiting["original_question"]})
        sources.extend({"source_id":message["message_id"],"kind":"bound_awaiting_supplement","text":message["text"]}
                       for message in awaiting["supplements"])
    return sources

def t60_recovery(view, awaiting, original_inputs):
    """Decline recovery: failed understanding has not established a standalone query."""
    return None

def clarification_original_question(raw_message, chat):
    """Use the existing validated original; unresolved ownership retains the raw context."""
    view = chat.get("knowledge_view")
    original = chat.get("effective_original_query")
    if type(view) is not dict or view.get("boundary_reliable") is not True or not original:
        return raw_message
    if (type(original) is not str or view.get("raw_message") != raw_message
            or view.get("effective_original_query") != original):
        raise ValueError("new clarification has no bound effective original")
    gates = chat.get("query_token_gates")
    checked = gates.get("original_before_any_model") if type(gates) is dict else None
    if (type(checked) is not dict or checked.get("truncation") is not False
            or type(checked.get("queries")) is not list or len(checked["queries"]) != 1
            or type(checked["queries"][0]) is not dict
            or checked["queries"][0].get("input_sha256") != text_sha256(original)):
        raise ValueError("new clarification effective original did not pass the original query gate")
    return original
