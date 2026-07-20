import { useEffect, useMemo, useState } from "react";

import { ChatComposer } from "./ChatComposer";
import { ConfirmDialog } from "./ConfirmDialog";
import { KnowledgebaseViewer } from "./KnowledgebaseViewer";
import { MessageList } from "./MessageList";
import { OrchestrationPanel } from "./OrchestrationPanel";
import { StatePanel } from "./StatePanel";
import { TracePanel } from "./TracePanel";
import {
  AppState,
  ChatMessage,
  ChatResponse,
  ConfirmationDecision,
  ConfirmationInfo,
  OrchestrationInfo,
  ProfilePayload,
  SafetyInfo,
  StepRecord,
  ToolCall,
  ToolResult,
} from "../types";

type ChatPageProps = {
  userId: string;
  onUserIdChange: (userId: string) => void;
};

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

type StreamEvent = { type: string; data: Record<string, unknown> };

/** 解析 SSE 文本缓冲，返回已解析事件与剩余未完成缓冲。 */
function parseSseBuffer(buffer: string): { events: StreamEvent[]; rest: string } {
  const events: StreamEvent[] = [];
  const frames = buffer.split("\n\n");
  const rest = frames.pop() ?? "";
  for (const frame of frames) {
    let eventType = "message";
    const dataLines: string[] = [];
    for (const line of frame.split("\n")) {
      if (line.startsWith("event:")) {
        eventType = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }
    if (dataLines.length === 0) {
      continue;
    }
    try {
      events.push({ type: eventType, data: JSON.parse(dataLines.join("\n")) });
    } catch {
      // 忽略无法解析的帧
    }
  }
  return { events, rest };
}

export function ChatPage({ userId, onUserIdChange }: ChatPageProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [state, setState] = useState<AppState | null>(null);
  const [steps, setSteps] = useState<StepRecord[]>([]);
  const [terminatedReason, setTerminatedReason] = useState("idle");
  const [safety, setSafety] = useState<SafetyInfo>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [activeFileName, setActiveFileName] = useState("");
  const [activeFileContent, setActiveFileContent] = useState("");
  const [profileStatus, setProfileStatus] = useState("");
  const [orchestration, setOrchestration] = useState<OrchestrationInfo>(null);
  const [pendingConfirmation, setPendingConfirmation] = useState<ConfirmationInfo | null>(null);

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

  async function handleNewSession() {
    const response = await fetch("/api/session/new", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setProfileStatus(payload.detail ?? "新开会话失败");
      return;
    }
    setSessionId(payload.session_id ?? null);
    setMessages([]);
    setSteps([]);
    setSafety(null);
    setTerminatedReason("idle");
    setOrchestration(null);
    setPendingConfirmation(null);
    setProfileStatus(`已新开会话：${payload.session_id ?? ""}`);
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
    setSafety(null);
    setTerminatedReason("idle");
    setOrchestration(null);
    setPendingConfirmation(null);
    setProfileStatus(payload.message ?? "已清空当前用户记忆");
    await loadState(userId);
  }

  /** 处理单个 SSE 事件，增量更新 UI 状态。 */
  function applyStreamEvent(event: StreamEvent, assistantIndex: number) {
    const { type, data } = event;
    if (type === "agent_start") {
      if (typeof data.session_id === "string") {
        setSessionId(data.session_id);
      }
    } else if (type === "tool_call_start") {
      const step = Number(data.step);
      const call: ToolCall = {
        id: String(data.id ?? ""),
        name: String(data.name ?? ""),
        arguments: String(data.arguments ?? ""),
      };
      setSteps((current) => upsertToolCall(current, step, call));
    } else if (type === "tool_call_finish") {
      const step = Number(data.step);
      const result: ToolResult = {
        tool_call_id: String(data.tool_call_id ?? ""),
        name: String(data.name ?? ""),
        status: String(data.status ?? ""),
        content: String(data.content ?? ""),
      };
      setSteps((current) => upsertToolResult(current, step, result));
    } else if (type === "thinking") {
      const step = Number(data.step);
      const thought = String(data.content ?? "");
      setSteps((current) => upsertThought(current, step, thought));
    } else if (type === "llm_chunk") {
      const delta = String(data.delta ?? "");
      setMessages((current) => appendToAssistant(current, assistantIndex, delta));
    } else if (type === "orchestrator_triage") {
      setOrchestration((current) => ({
        route: String(data.route ?? "simple"),
        reason: typeof data.reason === "string" ? data.reason : undefined,
        subagents: current?.subagents ?? [],
      }));
    } else if (type === "subagent_result") {
      setOrchestration((current) => {
        const subagents = [...(current?.subagents ?? [])];
        subagents.push({
          name: String(data.subagent ?? ""),
          success: Boolean(data.success),
          summary: typeof data.summary === "string" ? data.summary : undefined,
          steps: (data.steps as number | null) ?? null,
          duration_ms: (data.duration_ms as number | null) ?? null,
        });
        return { route: current?.route ?? "composite", reason: current?.reason, subagents };
      });
    } else if (type === "confirm") {
      setPendingConfirmation(data as unknown as ConfirmationInfo);
    } else if (type === "agent_finish") {
      if (Array.isArray(data.steps)) {
        setSteps(data.steps as StepRecord[]);
      }
      if (typeof data.terminated_reason === "string") {
        setTerminatedReason(data.terminated_reason);
      }
      setSafety((data.safety as SafetyInfo) ?? null);
      if (data.state) {
        setState(data.state as AppState);
      }
      if (data.orchestration) {
        setOrchestration(data.orchestration as OrchestrationInfo);
      }
      if (data.confirmation) {
        setPendingConfirmation(data.confirmation as ConfirmationInfo);
      }
      // 若最终答案与增量拼接不一致，以最终答案为准。
      if (typeof data.answer === "string") {
        setMessages((current) => replaceAssistant(current, assistantIndex, String(data.answer)));
      }
    } else if (type === "error") {
      setMessages((current) =>
        replaceAssistant(current, assistantIndex, `发生错误：${String(data.message ?? "未知错误")}`),
      );
    }
  }

  async function handleSend(message: string, confirmation?: ConfirmationDecision) {
    setIsLoading(true);
    setProfileStatus("");
    setSteps([]);
    setSafety(null);
    setTerminatedReason("running");
    if (!confirmation) {
      // 新一轮请求：清空上一轮的编排可视化。确认恢复轮保留展示。
      setOrchestration(null);
    }

    let assistantIndex = -1;
    setMessages((current) => {
      const next = [
        ...current,
        { role: "user", content: message } as ChatMessage,
        { role: "assistant", content: "" } as ChatMessage,
      ];
      assistantIndex = next.length - 1;
      return next;
    });

    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId, message, session_id: sessionId, confirmation }),
      });
      if (!response.ok || !response.body) {
        // 回退到非流式接口。
        await handleSendNonStream(message, assistantIndex, confirmation);
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const { events, rest } = parseSseBuffer(buffer);
        buffer = rest;
        for (const event of events) {
          applyStreamEvent(event, assistantIndex);
        }
      }
    } catch (error) {
      const content = error instanceof Error ? error.message : "发生未知错误";
      setMessages((current) => replaceAssistant(current, assistantIndex, content));
    } finally {
      setIsLoading(false);
      if (!activeFileName && state?.knowledgebase_files[0]) {
        await handleSelectKnowledgebaseFile(state.knowledgebase_files[0].name);
      }
    }
  }

  async function handleSendNonStream(
    message: string,
    assistantIndex: number,
    confirmation?: ConfirmationDecision,
  ) {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, message, session_id: sessionId, confirmation }),
    });
    const payload: ChatResponse = await response.json();
    if (!response.ok) {
      throw new Error((payload as unknown as { detail?: string }).detail ?? "对话失败");
    }
    setMessages((current) => replaceAssistant(current, assistantIndex, payload.answer));
    setSteps(payload.steps);
    setTerminatedReason(payload.terminated_reason);
    setSafety(payload.safety ?? null);
    setState(payload.state);
    setOrchestration(payload.orchestration ?? null);
    setPendingConfirmation(payload.confirmation ?? null);
    if (payload.session_id) {
      setSessionId(payload.session_id);
    }
  }

  /** 用户在 ConfirmDialog 做出决策：以 confirmation 发起下一轮，恢复挂起的编排。 */
  async function handleConfirmDecision(decision: ConfirmationDecision) {
    setPendingConfirmation(null);
    const label =
      decision.decision === "approve"
        ? "（已确认）"
        : decision.decision === "reject"
          ? "（已拒绝）"
          : "（已修改后继续）";
    await handleSend(label, decision);
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
          {sessionId ? <p className="session-tag">会话：{sessionId}</p> : null}
        </div>
        <div className="header-controls">
          <label>
            User ID
            <input value={userId} onChange={(event) => onUserIdChange(event.target.value.trim() || "web_user")} />
          </label>
          <button type="button" onClick={() => void handleApplyProfile()}>
            写入示例画像
          </button>
          <button type="button" onClick={() => void handleNewSession()}>
            新开会话
          </button>
          <button type="button" onClick={() => void handleClearMemories()}>
            清空当前用户记忆
          </button>
        </div>
      </header>

      {profileStatus ? <div className="status-banner">{profileStatus}</div> : null}

      <section className="top-layout">
        <div className="chat-column">
          <ChatComposer isLoading={isLoading} onSend={(m) => handleSend(m)} />
          <MessageList messages={messages} />
          <OrchestrationPanel orchestration={orchestration} />
        </div>
        <TracePanel steps={steps} terminatedReason={terminatedReason} safety={safety} />
      </section>

      {pendingConfirmation ? (
        <ConfirmDialog
          confirmation={pendingConfirmation}
          onDecide={(decision) => void handleConfirmDecision(decision)}
          onCancel={() => setPendingConfirmation(null)}
        />
      ) : null}

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

// ------------------------------------------------------------------ step helpers
function ensureStep(steps: StepRecord[], index: number): StepRecord[] {
  if (steps.some((s) => s.index === index)) {
    return steps;
  }
  const next: StepRecord = {
    index,
    role: "assistant",
    thought: null,
    tool_calls: [],
    tool_results: [],
    source: "function_calling",
    raw_response: null,
  };
  return [...steps, next].sort((a, b) => a.index - b.index);
}

function upsertThought(steps: StepRecord[], index: number, thought: string): StepRecord[] {
  return ensureStep(steps, index).map((s) =>
    s.index === index ? { ...s, thought } : s,
  );
}

function upsertToolCall(steps: StepRecord[], index: number, call: ToolCall): StepRecord[] {
  return ensureStep(steps, index).map((s) => {
    if (s.index !== index) return s;
    if (s.tool_calls.some((c) => c.id === call.id)) return s;
    return { ...s, tool_calls: [...s.tool_calls, call] };
  });
}

function upsertToolResult(steps: StepRecord[], index: number, result: ToolResult): StepRecord[] {
  return ensureStep(steps, index).map((s) => {
    if (s.index !== index) return s;
    if (s.tool_results.some((r) => r.tool_call_id === result.tool_call_id)) return s;
    return { ...s, tool_results: [...s.tool_results, result] };
  });
}

// ------------------------------------------------------------------ message helpers
function appendToAssistant(messages: ChatMessage[], index: number, delta: string): ChatMessage[] {
  if (index < 0 || index >= messages.length) return messages;
  return messages.map((m, i) => (i === index ? { ...m, content: m.content + delta } : m));
}

function replaceAssistant(messages: ChatMessage[], index: number, content: string): ChatMessage[] {
  if (index < 0 || index >= messages.length) {
    return [...messages, { role: "assistant", content }];
  }
  return messages.map((m, i) => (i === index ? { ...m, content } : m));
}
