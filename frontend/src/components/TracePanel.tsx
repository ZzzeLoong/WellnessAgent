import { StepRecord } from "../types";
import { ToolCallCard } from "./ToolCallCard";

type Props = {
  steps: StepRecord[];
  terminatedReason: string;
};

export function TracePanel({ steps, terminatedReason }: Props) {
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>步骤回放</h2>
        <span className="badge">{terminatedReason || "idle"}</span>
      </div>
      <div className="trace-list">
        {steps.length === 0 ? (
          <p className="muted">还没有可展示的 trace。</p>
        ) : (
          steps.map((step) => <ToolCallCard key={step.step_index} step={step} />)
        )}
      </div>
    </section>
  );
}
