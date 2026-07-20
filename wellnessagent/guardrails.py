"""Guardrails 安全校验层（WellnessAgent 自研，R3）。

两层校验：
1. 规则层（必开）：从 ``profile.allergies + dislikes + medical_notes`` 抽风险词，
   归一化/别名/子串匹配（"花生" 命中 "花生酱/花生油"）。确定性红线不漏放。
2. LLM 层（``mode=rule_llm`` 才开，默认关）：轻量 LLM 判定语义级隐性风险。

默认档 ``rule``；``rule_llm`` 的 llm 复用 ``service._get_distill_llm()``（可空则降级
为纯规则层）。处置：``rewrite`` / ``block`` / ``pass``。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from . import safety_rules


@dataclass
class GuardrailResult:
    """安全校验结果。"""

    action: str  # "pass" | "rewrite" | "block"
    reason: str
    hits: List[str] = field(default_factory=list)
    safe_text: str = ""


# 向后兼容：别名表已上提至共享模块 ``safety_rules``（P0-3），这里保留同名引用，
# 不打破引用 ``guardrails._ALIAS_MAP`` 的一期代码/单测。
_ALIAS_MAP = safety_rules.ALIAS_MAP


class DietGuardrails:
    """健康饮食输出安全校验。"""

    def __init__(self, llm: Any = None, mode: Optional[str] = None):
        self.llm = llm
        self.mode = (mode or os.getenv("WELLNESS_GUARDRAILS_MODE", "rule")).strip().lower()

    # ------------------------------------------------------------------ public
    def check(self, answer: str, profile: Any) -> GuardrailResult:
        """对最终答案做安全校验。"""
        if self.mode == "off" or not answer:
            return GuardrailResult(action="pass", reason="guardrails off", safe_text=answer)

        risk_terms = self._collect_risk_terms(profile)
        if not risk_terms:
            # 无用户显式风险词：规则层无可匹配，可选走 LLM 层。
            if self.mode == "rule_llm" and self.llm is not None:
                return self._llm_check(answer, [], profile)
            return GuardrailResult(action="pass", reason="no declared risk terms", safe_text=answer)

        hits = self._rule_hits(answer, risk_terms)

        if self.mode == "rule_llm" and self.llm is not None:
            llm_result = self._llm_check(answer, hits, profile)
            # LLM 层与规则层取并集（规则层是硬红线）。
            if hits or llm_result.action != "pass":
                combined_hits = sorted(set(hits) | set(llm_result.hits))
                return self._handle_risk(answer, combined_hits, profile, llm_result.reason)
            return GuardrailResult(action="pass", reason="passed rule+llm", safe_text=answer)

        if hits:
            return self._handle_risk(answer, hits, profile, "命中用户声明的风险词")
        return GuardrailResult(action="pass", reason="passed rule layer", safe_text=answer)

    # ------------------------------------------------------------------ rule layer
    # 下列三个方法保留为对外兼容入口，内部委托给共享模块 ``safety_rules``（P0-3），
    # 保证 guardrails 硬红线与 safety SubAgent 主动审查共用同一份别名表与匹配口径。
    @staticmethod
    def _collect_risk_terms(profile: Any) -> List[str]:
        """从画像抽取风险词（过敏原 + 忌口 + 医疗备注）。"""
        return safety_rules.collect_risk_terms(profile)

    def _rule_hits(self, answer: str, risk_terms: List[str]) -> List[str]:
        """归一化 + 别名子串匹配，返回命中的风险词。"""
        return safety_rules.rule_hits(answer, risk_terms)

    @staticmethod
    def _expand_aliases(term: str) -> List[str]:
        """把一个风险词扩展为别名列表。"""
        return safety_rules.expand_aliases(term)

    # ------------------------------------------------------------------ handling
    def _handle_risk(
        self, answer: str, hits: List[str], profile: Any, reason: str
    ) -> GuardrailResult:
        """命中风险后的处置：优先 rewrite，无法安全改写则 block。"""
        warning = (
            "⚠️ 安全提示：以下内容可能涉及你声明过的过敏原/忌口（"
            f"{', '.join(hits)}）。请谨慎，必要时咨询医生或注册营养师。\n\n"
        )
        # 简单处置：前置安全警示（保守 rewrite）。若可用 LLM 可做更强改写，
        # 这里保证确定性：加警示且不删除已生成建议之外的信息。
        safe_text = warning + answer
        return GuardrailResult(
            action="rewrite",
            reason=reason,
            hits=hits,
            safe_text=safe_text,
        )

    # ------------------------------------------------------------------ llm layer
    def _llm_check(self, answer: str, rule_hits: List[str], profile: Any) -> GuardrailResult:
        """LLM 语义级风险判定。可空则降级为纯规则结果。"""
        if self.llm is None:
            if rule_hits:
                return self._handle_risk(answer, rule_hits, profile, "规则层命中（LLM 不可用）")
            return GuardrailResult(action="pass", reason="llm unavailable", safe_text=answer)

        risk_terms = self._collect_risk_terms(profile)
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是健康饮食安全审查员。给定用户的过敏原/忌口/医疗备注，判断助手回答是否"
                    "存在触碰这些风险的内容（含隐性表达）。只返回 JSON："
                    '{"risk": true/false, "hits": ["..."], "reason": "..."}，不要多余文本。'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户风险词：{', '.join(risk_terms) or '无'}\n\n助手回答：\n{answer}"
                ),
            },
        ]
        try:
            raw = self.llm.invoke(prompt)
            data = self._parse_json(raw)
        except Exception:
            data = None

        if not data:
            if rule_hits:
                return self._handle_risk(answer, rule_hits, profile, "规则层命中（LLM 解析失败）")
            return GuardrailResult(action="pass", reason="llm parse failed", safe_text=answer)

        llm_hits = [str(h) for h in data.get("hits", []) if str(h).strip()]
        if data.get("risk"):
            return GuardrailResult(
                action="rewrite",
                reason=str(data.get("reason", "LLM 判定存在风险")),
                hits=llm_hits,
                safe_text=answer,
            )
        return GuardrailResult(action="pass", reason="llm passed", hits=[], safe_text=answer)

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        if not raw:
            return None
        cleaned = raw.strip()
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        try:
            data = json.loads(cleaned[start : end + 1])
        except Exception:
            return None
        return data if isinstance(data, dict) else None

