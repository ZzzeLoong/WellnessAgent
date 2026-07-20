import { SafetyInfo, StepRecord } from "../types";
import { ToolCallCard } from "./ToolCallCard";

type Props = {
  steps: StepRecord[];
  terminatedReason: string;
  safety?: SafetyInfo;
};

export function TracePanel({ steps, terminatedReason, safety }: Props) {
  const showSafety = safety && safety.action && safety.action !== "pass";
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>步骤回放</h2>
        <span className="badge">{terminatedReason || "idle"}</span>
      </div>
      {showSafety ? (
        <div className={`safety-banner ${safety!.action}`}>
          <strong>安全校验：{safety!.action === "block" ? "已拦截" : "已改写"}</strong>
          {safety!.reason ? <span>{safety!.reason}</span> : null}
          {safety!.hits && safety!.hits.length > 0 ? (
            <span>命中：{safety!.hits.join("、")}</span>
          ) : null}
        </div>
      ) : null}
      <div className="trace-list">
        {steps.length === 0 ? (
          <p className="muted">还没有可展示的 trace。</p>
        ) : (
          steps.map((step) => <ToolCallCard key={step.index} step={step} />)
        )}
      </div>
    </section>
  );
}
