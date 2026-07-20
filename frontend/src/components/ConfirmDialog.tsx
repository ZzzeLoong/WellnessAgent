import { useState } from "react";

import { ConfirmationDecision, ConfirmationInfo } from "../types";

type Props = {
  confirmation: ConfirmationInfo;
  onDecide: (decision: ConfirmationDecision) => void;
  onCancel: () => void;
};

/** HITL 人机确认弹窗（R7）：用户对高影响步骤同意/拒绝/修改。 */
export function ConfirmDialog({ confirmation, onDecide, onCancel }: Props) {
  const [note, setNote] = useState("");

  const isProfile = confirmation.kind === "profile_update";
  const suggested = (confirmation.payload?.suggested_updates ?? {}) as Record<string, unknown>;
  const hits = (confirmation.payload?.hits ?? []) as string[];

  function decide(decision: ConfirmationDecision["decision"]) {
    const patch: Record<string, unknown> = {};
    if (decision === "modify" && note.trim()) {
      if (isProfile) {
        // 画像修改：把用户输入按 "字段=值" 简单解析（值可用逗号分隔为数组）。
        for (const part of note.split(";")) {
          const [k, v] = part.split("=");
          if (k && v) {
            const key = k.trim();
            const val = v.trim();
            patch[key] = val.includes(",") ? val.split(",").map((s) => s.trim()) : val;
          }
        }
      } else {
        patch.note = note.trim();
      }
    }
    onDecide({ confirm_id: confirmation.confirm_id, decision, patch });
  }

  return (
    <div className="confirm-overlay" role="dialog" aria-modal="true">
      <div className="confirm-dialog">
        <div className="confirm-header">
          <span className={`badge kind-${confirmation.kind}`}>
            {isProfile ? "画像变更确认" : "安全风险确认"}
          </span>
        </div>
        <p className="confirm-prompt">{confirmation.prompt}</p>

        {isProfile && Object.keys(suggested).length > 0 ? (
          <div className="confirm-detail">
            <h4>建议写入的字段</h4>
            <ul>
              {Object.entries(suggested).map(([k, v]) => (
                <li key={k}>
                  <code>{k}</code>：{Array.isArray(v) ? v.join("、") : String(v)}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {!isProfile && hits.length > 0 ? (
          <div className="confirm-detail">
            <h4>命中的风险</h4>
            <p>{hits.join("、")}</p>
          </div>
        ) : null}

        <label className="confirm-note">
          {isProfile ? "修改内容（可选，格式 字段=值；值可逗号分隔）" : "补充约束（可选）"}
          <textarea
            rows={2}
            value={note}
            placeholder={isProfile ? "如 allergies=花生,虾；diet_pattern=纯素" : "如 也不能吃海鲜"}
            onChange={(e) => setNote(e.target.value)}
          />
        </label>

        <div className="confirm-actions">
          <button type="button" onClick={() => decide("approve")}>
            同意
          </button>
          <button type="button" className="secondary" onClick={() => decide("modify")}>
            修改后继续
          </button>
          <button type="button" className="secondary" onClick={() => decide("reject")}>
            拒绝
          </button>
          <button type="button" className="ghost" onClick={onCancel}>
            取消
          </button>
        </div>
      </div>
    </div>
  );
}

