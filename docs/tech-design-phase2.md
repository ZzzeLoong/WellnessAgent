# WellnessAgent 第二期技术方案（架构 / 能力上限：Orchestrator + SubAgent / HITL / 指标可观测）

<!-- 图例：✅ 已实现并有单测　🟡 部分实现　⬜ 未实现（本期目标，起始均为 ⬜） -->

## 📌 本期范围与状态总览

> 本方案对应需求 `docs/requirements.md` 第二期 **R6 / R7 / R8**，在一期（`docs/tech-design-phase1.md`，186 passed 全绿）已落地的**累积 messages + 原生 Function Calling 内核**之上，引入**编排层（Orchestrator）+ 专职 SubAgent**、**人机协同确认（HITL）** 与**指标可观测**。
>
> **当前进度（截至本次更新）**：Phase 2.0（P0-1~P0-4）+ **R6 / R7 / R8 全部完成**并有单测；全量 **275 passed 全绿**（一期 186 → 二期累计 +89）。二期需求已全部交付，仅里程碑第 10 步（`benchmark/evaluators/orchestration.py`）顺延至三期 R14。

| 模块/需求 | 交付物 | 状态 |
|-----------|--------|------|
| **R6** SubAgent 隔离执行 | `agents/sub_agent.py`（独立轻量 ReAct 实例 + 授权工具白名单 + 隔离 messages/trace + 摘要返回） | ✅ |
| **R6** 工具权限过滤 | `tools/tool_filter.py`（计算白名单策略）+ registry `allowed` 参数（P0-4，内建过滤） | ✅ |
| **R6** 4 个专职 SubAgent | `wellnessagent/subagents/`（profile 只读 / retrieval / planning / safety，集中定义于 `__init__.py`） | ✅ |
| **R6** Orchestrator 编排 | `wellnessagent/orchestrator.py`（LLM 分诊 → pipeline → 并行分派 → 聚合） | ✅ |
| **R7** HITL 回合边界确认闭环 | `core/hitl.py` + `SessionStore` 挂起态 + `CONFIRM` 流事件 + `/api/chat` resume + `ConfirmDialog.tsx` | ✅ |
| **R8** 指标聚合 | `observability/metrics.py`（从 trace JSONL 聚合）+ `/api/metrics` + `MetricsPage.tsx` | ✅ |
| **可观测** trace 事件扩展 | `trace_logger.py` `EVENT_TYPES` 扩 orchestrator/subagent/confirm 事件 | ✅ |

> **后续**：第三期方案见 `docs/tech-design-phase3.md`（R9 全链路多模态 / R10 主动推送 / R11 反馈闭环 / R12 记忆时间线 / R13 MCP / R14 A/B）。

---

## 🔧 一期改造项（二期实现前置，Phase 2.0）

> **背景与原则**：一期为"止血/稳健性"，部分实现为了保稳留了兼容尾巴与私有边界；进入二期编排/HITL 前，先做一批**低风险重构**清理技术债，让后续 SubAgent/编排/确认的实现更干净。经与需求方确认：**这些地方不需要为一期兼容让步，可直接改**（改后一期 186 单测须回归通过，允许调整因口径变化而失效的少量断言）。
>
> 本批次编号 **Phase 2.0**，是里程碑第 1 步之前的前置工作（见第 5 节里程碑已相应前置）。

| 编号 | 改造 | 现状问题 | 改造后 | 收益 | 风险 |
|------|------|---------|--------|------|------|
| **P0-1 ✅** | `ReActAgent` 增加**隔离运行入口** | SubAgent 只能靠猴补丁 `agent.build_system_prompt = lambda...` 覆盖钩子来注入上下文，别扭且易错 | 给 `run_with_trace/stream_run` 增加可选 `system_prompt_override`（或 `initial_context`）参数，一次性隔离运行走正规入口；不传=一期行为 | SubAgent 执行路径干净、可读、可测 | 低（纯加可选参数） |
| **P0-2 ✅** | **收敛两套对话历史为 messages 单一源** | 同时维护 `self.messages`（真正发 LLM）与基类 `self._history`（`_finalize` 里 `add_message` 冗余写入，`_get_completed_turns`/`_recent_dialogue_window` 依赖它），两套派生易漂移 | `_get_completed_turns`/`_recent_dialogue_window` 直接从 `self.messages` 派生；删除 `_finalize` 对 `_history` 的写入与依赖 | 单一数据源；二期编排/HITL 状态一致；distill 轮次计数更准 | 中（`get_state_dict` 展示口径变，回归相关单测） |
| **P0-3 ✅** | **安全风险词逻辑抽共享模块** | `guardrails.py` 的 `_collect_risk_terms`/`_expand_aliases`/`_ALIAS_MAP` 私有；二期 `safety` SubAgent 若各写一份，别名表会漂移、红线口径不一致 | 抽到 `wellnessagent/safety_rules.py`（或 guardrails 暴露公开函数），guardrails 与 safety SubAgent **共用同一份** | 安全逻辑单一事实源，两道关口径一致 | 低（纯重构，行为不变） |
| **P0-4 ✅** | **`ToolRegistry` 内建白名单过滤**（替代外挂 view） | 原方案 `filtered_view()` 要再包一层视图代理类；registry 本可直接支持过滤 | `build_tool_schemas(allowed=None)` / `execute_tool(name, input, allowed=None)` 加可选白名单参数（不传=一期行为）；**无状态过滤**，并行时靠"每次调用传 `allowed`"隔离，不引入实例状态 | 少一层包装类，SubAgent 直接复用主 registry + 传白名单 | 低（可选参数，默认行为不变） |

> **说明**：P0-4 落地后，第 2.1/2.2 节的 `registry.filtered_view()` 与 `WhitelistFilter` 视图语义**统一收敛为"传 `allowed` 集合"**；`ToolFilter` 类仍保留用于**表达"某 SubAgent 授予哪些工具"的策略**（其 `filter()` 产出 `allowed` 集合再传给 registry），职责从"包装 registry"降为"计算白名单"。
>
> **不纳入本批次**：LLM/内核 **async 化**（收益中但风险高，等于动执行模型，一期 20 个 `test_react_agent` 全需重审）。本期并行用 `ThreadPoolExecutor`（LLM 调用 IO 密集，线程池足够）；async 化留待未来做"真暂停 HITL / 逐 token 流式"时单独立项。

### Phase 2.0 落地顺序（先于第 5 节里程碑第 1 步）

| 序 | 任务 | 验证 |
|----|------|------|
| 0.1 ✅ | P0-3 抽 `safety_rules.py` | `test_guardrails.py` 回归绿 + `test_safety_rules.py` 共享函数单测 |
| 0.2 ✅ | P0-4 registry `allowed` 白名单参数 | `test_registry_allowed.py` 未授权工具被拒 + 一期 registry 单测回归 |
| 0.3 ✅ | P0-1 ReActAgent 隔离运行入口 | `test_react_agent_isolation.py` 传/不传 override 两路径 + `test_react_agent.py` 回归 |
| 0.4 ✅ | P0-2 收敛历史单一源 | `_get_completed_turns` 从 messages 派生单测 + 受影响状态单测调整 |

> 完成 Phase 2.0 后再进入第 5 节 R6/R7/R8 实现里程碑。

---

> 版本：v0.1（评审确认稿）
> 对应需求：`docs/requirements.md` 第二期 R6 / R7 / R8
>
> **本期已锁定的设计决策（评审确认）**：
> 1. **SubAgent 隔离粒度 = 独立轻量实例 + 工具子集**（非"复用主实例临时清空 messages"）。每个 SubAgent 有独立 `messages`、独立 registry 视图、独立子 trace，天然支持并行；共享**只读** `service`（画像/RAG/记忆读取），不共享写路径。
> 2. **编排策略 = LLM 分诊 + pipeline 兜底**：Orchestrator 先分诊（simple / composite），简单请求走一期单体 ReAct 原路径；复合请求走固定 pipeline 拆解为子任务并分派。
> 3. **4 个专职 SubAgent 全做**：`profile`（只读）、`retrieval`（知识检索）、`planning`（饮食规划）、`safety`（主动安全审查）。
> 4. **画像 SubAgent 只读**：只 `profile_get` + 整理/推断"画像补全建议"；**画像写入 `profile_set` 仍由主 agent 主循环完成**（长期记忆不可逆变更不下放子代理）。画像敏感变更若由 SubAgent 提议，走 R7 HITL 确认后由主 agent 落库。
> 5. **安全双层**：一期 `DietGuardrails`（`_finalize` 终止前钩子、规则层硬红线）**保留为不可绕过的最终兜底**；本期新增 `safety` SubAgent 做**规划阶段的主动审查**（给规划 SubAgent 出修改建议），二者互补不替代。
> 6. **R7 = 回合边界确认闭环**（非"步骤中间真暂停"）：高影响步骤产出 `CONFIRM` 事件、本回合正常结束并持久化"待确认态"；用户确认/修正作为下一轮输入带回，agent 据此继续。不改一期同步生成器执行模型。
> 7. **R8 = 后端聚合 API + 前端面板**：数据源为一期 R5 落盘的 trace JSONL，只读聚合、不引入新存储。
> 8. **知识库语料扩充**列为二期**可选后续项**（先不急）；`retrieval` SubAgent 与 `kb_*` 工具保持一期实现。
> 9. **不 import hello_agents**：上游 `TaskTool`/`ToolFilter`/`run_as_subagent` 仅作**设计参考**，按 WellnessAgent 累积 messages 内核重新实现。

---

## 0. 背景与定位

### 0.1 从一期到二期的演进

一期把内核换成了**累积 `messages` + 原生 FC** 的干净架构（`agents/react_agent.py`），并明确"面向二期 Orchestrator/SubAgent/HITL 的架构"。二期就是兑现这个方向：

- **一期（单体）**：一个 `WellnessPlanningAgent`（ReAct）在一个 messages 循环里，自己调所有工具、自己收尾。复合请求（"7 天减脂 + 避开多过敏原 + 预算/时间约束"）容易走偏或漏约束（需求 R6 背景）。
- **二期（编排）**：引入 `Orchestrator` 分诊与拆解，把子任务分派给**专职 SubAgent**（各自隔离上下文 + 受限工具集），最后聚合；关键决策点插入 **HITL 确认**；全过程指标沉淀为**可观测面板**。

### 0.2 与一期的边界（务必不破坏）

| 一期资产 | 二期动作 |
|---------|---------|
| `react_agent.py` 累积 messages + FC 内核 | **不动核心循环**；SubAgent 复用同一 `ReActAgent` 类构造轻量实例 |
| `DietGuardrails` `_finalize` 钩子（硬红线） | **保留**为最终兜底；新增 safety SubAgent 是"额外主动审查"而非替代 |
| `ToolRegistry`（结构化 ToolResponse + 熔断 + FC schema） | **P0-4 内建白名单**：`build_tool_schemas/execute_tool` 加可选 `allowed` 参数（无状态过滤，不传=一期行为） |
| `SessionStore`（会话消息落盘） | **扩展 metadata** 承载 HITL 挂起态，消息序列化不变 |
| `TraceLogger`（JSONL+HTML+脱敏） | **扩 `EVENT_TYPES`**，新增编排/子代理/确认事件；落盘格式不变 |
| `/api/chat`、`/api/chat/stream`（对外契约） | **保持字段**；新增可选 `confirmation` 入参、`CONFIRM` 事件、`/api/metrics` |
| 一期 186 单测 | **全部保持绿**；二期只新增测试，不改一期断言 |

### 0.3 本期落点文件总览

```
core/
  hitl.py              ← 新增（PendingConfirmation 数据结构 + 序列化，R7）
tools/
  tool_filter.py       ← 新增（ToolFilter/WhitelistFilter/ReadOnlyFilter，R6）
  registry.py          ← P0-4：build_tool_schemas/execute_tool 加可选 allowed 白名单参数（无状态过滤）
agents/
  sub_agent.py         ← 新增（SubAgent：轻量 ReAct 实例 + 隔离执行 + 摘要返回，R6）
observability/
  trace_logger.py      ← 扩 EVENT_TYPES（orchestrator_* / subagent_* / confirm_*）
  metrics.py           ← 新增（从 trace JSONL 聚合指标，R8）
wellnessagent/
  orchestrator.py      ← 新增（分诊 + pipeline + 并行分派 + 聚合，R6）
  subagents/
    __init__.py        ← 新增（SubAgent 工厂 + 注册表）
    profile_agent.py   ← 新增（只读画像整理，R6）
    retrieval_agent.py ← 新增（知识检索，R6）
    planning_agent.py  ← 新增（饮食规划，R6）
    safety_agent.py    ← 新增（主动安全审查，R6）
  agent.py             ← 扩：装配 orchestrator；chat/chat_stream 接入分诊与 HITL 挂起/恢复
  prompts.py           ← 新增各 SubAgent 与 Orchestrator 的 system prompt
  server/
    main.py            ← /api/chat 支持 confirmation 恢复；新增 /api/metrics
    streaming.py       ← StreamEventType 新增 CONFIRM / SUBAGENT_* / ORCHESTRATOR_*
    deps.py            ← metrics 读取入口
frontend/src/
  types.ts             ← 编排/子代理/确认事件类型
  components/
    OrchestrationPanel.tsx ← 编排/子任务可视化
    ConfirmDialog.tsx      ← HITL 确认交互
    MetricsPage.tsx        ← R8 指标面板
  ChatPage.tsx         ← 消费 CONFIRM 事件 + 恢复流程
benchmark/
  evaluators/orchestration.py ← 新增（约束遵守率 / 子任务覆盖 evaluator，R6/R14 预备）
```

---

## 1. 整体架构（编排内核）

### 1.1 一次复合对话的数据流

```
POST /api/chat(/stream)  {user_id, message, session_id?, confirmation?}
      │
      ▼
deps.get_agent(user_id)                         # 一期 LRU + SessionStore 重建
      │
      ▼
WellnessPlanningAgent.chat / chat_stream
   (0) 若 payload.confirmation 存在 → HITL 恢复：读挂起态，把用户确认结果并入上下文
      │
      ▼
Orchestrator.handle(message, ctx)
   (1) 分诊 triage：LLM（或规则）判定 simple | composite
        simple    → 直接一期单体 ReActAgent 路径（原样，零改动）
        composite → 进入 pipeline
      │
      ▼ (composite)
   (2) 规划 pipeline（固定 DAG，节点=SubAgent 子任务）:
        profile(只读) ┐
                      ├─(并行) → planning(依赖 profile+retrieval) → safety(审查 planning 产出)
        retrieval     ┘
      (3) 每个子任务：SubAgent.execute(task, tool_filter) 隔离执行 → 结构化摘要
          - 独立 messages、独立 registry 视图（仅授权工具）、独立子 trace
      (4) HITL 关卡（R7）：若命中高影响条件（画像敏感变更建议 / safety 命中过敏原）
          → 产出 CONFIRM，持久化"待确认态"到 SessionStore，本回合结束
      (5) 聚合 aggregate：Orchestrator 汇总子任务结果为最终答案草稿
      │
      ▼
   (6) 最终兜底：DietGuardrails.check（一期硬红线钩子，不可绕过）
   (7) SessionStore.save（含挂起态 metadata）；TraceLogger.finalize（含编排事件）
   返回 {answer, terminated_reason, steps, state, session_id, trace_id, safety, orchestration, confirmation?}
```

### 1.2 分诊两条路径对照

| 维度 | simple 路径（复用一期） | composite 路径（本期新增） |
|------|------------------------|---------------------------|
| 触发 | 单点问答、闲聊、单一知识查询 | 多约束/多目标/多步（食谱规划、含忌口/预算/时间约束） |
| 执行体 | 单体 `ReActAgent`（一期内核） | `Orchestrator` + 多 `SubAgent` |
| 上下文 | 主 messages | 主 messages + 各子任务隔离 messages |
| 成本 | 1 条推理链 | 分诊 1 次 + 各 SubAgent + 聚合（并行压延迟） |
| trace | 一期事件 | 一期事件 + `orchestrator_*`/`subagent_*` |

> **分诊降级**：分诊 LLM 不可用 / 解析失败 → 默认按 **composite** 处理（宁可多编排也不漏约束）；若连 SubAgent LLM 都不可用，最终降级回 simple 单体路径，保证永远有答案。

---

## 2. R6 Orchestrator + SubAgent（核心）

**关联缺陷**：—　**依赖**：R1（FC，✅）、R5（trace，✅）、R15（上下文，✅）、R16（熔断，✅）　**工作量**：L　**状态**：✅（已完成并有单测）

### 2.1 新增 `tools/tool_filter.py`（工具权限过滤）

```python
from abc import ABC, abstractmethod

class ToolFilter(ABC):
    @abstractmethod
    def is_allowed(self, tool_name: str) -> bool: ...
    def filter(self, names: list[str]) -> list[str]:
        return [n for n in names if self.is_allowed(n)]

class WhitelistFilter(ToolFilter):
    """白名单：仅允许显式授予的工具（SubAgent 默认策略）。"""
    def __init__(self, allowed: set[str]): self.allowed = set(allowed)
    def is_allowed(self, name): return name in self.allowed

class ReadOnlyFilter(ToolFilter):
    """只读：拒绝一切写类工具。"""
    DENIED = {"profile_set", "profile_remove", "memory_remember", "session_note"}
    def is_allowed(self, name): return name not in self.DENIED
```

- **registry 内建白名单（P0-4）**：`ToolRegistry.build_tool_schemas(allowed=None)` 只暴露 `allowed` 内的工具；`execute_tool(name, input, allowed=None)` 命中未授权工具直接返回 `ToolResponse.error(INVALID_PARAM, "工具未授权")`。**无状态过滤**（不传 `allowed`=一期行为），并行安全。
- **`ToolFilter` 的职责**：不再包装 registry，而是**计算某 SubAgent 的授权白名单集合**（`filter()` 产出 `allowed`），由 SubAgent 在每次调用时把 `allowed` 传给共享 registry。
- **finish 工具**：`allowed` 集合始终包含 `finish`（否则 SubAgent 无法收尾）。

### 2.2 新增 `agents/sub_agent.py`（SubAgent 隔离执行）

```python
@dataclass
class SubAgentResult:
    name: str                 # profile/retrieval/planning/safety
    success: bool
    summary: str              # 给 Orchestrator 阅读的结构化摘要（非完整历史）
    data: dict                # 结构化产物（planning 的食谱 / safety 的 hits 等）
    steps: list[dict]         # 该子代理 StepRecord（trace/评测用）
    metadata: dict            # {steps, tools_used, duration_ms, terminated_reason}

class SubAgent:
    """独立轻量 ReAct 子代理（上下文隔离 + 工具子集 + 摘要返回）。

    与上游 run_as_subagent 的区别：不"清空并恢复主 agent 历史"，而是每次
    构造独立 ReActAgent 实例，天然隔离、可并行；共享只读 service。
    """
    def __init__(self, name, llm, base_registry, tool_filter,
                 system_prompt, service, max_steps=6, parent_trace=None): ...

    def execute(self, task: str, context: dict) -> SubAgentResult:
        # P0-4：共享主 registry，仅传授权白名单（无需包装视图）
        self.allowed = set(self.tool_filter.filter(base_registry.list_tools())) | {"finish"}
        agent = ReActAgent(
            name=f"sub:{self.name}", llm=self.llm,
            tool_registry=self.base_registry, system_prompt=self.system_prompt,
            max_steps=self.max_steps, allowed_tools=self.allowed,   # 内核透传给 registry
        )
        agent.trace_logger = self._subtrace(self.parent_trace)  # 打 subagent 标签
        # P0-1：隔离运行入口，正规注入子上下文（不再猴补丁 build_system_prompt）
        result = agent.run_with_trace(task, system_prompt_override=self._compose_system(context))
        return self._summarize(result)
```

**要点**：
- **隔离**：独立 `messages`、授权工具白名单（P0-4 `allowed`）、独立 `max_steps`（子代理默认 6 步）；上下文经 P0-1 隔离入口 `system_prompt_override` 注入。
- **摘要返回**：`_summarize` 把子代理 `final_answer + 结构化 data` 压成短摘要，避免把完整 messages 灌回主上下文（防膨胀，呼应一期 R15）。
- **无写权限泄漏**：写类工具靠 `ToolFilter` 在 view 层挡掉，子代理即使"想写"也调不到。
- **熔断/截断复用**：registry view 透传一期 `CircuitBreaker` 与 `ObservationTruncator`，子代理照样受保护。

### 2.3 四个专职 SubAgent（`wellnessagent/subagents/`）

| SubAgent | 授权工具（WhitelistFilter） | 职责 | 输出 data |
|----------|---------------------------|------|-----------|
| `profile`（只读） | `profile_get`, `memory_search`, `finish` | 读画像+长期记忆，整理成规划所需结构化上下文；推断画像补全建议（不落库） | `{profile_summary, gaps, suggested_updates}` |
| `retrieval` | `kb_search`, `kb_answer`, `kb_status`, `finish` | 针对子问题检索营养知识库，返回带引用要点 | `{snippets, citations}` |
| `planning` | `kb_search`, `session_recall`, `finish` | 依据画像上下文 + 检索要点 + 约束，产出结构化食谱/计划 | `{plan:{days:[...]}, constraints_used}` |
| `safety` | `finish`（纯推理，风险词由 ctx 注入） | 对 planning 产出做主动安全审查，输出风险点与修改建议 | `{risk, hits, advice}` |

> **profile 写入边界**：`profile` SubAgent 的 `suggested_updates` 只是**建议**，不调 `profile_set`（filter 也不含写工具）。是否落库由主 agent 决定；敏感字段（allergies/medical_notes）走 R7 HITL 确认。
> **safety 与一期 guardrails 分工**：safety SubAgent = "规划中审查、给建议、可被 orchestrator 采纳改写"；一期 `DietGuardrails` = "最终输出前强制关卡、命中即加警示、不可绕过"。两道关，红线不失守。

### 2.4 新增 `wellnessagent/orchestrator.py`（编排）

```python
class Orchestrator:
    def __init__(self, llm, subagent_factory, service, trace_logger=None): ...

    def handle(self, message, ctx) -> OrchestrationResult:
        route = self._triage(message)                 # "simple" | "composite"
        if route == "simple":
            return OrchestrationResult(route="simple", delegate_to_monolith=True)
        return self._run_pipeline(message, ctx)

    def _triage(self, message) -> str:
        # 轻量 LLM 单次判定；失败/超时 → 默认 "composite"
        ...

    def _run_pipeline(self, message, ctx) -> OrchestrationResult:
        # 1) 并行：profile + retrieval
        profile_res, retrieval_res = self._run_parallel([
            ("profile",   ctx.profile_task),
            ("retrieval", ctx.retrieval_task(message)),
        ])
        # 2) 串行：planning 依赖前两者
        planning_res = self._run("planning",
            ctx.planning_task(message, profile_res, retrieval_res))
        # 3) HITL 关卡（画像敏感变更建议 / 高风险）→ 可能挂起
        pending = self._maybe_request_confirmation(profile_res, planning_res)
        if pending:
            return OrchestrationResult(route="composite", pending=pending)
        # 4) 串行：safety 审查 planning 产出
        safety_res = self._run("safety",
            ctx.safety_task(planning_res, ctx.risk_terms))
        # 5) 聚合
        answer = self._aggregate(message, profile_res, retrieval_res,
                                 planning_res, safety_res)
        return OrchestrationResult(route="composite", answer=answer,
                                   subagent_results=[...])
```

**编排要点**：
- **固定 DAG**（确定性、可评测）：`{profile, retrieval}` 并行 → `planning` → `safety` → 聚合。
- **并行实现**：一期 `Agent`/`LLM` 是同步接口（无 async），SubAgent 并行用 `concurrent.futures.ThreadPoolExecutor`（LLM 调用是 IO 密集，线程池足够），避免为并行改一期内核。每个 SubAgent 独立实例 + 独立 registry view，不共享可变状态、`service` 只读，线程安全。
- **聚合**：`_aggregate` 用一次 LLM 把子结果综合为面向用户的答案，safety 的修改建议在此并入。
- **失败隔离**：任一 SubAgent 失败（熔断/异常）→ 记 trace、用降级摘要占位，pipeline 继续（不整轮崩）。

### 2.5 主 agent 装配（`wellnessagent/agent.py` 扩展）

- `__init__` 末尾装配 `self.orchestrator = Orchestrator(...)`，注入 `subagent_factory`（用主 agent 的 `llm`/`tool_registry`/`service` 构造各 SubAgent）与只读 `service`。
- `chat_with_trace` / `chat_stream`：先 `orchestrator.handle`；
  - `route=="simple"` → 走一期 `run_with_trace` / `stream_run`（**完全原路径**）；
  - `route=="composite"` → 消费 `OrchestrationResult`，把 `answer` 交给一期 `_finalize`（复用 guardrails 兜底 + 状态写入）。
- **对外契约不变**：返回 dict 仍含一期字段，额外加 `orchestration`（route/子任务摘要）与可选 `confirmation`。

### 2.6 trace 扩展（`observability/trace_logger.py`）

`EVENT_TYPES` 新增（仅扩集合，向后兼容）：

```
orchestrator_triage      {route, reason}
orchestrator_dispatch    {subagent, task_preview}
subagent_start           {subagent, tools_allowed}
subagent_result          {subagent, success, summary, steps, tools_used, duration_ms}
orchestrator_aggregate   {subagents, answer_preview}
confirm_request          {confirm_id, kind, payload}       # R7
confirm_resume           {confirm_id, decision}            # R7
```

> 子代理内部的 `model_output`/`tool_call`/`tool_result` 事件仍写主 trace，但带 `subagent` 标签字段，前端/评测可按标签归组。

### 2.7 验收对照（R6）

| 验收标准（需求 R6） | 实现点 |
|---------------------|--------|
| 复合请求被拆解为可见子任务并逐一完成 | `orchestrator._run_pipeline` + `subagent_*` trace 事件 + 前端 OrchestrationPanel |
| 相比单体，约束遵守率不下降且可度量提升 | `benchmark/evaluators/orchestration.py`（过敏原/预算/时间约束遵守率），单体 vs 编排 A/B |
| 编排过程在 trace 中可见（子任务/分派/聚合） | 2.6 新增事件 |
| SubAgent 只能访问被授权工具子集 | `WhitelistFilter` 计算白名单 + registry `allowed` 参数（P0-4，未授权工具 execute 返回 error） |

---

## 3. R7 Human-in-the-Loop（回合边界确认闭环）

**关联缺陷**：—　**依赖**：R2（流式，✅）、R6（编排，✅）　**工作量**：L（本方案降为 M：不改执行模型）　**状态**：✅（已实现：`core/hitl.py` + `agent._suspend_for_confirmation`/`_resume_from_confirmation` + `orchestrator._maybe_request_confirmation` + `StreamEventType.CONFIRM` + `ConfirmDialog.tsx`；单测 `test_hitl.py`/`test_agent_hitl.py`/`test_orchestrator_hitl.py`/`test_streaming_events.py`）

### 3.1 设计取舍：为什么是"回合边界确认"而非"步骤中间真暂停"

一期内核是**同步生成器**（`react_agent._iter_events`）驱动，且服务是无状态 HTTP。要在工具调用中间真正"暂停 → 跨 HTTP 请求恢复"需改执行模型，风险高。健康场景的确认点天然在"给出建议之后 / 敏感变更之前"，因此采用**回合边界确认**：

- agent **不在循环中间阻塞**；命中确认条件时，把"待确认项"作为本回合产物返回，回合正常结束。
- 前端渲染确认 UI；用户确认/修正结果作为**下一轮输入**（`confirmation` 字段）带回。
- 用一个**挂起态**（`PendingConfirmation`）挂在 `SessionStore` 上衔接前后两轮。

### 3.2 新增 `core/hitl.py`

```python
@dataclass
class PendingConfirmation:
    confirm_id: str                 # c-YYYYMMDD-HHMMSS-xxxx
    kind: str                       # "profile_update" | "safety_risk"
    prompt: str                     # 给用户看的确认问题
    payload: dict                   # 待确认内容（如 suggested_updates / risk hits）
    created_at: str
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d) -> "PendingConfirmation": ...
```

### 3.3 触发条件（高影响步骤）

1. **画像敏感变更**：`profile` SubAgent 的 `suggested_updates` 涉及 `allergies` / `medical_notes` / `diet_pattern` → 需用户确认后主 agent 才 `profile_set` 落库。
2. **安全风险**：`safety` SubAgent 判定 `risk=true` 且命中用户过敏原 → 请用户确认是否接受"已规避风险"的替代方案，或修正约束。

> 其余低影响变更（如 `preferred_cuisines`）不触发确认，直接由主 agent 落库，避免打扰。

### 3.4 挂起与恢复

- **挂起**：`OrchestrationResult.pending` 非空 → agent 把 `PendingConfirmation` 写入 `SessionStore` 的 `metadata["pending_confirmation"]`（复用一期 save，不改消息序列化）；对外返回 `confirmation` 字段（含 `confirm_id/prompt/payload`），流式下发 `CONFIRM` 事件；本回合 `terminated_reason="awaiting_confirmation"`（**新增 trace 标记，不进对外稳定集合**，对外 `terminated_reason` 仍映射为 `finished`）。
- **恢复**：下一次 `/api/chat` 带 `confirmation={confirm_id, decision, patch?}`：
  - `decision=="approve"` → 主 agent 执行被确认的动作（如 `profile_set(suggested_updates)`），并把结果并入上下文继续聚合/回答；
  - `decision=="reject"` → 丢弃建议，按原约束继续；
  - `decision=="modify"` → 用 `patch` 覆盖后执行。
  - 清除 `SessionStore` 的挂起态，写 `confirm_resume` trace。

### 3.5 流式事件（`server/streaming.py`）

`StreamEventType` 新增：`CONFIRM`（挂起点，带 confirm_id/prompt/payload）、`SUBAGENT_START`/`SUBAGENT_RESULT`、`ORCHESTRATOR_TRIAGE`/`ORCHESTRATOR_AGGREGATE`。前端 `ChatPage` 收到 `CONFIRM` 弹 `ConfirmDialog`，提交后以 `confirmation` 发起下一轮。

### 3.6 验收对照（R7）

| 验收标准 | 实现点 |
|---------|--------|
| 触发确认节点能正确暂停并在响应后继续 | 3.4 挂起态 + `/api/chat` confirmation 恢复 |
| 用户中途修正能实际影响后续输出 | `decision=="modify"` 的 `patch` 并入上下文重跑聚合 |

---

## 4. R8 可观测性增强：指标与质量分析

**关联缺陷**：D5（延续）　**依赖**：R5（trace 落盘，✅）　**工作量**：M　**状态**：✅（已实现：`observability/metrics.py` `aggregate_metrics` + `deps.read_metrics` + `GET /api/metrics` + `MetricsPage.tsx`；单测 `test_metrics.py`）

### 4.1 新增 `observability/metrics.py`

从一期 trace JSONL（`logs/traces/<user>/trace-*.jsonl`）**只读聚合**，不引入新存储：

```python
def aggregate_metrics(user_id: str | None = None,
                      since: str | None = None) -> dict:
    """遍历 trace JSONL，产出汇总指标。"""
    return {
        "turns": int,                       # 总回合数
        "avg_steps_per_turn": float,        # 每回合平均步数
        "tool_calls": {name: count},        # 工具调用频次
        "terminated_reason_dist": {...},    # 终止原因分布
        "safety_blocks": int,               # 安全拦截次数（safety_block 事件）
        "circuit_open": int,                # 熔断次数
        "confirm_requests": int,            # HITL 触发次数（confirm_request）
        "route_dist": {"simple": n, "composite": m},  # 分诊分布
        "subagent_stats": {name: {calls, avg_steps, avg_duration_ms, fail_rate}},
        "latency_ms": {"p50": ..., "p95": ...},        # 从 model_output.latency_ms 聚合
    }
```

- 指标口径**只依赖已落盘事件字段**（一期已写 `usage`/`latency_ms`/`status`），可复现。
- 支持按 `user_id` 与时间窗过滤。

### 4.2 接口与前端

- `GET /api/metrics?user_id=&since=` → 返回 4.1 聚合结果。
- 前端新增 `MetricsPage.tsx`：步数分布、工具频次条形图、终止原因/分诊饼图、安全拦截与熔断计数、SubAgent 表格、时延 p50/p95。复用现有 React 调试台导航加一页。

### 4.3 验收对照（R8）

| 验收标准 | 实现点 |
|---------|--------|
| 能查看一段时间内关键指标汇总 | `/api/metrics` + MetricsPage |
| 指标口径清晰可复现 | 仅从 trace JSONL 已有字段聚合，无隐藏状态 |

---

## 5. 落地顺序与里程碑

| 序 | 任务 | 依赖 | 验证 | 状态 |
|----|------|------|------|------|
| 1 ✅ | `tools/tool_filter.py`（白名单策略，registry `allowed` 已在 P0-4 落地） | Phase 2.0 | SubAgent 授权集合计算单测 | ✅ |
| 2 ✅ | `agents/sub_agent.py`（隔离执行 + 摘要） | 1 | 子代理只用授权工具、独立 messages 单测 | ✅ |
| 3 ✅ | `wellnessagent/subagents/*`（4 个专职 SubAgent + 工厂） | 2 | 各 SubAgent 单测（mock LLM） | ✅ |
| 4 ✅ | trace `EVENT_TYPES` 扩展 | — | 事件写入/脱敏单测 | ✅ |
| 5 ✅ | `wellnessagent/orchestrator.py`（分诊 + pipeline + 并行 + 聚合） | 2,3,4 | 分诊两路径 + 并行 + 失败隔离单测 | ✅ |
| 6 ✅ | `agent.py` 装配 orchestrator（simple 走原路径 / composite 走编排） | 5 | 端到端一轮复合请求 | ✅ |
| 7 ✅ | `core/hitl.py` + SessionStore 挂起态 + `/api/chat` confirmation 恢复（R7） | 5,6 | 挂起→恢复→影响输出单测 | ✅ |
| 8 ✅ | `streaming.py` 新增 CONFIRM/SUBAGENT_*/ORCHESTRATOR_* + 前端消费 | 6,7 | 事件序列 + 前端渲染 | ✅ |
| 9 ✅ | `observability/metrics.py` + `/api/metrics` + MetricsPage（R8） | 4 | 聚合口径单测 + 面板 | ✅ |
| 10 | `benchmark/evaluators/orchestration.py` + 单体 vs 编排 A/B 基线 | 6 | 约束遵守率不下降对照 | ➡️ **顺延至三期 R14**（见 `tech-design-phase3.md` §7） |

> 分批合入：1–4（纯新增，零风险）→ 5–6（编排接入，中风险，须回归一期 simple 路径）→ 7–8（HITL+流式）→ 9–10（指标+评测）。

---

## 6. 对外接口契约

### 6.1 保持

- `POST /api/chat` 仍返回一期字段 `{answer, terminated_reason, steps, state, session_id, trace_id, safety}`；`terminated_reason` 对外取值集合不变（`awaiting_confirmation` 仅作内部/ trace 标记，对外映射 `finished`）。
- `ReActAgent.run()/run_with_trace()/stream_run()`、`WellnessPlanningAgent.chat_with_trace()/chat_stream()` 签名保持。
- simple 路径与一期完全一致。

### 6.2 变更 / 新增

- `/api/chat`、`/api/chat/stream` 请求新增可选 `confirmation={confirm_id, decision, patch?}`。
- 响应新增 `orchestration={route, subagents:[{name, success, summary, steps, duration_ms}]}` 与可选 `confirmation={confirm_id, kind, prompt, payload}`。
- 新增流式事件：`ORCHESTRATOR_TRIAGE`、`SUBAGENT_START`、`SUBAGENT_RESULT`、`ORCHESTRATOR_AGGREGATE`、`CONFIRM`。
- 新接口：`GET /api/metrics`。

### 6.3 新增环境变量

| 变量 | 默认 | 作用 |
|------|------|------|
| `WELLNESS_ORCHESTRATION_ENABLED` | true | 总开关；关闭则永远走一期单体路径 |
| `WELLNESS_TRIAGE_MODE` | llm | `llm`/`rule`/`always_composite`/`always_simple` |
| `WELLNESS_SUBAGENT_MAX_STEPS` | 6 | 子代理最大步数 |
| `WELLNESS_SUBAGENT_PARALLELISM` | 2 | 并行线程数（profile+retrieval） |
| `WELLNESS_HITL_ENABLED` | true | HITL 确认总开关 |
| `WELLNESS_HITL_SENSITIVE_FIELDS` | allergies,medical_notes,diet_pattern | 触发画像确认的敏感字段 |

---

## 7. 风险与兼容性

1. **编排接入不能污染 simple 路径**（最高优先）：`route=="simple"` 必须与一期字节级等价。缓解：总开关 + 分诊 `always_simple` 档跑一期全量回归（186 单测必须仍绿）。
2. **并行线程安全**：SubAgent 各自独立实例/registry view，`service` 仅读；严禁在并行子任务里写画像/记忆（写只在主线程主循环）。`memory_tool.current_session_id` 等可变状态不在子线程改。
3. **上下文膨胀**：子代理必须只回**摘要**给 Orchestrator，绝不把子代理完整 messages 灌回主 messages（否则 R15 token 治理形同虚设）。
4. **HITL 挂起态一致性**：挂起态存 `SessionStore.metadata`，恢复时校验 `confirm_id` 匹配；不匹配/过期则忽略确认按新请求处理。
5. **成本/延迟**：composite 最坏 6+ 次 LLM。缓解：profile+retrieval 并行、子代理小 `max_steps`、聚合与分诊用轻量模型（复用 `service._get_distill_llm()` 可空则降级）。
6. **安全红线不失守**：safety SubAgent 可被聚合"采纳/部分采纳"，但一期 `DietGuardrails._finalize` 硬红线**始终在最后跑**，不受编排影响。
7. **trace 体积**：子代理事件增多，JSONL 变大。缓解：子代理 `model_output` 只记 usage/预览，完整输出仍走一期 `logs/tool-output/` 截断机制。

---

## 8. 验收与度量（第二期整体）

- **功能**：R6/R7/R8 各"验收对照"逐条通过。
- **回归**：一期 186 单测保持绿；simple 路径与一期等价。
- **编排可见**：复合请求在 trace 与前端 OrchestrationPanel 可见子任务/分派/聚合。
- **权限隔离**：SubAgent 调用未授权工具被拒（单测覆盖）。
- **HITL**：敏感画像变更/安全风险触发确认，确认/修正影响后续输出。
- **指标**：`/api/metrics` 能出步数/工具/终止原因/分诊/SubAgent/时延汇总，口径可复现。
- **A/B（预备 R14）**：单体 vs 编排在同 task 集上约束遵守率对照，编排不下降。

---

## 9. 评审待确认 / 后续可选项

1. **知识库语料扩充**（本期先不急）：`retrieval` SubAgent 在当前 ~5KB 语料下价值有限；后续扩到几十篇后其隔离检索价值与 RAG 高级检索（MQE/HyDE/引用）才充分显现，届时可作为 R14 A/B 的对照维度。
2. **HITL 真暂停（步骤中间）**：如未来有强需求，可在 `chat_stream` 引入异步生成器 + resume token 的真中断，届时单独立项（改执行模型，非本期范围）。
3. **SubAgent 动态编排**：当前为固定 DAG；若要 LLM 动态决定子任务拓扑，可在 Orchestrator 加"规划器"层（成本/不确定性上升，评测后再定）。

