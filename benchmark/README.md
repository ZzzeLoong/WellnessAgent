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
