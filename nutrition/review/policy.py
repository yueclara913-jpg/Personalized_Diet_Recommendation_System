"""Pure USDA staging-record review; never approves or writes food data."""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from urllib.parse import urlsplit

from nutrition.normalization import UNITS
from nutrition.sources.usda_foundation import STANDARD_CODES

ENERGY_IDS = (1008, 2047, 2048)
SUGAR_IDS = (1063, 2000)
CORE_IDS = {
    "protein": (1003,), "fat": (1004,), "carbohydrates": (1005,),
    "fiber": (1079,), "sodium": (1093,), "iron": (1089,),
    "calories": ENERGY_IDS, "sugar": SUGAR_IDS,
}
LEVELS = ("ignore", "needs_review", "rejected_by_policy")


@dataclass(frozen=True)
class ReviewPolicy:
    version: str = "nutrition-review-v1"
    required_nutrients: tuple = ("calories", "protein", "fat", "carbohydrates")
    accepted_bases: tuple = ("per_100g",)
    allowed_data_types: tuple = ("Foundation",)
    missing_level: str = "needs_review"
    unknown_unit_level: str = "needs_review"
    multiple_energy_level: str = "needs_review"
    multiple_sugar_level: str = "needs_review"
    require_verified_official_file: bool = True

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("Policy version is required")
        if not set(self.required_nutrients) <= set(CORE_IDS):
            raise ValueError("Unknown required nutrient")
        if not self.accepted_bases or not set(self.accepted_bases) <= {"per_100g", "per_100ml", "per_serving", "unknown"}:
            raise ValueError("Invalid accepted basis")
        if not self.allowed_data_types or not all(isinstance(x, str) and x for x in self.allowed_data_types):
            raise ValueError("Invalid source data type policy")
        if any(getattr(self, name) not in LEVELS for name in
               ("missing_level", "unknown_unit_level", "multiple_energy_level", "multiple_sugar_level")):
            raise ValueError("Invalid issue treatment level")
        if type(self.require_verified_official_file) is not bool:
            raise ValueError("require_verified_official_file must be boolean")


DEFAULT_POLICY = ReviewPolicy()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _issue(rule_id, issue_type, severity, reason, location, action, nutrient_ids=()):
    return {"rule_id": rule_id, "issue_type": issue_type, "severity": severity,
            "reason": reason, "source_location": location,
            "suggested_action": action, "affected_nutrient_ids": list(nutrient_ids)}


def _number(value):
    if value is None:
        return "missing"
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return "invalid"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "invalid"
    if not number.is_finite() or number < 0 or number > Decimal("1e12"):
        return "invalid"
    return "zero" if number == 0 else "positive"


def _treatment(policy, name):
    return getattr(policy, name)


def evaluate_food_record(record, provenance_context, policy=DEFAULT_POLICY):
    """Return a deterministic recommendation while preserving pending status.

    ``provenance_context`` is explicit evidence supplied by the caller. A digest
    alone never proves that a file was obtained from the official publisher.
    """
    if not isinstance(policy, ReviewPolicy):
        raise TypeError("policy must be ReviewPolicy")
    if not isinstance(record, dict) or not isinstance(provenance_context, dict):
        raise TypeError("record and provenance_context must be dictionaries")
    try:
        record_hash = hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()
    except (TypeError, ValueError) as error:
        raise ValueError("Record is not canonical JSON") from error
    issues = []

    def add(rule, kind, severity, reason, location, action, ids=()):
        if severity != "ignore":
            issues.append(_issue(rule, kind, severity, reason, location, action, ids))

    fdc = record.get("source_food_id")
    if type(fdc) is not int or not 0 < fdc <= 9223372036854775807:
        add("SRC001", "invalid_food_id", "rejected_by_policy", "FDC ID 必须是正整数。",
            "source_food_id", "对照原始记录修正身份后重新解析。")
    name = record.get("food_name")
    if not isinstance(name, str) or not name.strip():
        add("STR001", "invalid_food_name", "rejected_by_policy", "食品名称为空或类型非法。",
            "food_name", "核对原始食品描述。")
    data_type = record.get("source_data_type")
    if data_type not in policy.allowed_data_types:
        add("SRC002", "disallowed_data_type", "rejected_by_policy", "数据类型不在策略允许范围。",
            "source_data_type", "确认来源数据类型或使用经批准的策略。")
    if record.get("review_status") != "pending":
        add("GOV001", "invalid_staging_status", "rejected_by_policy", "暂存记录必须保持 pending。",
            "review_status", "从只读暂存库重新读取记录。")

    version = record.get("source_version")
    locator = record.get("source_locator")
    sha = record.get("source_file_sha256")
    url = record.get("source_url")
    if not isinstance(version, str) or not version.strip():
        add("SRC003", "missing_source_version", "rejected_by_policy", "缺少来源版本。",
            "source_version", "补充可追溯的发布版本。")
    if not isinstance(locator, str) or not re.fullmatch(r"FoundationFoods\[\d+\]", locator):
        add("SRC004", "invalid_source_locator", "rejected_by_policy", "原始 JSON 位置缺失或格式非法。",
            "source_locator", "从完整快照重新解析。")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        add("SRC005", "invalid_source_hash", "rejected_by_policy", "原始文件 SHA-256 缺失或非法。",
            "source_file_sha256", "从只读原始文件重新计算哈希。")
    parts = urlsplit(url) if isinstance(url, str) else None
    if (parts is None or parts.scheme != "https" or parts.hostname != "fdc.nal.usda.gov" or
            parts.username or parts.password or parts.query or parts.fragment or parts.port not in (None, 443)):
        add("SRC006", "invalid_source_url", "rejected_by_policy", "来源 URL 不是允许的 USDA 官方 HTTPS 格式。",
            "source_url", "核对官方来源地址，禁止携带凭据或查询串。")
    verified = provenance_context.get("official_file_verified") is True
    matching_evidence = (verified and provenance_context.get("verified_source_file_sha256") == sha and
                         provenance_context.get("verified_source_version") == version and
                         provenance_context.get("verified_source_url") == url and
                         isinstance(provenance_context.get("verification_reference"), str) and
                         bool(provenance_context["verification_reference"].strip()))
    if policy.require_verified_official_file and not matching_evidence:
        add("SRC007", "official_authenticity_unverified", "needs_review",
            "SHA-256 只能证明文件一致，当前没有匹配的独立官方真实性证据。",
            "provenance_context", "人工核对官方发布页、版本、URL、哈希并记录证据引用。")
    if provenance_context.get("duplicate_fdc_count", 1) != 1:
        add("INT001", "duplicate_food_id", "rejected_by_policy", "同一审核批次存在重复 FDC ID。",
            "source_food_id", "排查重复快照记录，不要覆盖或合并。")

    basis = record.get("nutrition_basis")
    evidence = record.get("basis_evidence")
    if basis not in policy.accepted_bases:
        add("BAS001", "unaccepted_nutrition_basis", "needs_review", "计量基准不在策略允许范围。",
            "nutrition_basis", "核实分母；不得自动换算为每 100 g。")
    if not isinstance(evidence, str) or not evidence.strip():
        add("BAS002", "missing_basis_evidence", "needs_review", "缺少计量基准依据。",
            "basis_evidence", "提供对应数据类型的官方基准说明。")
    raw = record.get("raw_record")
    if not isinstance(raw, dict) or not isinstance(raw.get("foodNutrients"), list):
        add("STR002", "invalid_nutrient_structure", "rejected_by_policy", "缺少原始营养素数组。",
            "raw_record.foodNutrients", "从原始快照重新解析。")
        entries = []
    else:
        entries = raw["foodNutrients"]
    if isinstance(raw, dict) and "foodPortions" in raw and not isinstance(raw["foodPortions"], list):
        add("STR007", "invalid_portion_structure", "rejected_by_policy", "foodPortions 不是数组。",
            "raw_record.foodPortions", "从原始快照重新解析。")
    raw_ids = []
    for index, entry in enumerate(entries):
        nutrient = entry.get("nutrient") if isinstance(entry, dict) else None
        nutrient_id = nutrient.get("id") if isinstance(nutrient, dict) else None
        if isinstance(nutrient_id, str) and nutrient_id.isascii() and nutrient_id.isdigit() and len(nutrient_id) <= 20:
            nutrient_id = int(nutrient_id)
        if type(nutrient_id) is not int or nutrient_id <= 0:
            add("STR004", "invalid_raw_nutrient_id", "rejected_by_policy", "原始营养素 ID 非法，无法追溯。",
                f"raw_record.foodNutrients[{index}].nutrient.id", "对照原始快照重新解析。")
        else:
            raw_ids.append(nutrient_id)
    if isinstance(raw, dict) and raw.get("foodPortions") and basis == "per_100g":
        add("BAS003", "portion_conversion_requires_review", "needs_review",
            "存在份量信息，但不能据此确认是否发生了未经核实的份量换算。",
            "raw_record.foodPortions", "人工核对基准、克重与转换链。")
    description = (name or "").casefold() if isinstance(name, str) else ""
    if any(token in description for token in ("dry matter", "dry basis", "0% moisture")):
        add("BAS004", "possible_dry_matter_basis", "needs_review", "食品描述可能涉及干物质基准。",
            "food_name", "人工区分干物质与可食状态；不得自动换算。")

    seen, present = set(), set()
    nutrients = record.get("nutrients")
    if not isinstance(nutrients, list):
        add("STR003", "invalid_parsed_nutrients", "rejected_by_policy", "解析营养素列表结构非法。",
            "nutrients", "重新运行受支持版本解析器。")
        nutrients = []
    standardized = record.get("standardized")
    if not isinstance(standardized, dict) or set(standardized) != set(STANDARD_CODES):
        add("STR006", "invalid_standardized_structure", "rejected_by_policy", "标准营养字段结构不完整或含未知字段。",
            "standardized", "使用受支持解析器重新生成八个标准字段。")
        standardized = {}
    for index, item in enumerate(nutrients):
        location = f"nutrients[{index}]"
        if not isinstance(item, dict):
            add("NUT001", "invalid_nutrient_record", "rejected_by_policy", "营养素记录不是对象。",
                location, "核对原始记录并重新解析。")
            continue
        nutrient_id = item.get("source_nutrient_id")
        if type(nutrient_id) is not int or nutrient_id <= 0:
            add("NUT002", "invalid_nutrient_id", "rejected_by_policy", "营养素 ID 非法或无法追溯。",
                location + ".source_nutrient_id", "核对原始 nutrient.id。")
            continue
        if nutrient_id in seen:
            add("NUT003", "duplicate_nutrient_id", "rejected_by_policy", "同一食品存在重复营养素 ID。",
                location + ".source_nutrient_id", "人工排查重复测量，不自动选择。", (nutrient_id,))
        seen.add(nutrient_id)
        present.add(nutrient_id)
        raw_value, raw_unit = item.get("raw_value"), item.get("raw_unit")
        state = _number(raw_value)
        if state == "missing":
            add("NUT004", "missing_value", _treatment(policy, "missing_level"), "原始营养值缺失，不能填零。",
                location + ".raw_value", "人工核实缺测含义并保留 NULL。", (nutrient_id,))
        elif state == "invalid":
            add("NUT005", "invalid_numeric_value", "rejected_by_policy", "营养值非有限、为负或超出技术范围。",
                location + ".raw_value", "对照原始记录；禁止估算替换。", (nutrient_id,))
        if not isinstance(raw_unit, str) or raw_unit not in UNITS:
            add("NUT006", "unknown_unit", _treatment(policy, "unknown_unit_level"), "原始单位未知或不受支持。",
                location + ".raw_unit", "人工核对单位，不要按常用单位猜测。", (nutrient_id,))
        status, reason = item.get("status"), item.get("reason")
        if reason == "below_loq_or_qualified_value":
            add("NUT007", "loq_qualified_value", "needs_review", "数值带 LOQ 或其他限定，不能视为精确零。",
                location, "保留限定信息并由人工决定使用方式。", (nutrient_id,))
        elif status not in ("converted", "missing", "needs_review"):
            add("NUT008", "unexplained_conversion_status", "rejected_by_policy", "转换状态无法解释。",
                location + ".status", "使用受支持解析器重新生成记录。", (nutrient_id,))
        elif status != "converted" and reason not in {
            "missing_value", "unknown_unit", "non_finite_value", "out_of_range",
            "excessive_precision", "below_output_precision", "incompatible_units",
            "invalid_numeric_value", "unsupported_or_unverified_basis",
            "unknown_nutrient_id", "duplicate_nutrient_id", "below_loq_or_qualified_value"}:
            add("NUT008", "unexplained_conversion_status", "rejected_by_policy", "转换失败原因无法解释。",
                location + ".reason", "使用受支持解析器重新生成记录。", (nutrient_id,))
        elif status != "converted" and reason != "below_loq_or_qualified_value":
            add("NUT010", "conversion_requires_review", "needs_review", "营养值没有完成可解释的标准化转换。",
                location, "核对原值、单位、基准和转换依据；不得自动补值。", (nutrient_id,))
    for nutrient_name in policy.required_nutrients:
        ids = CORE_IDS[nutrient_name]
        if not present.intersection(ids):
            add("NUT009", "missing_required_nutrient", _treatment(policy, "missing_level"),
                f"缺少策略要求的 {nutrient_name}。", "nutrients",
                "人工核实缺测；不得生成或补零。", ids)
    if raw_ids != [item.get("source_nutrient_id") for item in nutrients if isinstance(item, dict)]:
        add("STR005", "nutrient_trace_mismatch", "rejected_by_policy",
            "解析营养素 ID 与原始数组不能逐项对应。", "nutrients",
            "从只读原始快照重新解析，禁止手工拼接。")
    selected = {1003: "protein", 1004: "fat", 1005: "carbohydrates", 1079: "fiber",
                1089: "iron", 1093: "sodium"}
    if record.get("energy_id") in ENERGY_IDS:
        selected[record["energy_id"]] = "calories"
    if record.get("sugar_id") in SUGAR_IDS:
        selected[record["sugar_id"]] = "sugar"
    expected_standard = {code: None for code in STANDARD_CODES}
    for item in nutrients:
        if isinstance(item, dict) and item.get("source_nutrient_id") in selected:
            expected_standard[selected[item["source_nutrient_id"]]] = item.get("standardized_value")
    if standardized and standardized != expected_standard:
        add("NUT011", "standardized_value_mismatch", "rejected_by_policy",
            "标准字段与逐营养素转换结果或显式定义选择不一致。", "standardized",
            "从原始记录重新解析；不得手工选择、相加或改写。",
            tuple(sorted(i for i in present if i in selected)))
    energy = sorted(present.intersection(ENERGY_IDS))
    sugar = sorted(present.intersection(SUGAR_IDS))
    if len(energy) > 1:
        add("DEF001", "multiple_energy_definitions", _treatment(policy, "multiple_energy_level"),
            "存在多个能量定义，规则引擎不选择、不相加，也不使用 4-4-9 覆盖。",
            "nutrients", "人工确定用途与语义，保留全部原值。", energy)
    if len(sugar) > 1:
        add("DEF002", "multiple_sugar_definitions", _treatment(policy, "multiple_sugar_level"),
            "存在多个糖定义，规则引擎不选择也不相加。", "nutrients",
            "人工确定目标语义，保留全部原值。", sugar)
    issues.sort(key=lambda item: (item["rule_id"], item["source_location"], item["affected_nutrient_ids"]))
    decision = ("rejected_by_policy" if any(x["severity"] == "rejected_by_policy" for x in issues)
                else "needs_review" if issues else "ready_for_manual_review")
    return {"source_food_id": fdc, "source_version": version, "source_locator": locator,
            "source_file_sha256": sha, "policy_version": policy.version,
            "decision": decision, "issues": issues,
            "severity": "error" if decision == "rejected_by_policy" else "warning" if issues else "info",
            "reason": ("存在违反准入规则的项目。" if decision == "rejected_by_policy" else
                       "存在需要人工核实的项目。" if issues else "基础规则通过，可进入人工审核。"),
            "affected_nutrient_ids": sorted({i for x in issues for i in x["affected_nutrient_ids"]}),
            "review_status": "pending", "evaluated_record_sha256": record_hash}


def policy_from_dict(value):
    if not isinstance(value, dict):
        raise ValueError("Policy JSON must be an object")
    allowed = set(asdict(DEFAULT_POLICY))
    if set(value) - allowed:
        raise ValueError("Unknown policy field")
    converted = dict(value)
    for field in ("required_nutrients", "accepted_bases", "allowed_data_types"):
        if field in converted:
            if not isinstance(converted[field], list):
                raise ValueError(f"{field} must be a JSON array")
            converted[field] = tuple(converted[field])
    return ReviewPolicy(**converted)
