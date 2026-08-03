import { Attachment, ChatMessage, VisionInfo } from "../types";
import { VisionCard } from "./VisionCard";

type Props = {
  messages: ChatMessage[];
  attachments?: Attachment[];
  vision?: VisionInfo | null;
  visionLoading?: boolean;
};

export function MessageList({ messages, attachments, vision, visionLoading }: Props) {
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>对话记录</h2>
      </div>
      <div className="message-list">
        {messages.map((message, index) => {
          const isLastUser =
            message.role === "user" &&
            index === messages.length - 1;
          return (
            <article key={`${message.role}-${index}`} className={`message ${message.role}`}>
              <div className="message-role">{message.role === "user" ? "用户" : "助手"}</div>
              {isLastUser && attachments && attachments.length > 0 && (
                <div className="message-attachments">
                  {attachments.map((att) => (
                    <img
                      key={att.file_id}
                      src={`/api/media/${att.file_id}?user_id=web_user`}
                      alt="用户上传"
                      className="message-attachment-img"
                    />
                  ))}
                </div>
              )}
              <pre>{message.content}</pre>
            </article>
          );
        })}
        {visionLoading && (
          <div className="vision-loading">
            <span className="vision-spinner" />
            正在识别图片中的食物...
          </div>
        )}
        {vision && !visionLoading && <VisionCard vision={vision} />}
      </div>
    </section>
  );
}
