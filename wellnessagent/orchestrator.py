"""编排器 Orchestrator（R6 核心）。

一次复合对话的编排内核（方案 §1 / §2.4）：

    triage（simple | composite）
      simple    → 交回一期单体 ReActAgent 原路径（delegate_to_monolith）
      composite → 固定 DAG pipeline：
          {profile, retrieval} 并行 → planning → safety → aggregate

设计要点：
- **固定 DAG**（确定性、可评测）。
- **并行**用 ``ThreadPoolExecutor``（LLM 调用 IO 密集；每个 SubAgent 独立实例 + 独立
  registry allowed，service 只读，线程安全）。
- **聚合**用一次 LLM 把子结果综合为面向用户的答案；LLM 不可用时降级为拼接摘要。
- **失败隔离**：任一 SubAgent 异常/未完成 → 记 trace + 降级摘要占位，pipeline 继续。
- **分诊降级**：triage LLM 不可用/解析失败 → 默认 composite（宁多编排也不漏约束）。

本期不含 HITL/指标/前端（见后续里程碑），但 pipeline 已预留 planning→safety 之间的
确认关卡位置（当前直接跳过）。
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from ..core.hitl import (
    KIND_PROFILE_UPDATE,
    KIND_SAFETY_RISK,
    PendingConfirmation,
    new_confirm_id,
)
from .prompts import ORCHESTRATOR_AGGREGATE_PROMPT, ORCHESTRATOR_TRIAGE_PROMPT

ROUTE_SIMPLE = "simple"
ROUTE_COMPOSITE = "composite"

# 触发画像确认的敏感字段（方案 §3.3；可经环境变量覆盖）。
_DEFAULT_SENSITIVE_FIELDS = "allergies,medical_notes,diet_pattern"


@dataclass
class OrchestrationResult:
    """编排结果（返回给主 agent 消费）。"""

    route: str  # "simple" | "composite"
    delegate_to_monolith: bool = False  # simple 路径：交回一期单体
    answer: Optional[str] = None  # composite 聚合后的答案草稿
    subagent_results: List[dict] = field(default_factory=list)
    reason: str = ""
    # R7 HITL：非空则本回合挂起等待用户确认（answer 为本次要展示的确认提示）。
    pending: Optional[PendingConfirmation] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "route": self.route,
            "delegate_to_monolith": self.delegate_to_monolith,
            "answer": self.answer,
            "subagents": self.subagent_results,
            "reason": self.reason,
        }
        if self.pending is not None:
            data["pending"] = self.pending.public_view()
        return data


@dataclass
class OrchestrationContext:
    """一次编排所需的只读上下文与子任务构造器。

    由主 agent 装配（注入只读 ``service`` 与用户画像风险词），Orchestrator 用它为
    各 SubAgent 生成任务文本与注入上下文。
    """

    message: str
    risk_terms: List[str] = field(default_factory=list)

    # 各子任务的注入上下文（可选，由主 agent 预填）。
    profile_context: dict = field(default_factory=dict)
    retrieval_context: dict = field(default_factory=dict)

    def profile_task(self) -> str:
        return (
            "请整理当前用户画像与长期记忆，产出规划所需的结构化上下文与画像补全建议。"
            f"\n用户本次请求：{self.message}"
        )

    def retrieval_task(self) -> str:
        return f"请针对以下用户请求检索营养知识库要点并附引用：\n{self.message}"

    def planning_task(self, profile_summary: str, retrieval_summary: str) -> str:
        return (
            "请依据下列画像上下文与检索要点，遵守全部约束，产出结构化饮食/食谱计划。\n\n"
            f"【用户请求】\n{self.message}\n\n"
            f"【画像上下文】\n{profile_summary or '（无）'}\n\n"
            f"【检索要点】\n{retrieval_summary or '（无）'}"
        )

    def safety_task(self, planning_summary: str) -> str:
        return (
            "请对下列饮食计划做主动安全审查，逐项检查是否触碰用户风险词并给出修改建议。\n\n"
            f"【用户风险词】\n{', '.join(self.risk_terms) or '（无声明）'}\n\n"
            f"【待审查计划】\n{planning_summary or '（无）'}"
        )


class Orchestrator:
    """LLM 分诊 + 固定 DAG pipeline + 并行分派 + 聚合。"""

    def __init__(
        self,
        llm: Any,
        subagent_factory: Callable[..., Any],
        service: Any = None,
        trace_logger: Any = None,
        parallelism: Optional[int] = None,
        triage_mode: Optional[str] = None,
        hitl_enabled: Optional[bool] = None,
        sensitive_fields: Optional[set] = None,
    ):
        self.llm = llm
        self.subagent_factory = subagent_factory
        self.service = service
        self.trace_logger = trace_logger
        self.parallelism = parallelism or int(os.getenv("WELLNESS_SUBAGENT_PARALLELISM", "2"))
        # llm / rule / always_composite / always_simple
        self.triage_mode = (
            triage_mode or os.getenv("WELLNESS_TRIAGE_MODE", "llm")
        ).strip().lower()
        # R7 HITL 总开关与触发画像确认的敏感字段集合。
        if hitl_enabled is None:
            hitl_enabled = os.getenv("WELLNESS_HITL_ENABLED", "true").lower() == "true"
        self.hitl_enabled = hitl_enabled
        if sensitive_fields is None:
            raw = os.getenv("WELLNESS_HITL_SENSITIVE_FIELDS", _DEFAULT_SENSITIVE_FIELDS)
            sensitive_fields = {f.strip().lower() for f in raw.split(",") if f.strip()}
        self.sensitive_fields = sensitive_fields

    # ------------------------------------------------------------------ public
    def handle(
        self,
        message: str,
        ctx: OrchestrationContext,
        resume: Optional[dict] = None,
    ) -> OrchestrationResult:
        """编排入口：先分诊，再决定 simple/composite。

        Args:
            resume: R7 HITL 恢复上下文。非空表示本轮是对上一轮挂起项的确认恢复，
                pipeline 跳过挂起、把确认结果（已批准的画像更新 / 风险决策）并入聚合。
                形如 ``{"decision": "approve|reject|modify", "kind": ..., "applied": {...}}``。
        """
        route, reason = self._triage(message)
        self._log("orchestrator_triage", {"route": route, "reason": reason})
        if route == ROUTE_SIMPLE:
            return OrchestrationResult(
                route=ROUTE_SIMPLE, delegate_to_monolith=True, reason=reason
            )
        return self._run_pipeline(message, ctx, reason, resume=resume)

    # ------------------------------------------------------------------ triage
    def _triage(self, message: str) -> tuple[str, str]:
        """判定 simple/composite。失败/超时默认 composite（宁多编排也不漏约束）。"""
        if self.triage_mode == "always_simple":
            return ROUTE_SIMPLE, "forced simple"
        if self.triage_mode == "always_composite":
            return ROUTE_COMPOSITE, "forced composite"
        if self.triage_mode == "rule":
            return self._rule_triage(message)

        # llm 模式：单次轻量判定，失败降级 composite。
        if self.llm is None:
            return ROUTE_COMPOSITE, "triage llm unavailable → composite"
        prompt = [
            {"role": "system", "content": ORCHESTRATOR_TRIAGE_PROMPT},
            {"role": "user", "content": message},
        ]
        try:
            raw = self.llm.invoke(prompt)
            data = self._parse_json(raw)
        except Exception:
            data = None
        if not data or data.get("route") not in {ROUTE_SIMPLE, ROUTE_COMPOSITE}:
            return ROUTE_COMPOSITE, "triage parse failed → composite"
        return data["route"], str(data.get("reason", ""))

    @staticmethod
    def _rule_triage(message: str) -> tuple[str, str]:
        """规则分诊：命中复合特征关键词即 composite。"""
        text = message or ""
        composite_signals = [
            "一周", "七天", "7天", "7 天", "每天", "三餐", "备餐", "食谱",
            "计划", "预算", "减脂", "增肌", "过敏", "忌口",
        ]
        hits = [s for s in composite_signals if s in text]
        # 多约束（命中 >=2）或明显长计划则 composite。
        if len(hits) >= 2:
            return ROUTE_COMPOSITE, f"rule hits: {', '.join(hits)}"
        return ROUTE_SIMPLE, "rule: single-intent"

    # ------------------------------------------------------------------ pipeline
    def _run_pipeline(
        self,
        message: str,
        ctx: OrchestrationContext,
        reason: str,
        resume: Optional[dict] = None,
    ) -> OrchestrationResult:
        """固定 DAG：{profile, retrieval} 并行 → planning → [HITL 关卡] → safety → 聚合。"""
        # 1) 并行：profile + retrieval。
        parallel_results = self._run_parallel(
            [
                ("profile", ctx.profile_task(), dict(ctx.profile_context)),
                ("retrieval", ctx.retrieval_task(), dict(ctx.retrieval_context)),
            ]
        )
        profile_res = parallel_results["profile"]
        retrieval_res = parallel_results["retrieval"]

        # 2) 串行：planning 依赖前两者。
        planning_res = self._run(
            "planning",
            ctx.planning_task(profile_res.summary, retrieval_res.summary),
            context={
                "画像上下文": profile_res.summary,
                "检索要点": retrieval_res.summary,
            },
        )

        # 4) 串行：safety 审查 planning 产出。
        safety_res = self._run(
            "safety",
            ctx.safety_task(planning_res.summary),
            context={
                "用户风险词": ", ".join(ctx.risk_terms) or "（无声明）",
                "待审查计划": planning_res.summary,
            },
        )

        sub_results = [profile_res, retrieval_res, planning_res, safety_res]

        # 3) HITL 关卡（R7）：仅在首轮（未 resume）且开关开启时检查高影响步骤。
        if self.hitl_enabled and not resume:
            pending = self._maybe_request_confirmation(profile_res, safety_res)
            if pending is not None:
                self._log(
                    "confirm_request",
                    {"confirm_id": pending.confirm_id, "kind": pending.kind,
                     "payload": pending.payload},
                )
                # 挂起：把恢复所需的编排快照写进 payload，本回合结束等待确认。
                pending.payload["message"] = message
                pending.payload["subagent_results"] = [
                    self._result_brief(r) for r in sub_results
                ]
                return OrchestrationResult(
                    route=ROUTE_COMPOSITE,
                    answer=pending.prompt,
                    subagent_results=[self._result_brief(r) for r in sub_results],
                    reason=reason,
                    pending=pending,
                )

        # 5) 聚合（首轮无挂起，或 resume 带回确认结果）。
        answer = self._aggregate(message, sub_results, resume=resume)
        self._log(
            "orchestrator_aggregate",
            {
                "subagents": [r.name for r in sub_results],
                "answer_preview": (answer or "")[:200],
            },
        )
        return OrchestrationResult(
            route=ROUTE_COMPOSITE,
            answer=answer,
            subagent_results=[self._result_brief(r) for r in sub_results],
            reason=reason,
        )

    # ------------------------------------------------------------------ HITL
    def _maybe_request_confirmation(
        self, profile_res: Any, safety_res: Any
    ) -> Optional[PendingConfirmation]:
        """检查高影响步骤，命中则产出 PendingConfirmation（方案 §3.3）。

        优先级：安全风险（safety 命中）> 画像敏感变更建议。两者只挂起一个，
        安全风险更高影响先确认。
        """
        # 1) 安全风险：safety 判定 risk=true 且有命中。
        safety_data = getattr(safety_res, "data", {}) or {}
        risk = bool(safety_data.get("risk"))
        hits = [str(h) for h in (safety_data.get("hits") or []) if str(h).strip()]
        if safety_res.success and risk and hits:
            advice = safety_data.get("advice") or safety_res.summary
            return PendingConfirmation(
                confirm_id=new_confirm_id(),
                kind=KIND_SAFETY_RISK,
                prompt=(
                    "⚠️ 安全审查发现你的计划可能触碰以下风险："
                    f"{', '.join(hits)}。\n建议：{advice}\n"
                    "是否接受已规避风险的替代方案？（同意=采纳建议 / 拒绝=按原约束 / 修改=补充你的约束）"
                ),
                payload={"hits": hits, "advice": advice},
            )

        # 2) 画像敏感字段变更建议。
        profile_data = getattr(profile_res, "data", {}) or {}
        suggested = profile_data.get("suggested_updates") or {}
        if isinstance(suggested, dict):
            sensitive = {
                k: v
                for k, v in suggested.items()
                if str(k).strip().lower() in self.sensitive_fields
            }
            if sensitive:
                return PendingConfirmation(
                    confirm_id=new_confirm_id(),
                    kind=KIND_PROFILE_UPDATE,
                    prompt=(
                        "我建议更新你画像中的敏感字段："
                        f"{self._render_updates(sensitive)}。\n"
                        "是否确认写入？（同意=写入 / 拒绝=不写 / 修改=调整后写入）"
                    ),
                    payload={"suggested_updates": sensitive},
                )
        return None

    @staticmethod
    def _render_updates(updates: dict) -> str:
        parts = []
        for k, v in updates.items():
            val = "、".join(v) if isinstance(v, (list, tuple)) else str(v)
            parts.append(f"{k}={val}")
        return "；".join(parts)

    def _run_parallel(self, jobs: List[tuple]) -> dict:
        """并行执行若干 SubAgent 任务，返回 {name: SubAgentResult}。"""
        results: dict = {}
        max_workers = max(1, min(self.parallelism, len(jobs)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(self._run, name, task, ctx): name
                for (name, task, ctx) in jobs
            }
            for future in future_map:
                name = future_map[future]
                try:
                    results[name] = future.result()
                except Exception as exc:  # noqa: BLE001 - 失败隔离
                    results[name] = self._failure_result(name, str(exc))
        return results

    def _run(self, name: str, task: str, context: Optional[dict] = None):
        """构造并执行一个 SubAgent，返回 SubAgentResult（失败隔离）。"""
        self._log("orchestrator_dispatch", {"subagent": name, "task_preview": task[:120]})
        try:
            subagent = self.subagent_factory(name, self.trace_logger)
        except Exception as exc:  # noqa: BLE001
            return self._failure_result(name, f"构造失败: {exc}")

        self._log(
            "subagent_start",
            {"subagent": name, "tools_allowed": sorted(subagent.tool_filter.filter(
                subagent.base_registry.list_tools()
            ))},
        )
        try:
            result = subagent.execute(task, context or {})
        except Exception as exc:  # noqa: BLE001 - 失败隔离：单个子代理异常不整轮崩
            return self._failure_result(name, f"执行异常: {exc}")
        self._log(
            "subagent_result",
            {
                "subagent": name,
                "success": result.success,
                "summary": (result.summary or "")[:200],
                "steps": result.metadata.get("steps"),
                "tools_used": result.metadata.get("tools_used"),
                "duration_ms": result.metadata.get("duration_ms"),
            },
        )
        return result

    # ------------------------------------------------------------------ aggregate
    def _aggregate(
        self, message: str, sub_results: List[Any], resume: Optional[dict] = None
    ) -> str:
        """把子代理结论综合为面向用户答案；LLM 不可用则降级为结构化拼接。

        resume 非空时（HITL 恢复），把用户的确认决策并入聚合上下文，使最终答案反映
        用户的确认/修正（方案 §3.4）。
        """
        rendered = self._render_sub_results(sub_results)
        resume_block = self._render_resume(resume)
        if self.llm is not None:
            user_content = f"【用户请求】\n{message}\n\n【子代理结论】\n{rendered}"
            if resume_block:
                user_content += f"\n\n【用户确认结果（必须遵守）】\n{resume_block}"
            prompt = [
                {"role": "system", "content": ORCHESTRATOR_AGGREGATE_PROMPT},
                {"role": "user", "content": user_content},
            ]
            try:
                answer = self.llm.invoke(prompt)
                if answer and str(answer).strip():
                    return str(answer).strip()
            except Exception:
                pass
        # 降级：拼接可读摘要。
        return self._fallback_aggregate(sub_results)

    @staticmethod
    def _render_resume(resume: Optional[dict]) -> str:
        """把 HITL 确认结果渲染为聚合可读文本。"""
        if not resume:
            return ""
        decision = str(resume.get("decision", ""))
        kind = str(resume.get("kind", ""))
        applied = resume.get("applied") or {}
        note = str(resume.get("note", ""))
        lines = [f"确认类型：{kind}", f"用户决策：{decision}"]
        if applied:
            lines.append(f"生效内容：{applied}")
        if note:
            lines.append(f"用户补充：{note}")
        return "\n".join(lines)

    @staticmethod
    def _render_sub_results(sub_results: List[Any]) -> str:
        blocks: List[str] = []
        for r in sub_results:
            status = "成功" if r.success else "未完成/失败"
            blocks.append(f"[{r.name}｜{status}]\n{r.summary or '（无输出）'}")
        return "\n\n".join(blocks)

    @staticmethod
    def _fallback_aggregate(sub_results: List[Any]) -> str:
        planning = next((r for r in sub_results if r.name == "planning"), None)
        safety = next((r for r in sub_results if r.name == "safety"), None)
        parts: List[str] = []
        if planning and planning.summary:
            parts.append(planning.summary)
        if safety and safety.summary:
            parts.append(f"\n【安全提示】\n{safety.summary}")
        if not parts:
            return "抱歉，暂时无法完成该复合请求，请补充更多信息后重试。"
        return "\n".join(parts)

    # ------------------------------------------------------------------ helpers
    def _failure_result(self, name: str, error: str):
        from ..agents.sub_agent import SubAgentResult

        self._log("subagent_result", {"subagent": name, "success": False, "error": error})
        return SubAgentResult(
            name=name,
            success=False,
            summary=f"[{name}] 子代理不可用（降级占位）：{error}",
            data={},
            steps=[],
            metadata={"steps": 0, "tools_used": [], "duration_ms": 0,
                      "terminated_reason": "error", "error": error},
        )

    @staticmethod
    def _result_brief(r: Any) -> dict:
        return {
            "name": r.name,
            "success": r.success,
            "summary": r.summary,
            "steps": r.metadata.get("steps"),
            "duration_ms": r.metadata.get("duration_ms"),
            "tools_used": r.metadata.get("tools_used"),
        }

    def _log(self, event: str, payload: dict) -> None:
        if self.trace_logger is not None:
            try:
                self.trace_logger.log_event(event, payload)
            except Exception:
                pass

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

