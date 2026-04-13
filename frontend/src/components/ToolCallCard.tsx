import { StepRecord } from "../types";

type Props = {
  step: StepRecord;
};

export function ToolCallCard({ step }: Props) {
  return (
    <article className="tool-card">
      <div className="tool-card-header">
        <span>第 {step.step_index} 步</span>
        <span>{step.tool_name ?? "Finish"}</span>
      </div>
      {step.thought ? (
        <div>
          <strong>Thought</strong>
          <pre>{step.thought}</pre>
        </div>
      ) : null}
      {step.action_text ? (
        <div>
          <strong>Action</strong>
          <pre>{step.action_text}</pre>
        </div>
      ) : null}
      {step.tool_input ? (
        <div>
          <strong>Tool Input</strong>
          <pre>{step.tool_input}</pre>
        </div>
      ) : null}
      {step.observation ? (
        <div>
          <strong>Observation</strong>
          <pre>{step.observation}</pre>
        </div>
      ) : null}
    </article>
  );
}
