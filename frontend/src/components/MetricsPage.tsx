import { useEffect, useState } from "react";

import { MetricsResponse } from "../types";

type Props = {
  userId: string;
};

/** 简易水平条形图：把 {label: count} 渲染为条形。 */
function BarChart({ data }: { data: Record<string, number> }) {
  const entries = Object.entries(data);
  if (entries.length === 0) {
    return <p className="muted">暂无数据。</p>;
  }
  const max = Math.max(...entries.map(([, v]) => v), 1);
  return (
    <div className="bar-chart">
      {entries.map(([label, value]) => (
        <div key={label} className="bar-row">
          <span className="bar-label">{label}</span>
          <div className="bar-track">
            <div className="bar-fill" style={{ width: `${(value / max) * 100}%` }} />
          </div>
          <span className="bar-value">{value}</span>
        </div>
      ))}
    </div>
  );
}

/** R8 指标面板：步数/工具频次/终止原因/分诊/安全拦截/SubAgent/时延。 */
export function MetricsPage({ userId }: Props) {
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [scope, setScope] = useState<"user" | "all">("all");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function loadMetrics() {
    setLoading(true);
    setError("");
    const params = scope === "user" ? `?user_id=${encodeURIComponent(userId)}` : "";
    try {
      const response = await fetch(`/api/metrics${params}`);
      if (!response.ok) {
        setError("加载指标失败");
        return;
      }
      setMetrics(await response.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载指标失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadMetrics();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, userId]);

  return (
    <section className="metrics-page">
      <div className="metrics-toolbar">
        <h2>指标面板</h2>
        <div className="metrics-controls">
          <label>
            范围
            <select value={scope} onChange={(e) => setScope(e.target.value as "user" | "all")}>
              <option value="all">全部用户</option>
              <option value="user">当前用户（{userId}）</option>
            </select>
          </label>
          <button type="button" onClick={() => void loadMetrics()} disabled={loading}>
            {loading ? "刷新中..." : "刷新"}
          </button>
        </div>
      </div>

      {error ? <div className="status-banner">{error}</div> : null}

      {metrics ? (
        <>
          <div className="metrics-cards">
            <MetricCard label="总回合数" value={metrics.turns} />
            <MetricCard label="每回合平均步数" value={metrics.avg_steps_per_turn} />
            <MetricCard label="安全拦截" value={metrics.safety_blocks} />
            <MetricCard label="熔断次数" value={metrics.circuit_open} />
            <MetricCard label="HITL 触发" value={metrics.confirm_requests} />
            <MetricCard label="HITL 恢复" value={metrics.confirm_resumes} />
            <MetricCard label="时延 p50" value={metrics.latency_ms.p50 ?? "-"} suffix="ms" />
            <MetricCard label="时延 p95" value={metrics.latency_ms.p95 ?? "-"} suffix="ms" />
          </div>

          <div className="metrics-grid">
            <div className="panel">
              <div className="panel-header"><h3>工具调用频次</h3></div>
              <BarChart data={metrics.tool_calls} />
            </div>
            <div className="panel">
              <div className="panel-header"><h3>终止原因分布</h3></div>
              <BarChart data={metrics.terminated_reason_dist} />
            </div>
            <div className="panel">
              <div className="panel-header"><h3>分诊路由分布</h3></div>
              <BarChart data={metrics.route_dist} />
            </div>
            <div className="panel">
              <div className="panel-header"><h3>SubAgent 统计</h3></div>
              {Object.keys(metrics.subagent_stats).length === 0 ? (
                <p className="muted">暂无子代理调用。</p>
              ) : (
                <table className="metrics-table">
                  <thead>
                    <tr>
                      <th>子代理</th>
                      <th>调用</th>
                      <th>平均步数</th>
                      <th>平均耗时</th>
                      <th>失败率</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(metrics.subagent_stats).map(([name, stat]) => (
                      <tr key={name}>
                        <td>{name}</td>
                        <td>{stat.calls}</td>
                        <td>{stat.avg_steps}</td>
                        <td>{stat.avg_duration_ms}ms</td>
                        <td>{(stat.fail_rate * 100).toFixed(1)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </>
      ) : (
        <p className="muted">{loading ? "加载中..." : "暂无指标数据。"}</p>
      )}
    </section>
  );
}

function MetricCard({
  label,
  value,
  suffix = "",
}: {
  label: string;
  value: number | string;
  suffix?: string;
}) {
  return (
    <div className="metric-card">
      <span className="metric-value">
        {value}
        {suffix}
      </span>
      <span className="metric-label">{label}</span>
    </div>
  );
}

