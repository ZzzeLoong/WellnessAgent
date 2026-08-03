"""wellnessagent/vision.py 单元测试（三期 R9，方案 §2.6）。

覆盖四组口径：

1. **JSON 归一化**：模型输出千奇百怪（```json 包裹 / 字符串数字 / 单值热量 /
   字符串食物列表），全部要收敛为稳定的 :class:`FoodRecognition`。
2. **风险比对确定性**：命中与否由 ``safety_rules.rule_hits`` 判定，**不依赖模型自觉**；
   隐藏食材（``ingredients_guess``）必须参与比对——"花生酱"要能命中"花生"。
3. **失败降级**：模型不可用 / 输出不是 JSON / 图片解析不出来，一律返回 ``None``，
   绝不抛异常拖垮整轮对话。
4. **红线**：base64 只出现在发往模型的临时 payload 里，绝不进 trace 事件载荷。
"""

import json

import pytest

from core.media_store import MediaStore
from wellnessagent.schemas import FoodRecognition, WellnessProfile
from wellnessagent.vision import (
    MAX_IMAGES_PER_CALL,
    FoodRecognizer,
    _coerce_items,
    _coerce_macros,
    _coerce_range,
    _coerce_str_list,
    _parse_json,
)

from .test_media_store import _png_bytes  # 复用最小合法 PNG 构造器


# ------------------------------------------------------------------ 假 VLM
class FakeVisionLLM:
    """记录收到的 messages，按预设返回文本。"""

    def __init__(self, payload, model="fake-vl"):
        self._payload = payload
        self.model = model
        self.calls: list[list] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        if isinstance(self._payload, Exception):
            raise self._payload
        if isinstance(self._payload, (dict, list)):
            return json.dumps(self._payload, ensure_ascii=False)
        return self._payload


_GOOD_PAYLOAD = {
    "items": [
        {"name": "花生酱吐司", "confidence": 0.91, "portion": "2 片"},
        {"name": "拿铁", "confidence": 0.7, "portion": "1 杯"},
    ],
    "ingredients_guess": ["花生酱", "全麦面包", "牛奶"],
    "calories_range": [420, 560],
    "macros": {"protein_g": [14, 20], "carb_g": [45, 60], "fat_g": [18, 26]},
    "summary": "花生酱吐司配拿铁",
}


@pytest.fixture()
def store(tmp_path):
    return MediaStore(media_dir=str(tmp_path / "uploads"))


@pytest.fixture()
def saved(store):
    """落盘一张图，返回 (file_id, user_id)。"""
    ref = store.save("u1", "meal.png", _png_bytes())
    return ref.file_id, "u1"


def _recognizer(payload, store, **kwargs):
    return FoodRecognizer(FakeVisionLLM(payload), store, **kwargs)


# ================================================================== _parse_json
class TestParseJson:
    def test_plain_object(self):
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_fenced_json_block(self):
        assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fenced_without_language(self):
        assert _parse_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_surrounding_prose_is_tolerated(self):
        assert _parse_json('好的，结果如下：{"a": 1} 以上。') == {"a": 1}

    @pytest.mark.parametrize("raw", ["", "   ", "not json", "[1,2,3]", "{oops"])
    def test_unparseable_returns_none(self, raw):
        assert _parse_json(raw) is None


# ================================================================== 归一化
class TestCoercers:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ([300, 500], (300, 500)),
            (["300", "500"], (300, 500)),
            ([500, 300], (300, 500)),          # 顺序颠倒要纠正
            (420, (420, 420)),                  # 单值退化为区间
            ([420], (420, 420)),
            ({"min": 100, "max": 200}, (100, 200)),
            (None, None),
            ("很多", None),
            ([], None),
        ],
    )
    def test_coerce_range(self, value, expected):
        assert _coerce_range(value) == expected

    def test_coerce_items_from_dicts(self):
        items = _coerce_items([{"name": " 米饭 ", "confidence": "0.5", "portion": "1 碗"}])
        assert items == [{"name": "米饭", "confidence": 0.5, "portion": "1 碗"}]

    def test_coerce_items_from_strings(self):
        assert _coerce_items(["米饭", "  "]) == [
            {"name": "米饭", "confidence": None, "portion": ""}
        ]

    def test_coerce_items_drops_nameless(self):
        assert _coerce_items([{"confidence": 0.9}, 42, None]) == []

    def test_coerce_items_bad_confidence_becomes_none(self):
        assert _coerce_items([{"name": "x", "confidence": "高"}])[0]["confidence"] is None

    def test_coerce_str_list_splits_delimiters(self):
        assert _coerce_str_list("花生, 牛奶、鸡蛋") == ["花生", "牛奶", "鸡蛋"]

    def test_coerce_str_list_dedupes_preserving_order(self):
        assert _coerce_str_list(["a", "b", "a"]) == ["a", "b"]

    def test_coerce_macros_keeps_known_keys_only(self):
        macros = _coerce_macros(
            {"protein_g": [1, 2], "carb_g": None, "sodium_mg": [3, 4]}
        )
        assert macros == {"protein_g": [1, 2]}

    def test_coerce_macros_non_dict(self):
        assert _coerce_macros("x") == {}


# ================================================================== 识别主流程
class TestRecognizeHappyPath:
    def test_returns_structured_result(self, store, saved):
        file_id, user = saved
        rec = _recognizer(_GOOD_PAYLOAD, store).recognize([file_id], user_id=user)
        assert isinstance(rec, FoodRecognition)
        assert rec.item_names == ["花生酱吐司", "拿铁"]
        assert rec.calories_range == (420, 560)
        assert rec.macros["protein_g"] == [14, 20]
        assert rec.summary == "花生酱吐司配拿铁"
        assert rec.file_ids == [file_id]
        assert rec.model == "fake-vl"

    def test_sends_data_url_to_model(self, store, saved):
        file_id, user = saved
        llm = FakeVisionLLM(_GOOD_PAYLOAD)
        FoodRecognizer(llm, store).recognize([file_id], user_id=user)
        content = llm.calls[0][-1]["content"]
        image_parts = [p for p in content if p.get("type") == "image_url"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_prompt_carries_profile_summary(self, store, saved):
        file_id, user = saved
        llm = FakeVisionLLM(_GOOD_PAYLOAD)
        FoodRecognizer(llm, store).recognize(
            [file_id], user_id=user, profile_summary="allergies: 花生"
        )
        text = llm.calls[0][-1]["content"][0]["text"]
        assert "allergies: 花生" in text

    def test_summary_falls_back_to_item_names(self, store, saved):
        """摘要缺失时用食物名兜底——demote_media 与时间线都拿它当标题。"""
        file_id, user = saved
        payload = dict(_GOOD_PAYLOAD, summary="")
        rec = _recognizer(payload, store).recognize([file_id], user_id=user)
        assert rec.summary == "花生酱吐司、拿铁"

    def test_raw_is_truncated(self, store, saved):
        from wellnessagent.schemas import VISION_RAW_MAX

        file_id, user = saved
        long_tail = " " * (VISION_RAW_MAX + 500)
        rec = _recognizer(
            json.dumps(_GOOD_PAYLOAD, ensure_ascii=False) + long_tail, store
        ).recognize([file_id], user_id=user)
        assert len(rec.raw) <= VISION_RAW_MAX

    def test_dedupes_and_caps_images(self, store):
        refs = [store.save("u1", f"m{i}.png", _png_bytes(3, 5 + i)) for i in range(6)]
        ids = [r.file_id for r in refs]
        llm = FakeVisionLLM(_GOOD_PAYLOAD)
        rec = FoodRecognizer(llm, store).recognize(ids + ids, user_id="u1")
        assert len(rec.file_ids) == MAX_IMAGES_PER_CALL

    def test_custom_max_images(self, store):
        refs = [store.save("u1", f"m{i}.png", _png_bytes(3, 5 + i)) for i in range(3)]
        rec = _recognizer(_GOOD_PAYLOAD, store, max_images=1).recognize(
            [r.file_id for r in refs], user_id="u1"
        )
        assert len(rec.file_ids) == 1


# ================================================================== 风险比对
class TestRiskDetection:
    def test_hidden_ingredient_hits_allergy(self, store, saved):
        """核心口径：'花生酱' 出现在 ingredients_guess 就要命中 '花生'。"""
        file_id, user = saved
        profile = WellnessProfile(allergies=["花生"])
        rec = _recognizer(_GOOD_PAYLOAD, store).recognize(
            [file_id], profile=profile, user_id=user
        )
        assert rec.risk_hits == ["花生"]
        assert rec.has_risk is True

    def test_alias_map_is_shared_with_guardrails(self, store, saved):
        """牛奶别名（乳制品/奶酪…）与 DietGuardrails 共用一张表。"""
        file_id, user = saved
        payload = dict(_GOOD_PAYLOAD, ingredients_guess=["奶酪"], items=[])
        rec = _recognizer(payload, store).recognize(
            [file_id], profile=WellnessProfile(allergies=["牛奶"]), user_id=user
        )
        assert rec.risk_hits == ["牛奶"]

    def test_item_name_alone_can_hit(self, store, saved):
        file_id, user = saved
        payload = dict(
            _GOOD_PAYLOAD, ingredients_guess=[], items=[{"name": "白灼虾"}]
        )
        rec = _recognizer(payload, store).recognize(
            [file_id], profile=WellnessProfile(allergies=["虾"]), user_id=user
        )
        assert rec.risk_hits == ["虾"]

    def test_dislikes_and_medical_notes_participate(self, store, saved):
        file_id, user = saved
        profile = WellnessProfile(dislikes=["拿铁"], medical_notes=["全麦面包"])
        rec = _recognizer(_GOOD_PAYLOAD, store).recognize(
            [file_id], profile=profile, user_id=user
        )
        assert set(rec.risk_hits) == {"拿铁", "全麦面包"}

    def test_no_profile_means_no_hits(self, store, saved):
        file_id, user = saved
        rec = _recognizer(_GOOD_PAYLOAD, store).recognize([file_id], user_id=user)
        assert rec.risk_hits == []
        assert rec.has_risk is False

    def test_empty_profile_means_no_hits(self, store, saved):
        file_id, user = saved
        rec = _recognizer(_GOOD_PAYLOAD, store).recognize(
            [file_id], profile=WellnessProfile(), user_id=user
        )
        assert rec.risk_hits == []

    def test_unrelated_allergy_not_hit(self, store, saved):
        file_id, user = saved
        rec = _recognizer(_GOOD_PAYLOAD, store).recognize(
            [file_id], profile=WellnessProfile(allergies=["麸质皮"]), user_id=user
        )
        assert rec.risk_hits == []

    def test_detect_risks_is_pure(self):
        """静态方法可独立复算——评测/时间线要能对落盘结果重新判定。"""
        rec = FoodRecognition(
            items=[{"name": "腰果鸡丁"}], ingredients_guess=["腰果"], summary="s"
        )
        hits = FoodRecognizer.detect_risks(rec, WellnessProfile(allergies=["坚果"]))
        assert hits == ["坚果"]

    def test_detect_risks_tolerates_broken_profile(self):
        class Boom:
            @property
            def allergies(self):
                raise RuntimeError("boom")

        rec = FoodRecognition(summary="s")
        assert FoodRecognizer.detect_risks(rec, Boom()) == []


# ================================================================== 失败降级
class TestDegradation:
    def test_no_file_ids(self, store):
        assert _recognizer(_GOOD_PAYLOAD, store).recognize([]) is None

    def test_no_vision_llm(self, store):
        assert FoodRecognizer(None, store).recognize(["x"]) is None

    def test_no_media_store(self):
        assert FoodRecognizer(FakeVisionLLM(_GOOD_PAYLOAD), None).recognize(["x"]) is None

    def test_unresolvable_file_id(self, store):
        assert _recognizer(_GOOD_PAYLOAD, store).recognize(["missing"], user_id="u1") is None

    def test_wrong_user_cannot_read(self, store, saved):
        file_id, _ = saved
        assert _recognizer(_GOOD_PAYLOAD, store).recognize([file_id], user_id="other") is None

    def test_model_raises_returns_none(self, store, saved):
        file_id, user = saved
        assert _recognizer(RuntimeError("down"), store).recognize(
            [file_id], user_id=user
        ) is None

    def test_non_json_output_returns_none(self, store, saved):
        file_id, user = saved
        assert _recognizer("我看不清这张图", store).recognize(
            [file_id], user_id=user
        ) is None

    def test_empty_payload_still_builds(self, store, saved):
        """合法但空的 JSON 不算失败——只是没识别出东西。"""
        file_id, user = saved
        rec = _recognizer({}, store).recognize([file_id], user_id=user)
        assert rec is not None
        assert rec.items == [] and rec.summary == "" and rec.calories_range is None

    def test_resolver_exception_is_swallowed(self, store, saved):
        file_id, user = saved

        class BoomStore:
            def resolve_data_url(self, *_a, **_k):
                raise RuntimeError("io")

        assert _recognizer(_GOOD_PAYLOAD, BoomStore()).recognize(
            [file_id], user_id=user
        ) is None


# ================================================================== 观测事件
class TestObservability:
    def _events(self, payload, store, file_id, user, **kwargs):
        seen: list[tuple[str, dict]] = []
        rec = _recognizer(payload, store).recognize(
            [file_id], user_id=user, on_event=lambda e, p: seen.append((e, p)), **kwargs
        )
        return rec, seen

    def test_start_and_result_events(self, store, saved):
        file_id, user = saved
        _rec, seen = self._events(_GOOD_PAYLOAD, store, file_id, user)
        names = [e for e, _ in seen]
        assert names == ["vision_start", "vision_result"]

    def test_start_payload_has_file_ids_and_model(self, store, saved):
        file_id, user = saved
        _rec, seen = self._events(_GOOD_PAYLOAD, store, file_id, user)
        payload = dict(seen)["vision_start"]
        assert payload == {"file_ids": [file_id], "model": "fake-vl"}

    def test_result_payload_shape(self, store, saved):
        file_id, user = saved
        _rec, seen = self._events(
            _GOOD_PAYLOAD, store, file_id, user, profile=WellnessProfile(allergies=["花生"])
        )
        payload = dict(seen)["vision_result"]
        assert payload["items_count"] == 2
        assert payload["risk_hits"] == ["花生"]
        assert payload["summary_preview"] == "花生酱吐司配拿铁"
        assert isinstance(payload["latency_ms"], int)

    def test_error_event_on_bad_json(self, store, saved):
        file_id, user = saved
        _rec, seen = self._events("nope", store, file_id, user)
        assert [e for e, _ in seen] == ["vision_start", "vision_error"]

    def test_error_event_when_nothing_resolves(self, store):
        seen: list[tuple[str, dict]] = []
        _recognizer(_GOOD_PAYLOAD, store).recognize(
            ["missing"], user_id="u1", on_event=lambda e, p: seen.append((e, p))
        )
        assert [e for e, _ in seen] == ["vision_error"]

    def test_base64_never_appears_in_events(self, store, saved):
        """红线：trace 载荷里绝不能出现 base64（脱敏正则会被撑爆）。"""
        file_id, user = saved
        _rec, seen = self._events(_GOOD_PAYLOAD, store, file_id, user)
        assert "base64" not in json.dumps(seen, ensure_ascii=False)

    def test_broken_callback_does_not_break_recognition(self, store, saved):
        file_id, user = saved

        def boom(_e, _p):
            raise RuntimeError("observer down")

        rec = _recognizer(_GOOD_PAYLOAD, store).recognize(
            [file_id], user_id=user, on_event=boom
        )
        assert rec is not None


# ================================================================== 序列化
class TestSerialization:
    def test_round_trip(self, store, saved):
        file_id, user = saved
        rec = _recognizer(_GOOD_PAYLOAD, store).recognize(
            [file_id], profile=WellnessProfile(allergies=["花生"]), user_id=user
        )
        restored = FoodRecognition.from_dict(rec.to_dict())
        assert restored.calories_range == rec.calories_range
        assert restored.risk_hits == rec.risk_hits
        assert restored.item_names == rec.item_names

    def test_to_dict_is_json_serializable(self, store, saved):
        file_id, user = saved
        rec = _recognizer(_GOOD_PAYLOAD, store).recognize([file_id], user_id=user)
        assert json.loads(json.dumps(rec.to_dict(), ensure_ascii=False))

    def test_to_dict_excludes_raw(self):
        """``raw`` 不进对外 JSON：它是存档用的，可能很长。"""
        assert "raw" not in FoodRecognition(raw="x" * 100).to_dict()

    def test_calories_range_becomes_list(self):
        assert FoodRecognition(calories_range=(1, 2)).to_dict()["calories_range"] == [1, 2]

    def test_from_dict_handles_garbage(self):
        rec = FoodRecognition.from_dict({"calories_range": ["a", "b"], "items": None})
        assert rec.calories_range is None and rec.items == []

    def test_from_dict_of_none(self):
        assert FoodRecognition.from_dict(None).summary == ""

    def test_summary_preview_truncates(self):
        rec = FoodRecognition(summary="很长" * 200)
        assert len(rec.summary_preview(limit=10)) == 10

