# Benchmark

`benchmark/` 是与主仓库解耦的评测包，用来评估 `WellnessAgent` 在多轮强约束饮食规划任务上的表现。

## 当前内容

- 20 个自建任务（位于 `benchmark/tasks/`），覆盖：
  - 硬约束（过敏原、饮食模式禁忌）
  - 长期画像追踪（`current_profile` 字段）
  - 短期上下文追踪（`session_note` 临时条件）
  - 多轮聚合 / 替换性更新
  - 动态重规划（late constraint）
  - RAG 知识落地
- 4 组 baseline（同一 LLM、同一 ReAct prompt，仅工具能力差异）：
  - `llm_only`：只有 LLM + 多轮历史，没有任何工具、画像或 RAG。
  - `llm_memory`：LLM + 画像/会话/长期记忆工具，无 RAG。
  - `llm_rag`：LLM + 知识库工具，无任何记忆能力。
  - `full_agent`：记忆 + RAG 全部工具的完整产线 Agent。
- 一组 deterministic 评估器，加上一个可选的 LLM judge 钩子。

## 一键运行

```powershell
# 1) 跑最小烟雾测试 (1 个任务 × 全部 baseline)
python -m benchmark.cli --limit 1

# 2) 跑精选 10 任务子集 (覆盖各类型 + 难度)
python -m benchmark.cli --limit 10

# 3) 跑全部 20 个任务
python -m benchmark.cli --all

# 4) 单任务诊断
python -m benchmark.cli --task task_15

# 5) 仅评估某些 baseline
python -m benchmark.cli --baseline llm_only --baseline llm_memory --limit 10
```

CLI 默认会跑 `llm_only / llm_memory / llm_rag / full_agent` 四组 baseline。可以用 `--baseline ...` 多次传入子集，用 `--no-progress` 关闭进度日志，用 `--quiet` 完全静默。

## 硬约束误判与 LLM judge

`hard_constraint_score` 默认采用「分层 hybrid」策略：

1. **子串检测**：forbidden_term 不出现在最终回答里 → 直接通过。
2. **关键词 heuristic**：term 命中后检查前后 16 字符内是否有 `避免/严禁/切忌/请勿/远离/拒绝/⚠️/avoid/exclude` 等警告词；命中视为安全。
3. **小型 LLM judge**：只有当 heuristic 仍然把 term 判为「疑似踩词」时，才把回答和 term 丢给一个轻量 LLM 复核，避免「严禁使用花生酱」「花生酱含有花生过敏成分」这种警告语境被错杀。

模式可通过 CLI 或环境变量切换：

```powershell
# 默认 hybrid（推荐）
python -m benchmark.cli --limit 10

# 完全确定性，不调用任何 LLM judge
python -m benchmark.cli --limit 10 --hard-judge heuristic

# 任何 term 命中都让 LLM 复核（更鲁棒，token 多）
python -m benchmark.cli --limit 10 --hard-judge llm
```

Judge 使用的 LLM 优先级：
- `BENCH_JUDGE_MODEL_ID` / `BENCH_JUDGE_API_KEY` / `BENCH_JUDGE_BASE_URL`（独立覆盖项）
- 回退到 `DISTILL_MODEL_ID` / `DISTILL_API_KEY` / `DISTILL_BASE_URL`（与记忆提纯共享小模型）
- 再回退到默认 `HelloAgentsLLM()`

Judge 的诊断信息会出现在 `scores/<baseline>/<task>.json` 的 `metric_details.hard_constraint.per_term[*]` 中，包含 `reason`（`term_not_in_answer` / `heuristic_safe` / `heuristic_violation`）以及 `judge_used` / `judge.judge_status`，方便事后回看为何被判通过 / 扣分。

## 子集如何选

- `--limit 1` → `task_01`，最快烟雾测试
- `--limit 10` → 精选 10 任务（`task_01/05/06/09/11/13/15/16/18/20`），覆盖所有类型并平衡难度
- `--limit 20` 或 `--all` → 全部 20 个任务

子集定义见 `benchmark/subsets.py`，可按需调整。

## 指标体系

每次评测都会输出以下分数（每项落在 `[0, 1]` 区间）：

| 指标 | 含义 | 是否计入 total |
|---|---|---|
| `hard_constraint_score` | 最终回答是否避免踩到过敏原/饮食禁忌 | ✅ |
| `state_tracking_score` | profile + session 综合状态追踪（兼容旧权重） | ✅ |
| `profile_tracking_score` | 长期画像字段是否被正确写入 | 诊断 |
| `session_tracking_score` | 临时上下文是否被记录到短期记忆 | 诊断 |
| `goal_alignment_score` | 回答是否覆盖目标关键词 | ✅ |
| `replanning_score` | late constraint 后是否真的改了方案 | ✅ |
| `knowledge_coverage_score` | 必要知识点是否在最终回答中出现 | ✅ |
| `rag_invocation_score` | 是否真的调用了 KB 工具（仅 `requires_rag` 任务） | 诊断 |
| `tool_usage_score` | 是否触发了 `must_use_tools_any_of` | 诊断 |
| `total_score` | 上面 ✅ 的指标按 `task.weights` 加权平均 | 总分 |

> ⚠️ 设计原则：**`total_score` 只混入对所有 baseline 公平的指标**。`tool_usage_score` 与 `rag_invocation_score` 对 `llm_only` 而言天然不可达，仅作诊断输出，避免把无工具的 baseline 一杆子打死。

## 报告产物

每次运行会在 `benchmark/reports/` 下生成：

```
reports/
├── raw_runs/<baseline>/<task_id>.json   原始 trace（每一步 thought/action/observation）
├── scores/<baseline>/<task_id>.json     单条评分明细 + metric_details
├── baselines/<baseline>.md              该 baseline 各任务扣分原因
├── summary.csv                          所有指标的 CSV 矩阵
├── summary.md                           按任务 × baseline 的汇总表
└── leaderboard.md                       按 baseline 平均分排序的 leaderboard
```

`baselines/<baseline>.md` 会列出每个任务的命中点、踩到的禁词、re-plan 时是否换工具、知识点覆盖等具体扣分依据，方便回归调试。

### 报告覆盖策略

为了避免「上一轮跑了 20 个 task、这一轮只跑 10 个，剩下 10 个旧 JSON 还在 `raw_runs/` 里」这种污染，runner 默认在每次运行开始时按 baseline 维度清理旧文件：

| 模式 | CLI 开关 | 行为 |
|---|---|---|
| `scoped`（默认） | 无 | 只清理本轮即将跑的 baseline 的 `raw_runs/<bl>/`、`scores/<bl>/`、`baselines/<bl>.md`；其他 baseline 的旧报告**不动**；同时重写 `summary.csv` / `summary.md` / `leaderboard.md`。 |
| `append` | `--append` | 不做任何清理，与 v1 行为一致；适合在已跑过的报告基础上**补**几个 task 的增量运行。 |
| `all` | `--clean-all` | 清空整个 `reports/` 目录后再写；最干净，适合完全重新评测。 |

`--append` 与 `--clean-all` 互斥。

```powershell
# 默认：scoped，正常使用即可
python -m benchmark.cli --limit 10

# 增量补几个 task，不动其他报告
python -m benchmark.cli --task task_15 --append

# 大改之后想完全重置
python -m benchmark.cli --all --clean-all
```

## 调试输出

控制台默认每跑完一个 (task, baseline) 就打一行：

```
[benchmark] [007/040] llm_memory :: task_05 (medium) "Multi-turn profile aggregation" -> running...
[benchmark]    -> done llm_memory::task_05 | hard=1.00 state=0.83 goal=1.00 replan=1.00 know=1.00 tool=1.00 rag_use=1.00 total=0.93 | status=ok | 47.2s
```

任意一对 (task, baseline) 抛错都会被 runner 捕获，结果记成「全 0」并继续后面的任务，不会让单点故障打断整轮 benchmark。

## 任务 schema 速查

`benchmark/tasks/*.json` 字段示例：

```json
{
  "task_id": "task_05",
  "title": "Multi-turn profile aggregation",
  "difficulty": "medium",
  "category": ["state_tracking", "goal_alignment"],
  "turns": [{"role": "user", "content": "..."}],
  "expected": {
    "profile_fields": {"allergies": ["花生"]},
    "profile_must_not_contain": {"dislikes": ["洋葱"]},
    "session_required": ["便利店", "30元以内"],
    "forbidden_terms": ["花生酱"],
    "required_terms": ["午餐"],
    "must_use_tools_any_of": ["profile_set"],
    "requires_replanning": false,
    "requires_rag": false,
    "required_knowledge_points": []
  },
  "weights": {
    "hard_constraint": 0.3,
    "state_tracking": 0.45,
    "goal_alignment": 0.25,
    "replanning": 0.0,
    "rag_grounding": 0.0
  }
}
```
