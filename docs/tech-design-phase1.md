# WellnessAgent 第一期技术方案（止血 / 工程稳健性 + ReAct 内核重构）

<!-- 图例：✅ 已实现并有单测　🟡 部分实现　⬜ 未实现 -->

## 📌 实现进度总览（截至当前）

> 本节反映代码库实际状态，与下文各章节的 `【状态】` 标记一致；离线单测位于 `tests/`（当前 **186 passed**）。

| 模块/需求 | 交付物 | 状态 | 单测 |
|-----------|--------|------|------|
| **R1** 原生 FC + 累积 messages | `core/llm.py`(`invoke_with_tools`/`supports_function_calling`)、`core/llm_response.py`、`agents/react_agent.py`（内核重写） | ✅ | `test_react_agent.py`(20)、`test_llm_response.py` |
| **R1** 结构化工具协议 | `tools/response.py`、`tools/registry.py`(`execute_tool→ToolResponse`/`build_tool_schemas`/`execute_tool_text`) | ✅ | `test_tool_response.py`、`test_registry.py`(23) |
| **R1** 内置 `finish` 工具结束判定 | `react_agent`（FC & JSON 回退双路径对齐） | ✅ | `test_react_agent.py` |
| **R1** `profile_set` 结构化多参数 | `wellnessagent/agent.py` + `registry._function_schema` | ✅ | `test_registry.py` |
| **R3** Guardrails 安全校验 | `wellnessagent/guardrails.py` + `react_agent._finalize` 钩子 | ✅ | `test_guardrails.py`、`test_react_agent.py` |
| **R5** Trace 落盘（JSONL+HTML+脱敏） | `observability/trace_logger.py` | ✅ | （随内核集成验证） |
| **R15** 上下文工程（Token/轮次压缩/截断） | `core/context.py` + `react_agent._maybe_compress` | ✅ | `test_context.py`、`test_react_agent.py` |
| **R16** 熔断器 | `tools/circuit_breaker.py` + `registry` 接入 | ✅ | `test_circuit_breaker.py`、`test_registry.py` |
| **R17** 会话持久化 | `core/session_store.py` + `agent.py`(save/load/new_session) | ✅ | `test_session_store.py` |
| **D4** timestamp 修复 + FC 字段 | `core/message.py` | ✅ | `test_message.py` |
| 三层记忆边界（删手工滑窗） | `agent.py`（`_recent_dialogue_window` 从 messages 派生） | ✅ | — |
| **R2** 流式输出（SSE） | `server/streaming.py`、`/api/chat/stream`、`agent.chat_stream`（逐 step/tool 事件 + 最终答案分段） | ✅ | `test_react_agent.py::TestStreamRun`(2) |
| **D3** Agent 缓存 LRU + 重建 | `server/deps.py`（`OrderedDict` LRU + 淘汰前 cleanup + SessionStore 重建） | ✅ | — |
| **D8** seed 幂等落盘 | `server/deps.py`（`logs/kb_seed/<user>.seeded` 指纹落盘） | ✅ | — |
| **steps 结构 + 前端 + benchmark 适配** | `frontend/`、`benchmark/schemas.py`+`evaluators/*`（去别名，换 `tool_calls[].name`） | ✅ | 前端 `tsc` 编译通过 |
| 新增 HTTP 接口 | `/api/chat/stream`、`/api/trace`、`/api/trace/list`、`/api/session/list`、`/api/session/new` | ✅ | — |

> **已交付范围**：方案落地顺序第 1–15 步全部完成——后端核心（基础设施 + 内核重写 + 安全/上下文/持久化 + 业务装配）+ 第 13 步（去 `StepRecord` 别名、前端与 benchmark 换新结构）+ 第 14 步（SSE 流式 R2）+ 第 15 步（LRU 淘汰 D3、seed 落盘幂等 D8、session/trace HTTP 接口）。离线单测 186 passed，前端 `tsc && vite build` 编译通过。

---

> 版本：v0.3（评审确认稿，在 v0.2 架构方向上锁定实现细节）
> 对应需求：`docs/requirements.md` 第一期 R1 / R2 / R3 / R4 / R5 / R15 / R16，并新增 **R17 会话持久化**（原 D11 升级为一期正式需求）
>
> **架构方向（v0.2 已定，v0.3 保持）**：
> 1. 定位修正——`hello_agents` 源码是**参考资料**，不是"上游依赖"。本项目**完全自研、不跟随、不同步** hello-agents，实现按 WellnessAgent 自己的接口来写，参考其思路但不受其约束。
> 2. ReAct 内核**彻底废弃"每步重建 prompt + 文本-JSON 解析"**，重写为 **累积 messages 数组 + 原生 Function Calling** 的标准对话架构（对齐 2025 主流 agent），保留 JSON 解析器仅作 Ollama 回退。
> 3. **D11 会话持久化升级为一期 R17**：messages 按 user_id 落盘，重启可续。
> 4. R15 上下文工程改回**标准轮次压缩**（累积 messages 天然适配 `HistoryManager.compress`）。
> 5. `StepRecord`（trace 步骤结构）**重新设计**，从 messages 派生，前端与 benchmark 连带改造。
>
> **v0.3 锁定的实现细节（本轮评审确认）**：
> 1. **无过渡期**：内核重写 + `StepRecord` 新结构 + 前端 + benchmark 作为**一个不可分割的提交单元**一次到位，不写临时兼容 shim（落地 8、13 合为一批）。
> 2. **结束判定用内置 `finish` 工具**（非"无 tool_calls 即终止"的轻语义）：所有 agent 内置 `finish(answer: string)`；FC 路径模型调用 `finish` 即终止，回退路径解析 JSON 的 `finish` action，二者语义对齐，对外 `terminated_reason` 仍映射为 `finished`。
> 3. **`profile_set` 拆为结构化多参数**：FC 下暴露显式字段 schema（list 字段用 array，其余 string/number），底层仍复用 `service.profile_set`。
> 4. **R15 压缩口径**：`compression_threshold=0.8`、`WELLNESS_MIN_RETAIN_ROUNDS=6`、**以"轮"为最小压缩单位**（整轮进出，绝不拆散 `assistant(tool_calls)` 与配对 `tool` 消息）。
> 5. **会话粒度统一**：对话 `session_id` 与 working memory `session_id` **统一为同一个 id**，"新开会话"= 对话历史清空 + working memory `clear_session()` 同步；长期记忆（画像/提纯）按 `user_id` 跨会话保留。
> 6. **Guardrails 默认 `rule` 档**；R15 智能摘要与 Guardrails `rule_llm` 层**复用 `service._get_distill_llm()`**（可空则降级：智能摘要退统计摘要、`rule_llm` 退纯规则）。

---

## 0. 背景与定位

### 0.1 关于 hello-agents 的定位（重要）

WellnessAgent 早期从 hello-agents 复制了部分可用源码作为起点，但**本项目是独立演进的作品，不以 hello-agents 为上游依赖**：

- 不 `import hello_agents`，不跟随其版本，不做同步。
- hello-agents 的实现（FC 循环、ToolResponse、SessionStore、上下文三件套等）作为**设计参考**，WellnessAgent 按自己的接口与场景重新实现，可自由取舍与改良。
- 因此本文所有能力均为 **WellnessAgent 自研实现**，参考成熟范式但不受其接口束缚。

### 0.2 本期的核心动作

第一期不再是"打补丁"，而是**借 R1 的契机把 ReAct 内核换成干净的、面向二期（Orchestrator/SubAgent/HITL/多模态）的架构**：

- **旧内核（废弃）**：每步用 `current_history: list[str]` 拼 `"Thought/Action/Observation"` 字符串 + `build_additional_context` 重建整个 prompt，单条 `user` 消息发出，模型按 JSON 协议输出，正则/JSON 解析。
- **新内核**：`messages` 数组累积（system + user + assistant + tool），原生 `tool_calls` 驱动，模型 API 保证结构。Ollama 等不支持 FC 的走 JSON 回退，但**同样基于累积 messages**。

### 0.3 三层"会记东西"的系统边界（务必先厘清）

改成累积 messages 后，项目里有三套持久/半持久系统，边界必须清晰、互不覆盖：

| 系统 | 存什么 | 组织维度 | 存储 | 生命周期 | 本期动作 |
|------|--------|---------|------|---------|---------|
| **SessionStore**（新, R17） | 当前会话原始对话消息（system/user/assistant/tool 全量，含 `tool_call_id`） | 按 `session_id`（隶属 user_id） | `logs/sessions/<user_id>/<session_id>.json` | 一次会话；可跨重启续上 | 新建 |
| **episodic 长期记忆**（现有） | 跨会话稳定信息：结构化画像、提纯偏好 | 按 `user_id` | SQLite + Qdrant（`memory/storage/`） | 长期，重启不丢 | 保持 |
| **working memory**（现有） | 本会话临时上下文（今天/本轮条件） | 按 `session_id` | 内存 + TTL | 短期 | 保持 |

**关键边界规则**：
1. **对话历史只由 messages 数组承担**（+SessionStore 落盘）。**删除** `WellnessPlanningAgent._format_recent_dialogue_window` 那套手工滑窗——它与 messages 数组重复。
2. **画像 / 提纯记忆 / working 摘要** 仍注入，但注入到 **system 消息**中，每轮开始时刷新一次，**不再每步重复注入**（省 token、缓存友好）。
3. 三套系统各自独立持久化，SessionStore 不碰长期记忆，反之亦然。

### 0.4 本期落点文件总览

```
core/
  llm.py            ← 新增 invoke_with_tools / astream_invoke；invoke 保持兼容
  llm_response.py   ← 新增（LLMToolResponse / ToolCall）
  message.py        ← 修复 timestamp（D4）；扩展 tool_calls/tool_call_id 字段；to_dict/from_dict
  context.py        ← 新增（TokenCounter / HistoryManager / ObservationTruncator）
  session_store.py  ← 新增（会话消息落盘，R17）
tools/
  response.py       ← 新增（ToolResponse / ToolStatus / ToolErrorCode）
  registry.py       ← execute_tool 返回 ToolResponse；build_tool_schemas；接入熔断器
  circuit_breaker.py← 新增（CircuitBreaker）
observability/
  trace_logger.py   ← 新增（TraceLogger，JSONL+HTML）
agents/
  react_agent.py    ← 【重写】累积 messages + FC 主循环；JSON 回退；trace；上下文压缩；Guardrails 钩子
wellnessagent/
  guardrails.py     ← 新增（安全校验层，R3）
  prompts.py        ← 【拆分】FC 版瘦身 system prompt + Ollama 回退版 JSON 说明
  agent.py          ← 重写上下文注入（注入 system 消息）；组装 session/trace/guardrails/context
  service.py        ← 删除对话窗口滑窗依赖（对话交给 messages）；其余记忆/RAG 逻辑保留
  schemas.py        ← StepRecord 新结构（从 messages 派生）
  server/
    deps.py         ← _AGENTS LRU（淘汰不丢数据，配合 SessionStore 重建）；seed 幂等落盘（D8）
    main.py         ← /api/chat 结构升级；/api/chat/stream（SSE, R2）；/api/trace；/api/session
    streaming.py    ← 新增（SSE 事件）
frontend/src/
  types.ts, components/TracePanel.tsx, ToolCallCard.tsx, ChatPage.tsx ← 适配新 steps + SSE + 安全/熔断
benchmark/
  evaluators/tool_usage.py 等 ← 适配新 steps 结构
```

---

## 1. 整体架构（新内核）

### 1.1 一次对话的数据流

```
POST /api/chat(/stream)  {user_id, message, session_id?}
      │
      ▼
deps.get_agent(user_id)               # 内存 LRU；未命中则新建
   └─ 若指定/存在活跃 session → SessionStore.load(session_id) 恢复 messages (R17)
      │
      ▼
WellnessPlanningAgent.chat / chat_stream
   (1) 刷新 system 消息：角色 + 安全要求 + 画像 + 提纯记忆 + working 摘要   (边界规则 2)
   (2) messages += user(message)
      │
      ▼
ReActAgent 主循环（累积 messages）
   loop until finish / max_steps:
      (3) TokenCounter 估算 messages；超阈值 → HistoryManager.compress()   (R15)
      (4) FC 路径:  resp = llm.invoke_with_tools(messages, tools)           (R1)
          回退路径:  resp = llm.invoke(messages) + JSON 解析 → tool_calls    (R1, Ollama)
      (5) 无 tool_calls → resp.content 即 final，跳出
          有 tool_calls:
             messages += assistant(content, tool_calls)
             for tc in tool_calls:
                CircuitBreaker.is_open? → ERROR 跳过                         (R16)
                resp = registry.execute_tool(tc) → ToolResponse             (R1)
                ObservationTruncator.truncate(resp.text)                    (R15)
                CircuitBreaker.record_result                                (R16)
                messages += tool(tool_call_id, content)
      (6) TraceLogger.log_event(...)                                        (R5)
      │
      ▼
终止前: Guardrails.check(final, profile) → 改写/拦截 + trace               (R3)
SessionStore.save(user_id, session_id, messages)                          (R17)
TraceLogger.finalize() → JSONL + HTML                                      (R5)
返回 {answer, steps(新结构), terminated_reason, state, session_id, trace_id, safety}
```

### 1.2 与旧内核的关键差异

| 项 | 旧内核（废弃） | 新内核 |
|----|------|------|
| 历史载体 | `current_history: list[str]`（拼字符串） | `messages: list[dict]`（标准角色消息） |
| 每步 prompt | 重建整个 prompt | 只 append 新消息 |
| 上下文注入 | 每步重复注入 additional_context | 注入 system 消息，每轮刷新一次 |
| 工具调用 | 模型输出 JSON → 正则/JSON 解析 | 原生 `tool_calls`（回退才解析 JSON） |
| 结束判定 | `Finish[...]` 文本 | 无 `tool_calls` 的 assistant 回复 |
| 上下文治理 | 无 | TokenCounter + HistoryManager 压缩 + Truncator |
| 持久化 | 无 | SessionStore 落盘 |

---

## 2. R1 原生 Function Calling + 累积 messages（核心重构）

**关联缺陷**：D2　**工作量**：**L**（内核重写，非增补）　**【状态】✅ 已实现（`core/llm.py`、`core/llm_response.py`、`tools/response.py`、`tools/registry.py`、`agents/react_agent.py`；单测 `test_react_agent.py`/`test_registry.py`/`test_tool_response.py`/`test_llm_response.py`）**

### 2.1 新增 `tools/response.py`

```python
class ToolStatus(Enum):
    SUCCESS = "success"; PARTIAL = "partial"; ERROR = "error"

@dataclass
class ToolResponse:
    status: ToolStatus
    text: str                       # 给 LLM 阅读
    data: dict = field(default_factory=dict)
    error_info: dict | None = None
    stats: dict | None = None
    @classmethod
    def success(cls, text, data=None, stats=None): ...
    @classmethod
    def partial(cls, text, data=None, stats=None): ...
    @classmethod
    def error(cls, code, message, stats=None): ...

class ToolErrorCode:
    EXECUTION_ERROR="EXECUTION_ERROR"; INVALID_PARAM="INVALID_PARAM"
    NOT_FOUND="NOT_FOUND"; CIRCUIT_OPEN="CIRCUIT_OPEN"
```

### 2.2 新增 `core/llm_response.py`

```python
@dataclass
class ToolCall:
    id: str; name: str; arguments: str      # arguments 为 JSON 字符串
@dataclass
class LLMToolResponse:
    content: str | None
    tool_calls: list[ToolCall]
    model: str
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
```

### 2.3 `core/llm.py` 新增（`invoke()` 返回 `str` 保持不变，供蒸馏/回退复用）

```python
def invoke_with_tools(self, messages, tools, tool_choice="auto", **kwargs) -> LLMToolResponse:
    resp = self._client.chat.completions.create(
        model=self.model, messages=messages, tools=tools, tool_choice=tool_choice,
        temperature=kwargs.get("temperature", self.temperature),
        max_tokens=kwargs.get("max_tokens", self.max_tokens))
    msg = resp.choices[0].message
    tool_calls = [ToolCall(tc.id, tc.function.name, tc.function.arguments)
                  for tc in (msg.tool_calls or [])]
    return LLMToolResponse(msg.content, tool_calls, self.model,
                           resp.usage.model_dump() if resp.usage else {})

def supports_function_calling(self) -> bool:
    # 白名单 openai/deepseek/qwen/modelscope/kimi/zhipu → True
    # ollama/vllm/local → False
    # WELLNESS_FORCE_FUNCTION_CALLING 可强制覆盖
```

### 2.4 `core/message.py` 扩展（承载 FC 消息，兼顾 D4）

累积 messages 需要 `assistant` 消息携带 `tool_calls`、`tool` 消息携带 `tool_call_id`。`Message` 扩展可选字段：

```python
class Message(BaseModel):
    content: str | None
    role: MessageRole                         # user/assistant/system/tool/summary
    timestamp: datetime = Field(default_factory=datetime.now)   # D4 修复
    metadata: dict | None = Field(default_factory=dict)
    tool_calls: list[dict] | None = None      # assistant 发起的调用
    tool_call_id: str | None = None           # tool 消息对应的调用 id
    name: str | None = None                   # tool 名（可选）
    def to_openai(self) -> dict: ...           # 转 OpenAI messages 元素（含 tool_calls/tool_call_id）
    def to_dict(self) -> dict: ...             # 落盘用（全字段）
    @classmethod
    def from_dict(cls, d) -> "Message": ...
```

> 兼容：`Message("x","user")` 位置参数构造必须继续可用（现有大量调用）。

### 2.5 `tools/registry.py` 改造

1. `execute_tool()` 返回 **`ToolResponse`**；函数工具（`register_function`）的 `str` 结果包裹为 `success`，抛异常为 `error(EXECUTION_ERROR)`，不存在为 `error(NOT_FOUND)`。
2. **`register_function` 升级支持显式参数 schema**：新增可选 `parameters: list[dict] | None` 入参（OpenAI schema 的 `properties` 片段）。未提供时默认沿用单参数 `input:string`（贴合大多数业务工具的字符串入参）；提供时按结构化多参数暴露给 FC（见 ③ `profile_set`）。回退路径把结构化参数序列化回底层可接受的形式。
3. 新增 `build_tool_schemas() -> list[dict]`：`register_function` 工具按其 `parameters` 生成 schema（无则单参数 `input:string`）；`Tool` 对象复用 `to_openai_schema()`。**内置 `finish` 工具**（`finish(answer: string)`）由此统一注入 schema 列表（见 2.6）。
4. 新增 `execute_tool_text()` 返回 `str`（`= execute_tool().text`），供回退路径/benchmark 平滑用。

**③ `profile_set` 结构化多参数**：`build_tool_schemas` 为 `profile_set` 暴露显式字段——list 字段 `allergies/dislikes/medical_notes` 用 `array<string>`，text 字段 `diet_pattern/goal/notes` 用 `string`（数值类字段若后续补充按 `number`）。ReAct 执行时把结构化 `arguments` 转回 `field=value;field=value` 字符串，复用底层 `service.profile_set`（`service` 零改动）。其余业务工具（`kb_search`/`memory_*` 等）保持单 `input:string`。

### 2.6 `agents/react_agent.py`【重写】

废弃 `current_history`、`_extract_action*`、每步 prompt 重建。核心循环见 1.1。两条路径**共享累积 messages 逻辑**，仅"如何得到 tool_calls"不同：

- **FC 路径**：`llm.invoke_with_tools(messages, tools)` 原生返回 `tool_calls`（`tools` 含业务工具 + 内置 `finish`）。
- **回退路径（Ollama）**：`llm.invoke(messages)` 得文本 → `_parse_json_output` 解析出 `{name, input}` 或 `finish` → 构造成 `ToolCall(id=uuid, ...)` → 后续逻辑与 FC 路径完全一致（append assistant + tool 消息）。

- **结束判定（内置 `finish` 工具，②确认）**：无论哪条路径，**模型调用 `finish(answer=...)` 即终止**，`answer` 作为最终答案。不采用"无 tool_calls 即终止"的轻语义——避免模型偶发空 content 收尾被误判。若某步既无 tool_calls 又非 finish（FC 下罕见），把该步 `content` 视作兜底最终答案并记 `invalid_action` 之外的正常收尾（trace 标注 `implicit_finish`）。
- **`finish` 不真正"执行"**：ReAct 循环识别到 `finish` 调用直接取 `answer` 收尾，不产生 `tool` 观察消息（也不写回 messages 作为工具结果）。
- **终止原因**保持对外集合 `finished/invalid_action/max_steps/llm_empty_response`；新增 `fc_fallback`/`implicit_finish` 仅作 trace 标记，不改对外取值集合。

### 2.7 `prompts.py`【拆分】

- **FC 版 system prompt（主）**：删除所有"输出 JSON 对象/action.type/JSON 协议硬约束"等格式教学（100+ 行），只保留：角色定位、健康安全要求、工具使用策略、字段语义边界、最终回答复核。格式由 API 的 tools 机制保证。
- **回退版 JSON 说明（Ollama 专用）**：保留一份**精简**的 JSON 工具调用格式说明，仅在 `supports_function_calling()=False` 时拼接到 system prompt 末尾。

### 2.8 验收对照（R1）

| 验收标准 | 实现点 |
|------|------|
| 支持的 provider 不依赖文本正则触发工具 | 2.3/2.6 FC 路径 |
| benchmark 工具使用类不低于改造前 | 回退保留 + 业务工具语义不变；跑 `benchmark/cli.py` 对照 |
| ≥1 个不支持原生协议 provider 走回退 | Ollama：`supports_function_calling()=False` |
| 失败返回 ToolResponse.ERROR 而非裸异常 | 2.5 registry try/except |

---

## 3. R17 会话持久化（原 D11 升级）

**关联缺陷**：D11　**工作量**：M（新增一期正式项）　**【状态】✅ 已实现（`core/session_store.py` + `agent.py` save/load/new_session/select_session；单测 `test_session_store.py`）。与第 8.2 节 D3 LRU 的自动重建已接入（`deps.get_agent` 缓存未命中时 `load` 最近会话）**

### 3.1 新增 `core/session_store.py`

```python
class SessionStore:
    def __init__(self, session_dir="logs/sessions"): ...
    def save(self, user_id, session_id, messages: list[Message], metadata: dict) -> str
    def load(self, user_id, session_id) -> dict           # {messages, metadata, ...}
    def latest_session_id(self, user_id) -> str | None    # 最近活跃会话
    def list_sessions(self, user_id) -> list[dict]        # session_id + 时间 + 轮数
    def new_session_id(self) -> str                       # s-YYYYMMDD-HHMMSS-xxxx
```

- 落盘：`logs/sessions/<user_id>/<session_id>.json`，内容 `{session_id, user_id, created_at, updated_at, messages:[...], metadata}`。
- `messages` 用 `Message.to_dict()` 全字段序列化，**必须含 `tool_calls`/`tool_call_id`**（否则续上时 FC messages 结构非法）。
- 一个 user 可有多个 session；`/api/chat` 不带 `session_id` 时默认续 `latest_session_id`，前端可"新开会话"。

### 3.2 会话粒度：对话 session 与 working memory session 统一（⑤确认）

**两套 session_id 统一为同一个 id**，心智模型：`session = 一次对话 = 对话历史 + working memory 同生共死`；`user = 跨会话的长期画像/记忆`。

- **现状**：`memory_tool.current_session_id`（`session_YYYYMMDD_HHMMSS`）自成一套；对话历史原本无独立 session（靠内存 agent 累积）。
- **统一规则**：
  - `/api/chat` 不带 `session_id` → 续 `latest_session_id`，并把 `memory_tool.current_session_id` 设为同一个 id。
  - `/api/session/new`（"新开会话"）→ `new_session_id()` 生成新 id，同时 `memory_tool.clear_session()` + 重置 `current_session_id` 为新 id；即**新开对话 = 对话历史清空 + working memory 清空**。
  - **长期记忆（episodic/画像/提纯）不受影响**，按 `user_id` 跨 session 保留。
- **理由**：两套 id 各走一套会导致"新开对话但 working memory 残留上一会话临时上下文"的诡异现象；统一后语义干净。

### 3.3 与 D3 LRU 的配合（见第 8.2 节）

淘汰内存 Agent 时数据不丢（已落盘），用户再来时 `load` 重建（重建同时恢复 `current_session_id`，保证 working memory 与对话 session 一致）。

### 3.4 验收对照（R17）

- 服务重启后，带原 `session_id` 请求能续上多轮上下文。
- 新开会话后，对话历史与 working memory 同时清空；长期记忆（画像/提纯）不受影响。
- 长期记忆与会话消息各自独立恢复。

---

## 4. R15 上下文工程（标准轮次压缩）

**关联缺陷**：D9　**工作量**：M　**【状态】✅ 已实现（`core/context.py` 三件套 + `react_agent._maybe_compress`/`_build_compression_summary`；单测 `test_context.py`、`test_react_agent.py::TestCompression`）**

### 4.1 新增 `core/context.py`

- `TokenCounter(model)`：`count_message/count_messages/count_text`，带缓存增量；无 tiktoken 降级 `len//4`。（`requirements.txt` 加 `tiktoken`）
- `HistoryManager(min_retain_rounds=6, compression_threshold=0.8)`：`append/get_history/estimate_rounds/find_round_boundaries/compress(summary)`。**因为改成累积 messages，轮次压缩现在天然适用**。
- `ObservationTruncator(max_lines,max_bytes,direction,output_dir)`：`truncate()` 返回 `{truncated, preview, full_output_path, stats}`。

### 4.2 接入点与压缩口径（④确认）

1. **工具输出截断**：`execute_tool` 得 `ToolResponse.text` → `truncate()` 取 `preview` 再 append 到 `tool` 消息；完整输出落 `logs/tool-output/`。
2. **历史压缩（阈值 0.8 / 保留 6 轮 / 整轮压缩）**：
   - **触发**：每步调 LLM 前，`TokenCounter` 估算 `messages`；超 `context_window * 0.8`（256k 下≈209k）才触发。256k 窗口下压缩几乎不会触发，这套是**保险丝**而非常态路径，故阈值取高避免误伤。
   - **保留**：`WELLNESS_MIN_RETAIN_ROUNDS=6`，早期轮次压成一条 `summary`（作为 `system` 或 `assistant` 消息）插在被压缩轮位置。
   - **整轮为最小单位（硬约束，否则 API 400）**：一"轮" = 一条 `user` + 其后所有 `assistant(tool_calls)`/`tool`/最终 `assistant`，**整轮一起进/出压缩**，绝不拆散 `assistant(tool_calls)` 与配对 `tool` 消息。
   - **两种摘要都支持**（早前确认）：`WELLNESS_SMART_COMPRESSION=true` 走 LLM 智能摘要（**复用 `service._get_distill_llm()`**，可空则降级）；`false`（默认）走统计摘要（轮数/工具调用/关键实体统计）。
3. token 偏差目标 <5%，落 trace `model_output.usage`。

### 4.3 验收对照（R15）

- 长对话不超 token：轮次压缩 + 工具截断。
- 压缩以整轮为单位，FC 消息结构保持合法（无 400）。
- 压缩后质量不显著下降：benchmark 状态跟踪类 evaluator 对照；两种摘要均验。
- token 偏差 <5%：tiktoken + usage 比对。

---

## 5. R5 可观测性：Trace 落盘

**关联缺陷**：D5　**工作量**：S　**【状态】✅ 已实现（`observability/trace_logger.py`，JSONL+HTML+脱敏，`react_agent` 全事件接入）。`GET /api/trace`、`/api/trace/list` HTTP 接口已在 `main.py` + `deps.list_traces/read_trace` 实现**

### 5.1 新增 `observability/trace_logger.py`

双格式（JSONL+HTML）、自动脱敏、`finalize()` 统计。事件：`session_start/message_written/model_output/tool_call/tool_result/circuit_open/safety_block/error/session_end`。

### 5.2 目录与接口

- 落盘：`logs/traces/<user_id>/trace-<session_id>.jsonl(.html)`；trace_id=session_id 随响应返回。
- `GET /api/trace?user_id=&session_id=`、`GET /api/trace/list?user_id=`。
- **trace（过程回放）与 SessionStore（对话续接）分离**：trace 存"这一轮怎么推理的"，SessionStore 存"对话消息本身"，用途不同。

### 5.3 验收对照（R5）

一轮结束可查完整 trace；能按 user/session 回放。

---

## 6. R2 流式输出（SSE）

**关联缺陷**：D1　**工作量**：S　**【状态】✅ 已实现（`server/streaming.py` 事件与 SSE 序列化 + `agent.chat_stream` 逐 step/tool 事件 + 最终答案分段 + `/api/chat/stream` `StreamingResponse` + 前端 `ChatPage` SSE 消费；一期不做逐 token，final_answer 按块切片）**

### 6.1 新增 `wellnessagent/server/streaming.py`

`StreamEventType`：`AGENT_START/STEP_START/THINKING/TOOL_CALL_START/TOOL_CALL_FINISH/LLM_CHUNK/SAFETY/AGENT_FINISH/ERROR`；`StreamEvent.to_sse()`。

### 6.2 Agent 侧 `chat_stream(user_input, session_id?) -> AsyncGenerator[StreamEvent]`

累积 messages 循环中，每步 yield step/tool 事件；最终答案分段 yield `LLM_CHUNK`。进阶可接 `astream_invoke` 逐 token（二期）。

### 6.3 服务侧 `/api/chat/stream`

`StreamingResponse(media_type="text/event-stream")`，设 `Cache-Control: no-cache`、`X-Accel-Buffering: no`；保留非流式 `/api/chat` 供 benchmark/回退。

### 6.4 前端 `ChatPage.tsx`

`fetch`+`ReadableStream` 消费：thinking→状态、tool_call_finish→追加 step、llm_chunk→拼接气泡、agent_finish→刷新 `/api/state`、error→提示。保留非流式开关。

### 6.5 验收对照（R2）

首反馈早于最终答案；逐步渲染；断流/报错有 `error` 事件与前端提示。

---

## 7. R3 Guardrails 安全校验层

**关联缺陷**：D6　**工作量**：M　**【状态】✅ 已实现（`wellnessagent/guardrails.py` 规则层+LLM 层 + `react_agent._finalize` 终止前钩子；单测 `test_guardrails.py`、`test_react_agent.py::TestGuardrailsHook`）。⚠️ 流式 `SAFETY` 事件依赖 R2，未实现**

### 7.1 新增 `wellnessagent/guardrails.py`

```python
@dataclass
class GuardrailResult:
    action: str          # "pass" | "rewrite" | "block"
    reason: str
    hits: list[str]
    safe_text: str
class DietGuardrails:
    def __init__(self, llm=None, mode="rule"):  # mode: off/rule/rule_llm
        ...
    def check(self, answer: str, profile: WellnessProfile) -> GuardrailResult: ...
```

**两层校验**：
1. **规则层（必开）**：从 `profile.allergies + dislikes + medical_notes` 抽风险词，归一化/别名/子串匹配（"花生"命中"花生酱/花生油"）。命中即风险，保证确定性红线不漏放。
2. **LLM 层（`mode=rule_llm` 才开，默认关）**：轻量 LLM 判定语义级隐性风险，输出 `{risk, hits, reason}`。

**默认档与 LLM 复用（⑥确认）**：`WELLNESS_GUARDRAILS_MODE` **默认 `rule`**（零额外 LLM 调用、零额外时延）；`rule_llm` 实现好但默认关，灰度评测误拦率后再定是否默认开。`rule_llm` 层的 `llm` **复用 `service._get_distill_llm()`**（与 R15 智能摘要同一入口）；该入口可空时 `rule_llm` 自动降级为纯规则层，不新建 client。

**处置**：`rewrite`（前置安全警示 + 剔除/替换风险项，必要时 LLM 重写）；`block`（无法安全改写时拦截 + 建议咨询医生/营养师）。

### 7.2 接入点

- `ReActAgent` 返回 final 之前调 `check()`，用 `safe_text` 替换对外答案。
- 写 trace（`event="safety_block"`，含 action/hits/reason）。
- 流式：`agent_finish` 前 check，命中则发 `SAFETY` 事件 + 修正后的 `agent_finish`。

### 7.3 误伤控制

仅匹配 profile 中用户显式声明的风险词；`WELLNESS_GUARDRAILS_MODE=off|rule|rule_llm` 三档评测误拦率。

### 7.4 验收对照（R3）

过敏原诱导用例被拦/改写；事件 trace 可见；正常请求不误伤（默认 rule 档）。

---

## 8. R16 熔断器 & R4 缺陷修复

### 8.1 R16 熔断器（关联 D10，工作量 S）　**【状态】✅ 已实现（本节下文保持）（`tools/circuit_breaker.py` + `registry.execute_tool` 接入；单测 `test_circuit_breaker.py`、`test_registry.py::TestCircuitBreakerIntegration`）**

新增 `tools/circuit_breaker.py`：`is_open/record_result/get_status/get_all_status`，状态机 Closed→Open→(recovery_timeout)→Closed。接入 `registry.execute_tool`：熔断中返回 `error(CIRCUIT_OPEN)` 跳过；按 `ToolResponse.ERROR` 计数。**仅真实异常计 ERROR**，业务工具的 `⚠️/错误：` 前缀软失败仍算 SUCCESS 文本（避免误熔断）。配置 `WELLNESS_CB_*`，事件写 trace。

验收：连续失败 N 次被跳过；恢复后可用；trace 可见。

### 8.2 R4 缺陷修复（关联 D3/D4/D8，工作量 S）　**【状态】✅ 已实现：D4（`message.py` + `test_message.py`）；D3（`deps.py` `OrderedDict` LRU + `WELLNESS_MAX_AGENTS` + 淘汰前 `cleanup()` 落盘 + 未命中 `load` 重建）；D8（`deps.py` 知识库指纹落盘 `logs/kb_seed/<user>.seeded`，指纹一致跳过、变化重 seed）**

- **D4（`core/message.py`）**：`timestamp = Field(default_factory=datetime.now)`；保留位置参数构造兼容（见 2.4）。
- **D3（`server/deps.py`）**：`_AGENTS` 换 LRU（`OrderedDict`+`move_to_end`），上限 `WELLNESS_MAX_AGENTS`(默认 50)。**淘汰不丢数据**——淘汰前 `SessionStore.save`，淘汰时 `agent.cleanup()`；用户再来 `get_agent` 时从 SessionStore `load` 重建。可选"空闲超时淘汰"替代纯 LRU（`WELLNESS_AGENT_IDLE_TTL`）。
- **D8（seed 幂等）**：seed 后写落盘标记 `logs/kb_seed/<user_id>.seeded`（记知识库文件指纹）；`ensure_knowledgebase_seeded` 先查标记且指纹一致则跳过，指纹变化则重 seed；与 D3 淘汰解耦。

验收：无时间戳异常；缓存受控且淘汰不丢会话；重复不重复 seed。

---

## 9. steps 结构重设 + benchmark 连带改造

**这是本次架构调整的连带项，需前端 + benchmark 同步改。**　**【状态】✅ 已实现：`react_agent.StepRecord` 已去除旧别名（仅 `index/role/thought/tool_calls/tool_results/source/raw_response`）；前端 `types.ts`+`TracePanel`+`ToolCallCard`+`ChatPage` 已换新结构并支持多工具/状态/源/安全展示；benchmark `schemas.BenchmarkStep`+`evaluators/*` 已改读 `tool_calls[].name`（新增 `tool_names` 属性）**

### 9.1 新 `StepRecord`（从 messages 派生，`wellnessagent/schemas.py`）

```python
@dataclass
class StepRecord:
    index: int
    role: str                 # "assistant" | "tool"
    thought: str | None       # assistant.content（推理文本）
    tool_calls: list[dict]    # [{id, name, arguments}] 该步发起的调用
    tool_results: list[dict]  # [{tool_call_id, name, status, content}]
    source: str               # "function_calling" | "json_fallback"
    def to_dict(self) -> dict: ...
```

- `ReActRunResult` 保留 `final_answer/steps/terminated_reason`；`steps` 元素换为新结构。
- 由累积 messages 派生：遍历 messages，把 `assistant(tool_calls)` 与其后对应的 `tool` 消息聚合成一个 Step。

### 9.2 前端适配

- `frontend/src/types.ts`：`StepRecord` 类型更新。
- `TracePanel.tsx` / `ToolCallCard.tsx`：按新结构渲染（一步可含多个 tool_call/result；展示 status、source）。
- `ChatPage.tsx`：`setSteps` 消费新结构；流式下增量拼装 step。

### 9.3 benchmark 适配

- `benchmark/evaluators/tool_usage.py` 等依赖旧 `step.tool_name/tool_input` 的评测器，改读 `step.tool_calls[].name / .arguments`。
- `benchmark/baselines/*`、`runner.py` 若断言旧字段，一并更新。
- **回归口径**：改造前先跑一遍存基线到 `benchmark/reports/baselines/`，改造后对照工具使用类/状态跟踪类得分不下降。

---

## 10. 落地顺序与里程碑

| 序 | 任务 | 依赖 | 验证 | 状态 |
|----|------|------|------|------|
| 1 | `tools/response.py` | — | 单测序列化 | ✅ |
| 2 | `core/message.py`（D4 + FC 字段 + to_dict/from_dict） | — | 位置参数构造 + timestamp + tool 字段 | ✅ |
| 3 | `registry.execute_tool→ToolResponse` + `build_tool_schemas` + `execute_tool_text` | 1 | 工具走通、异常成 ERROR | ✅ |
| 4 | `tools/circuit_breaker.py` + 接入（R16） | 1,3 | 熔断/恢复单测 | ✅ |
| 5 | `observability/trace_logger.py`（R5） | — | 落盘 + 脱敏单测 | ✅ |
| 6 | `core/llm_response.py` + `llm.invoke_with_tools` + 探测（R1） | 1 | 云 FC 通、Ollama 探测 | ✅ |
| 7 | `core/session_store.py`（R17） | 2 | save/load/latest 单测 | ✅ |
| 8 | **【核心】`react_agent.py` 重写：累积 messages + FC 双路径 + trace** | 2,3,5,6 | 端到端一轮对话（FC & 回退） | ✅ |
| 9 | `prompts.py` 拆分（FC 瘦身 + 回退版） | 8 | 两路径均能触发工具 | ✅ |
| 10 | `core/context.py` + 压缩/截断接入（R15） | 8 | 长对话不超 token、偏差<5% | ✅ |
| 11 | `guardrails.py` + 终止前钩子（R3） | 5,8 | 过敏原诱导被拦/改写 | ✅ |
| 12 | `agent.py`/`service.py`：上下文注入 system 消息、删滑窗、装配 session/guardrails | 7,8,10,11 | 三层记忆边界正确 | ✅ |
| 13 | `StepRecord` 去别名 + 前端 + benchmark 适配（第 9 节） | 8 | 前端 `tsc` 编译 + benchmark 读新字段 | ✅ |
| 14 | `server/streaming.py` + `/api/chat/stream` + `chat_stream`（R2） | 8,11,12 | `TestStreamRun` 事件序列 + 前端 SSE 消费 | ✅ |
| 15 | `server/deps.py` LRU + SessionStore 重建 + seed 幂等 + session/trace HTTP 接口（D3/D8/R17/R5） | 7,12 | 未命中 load 重建 + 指纹幂等 | ✅ |

> 分批合入：1–7（纯新增，零风险）→ **8–10（内核重写，最高风险，须 benchmark 全回归）** → 11–15（安全/前端/流式/运维）。

---

## 11. 对外接口契约

### 11.1 保持

- `POST /api/chat` 仍返回 `{answer, terminated_reason, steps, state}`（字段名不变）；`terminated_reason` 对外取值集合不变。
- `ReActAgent.run()->str`、`run_with_trace()->ReActRunResult`、`WellnessPlanningAgent.chat_with_trace()->dict` 签名保持。
- `register_function` 与业务工具字符串入参约定不变。

### 11.2 变更 / 新增

- **`steps` 元素结构变化**（第 9 节）——前端 + benchmark 同步改（已列入落地顺序 13）。这是唯一破坏性变更，集中在一次改造里完成。
- `/api/chat` 请求新增可选 `session_id`；响应新增 `session_id`、`trace_id`、`safety`。
- 新接口：`POST /api/chat/stream`、`GET /api/trace`、`GET /api/trace/list`、`GET /api/session/list`、`POST /api/session/new`。

### 11.3 新增环境变量

| 变量 | 默认 | 作用 |
|------|------|------|
| `WELLNESS_FORCE_FUNCTION_CALLING` | (unset) | 强制开/关 FC |
| `WELLNESS_TRACE_ENABLED` / `WELLNESS_TRACE_DIR` | true / `logs/traces` | Trace |
| `WELLNESS_SESSION_DIR` | `logs/sessions` | 会话落盘目录 |
| `WELLNESS_CB_FAILURE_THRESHOLD` / `WELLNESS_CB_RECOVERY_TIMEOUT` | 3 / 300 | 熔断 |
| `WELLNESS_GUARDRAILS_MODE` | rule | off/rule/rule_llm |
| `WELLNESS_MAX_AGENTS` / `WELLNESS_AGENT_IDLE_TTL` | 50 / (unset) | Agent 缓存淘汰 |
| `WELLNESS_CONTEXT_WINDOW` | 262144 | 上下文窗口估算基准（256k，按主力模型调整） |
| `WELLNESS_SMART_COMPRESSION` | false | R15 智能压缩（LLM 摘要）开关，关时用统计摘要 |
| `WELLNESS_MIN_RETAIN_ROUNDS` | 6 | 压缩时保留最近轮数 |

---

## 12. 风险与兼容性

1. **ReAct 内核重写是最高风险项**（落地 8–10）。缓解：先在 1–7 铺好零风险基础设施；重写后立即跑 `benchmark/` 全量对照基线；FC/回退双路径分别验证。
2. **steps 破坏性变更**：前端 + benchmark 必须与内核同批次改，避免中间态断裂（落地 13 紧跟 8）。
3. **三层记忆边界**：务必删除 `_format_recent_dialogue_window`，否则对话历史会与 messages 数组重复注入、加剧 token 膨胀与自相矛盾。
4. **SessionStore 与 FC 消息合法性**：`tool` 消息必须带 `tool_call_id` 且与前一条 `assistant.tool_calls` 匹配，否则续接时 API 报 400。落盘/恢复需保真。
5. **Ollama 回退**：回退路径也走累积 messages，仅 tool_calls 来源不同；回退版 prompt 只在探测为不支持 FC 时拼接。
6. **`invoke()` 返回 `str` 不动**：蒸馏（`service.py`）与回退解析依赖，避免连锁修改。
7. **tiktoken**：新增依赖，无则降级 `len//4`。
8. **Guardrails 误拦**：默认 rule 档、仅匹配显式风险词；rule_llm 灰度后再考虑默认开。

---

## 13. 验收与度量（第一期整体）

- **功能**：R1–R5/R15/R16/R17 各"验收对照"逐条通过。
- **回归**：`benchmark/` 全量对照，工具使用类/状态跟踪类不低于改造前（基线存档）。
- **可观测**：任意一轮可在 `logs/traces/<user>/` 找到 JSONL+HTML 并经 `/api/trace` 回放。
- **持久化**：重启后带 `session_id` 能续多轮；长期记忆（画像/提纯）独立恢复。
- **稳定性**：工具连续失败触发熔断；长对话触发压缩且不超 token。
- **安全**：过敏原诱导用例被拦/改写且 trace 可见；正常用例误拦可接受。

---

## 14. 评审已确认决策（v0.2 定稿）

1. **`context_window` = 262144（256k）**：`WELLNESS_CONTEXT_WINDOW` 默认 262144，按主力模型 256k 长上下文对齐。
2. **会话列表管理 UI 进第一期**：`SessionStore` + `/api/session/list`、`/api/session/new` 全量落地；前端提供会话列表、切换、新开会话入口（落地 15 + 前端第 9 节）。
3. **R15 两种压缩都支持**：统计摘要（默认）与 LLM 智能摘要（`WELLNESS_SMART_COMPRESSION=true` 开）均实现，运行时按开关切换。
4. **允许改 benchmark evaluator + 重跑基线**：`tool_usage` / `state_tracking` 等评测器随新 `StepRecord` 结构适配；改造前先跑基线存 `benchmark/reports/baselines/`，改造后对照不下降。

