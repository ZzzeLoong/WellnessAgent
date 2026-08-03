import { VisionInfo } from "../types";

type Props = {
  vision: VisionInfo;
};

/** R9 识餐结果展示卡片（方案 §2.7 前端渲染）。 */
export function VisionCard({ vision }: Props) {
  const hasRisks = vision.risk_hits.length > 0;
  return (
    <div className={`vision-card ${hasRisks ? "has-risk" : ""}`}>
      <div className="vision-header">
        <span className="vision-icon">{hasRisks ? "⚠️" : "📷"}</span>
        <span className="vision-title">识餐结果</span>
        <span className="vision-model muted">{vision.model}</span>
        <span className="vision-latency muted">{vision.latency_ms}ms</span>
      </div>
      {vision.summary && <p className="vision-summary">{vision.summary}</p>}
      {vision.items.length > 0 && (
        <div className="vision-items">
          {vision.items.map((item, i) => (
            <span key={i} className="vision-tag">
              {item.name}
              {item.confidence != null && (
                <span className="muted"> {(item.confidence * 100).toFixed(0)}%</span>
              )}
            </span>
          ))}
        </div>
      )}
      {vision.ingredients_guess.length > 0 && (
        <div className="vision-ingredients">
          <span className="muted">食材：</span>
          {vision.ingredients_guess.join("、")}
        </div>
      )}
      {vision.calories_range && (
        <div className="vision-calories">
          <span className="muted">热量：</span>
          {vision.calories_range[0]}–{vision.calories_range[1]} kcal
        </div>
      )}
      {hasRisks && (
        <div className="vision-risk-banner">
          <strong>⚠️ 过敏原风险命中：</strong>
          {vision.risk_hits.map((hit) => (
            <span key={hit} className="risk-pill">{hit}</span>
          ))}
        </div>
      )}
    </div>
  );
}

