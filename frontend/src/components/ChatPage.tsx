import { useEffect, useMemo, useState } from "react";

import { ChatComposer } from "./ChatComposer";
import { KnowledgebaseViewer } from "./KnowledgebaseViewer";
import { MessageList } from "./MessageList";
import { StatePanel } from "./StatePanel";
import { TracePanel } from "./TracePanel";
import { AppState, ChatMessage, ChatResponse, ProfilePayload } from "../types";

const defaultProfile: ProfilePayload = {
  allergies: ["花生", "虾"],
  diet_pattern: "奶蛋素",
  goal: "减脂并保持饱腹感",
  dislikes: ["芹菜"],
  medical_notes: ["晚餐后容易饥饿"],
  preferred_cuisines: ["中式", "简易家常"],
  cooking_constraints: ["工作日做饭不超过20分钟"],
  notes: "希望早餐简单，午餐可以带饭。",
};

export function ChatPage() {
  const [userId, setUserId] = useState("web_user");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [state, setState] = useState<AppState | null>(null);
  const [steps, setSteps] = useState<ChatResponse["steps"]>([]);
  const [terminatedReason, setTerminatedReason] = useState("idle");
  const [isLoading, setIsLoading] = useState(false);
  const [activeFileName, setActiveFileName] = useState("");
  const [activeFileContent, setActiveFileContent] = useState("");
  const [profileStatus, setProfileStatus] = useState("");

  const knowledgebaseFiles = useMemo(() => state?.knowledgebase_files ?? [], [state]);

  useEffect(() => {
    void loadState(userId);
  }, [userId]);

  async function loadState(targetUserId: string) {
    const response = await fetch(`/api/state?user_id=${encodeURIComponent(targetUserId)}`);
    if (!response.ok) {
      return;
    }
    const payload: AppState = await response.json();
    setState(payload);
    if (!activeFileName && payload.knowledgebase_files[0]) {
      void handleSelectKnowledgebaseFile(payload.knowledgebase_files[0].name);
    }
  }

  async function handleApplyProfile() {
    setProfileStatus("正在写入示例画像...");
    const response = await fetch("/api/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, profile: defaultProfile }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setProfileStatus(payload.detail ?? "写入画像失败");
      return;
    }
    setProfileStatus(payload.message ?? "示例画像已更新");
    await loadState(userId);
  }

  async function handleClearMemories() {
    const confirmed = window.confirm(
      `确认清空用户 ${userId} 的所有记忆吗？此操作只影响当前 user_id。`,
    );
    if (!confirmed) {
      return;
    }

    setProfileStatus("正在清空当前用户记忆...");
    const response = await fetch("/api/memory/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setProfileStatus(payload.detail ?? "清空记忆失败");
      return;
    }
    setMessages([]);
    setSteps([]);
    setTerminatedReason("idle");
    setProfileStatus(payload.message ?? "已清空当前用户记忆");
    await loadState(userId);
  }

  async function handleSend(message: string) {
    setIsLoading(true);
    setProfileStatus("");
    try {
      setMessages((current) => [...current, { role: "user", content: message }]);
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId, message }),
      });
      const payload: ChatResponse = await response.json();
      if (!response.ok) {
        throw new Error((payload as unknown as { detail?: string }).detail ?? "对话失败");
      }
      setMessages((current) => [...current, { role: "assistant", content: payload.answer }]);
      setSteps(payload.steps);
      setTerminatedReason(payload.terminated_reason);
      setState(payload.state);
      if (!activeFileName && payload.state.knowledgebase_files[0]) {
        await handleSelectKnowledgebaseFile(payload.state.knowledgebase_files[0].name);
      }
    } catch (error) {
      const content = error instanceof Error ? error.message : "发生未知错误";
      setMessages((current) => [...current, { role: "assistant", content }]);
    } finally {
      setIsLoading(false);
    }
  }

  async function handleSelectKnowledgebaseFile(name: string) {
    setActiveFileName(name);
    const response = await fetch(
      `/api/knowledgebase/files/${encodeURIComponent(name)}?user_id=${encodeURIComponent(userId)}`,
    );
    const payload = await response.json();
    if (!response.ok) {
      setActiveFileContent(payload.detail ?? "无法读取知识库文件");
      return;
    }
    setActiveFileContent(payload.content ?? "");
  }

  return (
    <main className="app-shell">
      <header className="app-header">
        <div>
          <h1>WellnessAgent Debug Console</h1>
          <p>对话、ReAct trace、工具调用结果、Memory 与 RAG 状态统一查看。</p>
        </div>
        <div className="header-controls">
          <label>
            User ID
            <input value={userId} onChange={(event) => setUserId(event.target.value.trim() || "web_user")} />
          </label>
          <button type="button" onClick={() => void handleApplyProfile()}>
            写入示例画像
          </button>
          <button type="button" onClick={() => void handleClearMemories()}>
            清空当前用户记忆
          </button>
        </div>
      </header>

      {profileStatus ? <div className="status-banner">{profileStatus}</div> : null}

      <section className="top-layout">
        <div className="chat-column">
          <ChatComposer isLoading={isLoading} onSend={handleSend} />
          <MessageList messages={messages} />
        </div>
        <TracePanel steps={steps} terminatedReason={terminatedReason} />
      </section>

      <section className="bottom-layout">
        <StatePanel state={state} />
        <KnowledgebaseViewer
          files={knowledgebaseFiles}
          activeFileName={activeFileName}
          activeContent={activeFileContent}
          onSelect={(name) => void handleSelectKnowledgebaseFile(name)}
        />
      </section>
    </main>
  );
}
