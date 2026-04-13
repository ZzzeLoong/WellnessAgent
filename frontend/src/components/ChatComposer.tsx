import { FormEvent, useState } from "react";

type Props = {
  isLoading: boolean;
  onSend: (message: string) => Promise<void>;
};

export function ChatComposer({ isLoading, onSend }: Props) {
  const [message, setMessage] = useState(
    "我对花生和虾过敏，平时是奶蛋素，现在想减脂。请给我安排一个 1 天的饮食建议，尽量简单、工作日能执行。",
  );

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!message.trim() || isLoading) {
      return;
    }
    await onSend(message.trim());
  }

  return (
    <form className="panel composer" onSubmit={handleSubmit}>
      <div className="panel-header">
        <h2>对话输入</h2>
      </div>
      <textarea
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        placeholder="输入你的饮食规划问题"
        rows={5}
      />
      <button type="submit" disabled={isLoading}>
        {isLoading ? "处理中..." : "发送"}
      </button>
    </form>
  );
}
