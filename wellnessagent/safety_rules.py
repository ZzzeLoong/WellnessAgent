"""安全风险词共享模块（Phase 2.0 · P0-3）。

把一期 ``guardrails.py`` 里私有的风险词收集/别名扩展/命中匹配逻辑抽为公开函数，
让 ``DietGuardrails``（最终输出前的硬红线兜底）与二期 ``safety`` SubAgent（规划阶段
的主动审查）**共用同一份**别名表与匹配口径，避免两道关口径漂移。

对外提供：
- ``ALIAS_MAP``：常见过敏原/风险词别名表（单一事实源）。
- ``collect_risk_terms(profile)``：从画像抽取风险词（过敏原 + 忌口 + 医疗备注）。
- ``expand_aliases(term)``：把一个风险词扩展为别名列表。
- ``rule_hits(text, risk_terms)``：归一化 + 别名子串匹配，返回命中的风险词。

纯函数、无状态、行为与一期私有实现一致（一期单测回归绿）。
"""

from __future__ import annotations

from typing import Any, List


# 常见过敏原/风险词别名，用于子串命中扩展（单一事实源，供 guardrails 与 safety SubAgent 共用）。
ALIAS_MAP = {
    "花生": ["花生", "花生酱", "花生油", "peanut"],
    "牛奶": ["牛奶", "奶油", "乳制品", "芝士", "奶酪", "milk", "cheese"],
    "鸡蛋": ["鸡蛋", "蛋清", "蛋黄", "蛋液", "egg"],
    "海鲜": ["海鲜", "虾", "蟹", "贝", "蛤", "生蚝", "shrimp", "crab"],
    "虾": ["虾", "虾仁", "虾米", "shrimp"],
    "麸质": ["麸质", "面筋", "小麦", "gluten", "wheat"],
    "大豆": ["大豆", "黄豆", "豆制品", "豆腐", "soy"],
    "坚果": ["坚果", "杏仁", "腰果", "核桃", "开心果", "nut"],
}


def collect_risk_terms(profile: Any) -> List[str]:
    """从画像抽取风险词（过敏原 + 忌口 + 医疗备注），去重后返回。"""
    if profile is None:
        return []
    terms: List[str] = []
    for attr in ("allergies", "dislikes", "medical_notes"):
        value = getattr(profile, attr, None)
        if isinstance(value, (list, tuple)):
            terms.extend(str(v).strip() for v in value if str(v).strip())
        elif isinstance(value, str) and value.strip():
            terms.append(value.strip())
    seen: List[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    return seen


def expand_aliases(term: str) -> List[str]:
    """把一个风险词扩展为别名列表（含自身）。"""
    base = term.strip()
    result = {base}
    for key, aliases in ALIAS_MAP.items():
        if base == key or base in aliases:
            result.update(aliases)
    return [a for a in result if a]


def rule_hits(text: str, risk_terms: List[str]) -> List[str]:
    """归一化 + 别名子串匹配，返回命中的风险词（顺序与 ``risk_terms`` 一致）。"""
    normalized = (text or "").lower()
    hits: List[str] = []
    for term in risk_terms:
        for candidate in expand_aliases(term):
            if candidate and candidate.lower() in normalized:
                if term not in hits:
                    hits.append(term)
                break
    return hits

