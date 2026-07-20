"""wellnessagent/safety_rules.py 共享风险词模块单测（Phase 2.0 · P0-3）。

验证共享函数与 guardrails 私有实现口径一致：collect_risk_terms / expand_aliases /
rule_hits / ALIAS_MAP。同时确认 guardrails 的兼容代理仍指向共享实现。
"""

from dataclasses import dataclass
from typing import List, Optional

from wellnessagent import safety_rules
from wellnessagent.guardrails import DietGuardrails, _ALIAS_MAP


@dataclass
class FakeProfile:
    allergies: Optional[List[str]] = None
    dislikes: Optional[List[str]] = None
    medical_notes: Optional[List[str]] = None


class TestAliasMap:
    def test_shared_map_has_expected_keys(self):
        assert "花生" in safety_rules.ALIAS_MAP
        assert "牛奶" in safety_rules.ALIAS_MAP
        assert "海鲜" in safety_rules.ALIAS_MAP

    def test_guardrails_reexports_same_object(self):
        # guardrails._ALIAS_MAP 应与共享模块是同一对象（单一事实源）。
        assert _ALIAS_MAP is safety_rules.ALIAS_MAP


class TestCollectRiskTerms:
    def test_all_sources_merged(self):
        profile = FakeProfile(allergies=["花生"], dislikes=["鸡蛋"], medical_notes=["大豆"])
        terms = safety_rules.collect_risk_terms(profile)
        assert set(terms) == {"花生", "鸡蛋", "大豆"}

    def test_deduplication(self):
        profile = FakeProfile(allergies=["花生"], dislikes=["花生"])
        assert safety_rules.collect_risk_terms(profile).count("花生") == 1

    def test_string_value(self):
        profile = FakeProfile(allergies="牛奶")  # type: ignore[arg-type]
        assert "牛奶" in safety_rules.collect_risk_terms(profile)

    def test_none_profile(self):
        assert safety_rules.collect_risk_terms(None) == []

    def test_empty_values(self):
        profile = FakeProfile(allergies=[], dislikes=None, medical_notes=[])
        assert safety_rules.collect_risk_terms(profile) == []


class TestExpandAliases:
    def test_known_alias(self):
        aliases = safety_rules.expand_aliases("花生")
        assert "花生酱" in aliases and "花生油" in aliases

    def test_unknown_term_returns_self(self):
        assert "未知食物" in safety_rules.expand_aliases("未知食物")


class TestRuleHits:
    def test_direct_hit(self):
        assert safety_rules.rule_hits("可以吃花生", ["花生"]) == ["花生"]

    def test_alias_hit(self):
        assert safety_rules.rule_hits("推荐花生酱", ["花生"]) == ["花生"]

    def test_case_insensitive(self):
        assert safety_rules.rule_hits("Peanut butter", ["花生"]) == ["花生"]

    def test_no_hit(self):
        assert safety_rules.rule_hits("推荐牛奶", ["海鲜"]) == []

    def test_order_follows_risk_terms(self):
        hits = safety_rules.rule_hits("花生牛奶饮品", ["牛奶", "花生"])
        assert hits == ["牛奶", "花生"]


class TestGuardrailsDelegation:
    def test_collect_risk_terms_matches_shared(self):
        profile = FakeProfile(allergies=["花生"], dislikes=["鸡蛋"])
        assert DietGuardrails._collect_risk_terms(profile) == safety_rules.collect_risk_terms(profile)

    def test_expand_aliases_matches_shared(self):
        assert DietGuardrails._expand_aliases("牛奶") == safety_rules.expand_aliases("牛奶")

