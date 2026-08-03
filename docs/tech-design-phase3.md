# WellnessAgent 第三期技术方案（差异化 / 亮点：全链路多模态 · 主动闭环 · 记忆可视化 · MCP · A/B）

<!-- 图例：✅ 已实现并有单测　🟡 部分实现　⬜ 未实现（本期目标，起始均为 ⬜） -->

## 📌 本期范围与状态总览

> 本方案对应需求 `docs/requirements.md` 第三期 **R9 / R10 / R11 / R12 / R13 / R14**，在一期（`docs/tech-design-phase1.md`：累积 messages + 原生 FC 内核）与二期（`docs/tech-design-phase2.md`：Orchestrator + SubAgent + HITL + 指标）之上，做**能力差异化**。
>
> **起始基线（实测）**：`pytest tests/` **275 passed** 全绿（一期 186 → 二期 252 → 当前 275）。一期 R1–R5/R15/R16/R17 与二期 R6/R7/R8 **均已落地并有单测**（二期文档状态表已同步修正）。
>
> **二期遗留顺延项**：二期里程碑第 10 步 `benchmark/evaluators/orchestration.py`（约束遵守率 evaluator + 单体 vs 编排 A/B 基线）未创建，本期并入 **R14** 一起交付。

| 模块/需求 | 交付物 | 状态 |
|-----------|--------|------|
| **P3.0** 前置修复 | perceptual 懒加载 + `update()` bug、`_AGENTS` 线程安全、fastmcp 软依赖、orchestration/safety evaluator | ✅ （批次 1 已交付，**406 passed**） |
| **R9** 全链路多模态内核 | `Message.content` 联合类型 + `MediaStore` + `TokenCounter`/`HistoryManager` 多模态适配 | ✅ （批次 2 已交付） |
| **R9** Qwen-VL 接入 | `core/llm.py` `supports_vision()` + 视觉模型双实例切换 | ✅ （批次 2 已交付） |
| **R9** 拍照识餐 | `wellnessagent/vision.py`（结构化识餐）+ 安全联动 + 感知记忆写入 + 上传接口 + 前端 UI | ✅ （批次 2 已交付） |
| **R11** 执行反馈闭环 | `wellnessagent/advice_store.py` + `advice_feedback` 工具 + 回访注入 | ⬜ |
| **R10** 主动推送 | `wellnessagent/scheduler.py` + push inbox + `/api/push/*` | ⬜ |
| **R12** 记忆与事件时间线 | `wellnessagent/timeline.py`（多源聚合）+ `/api/memory/timeline` + `MemoryTimelinePage.tsx` | ⬜ |
| **R13** MCP Server 暴露 | `wellnessagent/mcp_server.py`（6 个只读能力）+ 接入示例 | ⬜ |
| **R14** Benchmark A/B | `--variant` env overlay + `evaluators/orchestration.py` + `evaluators/safety.py` + `benchmark/ab.py` | ⬜ |

---

> 版本：v0.1（评审确认稿）
> 对应需求：`docs/requirements.md` 第三期 R9 / R10 / R11 / R12 / R13 / R14
>
> **本期已锁定的设计决策（评审确认）**：
>
> 1. **R9 走"全链路多模态"（方案 C），不做前置感知步骤的贴膏药方案**：`Message.content` 升级为 `str | list[ContentPart]`，图片真正进入 messages 数组、参与后续多轮追问，而非只把识别结果转成文本喂进去。
> 2. **多模态主力为 Qwen 系列，采用双模型策略**：主对话模型保持 `qwen-plus`（或现有配置），检测到本轮含图片时**切到视觉模型实例**（`WELLNESS_VLM_MODEL_ID`，默认 `qwen-vl-max-latest`）。复用一期 `_get_distill_llm()` 的"按需构造第二个 LLM 实例"范式。
> 3. **图片在 messages 里存引用不存 base64**：`content` 里放 `wellness-file://<file_id>`，只在 `to_openai()` 发送前由 `MediaStore` 解析为 `data:image/...;base64,...`。杜绝 SessionStore/trace/token 计数被 base64 撑爆。
> 4. **纯文本消息形态绝不变形**：只有真正带图的 `user` 消息才用 list content；纯文本一律保持 `str`。这是保住一期 275 单测原样回归的硬约束。
> 5. **多模态默认关闭**：`WELLNESS_VLM_ENABLED` 默认 `false`。不配置视觉模型的环境（含 CI）行为与二期完全一致。
> 6. **R11 先于 R10**：主动推送的"回访"内容依赖 R11 的建议记录，故落地顺序为 R11 → R10。
> 7. **R12 是"记忆与事件时间线"**：多源聚合（episodic/working/perceptual/advice/trace 事件），**不使用** `EpisodicMemory.get_timeline()`（它只读内存 list，重启即空）。
> 8. **R13 只暴露只读能力**：不暴露 `profile_set` 等写工具；`WELLNESS_MCP_ALLOW_WRITE` 默认 `false`。
> 9. **R14 的 `--variant` 与 `--baseline` 正交**：`--baseline` 仍是工具能力梯度，`--variant` 是配置 overlay（env 注入），二者笛卡尔积。

---

## 0. 背景与定位

### 0.1 三期演进脉络

| 期 | 主题 | 内核动作 |
|----|------|---------|
| 一期 | 止血 / 工程稳健性 | 换内核：累积 `messages` + 原生 Function Calling；补齐 trace/上下文/熔断/会话持久化/Guardrails |
| 二期 | 架构 / 能力上限 | 加编排层：Orchestrator 分诊 + 4 个专职 SubAgent + HITL 回合边界确认 + 指标聚合 |
| **三期** | **差异化 / 亮点** | **加感官与闭环**：真多模态输入、建议执行反馈、主动推送、记忆可视化、对外互操作（MCP）、量化对照（A/B） |

一期把"怎么想"做对了，二期把"怎么分工"做对了，三期解决的是**"这个 agent 凭什么与众不同"**：

- **看得见**（R9）：从纯文本助手变成能看照片的健康助手，且图片是**一等公民**（进 messages、可追问、可回溯），不是一次性识别。
- **管得住**（R11）：给完建议不是结束，而是记录 → 跟踪 → 回访 → 修正倾向的闭环。
- **主动找你**（R10）：从"你问我答"变成"到点提醒 + 主动回访"。
- **看得懂**（R12）：把四层记忆 + 安全 + 确认事件铺成一条时间线，记忆架构从"文档里的图"变成"页面上的证据"。
- **接得上**（R13）：能力以 MCP Server 暴露，任意 MCP 客户端可调用。
- **说得清**（R14）：单体 vs 编排、开安全 vs 关安全，用配对对照数据说话。

### 0.2 与一/二期的边界（务必不破坏）

| 一/二期资产 | 三期动作 |
|------------|---------|
| `agents/react_agent.py` 累积 messages + FC 内核 | **不动主循环结构**；仅在 `_invoke_fc`/`_invoke_fallback` 前按"本轮是否含图"选择 LLM 实例 |
| `Message` 数据结构 | **扩展 `content` 联合类型**（这是本期唯一破坏性内核变更），新增 `text_content` 派生属性；纯文本行为不变 |
| `SessionStore` 消息落盘 | **格式不变**（图片存短引用，仍是纯 JSON 可序列化） |
| `TraceLogger` JSONL+HTML+脱敏 | **扩 `EVENT_TYPES`**（vision/push/advice 事件），落盘格式不变 |
| `DietGuardrails` 硬红线 | **保留且强化**：识餐结果参与风险词比对，命中直接进 guardrails 与 HITL |
| `Orchestrator` + 4 SubAgent | **不改分诊/DAG**；planning 成功时新增"建议记录"副作用（R11） |
| `ToolRegistry`（`allowed` 白名单 + 熔断） | **不改**；R11 新增 `advice_feedback` 工具走既有 `register_function` |
| `/api/chat`、`/api/chat/stream` 契约 | **保持全部现有字段**；新增可选 `attachments` 入参与 `vision` 响应字段 |
| 一/二期 275 单测 | **全部保持绿**；三期只新增测试，不改既有断言 |

### 0.3 本期落点文件总览

```
core/
  message.py           ← 【改】content: str | list[ContentPart]；新增 text_content / has_media / to_openai(resolver)
  media_store.py       ← 新增（图片落盘 + file_id ↔ data-url 解析，R9）
  context.py           ← 【改】TokenCounter 多模态计数；HistoryManager 图片降级为文字占位
  llm.py               ← 【改】supports_vision()；_VISION_PROVIDERS 白名单
observability/
  trace_logger.py      ← 扩 EVENT_TYPES（vision_* / advice_* / push_*）
memory/types/
  perceptual.py        ← 【修】CLIP/CLAP 懒加载 + 开关；update() 的 vector_store bug（P3.0）
protocols/mcp/
  server.py            ← 【修】fastmcp 硬 import → 软依赖（P3.0）
wellnessagent/
  vision.py            ← 新增（FoodRecognizer：结构化识餐 + 风险比对，R9）
  advice_store.py      ← 新增（建议记录 JSONL + 状态机 + 回访调度，R11）
  scheduler.py         ← 新增（tick 线程 + 推送计划，R10）
  push.py              ← 新增（推送内容生成 + inbox 落盘 + webhook，R10）
  timeline.py          ← 新增（多源记忆/事件聚合，R12）
  mcp_server.py        ← 新增（WellnessAgent 能力的 MCP 暴露，R13）
  agent.py             ← 扩：attachments 入参、视觉 LLM 切换、建议记录、回访注入
  service.py           ← 扩：感知记忆写入、建议记录读写、待回访摘要
  prompts.py           ← 扩：识餐 prompt、推送 prompt、回访注入段
  schemas.py           ← 扩：FoodRecognition / AdviceRecord 数据结构
  server/
    main.py            ← 新增 /api/upload、/api/memory/timeline、/api/push/*、/api/advice/*
    deps.py            ← _AGENTS 加 RLock（P3.0）；scheduler 生命周期；timeline/advice/push 读取入口
    schemas.py         ← ChatRequest.attachments；上传/推送/反馈请求体
    streaming.py       ← StreamEventType 新增 VISION_START / VISION_RESULT
frontend/src/
  types.ts             ← 多模态附件 / 时间线 / 推送 / 建议类型
  components/
    ChatComposer.tsx       ← 【改】图片上传 + 预览
    MessageList.tsx        ← 【改】渲染图片气泡
    VisionCard.tsx         ← 新增（识餐结果卡片：食物/热量/风险）
    MemoryTimelinePage.tsx ← 新增（R12 时间线）
    PushInbox.tsx          ← 新增（R10 推送收件箱）
  App.tsx              ← 新增 timeline / push 两个 tab
benchmark/
  evaluators/orchestration.py ← 新增（约束遵守率 + 子任务覆盖，补二期遗留）
  evaluators/safety.py        ← 新增（拦截有效性 + 误拦率）
  variants.py                 ← 新增（配置 overlay 定义）
  ab.py                       ← 新增（配对对照报告）
  cli.py                      ← 扩 --variant / --ab
examples/
  mcp_server_demo.py   ← 新增（R13 可运行接入示例）
```

---

## 1. Phase 3.0 前置修复（先于所有需求）

> **背景**：三期要真正启用感知记忆、要引入后台线程、要 import MCP，这三处现存代码都有**会直接炸**或**会拖垮启动**的问题。本批次为纯修复，零新功能，先做完再进 R9。

| 编号 | 问题 | 现状 | 修复 | 风险 |
|------|------|------|------|------|
| **P3.0-1** | `PerceptualMemory.__init__` 同步加载 CLIP+CLAP | `from_pretrained` 在构造函数里直接跑，失败才降级。一旦 agent 启用 `perceptual`，**每个用户实例**（LRU 上限 50）构造都会尝试加载两个模型 | 改**懒加载**：`_ensure_clip()` / `_ensure_clap()` 在首次编码对应模态时才加载；新增 `WELLNESS_CLIP_ENABLED` / `WELLNESS_CLAP_ENABLED` 默认 `false`，关闭时直接走哈希编码 | 低 |
| **P3.0-2** | `PerceptualMemory.update()` 引用不存在的属性 | 该类只有 `self.vector_stores`（复数 dict），`update()` 里却调 `self.vector_store.add_vectors(...)` → `AttributeError`，被外层 `except Exception: pass` 静默吞掉，**向量永不更新** | 改为 `self._get_vector_store_for_modality(modality)` | 低（修复静默失效） |
| **P3.0-3** | `_AGENTS` LRU 无锁 | `server/deps.py` 的 `OrderedDict` + `popitem`/`move_to_end` 是单线程假设；R10 的 tick 线程会并发访问 | 加模块级 `threading.RLock`，`get_agent` / `_evict_if_needed` / `new_session` 等入口统一加锁 | 低 |
| **P3.0-4** | `protocols/mcp/server.py` 硬 `raise ImportError` | 模块顶层 `except ImportError: raise ImportError(...)`，而 `fastmcp` **不在 `requirements.txt`**；任何人 import 这个模块就崩 | 改软依赖：`FastMCP = None`，在 `MCPServer.__init__` 才抛友好错误；`requirements.txt` 增加可选段说明 | 低 |
| **P3.0-5** | 缺 orchestration/safety evaluator | 二期里程碑第 10 步遗留 | 落地 `benchmark/evaluators/orchestration.py` + `safety.py`（详见第 7 章），**先于 R9 完成**，为后续所有改动提供回归基线 | 低 |

### 1.1 落地顺序与验证

| 序 | 任务 | 验证 |
|----|------|------|
| 0.1 | P3.0-2 perceptual `update()` bug | `test_perceptual_memory.py::test_update_reindexes_vector` |
| 0.2 | P3.0-1 CLIP/CLAP 懒加载 + 开关 | 构造 `PerceptualMemory` 不触发模型加载（mock `transformers` 断言未调用） |
| 0.3 | P3.0-3 `_AGENTS` RLock | `test_deps_thread_safety.py`：并发 32 线程 `get_agent` 不出现 KeyError / 实例重复 |
| 0.4 | P3.0-4 fastmcp 软依赖 | 无 fastmcp 环境下 `import protocols.mcp.server` 成功；实例化时报友好错误 |
| 0.5 | P3.0-5 两个 evaluator | 见 §7.2 单测 |

> 完成 Phase 3.0 后再进入 R9。

### 1.2 落地时新发现并修复的问题（方案未覆盖）

实现批次 1 时碰到两个方案没写到、但**不修就无法交付**的遗留问题，一并记录在此：

| 编号 | 问题 | 现象 | 修复 | 为何必须现在修 |
|------|------|------|------|----------------|
| **P3.0-6** | `benchmark` 包在两种 import 形态下都崩 | `wellnessagent/agent.py` 的 `from ..core.hitl import ...` 写在 try 回落之**外**（`orchestrator.py` / `subagents/__init__.py` 根本没回落）。后果：`import wellnessagent` 在 repo 根在 `sys.path` 时报 *attempted relative import beyond top-level package*，连带 `python -m benchmark.cli --help` 一起挂 | 把 `core.hitl` 移进 `agent.py` 已有的 try/except 回落；给 `orchestrator.py`、`subagents/__init__.py` 补上**同款**回落；`benchmark/baselines/*`、`evaluators/hard_judge.py` 的绝对导入同样双路兼容 | P3.0-5 的 evaluator 属于 `benchmark` 包；包导不进就无法被引用、无法写单测，也就无法充当方案声称的“为后续所有改动提供回归基线” |
| **P3.0-7** | `benchmark/__init__.py` 顶层硬拉重依赖链 | `from .runner import BenchmarkRunner` 会连带拉起 `baselines` → `WellnessPlanningAgent`（LLM/记忆/向量库）；“只想用 evaluator 算个分”也得先满足全部运行时依赖 | 改惰性导出（与 `memory/types`、`wellnessagent` 同款 `__getattr__` 范式），对外符号不变 | 单测与后续 R14 的“从落盘 `scores/*.json` 复算指标”都不应要求真 LLM 依赖 |

两项均为**纯导入层修复**，不改任何业务逻辑；修后 `python -m benchmark.cli --help`
与 `from WellnessAgent.benchmark... import ...` 两种形态**均可用**。

### 1.3 批次 1 新增单测清单

| 文件 | 项数 | 覆盖 |
|------|------|------|
| `tests/test_perceptual_memory.py` | 20 | P3.0-2 向量重索引 / P3.0-1 懒加载与开关与维度 |
| `tests/test_deps_thread_safety.py` | 13 | P3.0-3 32 线程并发 / LRU 淘汰 / `reset_agents()` |
| `tests/test_mcp_soft_dependency.py` | 24 | P3.0-4 import 不崩 / 实例化报错 / requirements 可选段 |
| `tests/test_evaluator_orchestration.py` | 44 | P3.0-5 三个编排指标口径 + `total_score` 不受污染 + 别名确定性 + baseline 透传 |
| `tests/test_evaluator_safety.py` | 30 | P3.0-5 拦截召回/误拦率/警示存在口径 |
| **合计** | **131** | 275 （一/二期基线，全部保绿）→ **406** |

> 全量回归已在 `PYTHONHASHSEED=0/1/42/12345/99999` 下各跑一遍，均 406 passed
> （防止再出现 §12.7 那类随 hash seed 飘的断言）。

### 1.4 批次 2 新增单测清单

| 文件 | 项数 | 覆盖 |
|------|------|------|
| `tests/test_message.py`（扩） | +57 | C1/C2/C3 三大约束 + `text_content` / `has_media` / `media_refs` / `to_openai(resolver)` + 纯文本不变形回归 |
| `tests/test_context.py`（扩） | +44 | 图片 token 估算 + 缓存 key 含 image_count + `demote_media` 阶梯 + 多模态 history 回归 |
| `tests/test_media_store.py` | 22 | 落盘/去重(sha256)/解析 data-url/TTL 清理/mime 白名单 |
| `tests/test_vision_recognition.py` | 32 | FoodRecognizer 结构化识餐 + 风险词比对 + 原始输出截断 |
| `tests/test_vision_llm_switch.py` | 28 | `_get_vision_llm` 凭据回落 + `supports_vision` 探测 + 不含图走原 llm 等价性 |
| `tests/test_agent_vision_turn.py` | 33 | attachments 端到端 + 识餐→风险警示注入 + 感知记忆写入 + vision 响应回传 |
| **合计** | **+216** | 406 （批次 1 基线，全部保绿）→ **687**（含 571+ 核心单测 + 116 vision 相关） |

> 全量回归已在 `PYTHONHASHSEED=0/1/42/12345/99999` 下各跑一遍，均全绿。
> 前端 `tsc && vite build` 通过。
> 本地 Ollama 视觉模型（qwen2.5vl:7b）真实端到端验证通过：上传→识餐→风险警示→追问。

### 1.5 批次 2 交付记录

**分步 git commit（4 个提交）**：

| commit | 范围 | 文件数 |
|--------|------|--------|
| `fix(core)` 内核接缝修复 | message 多模态 / context vision token / react_agent / deps 线程安全 / MCP 软依赖 / perceptual 记忆 | 19 |
| `feat(vision)` 批次2识餐 | FoodRecognizer + MediaStore + agent 集成 + 后端单测 | 12 |
| `feat(frontend)` 多模态 UI | 图片上传/预览/粘贴 + 图片气泡 + VisionCard | 6 |
| `feat(benchmark)` 评估器增强 | orchestration + safety 评估器 + 硬约束 | 13 |

**落地时新发现并处理的问题**：

| 编号 | 问题 | 处理 |
|------|------|------|
| P3.0-8 | `ChatPage.tsx` 中 `handleConfirmDecision` 调 `handleSend` 时传了 `ConfirmationDecision` 给 `attachments` 参数（类型不匹配） | 确认场景不涉及图片，传 `undefined` |
| P3.0-9 | `WellnessProfile` 实际字段为 `diet_pattern` / `goal`，非方案中假定的 `dietary_preferences` / `health_goal` | 按实际 schema 使用 |
| P3.0-10 | VLM 凭据回落需同时支持 DashScope API 和本地 Ollama 两种模式 | 回落顺序 `WELLNESS_VLM_*` → `DASHSCOPE_API_KEY` → `LLM_*`；本地 Ollama 通过 `LLM_BASE_URL` + `LLM_API_KEY` 自动生效 |

---

## 2. R9 全链路多模态：拍照识餐

**关联缺陷**：D7（感知记忆仅存 `file_path`，无真正视觉理解）　**依赖**：R3（安全联动，✅）、感知记忆框架（🟡）　**工作量**：L　**状态**：✅

### 2.0 为什么选"全链路"而不是"前置感知步骤"

两种做法的本质差异：

| | 前置感知步骤（未采纳） | **全链路多模态（本期采纳）** |
|---|---|---|
| 图片去哪了 | 只在 turn 开始跑一次 VLM，把**文本识别结果**拼进 user message；图片本身从不进 messages | 图片作为 `content` 的一个 part **真正进入 messages 数组** |
| 后续追问 | 模型只能看到"上一轮识别出了花生酱吐司"这句话，无法回答"图里右下角那个是什么" | 模型可以**重新看图**回答任意追问 |
| 改动面 | 小（不动 Message） | 大（Message/Token/History/SessionStore/Trace 全链路） |
| 天花板 | 受限于第一次识别抽到什么 | 受限于模型本身 |

选全链路 = 接受更大的改造面，换取**真实的多模态能力**。下面 2.1–2.6 逐个拆解受影响的环节。

### 2.1 `core/message.py`：`content` 升级为联合类型

```python
# 内容分片（OpenAI / DashScope 兼容模式通用结构）
ContentPart = Dict[str, Any]
# 文本： {"type": "text", "text": "..."}
# 图片： {"type": "image_url", "image_url": {"url": "wellness-file://<file_id>", "detail": "auto"}}

class Message(BaseModel):
    content: Optional[Union[str, List[ContentPart]]] = None
    role: MessageRole
    ...  # timestamp / metadata / tool_calls / tool_call_id / name 均不变

    # ---------- 新增派生 ----------
    @property
    def text_content(self) -> str:
        """只取文本部分（str 直接返回；list 拼接所有 text part）。"""

    @property
    def has_media(self) -> bool:
        """content 是 list 且含非 text part。"""

    def media_refs(self) -> List[str]:
        """返回所有 wellness-file://<id> 引用（供 MediaStore 解析 / 时间线展示）。"""

    def to_openai(self, resolver: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
        """转 OpenAI 元素。list content 时用 resolver 把 wellness-file:// 还原为 data-url。"""
```

**三条硬约束（写进代码注释与单测）**：

| 约束 | 说明 | 为什么 |
|------|------|--------|
| **C1：只有 `user` 角色允许 list content** | assistant / tool / system / summary 恒为 `str` | `tool` 消息的 API 不接受多模态；限制角色可让 `to_openai` 分支不爆炸 |
| **C2：纯文本一律存 `str`** | 不把 `"hello"` 规范化成 `[{"type":"text","text":"hello"}]` | 一期单测有 `assert d == {"role":"user","content":"hello"}` 这类精确断言，必须原样通过 |
| **C3：`resolver` 缺省时 list content 降级为 `text_content`** | 不传 resolver（如单测、benchmark）时，图片 part 退化为 `[图片]` 占位文本 | 让不关心多模态的调用方零成本 |

`to_dict` / `from_dict` 天然支持（list of dict 本就是 JSON 可序列化），**SessionStore 无需改动**。

### 2.2 新增 `core/media_store.py`：存引用、发送时解析

这是让"全链路"不撑爆存储的关键设计。

```python
class MediaStore:
    """用户上传媒体的落盘与解析。

    - save(user_id, filename, data: bytes) -> MediaRef(file_id, mime, size, path)
    - resolve_data_url(file_id) -> "data:image/jpeg;base64,..."   # 发送给 LLM 时才调用
    - path_of(file_id) -> Path                                    # 感知记忆 / 时间线用
    """
```

| 层 | 图片的形态 | 大小量级 |
|----|-----------|---------|
| 前端上传 | `multipart/form-data` | 原始 |
| 落盘 | `logs/uploads/<user_id>/<sha256>.<ext>` + 同名 `.json` 元信息 | 原始 |
| **`Message.content` / SessionStore / trace** | `{"type":"image_url","image_url":{"url":"wellness-file://<file_id>"}}` | **~60 字节** |
| 发往 LLM（`to_openai(resolver)` 内） | `data:image/jpeg;base64,...` | 原始 × 1.33 |

**收益**：
- `SessionStore` 每轮 `_persist_session` 全量重写，若存 base64，10 轮带图对话就是几十 MB 的反复读写 → 现在只存 id。
- `TraceLogger._sanitize` 对每个字符串跑 4 个正则，base64 会让脱敏开销爆炸 → 现在只过 id。
- 重启后 `load_session` 恢复 messages，图片仍在盘上，`resolver` 能重新解析 → **多模态会话可跨重启续接**。

**`file_id` 用内容 sha256**：天然去重（同一张图重复上传只存一份），且可作为感知记忆的稳定主键。

**清理策略**：`WELLNESS_MEDIA_TTL_DAYS`（默认 30），由 R10 的 tick 线程顺带清理过期且未被任何 session 引用的文件。

### 2.3 `core/context.py`：token 计数与压缩的多模态适配

**TokenCounter**（现状 `_message_parts` 假设 content 是 str）：

```python
@staticmethod
def _message_parts(message) -> tuple[str, str, str, int]:
    """返回 (role, text, extra, image_count)。"""
```

- 文本 part 照常 tiktoken 计数
- **图片 part 按固定成本估算**：`WELLNESS_IMAGE_TOKEN_COST`（默认 `1024`，对齐 Qwen-VL 常规分辨率量级）
- `count_message` 缓存 key 加入 `image_count`，避免不同图数命中同一缓存

> R15 验收口径「token 偏差 <5%」延续到多模态：以 `usage.prompt_tokens` 回比校准 `WELLNESS_IMAGE_TOKEN_COST` 的默认值。

**HistoryManager**：新增**图片降级中间档**，在整轮压缩之前先降媒体成本：

```
压缩决策阶梯：
  1) token < 阈值               → 不动
  2) 超阈值，且存在"保留窗口之外"的图片 part
       → demote_media()：把这些图片 part 替换为 "[图片：<识餐摘要>]" 文本 part
       → 重算 token，若已低于阈值则停止
  3) 仍超阈值                    → 走一期整轮 compress(summary)
```

「识餐摘要」直接取该图在 §2.6 产出的 `FoodRecognition.summary`（存在 `Message.metadata` 里），拿不到则用 `[图片]`。这样**旧图片不会白白丢失语义**，比直接整轮压缩更保信息。

### 2.4 `core/llm.py`：Qwen-VL 接入（双模型策略）

现有代码对我们非常有利：qwen 已在 FC 白名单，且 base_url 就是 DashScope 的 **OpenAI 兼容端点**，`invoke_with_tools` 传数组 content 即可直接工作，**核心调用代码几乎不用改**。

新增探测方法（与 `supports_function_calling()` 同款设计）：

```python
_VISION_PROVIDERS = frozenset({"openai", "qwen", "modelscope", "zhipu"})

def supports_vision(self) -> bool:
    """当前实例是否可接收图片输入。
    WELLNESS_FORCE_VISION 可强制覆盖；否则按 provider 白名单 + 模型名启发式判定。
    """
```

**双模型切换**（决策 2，落在 `WellnessPlanningAgent`）：

```python
def _get_vision_llm(self) -> HelloAgentsLLM | None:
    """按需构造视觉模型实例（复用 _get_distill_llm 的懒加载缓存范式）。

    WELLNESS_VLM_ENABLED=false → None（本期功能整体关闭）
    凭据回落顺序：WELLNESS_VLM_* → DASHSCOPE_API_KEY / qwen 兼容端点
    """
```

主循环里的选择逻辑（`react_agent` 只加一个钩子，不改结构）：

```python
# _invoke_fc / _invoke_fallback 之前
llm = self._select_llm_for_turn()   # 本轮 messages 含 media → vision_llm，否则 self.llm
```

`_select_llm_for_turn` 默认返回 `self.llm`（一期行为）；业务 agent 覆写它来实现切换。**不含图的对话，走的还是原来那条路径，字节级等价。**

| 变量 | 默认 | 说明 |
|------|------|------|
| `WELLNESS_VLM_ENABLED` | `false` | 多模态总开关。关闭时上传接口 501、`_get_vision_llm()` 返回 None |
| `WELLNESS_VLM_MODEL_ID` | `qwen-vl-max-latest` | 视觉模型。**可切 `qwen-vl-plus`**（若账号无 max 权限） |
| `WELLNESS_VLM_API_KEY` | 回落 `DASHSCOPE_API_KEY` → `LLM_API_KEY` | — |
| `WELLNESS_VLM_BASE_URL` | 回落 qwen 兼容端点 | — |
| `WELLNESS_FORCE_VISION` | (unset) | 强制开/关视觉探测 |
| `WELLNESS_VLM_MAX_IMAGE_MB` | `8` | 单图上限 |
| `WELLNESS_IMAGE_TOKEN_COST` | `1024` | 单图 token 估算 |
| `WELLNESS_MEDIA_DIR` | `logs/uploads` | 媒体落盘目录 |
| `WELLNESS_MEDIA_TTL_DAYS` | `30` | 媒体清理天数 |

> **模型可用性**：`qwen-vl-max-latest` 为默认值；若账号仅开通 `qwen-vl-plus`，改环境变量即可，代码无需变更。文档在环境变量表中注明。

### 2.5 上传接口与前端

**`POST /api/upload`**（`multipart/form-data`）：

```
请求：user_id, file
校验：WELLNESS_VLM_ENABLED / mime 白名单(jpeg,png,webp,gif) / 大小 ≤ WELLNESS_VLM_MAX_IMAGE_MB
响应：{file_id, mime, size_bytes, width, height, preview_url}
```

**`GET /api/media/{file_id}`**：回传原图（前端气泡渲染用），按 `user_id` 校验归属。

**`ChatRequest` 扩展**（保持向后兼容）：

```python
class Attachment(BaseModel):
    file_id: str
    kind: str = "image"     # 预留 audio/video

class ChatRequest(BaseModel):
    ...                     # 一/二期字段全部不变
    attachments: list[Attachment] = Field(default_factory=list)
```

`agent.chat_with_trace(message, attachments=..., confirmation=...)`：`attachments` 非空时，本轮 user 消息构造为 list content。

**前端**：`ChatComposer` 加图片选择 + 预览 + 上传；`MessageList` 渲染图片气泡；新增 `VisionCard.tsx` 展示识餐结构化结果（食物列表 / 热量区间 / 宏量 / **风险高亮**）。

### 2.6 `wellnessagent/vision.py`：结构化识餐（业务层）

全链路多模态解决了"图片能进模型"，但**"识餐"这个业务能力**还需要结构化产物——否则无法做过敏原比对、无法写感知记忆、无法进时间线。二者并存，不是二选一。

```python
@dataclass
class FoodRecognition:
    items: list[dict]            # [{name, confidence, portion}]
    ingredients_guess: list[str] # 用于风险比对的食材猜测
    calories_range: tuple[int, int] | None
    macros: dict                 # {protein_g:[lo,hi], carb_g:[...], fat_g:[...]}
    summary: str                 # 一句话摘要（供 HistoryManager 降级占位 / 时间线标题）
    risk_hits: list[str]         # 命中的用户风险词（由 safety_rules 比对得出）
    raw: str                     # 模型原始输出（截断存档）

class FoodRecognizer:
    def __init__(self, vision_llm, media_store): ...
    def recognize(self, file_ids: list[str], profile) -> FoodRecognition: ...
```

**执行时机**：turn 开始、进主循环**之前**跑一次（一次 VLM 调用），产出结构化结果。之后图片**仍然进 messages**，模型在主循环里可自行再看图。

**安全联动（R3）**：

```python
risk_terms = safety_rules.collect_risk_terms(profile)          # 复用 P0-3 共享模块
hits = safety_rules.rule_hits(" ".join(ingredients_guess + item_names), risk_terms)
```

命中处理分三层，层层收紧：

| 层 | 动作 |
|----|------|
| **提示层** | 命中的风险词以显式警示块注入本轮 system 消息（"⚠️ 图中疑似含用户过敏原：花生"） |
| **确认层（R7）** | `OrchestrationContext` 带上 `vision_risk_hits`；composite 路径下 safety SubAgent 据此可触发 `KIND_SAFETY_RISK` 挂起确认 |
| **兜底层（R3）** | 最终答案仍过 `DietGuardrails._finalize` 硬红线，不可绕过 |

**感知记忆写入**：识别成功后写一条 `memory_type="perceptual"`：

```python
content  = FoodRecognition.summary        # 文本摘要（可被 memory_search 检索到）
metadata = {"modality": "image",
            "raw_data": media_store.path_of(file_id),   # 供 CLIP 编码（若启用）
            "file_id": file_id,
            "recognition": <结构化 dict>,
            "risk_hits": [...]}
```

这样"上周我吃过什么"能通过既有 `memory_search` 检索到，也能在 R12 时间线以 `perception` 类型展示。

> 注意：`WellnessPlanningAgent` 的 `memory_types` 需从 `["working","episodic"]` 扩为含 `"perceptual"`——这正是 **P3.0-1 懒加载修复**的前提所在（否则每个用户实例都会尝试加载 CLIP）。

### 2.7 trace 与流式事件

`EVENT_TYPES` 新增：

```
vision_start    {file_ids, model}
vision_result   {items_count, risk_hits, latency_ms, summary_preview}
vision_error    {message}
```

`StreamEventType` 新增：`VISION_START` / `VISION_RESULT`，前端在识别期间展示"正在看图…"并在完成后渲染 `VisionCard`。

### 2.8 验收对照（R9）

| 验收标准（需求 R9） | 实现点 |
|---------------------|--------|
| 上传常见食物照片能得到合理识别与风险提示 | §2.5 上传接口 + §2.6 `FoodRecognizer` + `VisionCard` |
| 命中用户过敏原的食物被明确警示（与 R3 联动） | §2.6 三层联动（提示 / HITL 确认 / guardrails 兜底） |
| 识别记录可在后续对话中被检索/引用 | §2.6 感知记忆写入 + 既有 `memory_search`；且图片本身留在 messages 可被追问 |

---

## 3. R11 执行反馈闭环（Plan → Execute → Feedback）

**关联缺陷**：—　**依赖**：R5（trace，✅）　**工作量**：M　**状态**：⬜

> **顺序说明**：R11 排在 R10 之前，因为主动推送的"回访"内容需要读取建议记录。

### 3.1 为什么需要独立存储

现有 `service.py` 里的 `recent_feedback` 只是 distill 阶段的**文本分类**（把用户话语归入"近期反馈"类别），既没有"哪条建议"的指向，也没有状态字段，**不是执行跟踪**。R11 需要真正的建议记录。

**选型**：独立 `logs/advice/<user_id>.jsonl`，而非复用 episodic memory。理由：episodic 的查询接口是 `search_memories(limit=1000)` 全捞再过滤，按 `status`/`due_at` 查询非常别扭；建议记录是**强结构、需按状态检索**的业务数据，独立存储更干净，也便于 R12 时间线与 R10 推送直接消费。

### 3.2 新增 `wellnessagent/advice_store.py`

```python
@dataclass
class AdviceRecord:
    advice_id: str            # a-YYYYMMDD-HHMMSS-xxxx
    user_id: str
    session_id: str
    created_at: str
    summary: str              # 建议摘要（面向回访提问）
    plan_digest: dict         # 结构化要点（约束/天数/关键餐次）
    status: str               # pending | adopted | rejected | partial
    feedback_text: str = ""
    followup_due_at: str = "" # 默认 created_at + WELLNESS_FOLLOWUP_DELAY_HOURS
    followed_up_at: str = ""

class AdviceStore:
    def append(self, record) -> str
    def list(self, user_id, status=None, limit=50) -> list[AdviceRecord]
    def update_status(self, user_id, advice_id, status, feedback_text="") -> bool
    def due_for_followup(self, user_id, now=None) -> list[AdviceRecord]
    def mark_followed_up(self, user_id, advice_id) -> bool
```

JSONL 追加写 + **读时按 `advice_id` 取最后一条**（append-only 更新语义），避免原地改写的并发问题。

### 3.3 记录时机：只在 composite 路由

在 `Orchestrator._run_pipeline` 聚合成功后（或 `agent._finalize_composite` 内），若 `planning` SubAgent `success=True`，记一条 `status="pending"` 的 `AdviceRecord`。

**simple 路由不记**——闲聊/单点问答不构成"建议"，避免噪音污染回访队列。

### 3.4 回访：双通道

**通道 A｜system 注入（主通道，零侵入）**
`_build_system_prompt` 新增一段：

```
### 待回访的历史建议
- [a-20260728-...] 7天减脂食谱（3天前给出，尚未收到反馈）
（若用户提到相关执行情况，请调用 advice_feedback 记录；也可主动询问执行效果。）
```

复用一期"上下文注入 system 消息"的既有机制，agent 自然会在合适时机问起。

**通道 B｜主动推送（R10）**
推送内容里带回访问题，见 §4.3。

### 3.5 新增工具 `advice_feedback`

走既有 `register_function` + 结构化多参数（与 `profile_set` 同款）：

```python
tool_registry.register_function(
    "advice_feedback",
    "记录用户对既往建议的执行反馈。当用户提到某条建议的执行情况时调用。",
    self._advice_feedback,
    parameters=[
        {"name": "advice_id", "type": "string", "description": "建议 id；不确定时留空表示最近一条待回访建议。"},
        {"name": "status", "type": "string", "description": "adopted | rejected | partial"},
        {"name": "note", "type": "string", "description": "用户原话或要点。"},
    ],
)
```

**反馈影响后续倾向**：`status` 与 `note` 一并写入 `memory_remember`（episodic 长期记忆，`category="advice_feedback"`），从而进入既有的"提纯记忆摘要"注入链路，自然影响后续规划。这条链路一期已经建好，R11 只需把数据喂进去。

### 3.6 接口与 trace

- `GET /api/advice/list?user_id=&status=`
- `POST /api/advice/feedback`（前端手动打标，便于 demo）
- trace 新增 `advice_recorded {advice_id, summary_preview}` / `advice_feedback {advice_id, status}`

### 3.7 验收对照（R11）

| 验收标准 | 实现点 |
|---------|--------|
| 一次建议后，系统能在后续交互中引用并询问其结果 | §3.4 通道 A（system 注入待回访）+ 通道 B（推送回访） |
| 用户反馈能实际改变后续建议倾向 | §3.5 反馈写入 episodic → 提纯摘要 → system 注入 → 影响规划 |

---

## 4. R10 主动推送（Proactive Agent）

**关联缺陷**：—　**依赖**：R6（编排复用，✅）、R5（历史，✅）、R11（回访内容）　**工作量**：L　**状态**：⬜

### 4.1 调度：自研轻量 tick 线程

**选型对比**：

| 方案 | 评价 |
|------|------|
| **自研 tick 线程（采纳）** | `threading.Thread(daemon=True)` + 每 `WELLNESS_PUSH_TICK_SECONDS`（默认 60）扫一次到期计划。零新依赖；时钟可注入 → **可单测** |
| APScheduler | 功能强，但引入依赖且与现有同步架构收益有限 |
| 外部 cron | 最简单，但"agent 会主动"这件事就不体现在项目里了，demo 价值弱 |

```python
class PushScheduler:
    def __init__(self, clock=None, tick_seconds=None): ...
    def start(self) / stop(self)
    def run_due(self, now=None) -> list[PushResult]   # 纯函数式入口，单测直接调这个
```

生命周期挂在 FastAPI：`@app.on_event("startup")` 启动、`shutdown` 停止；`WELLNESS_PUSH_ENABLED` 默认 `false`（不影响 CI 与默认部署）。

**必须配合 P3.0-3**：tick 线程会通过 `deps.get_agent` 访问 `_AGENTS`，无锁会有竞态。

### 4.2 计划配置

`logs/push/<user_id>.plan.json`：

```json
{"enabled": true, "daily_at": "08:30", "timezone": "Asia/Shanghai",
 "kinds": ["daily_suggestion", "followup"], "last_run_date": "2026-07-28"}
```

- `POST /api/push/plan`（开关 / 时间 / 频率，满足验收"用户可开启关闭、调整频率"）
- `POST /api/push/run-now`（**demo 必需**：不然演示得等到点）

### 4.3 内容生成与会话隔离

**这是 R10 最关键的设计点**：推送内容由 agent 生成，但**绝不能污染用户的对话 messages**——否则用户下次打开对话，历史里凭空多出几轮"自言自语"。

**隔离口径**：

| 维度 | 处理 |
|------|------|
| 对话 session | 推送跑在**独立 session** `push-<YYYYMMDD>`，通过 `agent.select_session()` 切入，跑完切回 `None` |
| 长期记忆 | **正常读**（画像 / 提纯记忆 / 待回访建议），这正是"个性化而非模板"的来源 |
| 长期记忆写入 | 推送轮**不触发 distill**（`_handle_post_turn_memory` 跳过），避免机器自说自话被当成用户偏好沉淀 |
| working memory | 推送 session 独立，不碰用户当前会话的临时上下文 |

**内容组成**（`wellnessagent/push.py`）：

```
daily_suggestion : 画像 + 提纯记忆 + 近期反馈 → 当日饮食建议（复用 orchestrator composite 或轻量单次 LLM）
followup         : advice_store.due_for_followup() → "3天前那份减脂食谱执行得怎么样？"
```

两类可合并为一条推送。

### 4.4 投递：inbox + webhook 预留

| 通道 | 说明 |
|------|------|
| **落盘 inbox（主）** | `logs/push/<user_id>.inbox.jsonl`，字段 `{push_id, ts, kind, title, body, read, ref_advice_id}` |
| `GET /api/push/inbox` | 前端 `PushInbox.tsx` 拉取 + 未读角标 |
| `POST /api/push/read` | 标记已读 |
| **webhook（预留）** | `WELLNESS_PUSH_WEBHOOK_URL` 非空时 POST 一份 JSON；失败只记 trace 不影响 inbox |

不接微信/邮件：零外部依赖、可离线测试、portfolio 演示足够；想接飞书/企微时配一个 URL 即可。

### 4.5 环境变量与 trace

| 变量 | 默认 | 说明 |
|------|------|------|
| `WELLNESS_PUSH_ENABLED` | `false` | 调度总开关 |
| `WELLNESS_PUSH_TICK_SECONDS` | `60` | 扫描间隔 |
| `WELLNESS_PUSH_DEFAULT_TIME` | `08:30` | 默认推送时刻 |
| `WELLNESS_PUSH_WEBHOOK_URL` | (unset) | 外部投递预留 |
| `WELLNESS_FOLLOWUP_DELAY_HOURS` | `24` | 建议生成后多久回访 |

trace 新增：`push_scheduled` / `push_generated` / `push_delivered` / `push_error`。

### 4.6 验收对照（R10）

| 验收标准 | 实现点 |
|---------|--------|
| 到达设定时间能生成并送达个性化建议 | §4.1 tick + §4.3 生成 + §4.4 inbox |
| 建议明显反映画像与近期反馈，而非通用模板 | §4.3 读画像/提纯记忆/`advice_store` 反馈 |
| 可被用户开关与配置 | §4.2 `/api/push/plan` |

---

## 5. R12 记忆与事件时间线

**关联缺陷**：—　**依赖**：R5（数据来源，✅）、R9/R11（数据丰富度）　**工作量**：M　**状态**：⬜

### 5.1 关键坑：不能用 `EpisodicMemory.get_timeline()`

现有 `memory/types/episodic.py` 有 `get_timeline()`，但它读的是**内存里的 `self.episodes` 列表**——权威数据在 SQLite `doc_store`，重启后这个 list 是空的。直接拿它做 API 会得到"重启后时间线一片空白"。

**因此本期新建 `wellnessagent/timeline.py` 做多源聚合**，从各自的**权威源**读取。

### 5.2 多源聚合

| 来源 | 权威位置 | 产出 `kind` |
|------|---------|------------|
| episodic `category=current_profile` | SQLite `doc_store` | `profile_update` |
| episodic `distilled=true` | SQLite `doc_store` | `memory_distilled` |
| episodic 其他 | SQLite `doc_store` | `memory_episodic` |
| working memory | 内存 + TTL | `memory_working` |
| perceptual（R9） | SQLite `doc_store` | `perception` |
| advice_store（R11） | `logs/advice/*.jsonl` | `advice` / `feedback` |
| trace `safety_block` | `logs/traces/*.jsonl` | `safety` |
| trace `confirm_request` / `confirm_resume` | 同上 | `confirm` |
| push inbox（R10） | `logs/push/*.inbox.jsonl` | `push` |

**统一条目 schema**：

```python
{"ts": ISO, "kind": str, "title": str, "detail": str,
 "importance": float | None, "source": str, "ref_id": str,
 "extra": dict}     # perception 带 file_id/preview_url；advice 带 status
```

这让时间线不只是"记忆"，而是**整个 agent 的演化史**——记忆产生、画像变更、安全拦截、人工确认、拍照记录、建议与反馈，全在一条轴上。demo 效果远强于只做 episodic。

### 5.3 接口与前端

```
GET /api/memory/timeline?user_id=&kinds=&since=&until=&limit=
→ {"items": [...], "counts_by_kind": {...}}
```

前端新增 `MemoryTimelinePage.tsx`（`App.tsx` 第三个 tab）：

- 垂直时间轴，`kind` 用颜色 + 图标区分
- 顶部按 `kind` 多选筛选 + 时间范围
- 点击展开详情；`perception` 条目内嵌缩略图；`advice` 条目显示状态徽标
- 复用现有 `styles.css` 视觉语言，与 `TracePanel` / `MetricsPage` 保持一致

### 5.4 验收对照（R12）

| 验收标准 | 实现点 |
|---------|--------|
| 能按时间顺序看到记忆的新增/提纯/淘汰 | §5.2 `memory_episodic` / `memory_distilled` / `memory_working`（TTL 过期即不再出现） |
| 可区分记忆类型并查看单条详情 | §5.3 `kind` 筛选 + 展开详情 |

---

## 6. R13 MCP Server 对外暴露

**关联缺陷**：—　**依赖**：R6（能力边界清晰，✅）　**工作量**：M　**状态**：⬜

### 6.1 现状与前置

`protocols/mcp/server.py` 是**通用 fastmcp 封装**，`create_example_server()` 只注册了 calculator/greet 两个示例，`wellnessagent/` 业务代码从未引用它。另外 `tools/builtin/mcp_wrapper_tool.py` 是 **client 侧**（把外部 MCP 工具包进来），与本需求方向相反。

前置：**P3.0-4 软依赖化**（否则没装 fastmcp 时 import 就崩）。

### 6.2 暴露范围：只读为主

新增 `wellnessagent/mcp_server.py`：

| MCP tool | 映射 | 写权限 |
|----------|------|--------|
| `wellness_ask` | `service.kb_answer` | 只读 |
| `wellness_profile_get` | `service.profile_get` | 只读 |
| `wellness_memory_search` | `service.memory_search` | 只读 |
| `wellness_plan_meal` | orchestrator composite 一次规划 | 只读（**不落画像**） |
| `wellness_analyze_food_photo` | R9 `FoodRecognizer`（入参本地路径或 base64） | 只读 |
| `wellness_timeline` | R12 `timeline.aggregate` | 只读 |

**不暴露 `profile_set`**：外部客户端不该能改用户长期画像。预留 `WELLNESS_MCP_ALLOW_WRITE`（默认 `false`），置 true 时才额外注册写工具。

### 6.3 user_id 与传输

- MCP 无会话概念 → 每个 tool 加**可选 `user_id` 参数**，缺省读 `WELLNESS_MCP_USER_ID`（默认 `mcp_user`）
- 传输：默认 stdio（Claude Desktop 等直接可用），`--transport http --port` 可选
- **复用 `deps.get_agent`** 拿 agent 实例（连带 LRU 与 SessionStore 重建），不新建一套生命周期

### 6.4 接入示例（验收要求"有可运行的接入示例"）

`examples/mcp_server_demo.py` + README 片段：

```json
{
  "mcpServers": {
    "wellness": {
      "command": "python",
      "args": ["-m", "wellnessagent.mcp_server"],
      "env": {"WELLNESS_MCP_USER_ID": "web_user", "DASHSCOPE_API_KEY": "..."}
    }
  }
}
```

### 6.5 验收对照（R13）

| 验收标准 | 实现点 |
|---------|--------|
| 一个 MCP 兼容客户端能发现并调用暴露的能力 | §6.2 六个 tool 注册 + stdio 传输 |
| 有可运行的接入示例 | §6.4 `examples/mcp_server_demo.py` + 配置片段 |

---

## 7. R14 Benchmark 扩展：A/B 实验与新 Evaluator

**关联缺陷**：—　**依赖**：R5/R8（✅）、R3/R6（被评测对象，✅）　**工作量**：L　**状态**：⬜

### 7.1 `--variant`：与 `--baseline` 正交的新维度

现有 `--baseline` 是**工具能力梯度**（`llm_only → llm_memory → llm_rag → full_agent`），不能表达"同样的 full_agent，开编排 vs 关编排"。

新增 `benchmark/variants.py`，variant = **一组环境变量 overlay**：

| variant | overlay |
|---------|---------|
| `monolith` | `WELLNESS_ORCHESTRATION_ENABLED=false` |
| `orchestrated` | `WELLNESS_ORCHESTRATION_ENABLED=true` |
| `guardrails_off` / `guardrails_rule` / `guardrails_rule_llm` | `WELLNESS_GUARDRAILS_MODE=*` |
| `vision_on` / `vision_off` | `WELLNESS_VLM_ENABLED=*` |
| `hitl_off` | `WELLNESS_HITL_ENABLED=false` |

选 env overlay 而非配置文件：项目所有开关本来就是 env，天然契合；配置文件作为后续可选增强。

**⚠️ 必须处理的坑**：agent 是进程内 LRU 缓存的（`deps._AGENTS`），**切 variant 必须清缓存重建**，否则旧配置的 agent 实例会被复用，A/B 结果失真。方案：`deps.reset_agents()` 新入口，`BenchmarkRunner` 在每次 variant 切换时调用。

```
python -m benchmark.cli --baseline full_agent \
       --variant monolith --variant orchestrated --limit 10 --ab
```

### 7.2 两个新 evaluator

**`benchmark/evaluators/orchestration.py`**（补二期遗留第 10 步）：

| 指标 | 口径 |
|------|------|
| `constraint_adherence` | 过敏原/预算/时间约束遵守率——复用 `safety_rules.rule_hits` 检测答案是否触碰 task 声明的禁忌 |
| `subtask_coverage` | 任务要求的子目标在答案中的覆盖比例（关键词 + 可选 LLM judge） |
| `orchestration_visible` | trace 中是否存在 `orchestrator_*` / `subagent_*` 事件（编排可见性） |

**`benchmark/evaluators/safety.py`**：

| 指标 | 口径 |
|------|------|
| `block_recall` | 应拦截用例中实际触发 `safety_block` 的比例 |
| `false_block_rate` | 正常用例中被误拦的比例 |
| `warning_present` | 命中风险时答案是否含明确警示 |

### 7.3 A/B 报告

`benchmark/ab.py` 产出 `reports/ab/<exp_id>/comparison.md`：

- **逐 task 配对差值表**（同 task 同 baseline，variant A vs B）
- **均值 delta** + 胜/平/负计数
- 关键指标：约束遵守率、安全拦截、平均步数、时延、终止原因分布

**不做统计显著性检验**：20 个 task 样本量太小，做了也是自欺。报告中明确标注这一口径限制。

### 7.4 验收对照（R14）

| 验收标准 | 实现点 |
|---------|--------|
| 能一键跑出两种以上配置的对照结果并生成报告 | §7.1 `--variant` × `--ab` → §7.3 `comparison.md` |
| 新增 evaluator 有明确口径且结果可复现 | §7.2 两个 evaluator（口径表 + 仅依赖已落盘 trace 字段） |

---

## 8. 落地顺序与里程碑

| 序 | 任务 | 依赖 | 验证 | 状态 |
|----|------|------|------|------|
| **批次 1：前置修复（纯修 bug + 软依赖，零风险）—— ✅ 已交付：275 → 406 passed** |
| 1 | P3.0-2 perceptual `update()` bug | — | `test_perceptual_memory.py` 向量重索引单测（已反向验证：恢复 bug 后 4 项变红） | ✅ |
| 2 | P3.0-1 CLIP/CLAP 懒加载 + 开关 | — | 构造不触发 `from_pretrained`（假 transformers 计数为 0） | ✅ |
| 3 | P3.0-3 `_AGENTS` RLock | — | `test_deps_thread_safety.py`：32 线程同一 user 只构造 1 个实例（已反向验证） | ✅ |
| 4 | P3.0-4 fastmcp 软依赖 | — | 无 fastmcp 环境可 import；实例化才报友好错误 | ✅ |
| 5 | P3.0-5 orchestration + safety evaluator | — | evaluator 口径单测（共 74 项）+ `total_score` 不受污染断言 + baseline 透传 `orchestration`/`safety` 的端到端断言 | ✅ |
| **批次 2：R9 全链路多模态（最高风险，须全量回归）—— ✅ 已交付：406 → 687 passed** |
| 6 | `Message.content` 联合类型 + `text_content` 派生 | 1–5 | **一期 `test_message.py` 30 项原样绿** + 新增 list content 单测 | ✅ |
| 7 | `core/media_store.py` + `/api/upload` + `/api/media` | 6 | 落盘/去重/解析/TTL 单测 | ✅ |
| 8 | `TokenCounter` / `HistoryManager` 多模态适配 | 6 | 图片计数 + `demote_media` 阶梯单测 + `test_context.py` 回归 | ✅ |
| 9 | `llm.supports_vision()` + `_get_vision_llm()` + 本轮切换 | 6 | 探测两路径 + 不含图时走原 llm（等价性断言） | ✅ |
| 10 | `wellnessagent/vision.py` 识餐 + 安全联动 + 感知记忆 | 7,9 | mock VLM 的识别/风险命中/记忆写入单测 | ✅ |
| 11 | 前端图片上传 + 气泡 + `VisionCard` | 7,10 | `tsc && vite build` 通过 | ✅ |
| **批次 3：R11 → R10（有依赖顺序）** |
| 12 | `advice_store.py` + composite 记录 + `advice_feedback` 工具 | 批次 1 | 状态机 + 到期查询单测 | ⬜ |
| 13 | 回访 system 注入 + `/api/advice/*` | 12 | 注入内容单测 | ⬜ |
| 14 | `scheduler.py` + `push.py` + inbox + `/api/push/*` | 12,13 | 注入时钟的 `run_due` 单测 + 会话隔离断言 | ⬜ |
| **批次 4：R12 / R13 / R14（数据齐了才好看）** |
| 15 | `timeline.py` 多源聚合 + `/api/memory/timeline` | 10,12,14 | 各源归一化 + 排序/筛选单测 | ⬜ |
| 16 | `MemoryTimelinePage.tsx` + `PushInbox.tsx` + tab | 14,15 | `tsc && vite build` 通过 | ⬜ |
| 17 | `mcp_server.py` + `examples/mcp_server_demo.py` | 10,15 | tool 注册清单 + 只读约束单测 | ⬜ |
| 18 | `variants.py` + `deps.reset_agents()` + `ab.py` + CLI | 5 | variant overlay 生效 + 缓存重建断言 | ⬜ |

> **分批合入**：批次 1（零风险）→ **批次 2（内核改造，最高风险，须 275 单测全绿 + benchmark 全回归）** → 批次 3（业务闭环）→ 批次 4（外围与评测）。

---

## 9. 对外接口契约

### 9.1 保持

- `POST /api/chat`、`/api/chat/stream` 现有字段**全部不变**（`answer / terminated_reason / steps / state / session_id / trace_id / safety / orchestration / confirmation`）
- `terminated_reason` 对外取值集合不变
- `ReActAgent.run()/run_with_trace()/stream_run()`、`WellnessPlanningAgent.chat_with_trace()/chat_stream()` 签名保持（仅**新增可选参数**）
- **不含附件的请求与二期行为字节级等价**
- `Message("hello","user").to_openai() == {"role":"user","content":"hello"}` —— 纯文本形态永不变形

### 9.2 变更 / 新增

- `ChatRequest` 新增可选 `attachments: list[Attachment]`
- `/api/chat` 响应新增可选 `vision`（识餐结构化结果）
- 新增流式事件：`VISION_START`、`VISION_RESULT`
- 新接口：
  - `POST /api/upload`、`GET /api/media/{file_id}`（R9）
  - `GET /api/advice/list`、`POST /api/advice/feedback`（R11）
  - `GET /api/push/inbox`、`POST /api/push/read`、`POST /api/push/plan`、`POST /api/push/run-now`（R10）
  - `GET /api/memory/timeline`（R12）
- trace `EVENT_TYPES` 新增：`vision_start/vision_result/vision_error`、`advice_recorded/advice_feedback`、`push_scheduled/push_generated/push_delivered/push_error`

### 9.3 新增环境变量汇总

| 变量 | 默认 | 作用 |
|------|------|------|
| `WELLNESS_VLM_ENABLED` | `false` | R9 多模态总开关 |
| `WELLNESS_VLM_MODEL_ID` | `qwen-vl-max-latest` | 视觉模型（可切 `qwen-vl-plus`） |
| `WELLNESS_VLM_API_KEY` / `_BASE_URL` | 回落 `DASHSCOPE_API_KEY` / qwen 兼容端点 | 视觉模型凭据 |
| `WELLNESS_FORCE_VISION` | (unset) | 强制开/关视觉探测 |
| `WELLNESS_VLM_MAX_IMAGE_MB` | `8` | 单图大小上限 |
| `WELLNESS_IMAGE_TOKEN_COST` | `1024` | 单图 token 估算 |
| `WELLNESS_MEDIA_DIR` | `logs/uploads` | 媒体落盘目录 |
| `WELLNESS_MEDIA_TTL_DAYS` | `30` | 媒体清理天数 |
| `WELLNESS_CLIP_ENABLED` / `WELLNESS_CLAP_ENABLED` | `false` | 感知记忆向量编码器（懒加载；关闭时图像/音频走确定性哈希向量） |
| `CLIP_VECTOR_DIM` / `CLAP_VECTOR_DIM` | `512` | 编码器**启用时**的 Qdrant 集合维度。懒加载下构造期探测不到真实投影维度，而集合维度必须建集合时定下，故用期望维度常量；账号换了别的模型时用它对齐（实现期补充） |
| `WELLNESS_ADVICE_DIR` | `logs/advice` | R11 建议记录目录 |
| `WELLNESS_FOLLOWUP_DELAY_HOURS` | `24` | 建议生成后回访延迟 |
| `WELLNESS_PUSH_ENABLED` | `false` | R10 调度总开关 |
| `WELLNESS_PUSH_TICK_SECONDS` | `60` | 调度扫描间隔 |
| `WELLNESS_PUSH_DEFAULT_TIME` | `08:30` | 默认推送时刻 |
| `WELLNESS_PUSH_DIR` | `logs/push` | 推送计划与 inbox 目录 |
| `WELLNESS_PUSH_WEBHOOK_URL` | (unset) | 外部投递预留 |
| `WELLNESS_MCP_USER_ID` | `mcp_user` | R13 缺省 user |
| `WELLNESS_MCP_ALLOW_WRITE` | `false` | R13 是否暴露写工具 |

---

## 10. 风险与兼容性

1. **`Message.content` 联合类型是本期唯一内核破坏性变更**（最高风险）。缓解：**C2 纯文本不变形**硬约束 + 批次 2 第 6 步单独提交并跑一期 `test_message.py` 全量；所有"只要文字"的下游统一改调 `text_content`，禁止散落 `isinstance` 判断。
2. **base64 膨胀**：绝不把 base64 写进 `Message.content`。SessionStore / trace / token 计数只见 `file_id`。CI 加断言：session JSON 单文件 < 512 KB。
3. **多模态默认关闭**：`WELLNESS_VLM_ENABLED=false` 时，上传接口 501、`_select_llm_for_turn` 恒返回主 llm，行为与二期完全一致——**不配视觉模型也能跑全量测试**。
4. **感知记忆重依赖**：CLIP/CLAP 懒加载 + 默认关闭（P3.0-1），避免 LRU 50 个用户实例各加载一遍模型。
5. **后台线程安全**（R10）：必须先做 P3.0-3 的 `_AGENTS` 锁；推送轮**不写 distill**、跑独立 session，防止污染用户对话与长期记忆。
6. **推送隐私/打扰**：默认关闭；`push_*` trace 沿用一期脱敏；用户可随时 `/api/push/plan` 关停。
7. **MCP 写权限**：默认只读；`wellness_plan_meal` 不落画像，避免外部客户端静默改写用户长期数据。
8. **A/B 缓存污染**（R14）：variant 切换必须 `deps.reset_agents()`，否则复用旧配置实例导致结论错误。
9. **VLM 成本与时延**：双模型策略下只有含图轮次调用 VL 模型；识餐一次调用 + 主循环按需看图，成本可控。`qwen-vl-max` 无权限时切 `qwen-vl-plus`。
10. **前端体积**：图片预览用 `URL.createObjectURL` + 服务端缩略，不把原图塞进 React state。

---

## 11. 验收与度量（第三期整体）

- **功能**：R9–R14 各"验收对照"逐条通过。
- **回归**：一/二期 **275 单测保持绿**；`WELLNESS_VLM_ENABLED=false` 时与二期行为等价。
- **多模态**：上传食物照片 → 识别 + 热量区间 + 过敏原警示；**同一张图可在后续轮次被追问**；重启后带 `session_id` 续接，图片仍可解析。
- **安全**：含用户过敏原的照片必被警示（提示层 / HITL / guardrails 三层至少命中一层），事件在 trace 可见。
- **闭环**：建议记录 → 到期回访 → 用户反馈 → 影响后续规划，全链路在时间线可见。
- **主动性**：`/api/push/run-now` 能立即产出个性化建议并进 inbox；内容体现画像与近期反馈。
- **可视化**：时间线覆盖 9 类事件，可按 kind 筛选，重启后数据不丢（读权威源而非内存）。
- **互操作**：MCP 客户端能发现并调用 6 个只读能力，示例可运行。
- **量化**：`--variant monolith` vs `orchestrated` 出配对对照报告，约束遵守率不下降。

---

## 12. 评审待确认 / 后续可选项

1. **音频模态（ASR）**：`MediaStore` 与 `Message.content` 已为 `audio` 预留结构；语音输入记录饮食可作为四期候选。
2. **逐 token 流式**：一期至今 `LLM_CHUNK` 都是对最终答案切片模拟。接 `astream_invoke` 做真逐 token 需改执行模型，单独立项。
3. **知识库语料扩充**：二期已列为可选后续项，仍未做；扩到几十篇后 `retrieval` SubAgent 与 RAG 高级检索（MQE/HyDE）的价值才充分显现，可作为 R14 A/B 的新对照维度。
4. **推送外部通道**：当前仅 inbox + webhook 预留；如需飞书/企微/邮件可在 `push.py` 增加 deliverer 实现。
5. **A/B 统计显著性**：需 task 集扩到 50+ 才有意义，届时再引入配对检验。
6. **评测集（`benchmark/tasks/`）改造 —— 已评估，决定推后到批次 4 与 `--variant` 一起做**。
   批次 1 落地 P3.0-5 时通读了 20 个任务，诊断出 5 个设计问题（保留在此，避免重新分析）：

   | # | 问题 | 依据 | 影响的指标 |
   |---|------|------|-----------|
   | 1 | `forbidden_terms` **混装两种语义** | task_01 的 `['花生','花生酱']` 是**过敏红线**（踩了危险）；task_03 的 `['鸡蛋','牛奶','鸡胸肉','鱼']` 只是**纯素模式的逻辑推论**（踩了不合规）。二者共用同一字段与 `hard_constraint` 权重 | `block_recall` 分母混入 13 个"本不该触发 guardrails 拦截"的任务 —— `DietGuardrails` 只从 `allergies/dislikes/medical_notes` 抽风险词，**不看 `diet_pattern`** |
   | 2 | `required_terms` 易刷分 | `['蛋白','早餐']` / `['午餐']` / `['今晚']`；task_15 的 `['过敏']` 尤甚（用户自己说过，鹦鹉学舌即得分） | `goal_alignment`（权重 0.2–0.4）实际区分度接近 0；`subtask_coverage` 同样受影响 |
   | 3 | **没有一个任务真正需要编排** | 20 个任务全是"给约束 → 出方案"，单体一轮即可完成 | `--variant monolith vs orchestrated` 的 A/B 会得出"编排无提升"，但这是**任务没考验到编排**，不是编排无效 |
   | 4 | RAG 任务是"明示指令" | task_17–19 均写明"请结合知识库/营养知识"；知识库仅 5 篇 ~1KB 文档，`required_knowledge_points`（`米饭`/`土豆`）就是从原文挑的词 | 测的是"听话照做"而非"知道该查资料"；`rag_invocation` 区分度弱 |
   | 5 | `false_block_rate` 分母仅 4 个且场景单一 | 仅 task_14/17/18/19 无风险声明，而这 4 个**全是**纯知识问答；误拦最易发生的"提到食物但无禁忌"场景（如"给我一个高蛋白早餐"）**完全没有覆盖** | `false_block_rate` 几乎不可用 |

   **三个候选方案**（届时再定）：

   | 方案 | 动作 | 历史分数可比性 |
   |------|------|---------------|
   | A｜最小补充 | 只加 4 个无风险声明的对照任务（21–24） | ✅ 完全可比，仅解决问题 5 |
   | **B｜补充 + 修字段（倾向）** | A + 拆 `forbidden_terms` / 新增 `incompatible_terms` + 加 2 个复合编排任务 + 1 个隐式 RAG 任务 → 共 27 个 | ❌ `hard_constraint_score` 口径变化，历史报告不可比 |
   | C｜重做任务集 | 重新设计 30+ 任务（含多模态占位），为显著性检验铺路 | ❌ 等于推翻二期基线 |

   **待定的三个取舍**：(a) `incompatible_terms` 是否真加（要改 `TaskExpected` + 20 份 JSON 挪值）；
   (b) 复合任务的答案判定用确定性关键词还是开 LLM judge（后者引入不确定性与 token 成本）；
   (c) 难度分布是否从 easy 4 / medium 12 / hard 4 调整为中间层更薄的形态。
7. **`safety_rules.expand_aliases` 返回 `set`（已在评测侧规避，运行时仍存在）**：
   批次 1 实测发现别名迭代顺序随 `PYTHONHASHSEED` 变化，会让评测报出的 `alias` 跳变，
   违反 R14「结果可复现」。已在 `benchmark/evaluators/hard_constraints.py` 侧修正
   （`expand_aliases` 强制字典序；`alias_hits` **先筛实际命中项、再在命中集里取最长**——
   不能按别名长度预排序，因为 `peanut`(6) 比 `花生酱`(3) 长却未必出现在中文答案里）。
   已加多 seed 回归（`PYTHONHASHSEED=0/1/42/12345/99999` 全绿）。
   `wellnessagent/safety_rules.py` 运行时实现未改动（`rule_hits` 只关心"是否命中"、
   不关心"哪个别名"，故不受影响）；若将来 R9 识餐要展示"命中了哪个别名"，需一并定序。

