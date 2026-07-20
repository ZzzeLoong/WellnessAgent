import { useState } from "react";

import { ChatPage } from "./components/ChatPage";
import { MetricsPage } from "./components/MetricsPage";

type Tab = "chat" | "metrics";

export default function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [userId, setUserId] = useState("web_user");

  return (
    <div className="app-root">
      <nav className="app-nav">
        <span className="app-nav-brand">WellnessAgent</span>
        <div className="app-nav-tabs">
          <button
            type="button"
            className={tab === "chat" ? "active" : "ghost"}
            onClick={() => setTab("chat")}
          >
            对话调试
          </button>
          <button
            type="button"
            className={tab === "metrics" ? "active" : "ghost"}
            onClick={() => setTab("metrics")}
          >
            指标面板
          </button>
        </div>
      </nav>
      {tab === "chat" ? (
        <ChatPage userId={userId} onUserIdChange={setUserId} />
      ) : (
        <MetricsPage userId={userId} />
      )}
    </div>
  );
}
