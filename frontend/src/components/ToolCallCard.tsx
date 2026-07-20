import { StepRecord, ToolResult } from "../types";

type Props = {
  step: StepRecord;
};

function statusClass(status: string): string {
  if (status === "error") return "tool-status error";
  if (status === "partial") return "tool-status partial";
  return "tool-status success";
}

function isCircuitOpen(result: ToolResult): boolean {
  return (result.content || "").includes("熔断");
}

export function ToolCallCard({ step }: Props) {
  const hasToolCalls = step.tool_calls.length > 0;
  const resultById = new Map(step.tool_results.map((r) => [r.tool_call_id, r]));

  return (
    <article className="tool-card">
      <div className="tool-card-header">
        <span>第 {step.index} 步</span>
        <span className="source-badge">{step.source}</span>
      </div>

      {step.thought ? (
        <div>
          <strong>Thought</strong>
          <pre>{step.thought}</pre>
        </div>
      ) : null}

      {hasToolCalls ? (
        <div className="tool-call-group">
          {step.tool_calls.map((call) => {
            const result = resultById.get(call.id);
            return (
              <div key={call.id} className="tool-call-entry">
                <div className="tool-call-line">
                  <strong>{call.name}</strong>
                  {result ? (
                    <span className={statusClass(result.status)}>{result.status}</span>
                  ) : null}
                  {result && isCircuitOpen(result) ? (
                    <span className="tool-status circuit">熔断</span>
                  ) : null}
                </div>
                {call.arguments && call.arguments !== "{}" ? (
                  <pre className="tool-args">{call.arguments}</pre>
                ) : null}
                {result ? (
                  <pre className="tool-observation">{result.content}</pre>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : (
        <div className="tool-call-line">
          <span className="source-badge">最终回答</span>
        </div>
      )}
    </article>
  );
}
