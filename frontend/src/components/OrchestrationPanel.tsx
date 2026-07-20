import { OrchestrationInfo } from "../types";

type Props = {
  orchestration: OrchestrationInfo;
};

/** 编排可视化面板（R6）：分诊路由 + 各子任务执行摘要。 */
export function OrchestrationPanel({ orchestration }: Props) {
  if (!orchestration) {
    return null;
  }
  const isComposite = orchestration.route === "composite";
  return (
    <section className="panel orchestration-panel">
      <div className="panel-header">
        <h2>编排</h2>
        <span className={`badge route-${orchestration.route}`}>{orchestration.route}</span>
      </div>
      {orchestration.reason ? (
        <p className="muted">分诊理由：{orchestration.reason}</p>
      ) : null}

      {!isComposite ? (
        <p className="muted">单点请求，走单体 ReAct 路径。</p>
      ) : (
        <div className="subagent-list">
          {(orchestration.subagents ?? []).map((sub) => (
            <div key={sub.name} className={`subagent-card ${sub.success ? "ok" : "fail"}`}>
              <div className="subagent-head">
                <strong>{sub.name}</strong>
                <span className="badge">{sub.success ? "完成" : "失败"}</span>
              </div>
              {sub.summary ? <p className="subagent-summary">{sub.summary}</p> : null}
              <div className="subagent-meta">
                {typeof sub.steps === "number" ? <span>步数 {sub.steps}</span> : null}
                {typeof sub.duration_ms === "number" ? <span>{sub.duration_ms}ms</span> : null}
                {sub.tools_used && sub.tools_used.length > 0 ? (
                  <span>工具 {sub.tools_used.join(", ")}</span>
                ) : null}
              </div>
            </div>
          ))}
          {orchestration.pending ? (
            <div className="subagent-card pending">
              <div className="subagent-head">
                <strong>等待确认</strong>
                <span className={`badge kind-${orchestration.pending.kind}`}>
                  {orchestration.pending.kind}
                </span>
              </div>
              <p className="subagent-summary">{orchestration.pending.prompt}</p>
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}

