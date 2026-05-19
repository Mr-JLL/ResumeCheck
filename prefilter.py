"""
规则引擎预过滤
============
声明式规则引擎：从 rules.py 读取岗位规则配置并评估。
旧的三条硬编码规则（年龄/学历/年限）已迁移为 rule_config JSON，
本模块仅作为 pipeline.py 的调用入口。

设计原则：保守优先——只过滤"一定不符合"的，"边界情况"全部交给 LLM。
"""

import logging
import rules as rule_engine

logger = logging.getLogger(__name__)


def prefilter(structured, job_config, job_name=None):
    """
    规则引擎预过滤。
    优先使用 rule_config 声明式规则；回退到旧三条规则。
    返回 dict: {
      "passed": bool,
      "hard_fail_reasons": [str],
      "rule_results": [...],        # 每条规则的调试结果
      "source": "rule_engine"|"legacy"
    }
    """
    # 尝试声明式规则
    if job_name:
        try:
            rule_config = rule_engine.get_job_rules(job_name)
            if rule_config:
                result = rule_engine.apply_rules(structured, rule_config)
                result["source"] = "rule_engine"
                return result
        except Exception as e:
            logger.warning(f"声明式规则评估失败，回退到旧逻辑: {e}")

    # 旧逻辑回退（保持兼容）
    hard_fails = []

    max_age = job_config.get("max_age", 0) or 0
    if max_age > 0:
        age = structured.get("age")
        if isinstance(age, int) and age > 0 and age > max_age:
            hard_fails.append(f"年龄{age}岁，超过≤{max_age}岁要求")

    min_edu = job_config.get("min_education", "全日制本科")
    if min_edu in ("全日制本科", "本科"):
        fd = structured.get("first_degree", "未明确") or "未明确"
        qualified = {"全日制本科", "全日制硕士", "全日制博士"}
        if fd not in ("未明确", "") and fd not in qualified:
            hard_fails.append(f"第一学历为「{fd}」，不符合全日制本科及以上要求")

    min_years = job_config.get("min_years", 0) or 0
    if min_years > 0:
        total = structured.get("total_work_years")
        if isinstance(total, int) and total > 0 and total < min_years:
            hard_fails.append(f"工作年限{total}年，不满足≥{min_years}年要求")

    return {
        "passed": len(hard_fails) == 0,
        "hard_fail_reasons": hard_fails,
        "rule_results": [],
        "source": "legacy",
    }


if __name__ == "__main__":
    print("预过滤模块已加载")
