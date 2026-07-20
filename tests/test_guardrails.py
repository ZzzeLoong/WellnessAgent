"""wellnessagent/guardrails.py 单元测试。"""

import json
from dataclasses import dataclass
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from wellnessagent.guardrails import DietGuardrails, GuardrailResult, _ALIAS_MAP


# ------------------------------------------------------------------ fakes
@dataclass
class FakeProfile:
    """模拟用户画像。"""
    allergies: Optional[List[str]] = None
    dislikes: Optional[List[str]] = None
    medical_notes: Optional[List[str]] = None


# ------------------------------------------------------------------ 规则层
class TestDietGuardrailsRuleLayer:
    def test_pass_when_no_risk_terms(self):
        g = DietGuardrails(mode="rule")
        profile = FakeProfile()
        result = g.check("推荐牛奶补充蛋白质", profile)
        assert result.action == "pass"

    def test_pass_when_no_hits(self):
        g = DietGuardrails(mode="rule")
        profile = FakeProfile(allergies=["海鲜"])
        result = g.check("推荐一杯牛奶补充蛋白质", profile)
        assert result.action == "pass"

    def test_hit_allergy_direct_match(self):
        g = DietGuardrails(mode="rule")
        profile = FakeProfile(allergies=["花生"])
        result = g.check("可以吃花生补充营养", profile)
        assert result.action == "rewrite"
        assert "花生" in result.hits

    def test_hit_alias_expansion(self):
        """别名扩展：用户过敏'花生'，回答中含'花生酱'也命中。"""
        g = DietGuardrails(mode="rule")
        profile = FakeProfile(allergies=["花生"])
        result = g.check("推荐花生酱搭配面包", profile)
        assert result.action == "rewrite"
        assert "花生" in result.hits

    def test_hit_case_insensitive(self):
        g = DietGuardrails(mode="rule")
        profile = FakeProfile(allergies=["花生"])
        result = g.check("Peanut butter is great", profile)
        assert result.action == "rewrite"

    def test_hit_dislikes(self):
        g = DietGuardrails(mode="rule")
        profile = FakeProfile(dislikes=["鸡蛋"])
        result = g.check("早餐吃鸡蛋很好", profile)
        assert result.action == "rewrite"
        assert "鸡蛋" in result.hits

    def test_hit_medical_notes(self):
        g = DietGuardrails(mode="rule")
        profile = FakeProfile(medical_notes=["大豆"])
        result = g.check("豆制品有益健康", profile)
        assert result.action == "rewrite"

    def test_multiple_risk_terms(self):
        g = DietGuardrails(mode="rule")
        profile = FakeProfile(allergies=["花生", "牛奶"])
        result = g.check("推荐花生牛奶饮品", profile)
        assert result.action == "rewrite"
        assert "花生" in result.hits
        assert "牛奶" in result.hits

    def test_rewrite_includes_warning(self):
        g = DietGuardrails(mode="rule")
        profile = FakeProfile(allergies=["花生"])
        result = g.check("吃花生补充蛋白质", profile)
        assert result.action == "rewrite"
        assert "安全提示" in result.safe_text
        assert "花生" in result.safe_text

    def test_empty_answer_passes(self):
        g = DietGuardrails(mode="rule")
        profile = FakeProfile(allergies=["花生"])
        result = g.check("", profile)
        assert result.action == "pass"

    def test_off_mode_always_passes(self):
        g = DietGuardrails(mode="off")
        profile = FakeProfile(allergies=["花生"])
        result = g.check("吃花生酱", profile)
        assert result.action == "pass"

    def test_none_profile_passes(self):
        g = DietGuardrails(mode="rule")
        result = g.check("推荐牛奶", None)
        assert result.action == "pass"


# ------------------------------------------------------------------ 别名扩展
class TestAliasExpansion:
    def test_expand_known_alias(self):
        aliases = DietGuardrails._expand_aliases("花生")
        assert "花生酱" in aliases
        assert "花生油" in aliases

    def test_expand_unknown_term(self):
        aliases = DietGuardrails._expand_aliases("未知食物")
        assert "未知食物" in aliases

    def test_alias_map_has_expected_keys(self):
        assert "花生" in _ALIAS_MAP
        assert "牛奶" in _ALIAS_MAP
        assert "海鲜" in _ALIAS_MAP


# ------------------------------------------------------------------ _collect_risk_terms
class TestCollectRiskTerms:
    def test_all_sources_merged(self):
        profile = FakeProfile(
            allergies=["花生"],
            dislikes=["鸡蛋"],
            medical_notes=["大豆"],
        )
        terms = DietGuardrails._collect_risk_terms(profile)
        assert "花生" in terms
        assert "鸡蛋" in terms
        assert "大豆" in terms

    def test_deduplication(self):
        profile = FakeProfile(allergies=["花生"], dislikes=["花生"])
        terms = DietGuardrails._collect_risk_terms(profile)
        assert terms.count("花生") == 1

    def test_string_value(self):
        """allergies 为字符串而非列表。"""
        profile = FakeProfile(allergies="牛奶")  # type: ignore
        # allergies 是 str 不是 list，走 str 分支
        terms = DietGuardrails._collect_risk_terms(profile)
        assert "牛奶" in terms

    def test_empty_values(self):
        profile = FakeProfile(allergies=[], dislikes=None, medical_notes=[])
        terms = DietGuardrails._collect_risk_terms(profile)
        assert terms == []


# ------------------------------------------------------------------ LLM 层
class TestDietGuardrailsLLMLayer:
    def test_rule_llm_no_llm_falls_back_to_rule(self):
        g = DietGuardrails(llm=None, mode="rule_llm")
        profile = FakeProfile(allergies=["花生"])
        result = g.check("吃花生", profile)
        # LLM 不可用，降级为规则层
        assert result.action == "rewrite"
        assert "花生" in result.hits

    def test_rule_llm_with_llm_pass(self):
        """LLM 判定无风险，规则层也无命中 → pass。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = json.dumps({"risk": False, "hits": [], "reason": "safe"})
        g = DietGuardrails(llm=mock_llm, mode="rule_llm")
        profile = FakeProfile(allergies=["花生"])
        result = g.check("推荐一杯豆浆", profile)
        assert result.action == "pass"

    def test_rule_llm_with_llm_risk(self):
        """LLM 判定有风险 → rewrite。"""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = json.dumps(
            {"risk": True, "hits": ["花生制品"], "reason": "隐性过敏原"}
        )
        g = DietGuardrails(llm=mock_llm, mode="rule_llm")
        profile = FakeProfile(allergies=["花生"])
        result = g.check("这个酱料很安全", profile)
        assert result.action == "rewrite"

    def test_rule_llm_exception_falls_back(self):
        """LLM 异常 → 降级为规则层。"""
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = Exception("LLM error")
        g = DietGuardrails(llm=mock_llm, mode="rule_llm")
        profile = FakeProfile(allergies=["花生"])
        result = g.check("吃花生酱", profile)
        # 规则层命中
        assert result.action == "rewrite"


# ------------------------------------------------------------------ _parse_json
class TestParseJson:
    def test_valid_json(self):
        data = DietGuardrails._parse_json('{"risk": true, "hits": ["a"]}')
        assert data == {"risk": True, "hits": ["a"]}

    def test_json_with_markdown_fences(self):
        raw = '```json\n{"risk": false}\n```'
        data = DietGuardrails._parse_json(raw)
        assert data == {"risk": False}

    def test_json_with_surrounding_text(self):
        raw = 'Here is the result: {"risk": true, "hits": ["x"]} done'
        data = DietGuardrails._parse_json(raw)
        assert data is not None
        assert data["risk"] is True

    def test_invalid_json(self):
        data = DietGuardrails._parse_json("not json at all")
        assert data is None

    def test_empty_string(self):
        data = DietGuardrails._parse_json("")
        assert data is None

    def test_none_input(self):
        data = DietGuardrails._parse_json(None)
        assert data is None

    def test_non_dict_json(self):
        data = DietGuardrails._parse_json('[1, 2, 3]')
        assert data is None

