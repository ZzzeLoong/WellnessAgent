import { ChatMessage } from "../types";

type Props = {
  messages: ChatMessage[];
};

export function MessageList({ messages }: Props) {
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>对话记录</h2>
      </div>
      <div className="message-list">
        {messages.map((message, index) => (
          <article key={`${message.role}-${index}`} className={`message ${message.role}`}>
            <div className="message-role">{message.role === "user" ? "用户" : "助手"}</div>
            <pre>{message.content}</pre>
          </article>
        ))}
      </div>
    </section>
  );
}
