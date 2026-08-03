"""WellnessPlanningAgent 多模态回合端到端单测（三期 R9，方案 §2.5/§2.6）。

沿用 ``test_agent_orchestration_routing.py`` 的 ``__new__`` + 桩装配范式，
不触碰真实 memory/RAG/LLM 后端，聚焦验证**接缝是否焊上**：

``/api/chat`` 的 ``attachments``
  → ``chat_with_trace(attachments=...)``
  → ``_prepare_vision_turn``（识餐 + 感知记忆 + system 注入 + 编排上下文）
  → ``run_with_trace(user_message=<list content>)``
  → 响应 ``vision`` 字段

**最重要的一组断言是"不带附件时与二期字节级等价"**：``user_message=None``、
system 消息不含任何 vision 段、响应里没有 ``vision`` 键、编排上下文的
``vision_risk_hits`` 为空。这是 R9 不破坏一/二期资产的硬保证。
"""

import json

import pytest

from WellnessAgent.agents.react_agent import ReActRunResult, TERMINATED_FINISHED
from WellnessAgent.core.media_store import MediaStore
from WellnessAgent.core.message import Message
from WellnessAgent.wellnessagent.agent import WellnessPlanningAgent
from WellnessAgent.wellnessagent.orchestrator import (
    OrchestrationResult,
    ROUTE_COMPOSITE,
    ROUTE_SIMPLE,
)
from WellnessAgent.wellnessagent.schemas import FoodRecognition, WellnessProfile

from .test_media_store import _png_bytes


# ------------------------------------------------------------------ 桩
class _FakeOrchestrator:
    def __init__(self, result):
        self._result = result
        self.trace_logger = None
        self.contexts: list = []

    def handle(self, message, ctx, resume=None):
        self.contexts.append(ctx)
        return self._result


class _RecordingService:
    """只记录被写入的感知记忆与画像读取。"""

    def __init__(self, profile=None):
        self.profile = profile
        self.perceptual: list[tuple[str, dict, float]] = []

    def get_current_profile(self):
        return self.profile

    def get_current_profile_summary(self):
        return self.profile.to_summary_text() if self.profile else ""

    def get_distilled_memory_summary(self, limit=4):
        return "（无）"

    def get_working_context_summary(self, limit=6, max_chars=900):
        return "（无）"

    def perceptual_note(self, content, metadata=None, importance=0.6):
        self.perceptual.append((content, metadata or {}, importance))
        return "ok"


class _StubRecognizer:
    """假 FoodRecognizer：返回预设结果并记录调用。"""

    def __init__(self, recognition):
        self._recognition = recognition
        self.calls: list[dict] = []

    def recognize(self, file_ids, profile=None, user_id=None, profile_summary="", on_event=None):
        self.calls.append(
            {
                "file_ids": list(file_ids),
                "profile": profile,
                "user_id": user_id,
                "profile_summary": profile_summary,
            }
        )
        if self._recognition is None:
            if on_event:
                on_event("vision_error", {"message": "stub failure"})
            return None
        if on_event:
            on_event("vision_start", {"file_ids": list(file_ids), "model": "stub-vl"})
            on_event(
                "vision_result",
                {
                    "items_count": len(self._recognition.items),
                    "risk_hits": self._recognition.risk_hits,
                    "latency_ms": 1,
                    "summary_preview": self._recognition.summary_preview(),
                },
            )
        self._recognition.file_ids = list(file_ids)
        return self._recognition


class _TraceRecorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def log_event(self, event, payload=None, step=None):
        self.events.append((event, payload or {}))

    def finalize(self):
        pass


def _recognition(risk_hits=None, summary="花生酱吐司配拿铁"):
    return FoodRecognition(
        items=[{"name": "花生酱吐司", "confidence": 0.9, "portion": "2 片"}],
        ingredients_guess=["花生酱"],
        calories_range=(420, 560),
        macros={"protein_g": [14, 20]},
        summary=summary,
        risk_hits=list(risk_hits or []),
        model="stub-vl",
    )


def _bare_agent(tmp_path, profile=None, recognition=..., orchestration_enabled=False):
    """装配一个不触发重依赖的 agent，media_store 指向 tmp_path。"""
    agent = WellnessPlanningAgent.__new__(WellnessPlanningAgent)
    agent.name = "test"
    agent.user_id = "u1"
    agent.messages = []
    agent.session_id = "s-test"
    agent.tool_groups = frozenset({"memory"})
    agent._orchestration_enabled = orchestration_enabled
    agent._hitl_enabled = True
    agent._pending_confirmation = None
    agent.trace_logger = None
    agent.guardrails = None
    agent.get_guardrail_profile = lambda: profile
    agent.system_prompt = "系统提示"
    agent.fallback_prompt_suffix = ""
    agent.build_system_prompt = agent._build_system_prompt

    agent._vision_llm = None
    agent._last_vision = None
    agent._turn_user_message = None
    agent._turn_vision_file_ids = []
    agent.media_store = MediaStore(media_dir=str(tmp_path / "uploads"))
    agent.media_resolver = agent._resolve_media
    agent.service = _RecordingService(profile)

    # 识餐器：默认给一个无风险结果；传 None 表示识别失败。
    rec = _recognition() if recognition is ... else recognition
    agent._food_recognizer = _StubRecognizer(rec)

    class _LLM:
        model = "stub"

        def supports_function_calling(self):
            return True

    agent.llm = _LLM()

    agent._ensure_session = lambda: None
    agent._handle_post_turn_memory = lambda: None
    agent._persist_session = lambda pending=None: None
    agent.get_state_dict = lambda: {"stub": True}

    class _HM:
        def append(self, m):
            pass

        def set_history(self, m):
            pass

    agent.history_manager = _HM()
    agent.orchestrator = _FakeOrchestrator(
        OrchestrationResult(route=ROUTE_SIMPLE, delegate_to_monolith=True)
    )
    return agent


def _save_image(agent, name="meal.png", height=5):
    return agent.media_store.save(agent.user_id, name, _png_bytes(3, height)).file_id


def _stub_run(agent, answer="答案"):
    """把 run_with_trace 换成记录桩，返回记录字典。"""
    captured: dict = {}

    def fake(user_input, **kwargs):
        captured["user_input"] = user_input
        captured["kwargs"] = kwargs
        return ReActRunResult(
            final_answer=answer, steps=[], terminated_reason=TERMINATED_FINISHED
        )

    agent.run_with_trace = fake
    return captured


@pytest.fixture(autouse=True)
def _vision_on(monkeypatch):
    monkeypatch.setenv("WELLNESS_VLM_ENABLED", "true")


# ================================================================== 无附件等价性
class TestNoAttachmentEquivalence:
    """不带附件时必须与二期字节级等价——这是 R9 最重要的回归红线。"""

    def test_user_message_stays_none(self, tmp_path):
        agent = _bare_agent(tmp_path)
        captured = _stub_run(agent)
        agent.chat_with_trace("你好")
        assert captured["kwargs"]["user_message"] is None

    def test_response_has_no_vision_key(self, tmp_path):
        agent = _bare_agent(tmp_path)
        _stub_run(agent)
        assert "vision" not in agent.chat_with_trace("你好")

    def test_recognizer_not_invoked(self, tmp_path):
        agent = _bare_agent(tmp_path)
        _stub_run(agent)
        agent.chat_with_trace("你好")
        assert agent._food_recognizer.calls == []

    def test_system_prompt_has_no_vision_section(self, tmp_path):
        agent = _bare_agent(tmp_path)
        _stub_run(agent)
        agent.chat_with_trace("你好")
        assert agent._build_vision_prompt_section() == ""
        assert "图片" not in agent._build_system_prompt("你好")

    def test_orchestration_context_vision_hits_empty(self, tmp_path):
        agent = _bare_agent(tmp_path, orchestration_enabled=True)
        _stub_run(agent)
        agent.chat_with_trace("你好")
        ctx = agent.orchestrator.contexts[0]
        assert ctx.vision_risk_hits == [] and ctx.vision_summary == ""

    def test_no_perceptual_memory_written(self, tmp_path):
        agent = _bare_agent(tmp_path)
        _stub_run(agent)
        agent.chat_with_trace("你好")
        assert agent.service.perceptual == []

    def test_vision_disabled_ignores_attachments(self, tmp_path, monkeypatch):
        """总开关关闭时，即便前端硬塞 attachments 也完全按二期走。"""
        monkeypatch.setenv("WELLNESS_VLM_ENABLED", "false")
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        resp = agent.chat_with_trace("这盘多少卡", attachments=[{"file_id": file_id}])
        assert captured["kwargs"]["user_message"] is None
        assert "vision" not in resp
        assert agent._food_recognizer.calls == []


# ================================================================== 带附件链路
class TestAttachmentWiring:
    def test_user_message_carries_media_ref(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        agent.chat_with_trace("这盘多少卡", attachments=[{"file_id": file_id}])
        msg = captured["kwargs"]["user_message"]
        assert isinstance(msg, Message)
        assert msg.has_media is True
        assert msg.media_refs() == [file_id]

    def test_text_part_precedes_image_part(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        agent.chat_with_trace("这盘多少卡", attachments=[{"file_id": file_id}])
        content = captured["kwargs"]["user_message"].content
        assert content[0] == {"type": "text", "text": "这盘多少卡"}
        assert content[1]["type"] == "image_url"

    def test_attachment_as_plain_string(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[file_id])
        assert captured["kwargs"]["user_message"].media_refs() == [file_id]

    def test_attachment_as_pydantic_like_object(self, tmp_path):
        class Att:
            def __init__(self, file_id):
                self.file_id = file_id
                self.kind = "image"

        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[Att(file_id)])
        assert captured["kwargs"]["user_message"].media_refs() == [file_id]

    def test_unknown_file_id_is_dropped(self, tmp_path):
        agent = _bare_agent(tmp_path)
        captured = _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": "does-not-exist"}])
        assert captured["kwargs"]["user_message"] is None

    def test_other_users_file_is_dropped(self, tmp_path):
        agent = _bare_agent(tmp_path)
        other = agent.media_store.save("someone-else", "x.png", _png_bytes()).file_id
        captured = _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": other}])
        assert captured["kwargs"]["user_message"] is None

    def test_non_image_kind_is_dropped(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        agent.chat_with_trace("听音频", attachments=[{"file_id": file_id, "kind": "audio"}])
        assert captured["kwargs"]["user_message"] is None

    def test_duplicate_file_ids_deduped(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        agent.chat_with_trace(
            "看图", attachments=[{"file_id": file_id}, {"file_id": file_id}]
        )
        assert captured["kwargs"]["user_message"].media_refs() == [file_id]

    def test_multiple_images_all_attached(self, tmp_path):
        agent = _bare_agent(tmp_path)
        ids = [_save_image(agent, f"m{i}.png", 5 + i) for i in range(3)]
        captured = _stub_run(agent)
        agent.chat_with_trace("看这几张", attachments=[{"file_id": i} for i in ids])
        assert captured["kwargs"]["user_message"].media_refs() == ids

    def test_stored_message_never_holds_base64(self, tmp_path):
        """红线：messages 落盘结构里只有短引用。"""
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        dumped = json.dumps(captured["kwargs"]["user_message"].to_dict(), ensure_ascii=False)
        assert "base64" not in dumped
        assert f"wellness-file://{file_id}" in dumped


# ================================================================== 响应 vision 字段
class TestVisionResponseField:
    def test_response_carries_vision(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        _stub_run(agent)
        resp = agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        assert resp["vision"]["summary"] == "花生酱吐司配拿铁"
        assert resp["vision"]["calories_range"] == [420, 560]
        assert resp["vision"]["file_ids"] == [file_id]

    def test_legacy_fields_all_present(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        _stub_run(agent)
        resp = agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        for key in ("answer", "terminated_reason", "steps", "state", "session_id", "trace_id"):
            assert key in resp

    def test_failed_recognition_omits_vision(self, tmp_path):
        agent = _bare_agent(tmp_path, recognition=None)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        resp = agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        assert "vision" not in resp
        # 但图片仍然进 messages —— 识餐失败不该剥夺模型看图的能力。
        assert captured["kwargs"]["user_message"].media_refs() == [file_id]

    def test_vision_reset_between_turns(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        _stub_run(agent)
        first = agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        second = agent.chat_with_trace("那晚饭吃什么")
        assert "vision" in first and "vision" not in second


# ================================================================== 安全联动
class TestSafetyLayers:
    def test_risk_hits_injected_as_warning_block(self, tmp_path):
        profile = WellnessProfile(allergies=["花生"])
        agent = _bare_agent(tmp_path, profile=profile, recognition=_recognition(["花生"]))
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("能吃吗", attachments=[{"file_id": file_id}])
        block = agent._build_vision_prompt_section()
        assert "图片安全警示" in block
        assert "花生" in block

    def test_no_risk_uses_context_block(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("这是什么", attachments=[{"file_id": file_id}])
        block = agent._build_vision_prompt_section()
        assert "本轮图片识别结果" in block
        assert "图片安全警示" not in block

    def test_warning_block_reaches_system_prompt(self, tmp_path):
        profile = WellnessProfile(allergies=["花生"])
        agent = _bare_agent(tmp_path, profile=profile, recognition=_recognition(["花生"]))
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("能吃吗", attachments=[{"file_id": file_id}])
        assert "图片安全警示" in agent._build_system_prompt("能吃吗")

    def test_risk_hits_reach_orchestration_context(self, tmp_path):
        profile = WellnessProfile(allergies=["花生"])
        agent = _bare_agent(
            tmp_path,
            profile=profile,
            recognition=_recognition(["花生"]),
            orchestration_enabled=True,
        )
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("能吃吗", attachments=[{"file_id": file_id}])
        ctx = agent.orchestrator.contexts[0]
        assert ctx.vision_risk_hits == ["花生"]
        assert ctx.vision_summary == "花生酱吐司配拿铁"

    def test_safety_task_mentions_vision_risk(self, tmp_path):
        profile = WellnessProfile(allergies=["花生"])
        agent = _bare_agent(
            tmp_path,
            profile=profile,
            recognition=_recognition(["花生"]),
            orchestration_enabled=True,
        )
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("能吃吗", attachments=[{"file_id": file_id}])
        task = agent.orchestrator.contexts[0].safety_task("计划")
        assert "图片识别风险" in task and "花生" in task

    def test_profile_passed_to_recognizer(self, tmp_path):
        profile = WellnessProfile(allergies=["花生"])
        agent = _bare_agent(tmp_path, profile=profile)
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        call = agent._food_recognizer.calls[0]
        assert call["profile"] is profile
        assert "花生" in call["profile_summary"]
        assert call["user_id"] == "u1"


# ================================================================== 感知记忆
class TestPerceptualMemoryWrite:
    def test_memory_written_with_summary(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        content, metadata, importance = agent.service.perceptual[0]
        assert content == "花生酱吐司配拿铁"
        assert metadata["file_id"] == file_id
        assert metadata["modality"] == "image"
        assert metadata["category"] == "food_photo"
        assert importance == pytest.approx(0.6)

    def test_metadata_carries_structured_recognition(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        _content, metadata, _imp = agent.service.perceptual[0]
        assert metadata["recognition"]["calories_range"] == [420, 560]

    def test_raw_data_points_to_real_file(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        _content, metadata, _imp = agent.service.perceptual[0]
        assert metadata["raw_data"].endswith(f"{file_id}.png")

    def test_risk_raises_importance(self, tmp_path):
        agent = _bare_agent(
            tmp_path,
            profile=WellnessProfile(allergies=["花生"]),
            recognition=_recognition(["花生"]),
        )
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("能吃吗", attachments=[{"file_id": file_id}])
        _content, metadata, importance = agent.service.perceptual[0]
        assert importance == pytest.approx(0.8)
        assert metadata["risk_hits"] == ["花生"]

    def test_one_entry_per_image(self, tmp_path):
        agent = _bare_agent(tmp_path)
        ids = [_save_image(agent, f"m{i}.png", 5 + i) for i in range(2)]
        _stub_run(agent)
        agent.chat_with_trace("看这两张", attachments=[{"file_id": i} for i in ids])
        assert [m["file_id"] for _c, m, _i in agent.service.perceptual] == ids

    def test_empty_summary_skips_write(self, tmp_path):
        agent = _bare_agent(tmp_path, recognition=_recognition(summary=""))
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        assert agent.service.perceptual == []

    def test_write_failure_does_not_break_turn(self, tmp_path):
        agent = _bare_agent(tmp_path)

        def boom(*_a, **_k):
            raise RuntimeError("db down")

        agent.service.perceptual_note = boom
        file_id = _save_image(agent)
        _stub_run(agent, answer="仍然有答案")
        resp = agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        assert resp["answer"] == "仍然有答案"


# ================================================================== 压缩占位语义
class TestDemotePlaceholderMetadata:
    def test_summary_attached_for_demotion(self, tmp_path):
        """识餐摘要挂到消息 metadata，供 demote_media 生成 "[图片：<摘要>]"。"""
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        meta = captured["kwargs"]["user_message"].metadata
        assert meta["vision"][file_id]["summary"] == "花生酱吐司配拿铁"

    def test_demote_media_uses_that_summary(self, tmp_path):
        from WellnessAgent.core.context import HistoryManager

        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])

        hm = HistoryManager(min_retain_rounds=0)
        hm.set_history([captured["kwargs"]["user_message"]])
        assert hm.demote_media() == 1
        assert "[图片：花生酱吐司配拿铁]" in hm.get_history()[0].text_content

    def test_failed_recognition_leaves_no_vision_meta(self, tmp_path):
        agent = _bare_agent(tmp_path, recognition=None)
        file_id = _save_image(agent)
        captured = _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        assert "vision" not in (captured["kwargs"]["user_message"].metadata or {})


# ================================================================== trace
class TestTraceEvents:
    def test_vision_events_logged(self, tmp_path):
        agent = _bare_agent(tmp_path)
        agent.trace_logger = _TraceRecorder()
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        names = [e for e, _ in agent.trace_logger.events]
        assert "vision_start" in names and "vision_result" in names

    def test_vision_error_logged_on_failure(self, tmp_path):
        agent = _bare_agent(tmp_path, recognition=None)
        agent.trace_logger = _TraceRecorder()
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        assert "vision_error" in [e for e, _ in agent.trace_logger.events]

    def test_events_are_in_declared_event_types(self, tmp_path):
        """新事件必须已登记进 EVENT_TYPES，否则 HTML 回放页会漏渲染。"""
        from WellnessAgent.observability.trace_logger import EVENT_TYPES

        assert {"vision_start", "vision_result", "vision_error"} <= EVENT_TYPES

    def test_no_base64_in_trace(self, tmp_path):
        agent = _bare_agent(tmp_path)
        agent.trace_logger = _TraceRecorder()
        file_id = _save_image(agent)
        _stub_run(agent)
        agent.chat_with_trace("看图", attachments=[{"file_id": file_id}])
        assert "base64" not in json.dumps(agent.trace_logger.events, ensure_ascii=False)


# ================================================================== composite / HITL
class TestCompositeAndSuspendKeepMedia:
    def test_composite_appends_media_message(self, tmp_path):
        agent = _bare_agent(tmp_path, orchestration_enabled=True)
        agent.orchestrator = _FakeOrchestrator(
            OrchestrationResult(route=ROUTE_COMPOSITE, answer="编排答案")
        )
        file_id = _save_image(agent)
        agent.chat_with_trace("给我一周计划", attachments=[{"file_id": file_id}])
        user_msgs = [m for m in agent.messages if m.role == "user"]
        assert user_msgs[0].media_refs() == [file_id]

    def test_composite_without_media_keeps_str_content(self, tmp_path):
        agent = _bare_agent(tmp_path, orchestration_enabled=True)
        agent.orchestrator = _FakeOrchestrator(
            OrchestrationResult(route=ROUTE_COMPOSITE, answer="编排答案")
        )
        agent.chat_with_trace("给我一周计划")
        user_msgs = [m for m in agent.messages if m.role == "user"]
        assert user_msgs[0].content == "给我一周计划"

    def test_suspend_keeps_media_message(self, tmp_path):
        from WellnessAgent.core.hitl import (
            KIND_SAFETY_RISK,
            PendingConfirmation,
            new_confirm_id,
        )

        agent = _bare_agent(tmp_path, orchestration_enabled=True)
        pending = PendingConfirmation(
            confirm_id=new_confirm_id(),
            kind=KIND_SAFETY_RISK,
            prompt="确认一下？",
            payload={},
        )
        agent.orchestrator = _FakeOrchestrator(
            OrchestrationResult(route=ROUTE_COMPOSITE, answer="", pending=pending)
        )
        file_id = _save_image(agent)
        resp = agent.chat_with_trace("能吃吗", attachments=[{"file_id": file_id}])
        assert resp["confirmation"]["confirm_id"] == pending.confirm_id
        user_msgs = [m for m in agent.messages if m.role == "user"]
        assert user_msgs[0].media_refs() == [file_id]


# ================================================================== 流式
class TestStreamVisionEvents:
    def _events(self, agent, message, attachments=None):
        agent.stream_run = lambda user_input, **kwargs: iter(
            [{"type": "result", "result": ReActRunResult(
                final_answer="流式答案", steps=[], terminated_reason=TERMINATED_FINISHED
            )}]
        )
        return list(agent.chat_stream(message, attachments=attachments))

    def test_vision_events_emitted(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        events = self._events(agent, "看图", [{"file_id": file_id}])
        types = [e.type.value for e in events]
        assert "vision_start" in types and "vision_result" in types
        assert types.index("vision_start") < types.index("vision_result")

    def test_vision_result_payload_is_full_structure(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        events = self._events(agent, "看图", [{"file_id": file_id}])
        payload = next(e.data for e in events if e.type.value == "vision_result")
        assert payload["calories_range"] == [420, 560]
        assert payload["items"][0]["name"] == "花生酱吐司"

    def test_no_vision_events_without_attachments(self, tmp_path):
        agent = _bare_agent(tmp_path)
        events = self._events(agent, "你好")
        types = [e.type.value for e in events]
        assert "vision_start" not in types and "vision_result" not in types

    def test_stream_event_types_declared(self):
        from WellnessAgent.wellnessagent.server.streaming import StreamEventType

        assert StreamEventType.VISION_START.value == "vision_start"
        assert StreamEventType.VISION_RESULT.value == "vision_result"

    def test_failed_recognition_emits_error(self, tmp_path):
        agent = _bare_agent(tmp_path, recognition=None)
        file_id = _save_image(agent)
        events = self._events(agent, "看图", [{"file_id": file_id}])
        assert "error" in [e.type.value for e in events]

    def test_stream_passes_user_message_to_kernel(self, tmp_path):
        agent = _bare_agent(tmp_path)
        file_id = _save_image(agent)
        seen: dict = {}

        def fake_stream_run(user_input, **kwargs):
            seen.update(kwargs)
            yield {"type": "result", "result": ReActRunResult(
                final_answer="x", steps=[], terminated_reason=TERMINATED_FINISHED
            )}

        agent.stream_run = fake_stream_run
        list(agent.chat_stream("看图", attachments=[{"file_id": file_id}]))
        assert seen["user_message"].media_refs() == [file_id]

    def test_recognizer_restored_after_stream(self, tmp_path):
        """流式实现临时替换了 _run_food_recognition，必须复原。"""
        agent = _bare_agent(tmp_path)
        original = agent._run_food_recognition
        file_id = _save_image(agent)
        self._events(agent, "看图", [{"file_id": file_id}])
        assert agent._run_food_recognition == original

