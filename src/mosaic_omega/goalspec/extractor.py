"""规则版 GoalSpec 抽取器。

作用：无 API Key、无网络时的本地 fallback。
局限：主要靠规则和关键词，对隐含语义理解有限。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List


def split_sentences(text: str) -> List[str]:
    parts = re.split(r"[。；;，,\n]+", text)
    return [p.strip(" ，,：: \t") for p in parts if p.strip(" ，,：: \t")]


def infer_goal_type(text: str) -> str:
    if any(w in text for w in ["代码", "编译器", "程序", "接口", "JSON", "json", "实现", "脚本", "ROS", "ROS2", "Python", "C++"]):
        return "coding"
    if any(w in text for w in ["汇报", "报告", "说明", "文档"]):
        return "report_generation"
    if any(w in text for w in ["规划", "计划", "DAG", "拆任务", "任务图"]):
        return "planning"
    if any(w in text for w in ["火星车", "机器人", "小车", "Rover", "路径", "导航"]):
        return "robot_task"
    if any(w in text for w in ["分析", "评价", "判断"]):
        return "analysis"
    if any(w in text for w in ["调研", "搜索", "查找"]):
        return "research"
    return "other"


def infer_domain(text: str) -> str:
    if "火星车" in text or "Rover" in text or "rover" in text:
        return "火星车多智能体非合作博弈"
    if "ROS" in text or "机器人" in text or "小车" in text:
        return "机器人软件与多智能体系统"
    if "DAG" in text or "zdy" in text or "任务图" in text or "多智能体" in text:
        return "多智能体任务编排"
    if "GoalSpec" in text or "goalspec" in text:
        return "GoalSpec 目标规格编译"
    return "通用任务编排"


def infer_check_method(sentence: str) -> str:
    lower = sentence.lower()
    if "json" in lower or "schema" in lower or "字段" in sentence:
        return "schema_check"
    if any(w in sentence for w in ["文件", "输出", "生成", "保存"]):
        return "file_check"
    if any(w in sentence for w in ["测试", "单元测试", "运行"]):
        return "unit_test"
    if any(w in sentence for w in ["准确率", "命中率", "比例", "指标"]):
        return "metric_check"
    return "content_check"


def extract_main_goal(sentences: List[str], text: str) -> Dict[str, str]:
    # 优先取“任务：xxx”后的内容。
    m = re.search(r"任务[:：]\s*([^。；;\n]+)", text)
    if m:
        goal_text = m.group(1).strip()
    else:
        # 否则取第一句，去掉口语开头。
        goal_text = sentences[0] if sentences else text.strip()
        for prefix in ["帮我", "请", "你", "我想", "我需要", "麻烦", "把"]:
            if goal_text.startswith(prefix):
                goal_text = goal_text[len(prefix):].strip()
        goal_text = goal_text.strip(" ，,。")

    if len(goal_text) > 80:
        goal_text = goal_text[:80].rstrip("，,")

    return {
        "goal_text": goal_text or "未明确的用户任务",
        "goal_type": infer_goal_type(text),
        "domain": infer_domain(text),
    }


#: 显式验收枚举的引导词。用户写“验收条件：A；B；C”时，A/B/C 全部是验收条件，
#: 不应该再要求每一条自己带“必须/输出”之类的关键词。之前只有恰好命中关键词的
#: 那一条会被保留，其余直接丢失，导致 DAG 少了整段工作。
_ACCEPTANCE_LEAD = re.compile(
    r"^\s*(验收条件|验收标准|验收要求|交付要求|完成标准|acceptance\s+criteria|acceptance\s+conditions)\s*[:：]?\s*",
    re.IGNORECASE,
)


def split_acceptance_enumeration(sentences: List[str]) -> List[str]:
    """返回被显式验收枚举引导的所有条件文本。"""
    for index, sentence in enumerate(sentences):
        match = _ACCEPTANCE_LEAD.match(sentence)
        if not match:
            continue
        conditions: List[str] = []
        head = sentence[match.end():].strip(" ，,。：: \t")
        if head:
            conditions.append(head)
        # 引导词之后的每一句都属于该枚举。
        conditions.extend(item.strip(" ，,。：: \t") for item in sentences[index + 1:])
        return [item for item in conditions if item]
    return []


def extract(text: str) -> Dict[str, Any]:
    """从用户自然语言中抽取 GoalSpec 草稿。"""
    if not text or not text.strip():
        raise ValueError("user_text cannot be empty")

    sentences = split_sentences(text)
    hard_constraints = []
    soft_preferences = []
    prohibitions = []
    acceptance_conditions = []

    # 显式枚举优先：先把“验收条件：…”里的每一条都收下，再走关键词兜底。
    enumerated = split_acceptance_enumeration(sentences)
    seen_conditions: set[str] = set()
    for condition in enumerated:
        if condition in seen_conditions:
            continue
        seen_conditions.add(condition)
        acceptance_conditions.append({
            "condition": condition,
            "check_type": infer_check_method(condition),
            "expected_result": "条件被满足",
        })
        hard_constraints.append({
            "constraint": condition,
            "checkable": True,
            "check_method": infer_check_method(condition),
            "source_span": condition,
        })

    hard_keywords = [
        "必须", "需要", "要求", "一定", "只能", "确保", "包含", "输出", "DAG", "JSON", "json", "字段",
        # 常见的陈述式要求写法，之前会被整句丢弃。
        "完成", "实现", "通过", "生成", "交付", "达到", "支持", "修复", "构建",
    ]
    soft_keywords = ["尽量", "最好", "希望", "优先", "方便", "适合", "正式", "简洁", "清楚", "不要太", "一点"]
    prohibition_keywords = ["不要", "不能", "不得", "禁止", "别", "不允许", "不许"]

    for s in sentences:
        if not s:
            continue
        if s in seen_conditions or _ACCEPTANCE_LEAD.match(s):
            # 已经由显式枚举处理过，不要重复登记。
            continue
        is_prohibition = any(k in s for k in prohibition_keywords)
        is_hard = any(k in s for k in hard_keywords) or is_prohibition
        if is_hard:
            seen_conditions.add(s)
            hard_constraints.append({
                "constraint": s,
                "checkable": True,
                "check_method": infer_check_method(s),
                "source_span": s,
            })
            acceptance_conditions.append({
                "condition": s,
                "check_type": infer_check_method(s),
                "expected_result": "条件被满足",
            })
        if any(k in s for k in soft_keywords):
            priority = 4 if any(k in s for k in ["优先", "正式", "清楚", "方便"]) else 3
            soft_preferences.append({
                "preference": s,
                "priority": priority,
            })
            # 软偏好一般不作为强验收条件，但 MVP 中可保留为 content_check。
            acceptance_conditions.append({
                "condition": s,
                "check_type": "content_check",
                "expected_result": "尽量满足该偏好",
            })
        if is_prohibition:
            prohibitions.append({
                "rule": s,
                "reason": "用户明确禁止或否定该行为",
            })

    time_limit = None
    compute_limit = None
    cost_limit = None
    token_limit = None

    if any(k in text for k in ["今晚", "今天", "几个小时", "半小时", "一小时", "明天", "马上"]):
        if "今晚" in text:
            time_limit = "tonight"
        elif "半小时" in text:
            time_limit = "30 minutes"
        elif "一小时" in text:
            time_limit = "1 hour"
        elif "几个小时" in text:
            time_limit = "several hours"
        elif "明天" in text:
            time_limit = "by tomorrow"
        else:
            time_limit = "as soon as possible"

    if any(k.lower() in text.lower() for k in ["cpu only", "CPU only", "没有独显", "无独显", "不用服务器", "本地", "VSCode"]):
        compute_parts = []
        if "本地" in text or "VSCode" in text:
            compute_parts.append("local VSCode")
        if "没有独显" in text or "无独显" in text:
            compute_parts.append("no discrete GPU")
        if "CPU only" in text or "cpu only" in text.lower():
            compute_parts.append("CPU only")
        if "不用服务器" in text:
            compute_parts.append("no server")
        compute_limit = ", ".join(compute_parts) if compute_parts else "CPU only"

    if any(k in text for k in ["不花钱", "免费", "不要付费", "低成本"]):
        cost_limit = "low cost / no paid API preferred"

    return {
        "main_goal": extract_main_goal(sentences, text),
        "hard_constraints": hard_constraints,
        "soft_preferences": soft_preferences,
        "acceptance_conditions": acceptance_conditions,
        "budget": {
            "time_limit": time_limit,
            "token_limit": token_limit,
            "compute_limit": compute_limit,
            "cost_limit": cost_limit,
        },
        "prohibitions": prohibitions,
    }
