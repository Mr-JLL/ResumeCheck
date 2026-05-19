"""
声明式规则引擎
=============
将岗位筛选规则存储为 JSON，统一由本模块评估，取代 prefilter.py 的三条硬编码规则。

规则格式（rule_config 列，JSON 数组）：
[
  {
    "id":         "age_max",           # 唯一 ID（字符串）
    "label":      "年龄上限",           # 用户可读描述
    "field":      "age",               # 简历字段路径（支持 . 访问嵌套）
    "op":         "lte",               # 运算符
    "value":      35,                  # 比较值
    "fail_reason":"年龄超过35岁",        # 不通过时的描述
    "type":       "hard",              # 目前只支持 hard（强制排除）
    "enabled":    true
  },
  ...
]

支持的运算符：
  lte / gte / lt / gt / eq / neq    — 数值比较
  in / not_in                        — 值是否在列表内
  contains_any / contains_all        — list 字段包含任意/全部指定值
  regex                              — 字符串正则（re.search）
  truthy / falsy                     — 非空/空

类型：
  hard — 不通过→预筛排除（软性条件请写在 AI Prompt 里）
"""

import re
import json
import logging
import database

logger = logging.getLogger(__name__)

# 默认规则集（与旧 prefilter.py 三条规则等价，作为新岗位的初始规则）
DEFAULT_RULES = [
    {
        "id": "age_max",
        "label": "年龄上限",
        "field": "age",
        "op": "lte",
        "value": 0,          # 0 = 不启用
        "fail_reason": "年龄超过上限",
        "type": "hard",
        "enabled": False,
    },
    {
        "id": "min_education",
        "label": "最低学历",
        "field": "first_degree",
        "op": "not_in",
        "value": ["高中", "全日制大专", "成人大专", "成人本科"],
        "fail_reason": "学历不符合要求（大专及以下）",
        "type": "hard",
        "enabled": True,
    },
    {
        "id": "min_years",
        "label": "最低年限",
        "field": "total_work_years",
        "op": "gte",
        "value": 0,          # 0 = 不启用
        "fail_reason": "工作年限不足",
        "type": "hard",
        "enabled": False,
    },
]


def _get_field(structured: dict, field_path: str):
    """从结构化简历中提取字段值，支持 'a.b.c' 路径。"""
    parts = field_path.split(".")
    val = structured
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            return None
    return val


def _eval_rule(structured: dict, rule: dict) -> bool:
    """
    对单条规则求值。返回 True = 通过，False = 不通过。
    缺失数据默认通过（保守策略，交给 LLM 处理）。
    """
    field = rule.get("field", "")
    op = rule.get("op", "eq")
    threshold = rule.get("value")
    val = _get_field(structured, field)

    # 数据缺失时保守放行
    if val is None or val == "" or val == "未明确":
        return True

    try:
        if op == "lte":
            return float(val) <= float(threshold)
        elif op == "gte":
            return float(val) >= float(threshold)
        elif op == "lt":
            return float(val) < float(threshold)
        elif op == "gt":
            return float(val) > float(threshold)
        elif op == "eq":
            return str(val) == str(threshold)
        elif op == "neq":
            return str(val) != str(threshold)
        elif op == "in":
            return str(val) in (threshold if isinstance(threshold, list) else [threshold])
        elif op == "not_in":
            return str(val) not in (threshold if isinstance(threshold, list) else [threshold])
        elif op == "contains_any":
            items = val if isinstance(val, list) else [val]
            targets = threshold if isinstance(threshold, list) else [threshold]
            return any(str(i) in targets for i in items)
        elif op == "contains_all":
            items = val if isinstance(val, list) else [val]
            targets = threshold if isinstance(threshold, list) else [threshold]
            return all(str(t) in [str(i) for i in items] for t in targets)
        elif op == "regex":
            return bool(re.search(str(threshold), str(val)))
        elif op == "truthy":
            return bool(val)
        elif op == "falsy":
            return not bool(val)
    except Exception as e:
        logger.debug(f"规则 {rule.get('id')} 求值异常: {e}")
        return True  # 异常时保守放行

    return True


def apply_rules(structured: dict, rule_config: list) -> dict:
    """
    对结构化简历应用全部规则。
    返回：
    {
      "passed": bool,
      "hard_fail_reasons": [str],   # hard 规则失败原因
      "rule_results": [              # 每条规则的求值结果（调试用）
        {"id": ..., "passed": ..., "type": ...}
      ]
    }
    """
    hard_fails = []
    rule_results = []

    for rule in (rule_config or []):
        if not rule.get("enabled", True):
            continue

        rule_type = rule.get("type", "hard")
        passed = _eval_rule(structured, rule)
        rule_results.append({"id": rule.get("id"), "passed": passed, "type": rule_type})

        if not passed and rule_type == "hard":
            reason = rule.get("fail_reason", rule.get("label", "规则不通过"))
            hard_fails.append(reason)

    return {
        "passed": len(hard_fails) == 0,
        "hard_fail_reasons": hard_fails,
        "rule_results": rule_results,
    }


def get_job_rules(job_name: str) -> list:
    """
    从数据库读取岗位规则配置。
    优先读 rule_config 列；未配置则从 config_json 的旧字段生成兼容规则。
    """
    job = database.get_job(job_name)
    if not job:
        return []

    rule_config_raw = job.get("rule_config")
    if rule_config_raw:
        try:
            return json.loads(rule_config_raw)
        except Exception:
            pass

    # 兼容旧格式：从 config_json 生成规则
    try:
        cfg = json.loads(job.get("config_json") or "{}")
    except Exception:
        cfg = {}

    rules = []
    max_age = cfg.get("max_age", 0) or 0
    if max_age > 0:
        rules.append({
            "id": "age_max", "label": "年龄上限",
            "field": "age", "op": "lte", "value": max_age,
            "fail_reason": f"年龄超过≤{max_age}岁要求",
            "type": "hard", "enabled": True,
        })

    min_edu = cfg.get("min_education", "")
    if min_edu in ("全日制本科", "本科"):
        rules.append({
            "id": "min_education", "label": "最低学历（全日制本科）",
            "field": "first_degree", "op": "not_in",
            "value": ["高中", "全日制大专", "成人大专", "成人本科"],
            "fail_reason": "学历不符合要求（大专及以下）",
            "type": "hard", "enabled": True,
        })

    min_years = cfg.get("min_years", 0) or 0
    if min_years > 0:
        rules.append({
            "id": "min_years", "label": f"最低工作年限（{min_years}年）",
            "field": "total_work_years", "op": "gte", "value": min_years,
            "fail_reason": f"工作年限不足{min_years}年",
            "type": "hard", "enabled": True,
        })

    return rules


def save_job_rules(job_name: str, rules: list) -> bool:
    """将规则写回 jobs.rule_config 列。"""
    job = database.get_job(job_name)
    if not job:
        return False
    try:
        database.update_job_rule_config(job["id"], json.dumps(rules, ensure_ascii=False))
        return True
    except Exception as e:
        logger.error(f"保存规则失败: {e}")
        return False
