# Baseline detail: llm_rag

共评估 1 个任务。详细的扣分原因摘自 `metric_details`，方便回归调试。

## task_01 (easy) — total=0.70

- Hard=1.00, State=0.00 (profile=0.00, session=1.00), Goal=1.00, Replan=1.00, Know=1.00, RAGUse=1.00, ToolUse=1.00
- tool_usage: required_any_of=['profile_get', 'profile_set'], matched=['profile_get'], actually_invoked=['kb_answer', 'profile_get']
- weighted total breakdown: hard_constraint_score=1.00*0.40, state_tracking_score=0.00*0.30, goal_alignment_score=1.00*0.30
- final answer preview: 没问题，针对您“早餐要简单、适合普通工作日”的新要求，结合您**花生过敏**和**增肌**的目标，我为您调整了早餐方案。重点在于**省时**和**高蛋白**。 ### 🌞 极简工作日早餐推荐（任选其一） #### 方案一：隔夜燕麦杯 (Overnight Oats) —— 前一天晚上准备，早上直接吃 * **做法**：在前一晚将 **40-50g 即食燕麦片** + **200ml 牛奶/无糖豆浆** + **1勺乳清蛋白粉**（可选，若不加则需多吃一个鸡蛋）混合均匀，放入冰

