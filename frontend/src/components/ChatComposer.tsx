import { FormEvent, useRef, useState } from "react";
import { Attachment, UploadResponse } from "../types";

type Props = {
  isLoading: boolean;
  onSend: (message: string, attachments?: Attachment[]) => Promise<void>;
};

export function ChatComposer({ isLoading, onSend }: Props) {
  const [message, setMessage] = useState(
    "我对花生和虾过敏，平时是奶蛋素，现在想减脂。请给我安排一个 1 天的饮食建议，尽量简单、工作日能执行。",
  );
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [previews, setPreviews] = useState<{ file_id: string; url: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  async function handleFileSelect(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setUploadError("");
    for (const file of Array.from(files)) {
      if (!file.type.startsWith("image/")) {
        setUploadError("只支持图片文件");
        continue;
      }
      const formData = new FormData();
      formData.append("file", file);
      formData.append("user_id", "web_user");
      try {
        const resp = await fetch("/api/upload", {
          method: "POST",
          body: formData,
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          setUploadError(err.detail ?? `上传失败: ${resp.status}`);
          continue;
        }
        const data: UploadResponse = await resp.json();
        setAttachments((prev) => [...prev, { file_id: data.file_id, kind: "image" }]);
        setPreviews((prev) => [
          ...prev,
          { file_id: data.file_id, url: `/api/media/${data.file_id}?user_id=web_user` },
        ]);
      } catch (e) {
        setUploadError(e instanceof Error ? e.message : "上传失败");
      }
    }
    setUploading(false);
  }

  function removeAttachment(fileId: string) {
    setAttachments((prev) => prev.filter((a) => a.file_id !== fileId));
    setPreviews((prev) => prev.filter((p) => p.file_id !== fileId));
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if ((!message.trim() && attachments.length === 0) || isLoading) {
      return;
    }
    const msg = message.trim() || "请帮我看看这些食物。";
    const atts = attachments.length > 0 ? attachments : undefined;
    await onSend(msg, atts);
    // 发送后清空附件
    setAttachments([]);
    setPreviews([]);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }

  function handlePaste(e: React.ClipboardEvent) {
    const items = e.clipboardData?.items;
    if (!items) return;
    const imageFiles: File[] = [];
    for (const item of Array.from(items)) {
      if (item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) imageFiles.push(file);
      }
    }
    if (imageFiles.length > 0) {
      e.preventDefault();
      const dt = new DataTransfer();
      imageFiles.forEach((f) => dt.items.add(f));
      void handleFileSelect(dt.files);
    }
  }

  return (
    <form className="panel composer" onSubmit={handleSubmit}>
      <div className="panel-header">
        <h2>对话输入</h2>
        {attachments.length > 0 && (
          <span className="badge">{attachments.length} 张图片</span>
        )}
      </div>
      {previews.length > 0 && (
        <div className="attachment-previews">
          {previews.map((p) => (
            <div key={p.file_id} className="attachment-thumb">
              <img src={p.url} alt="预览" />
              <button
                type="button"
                className="attachment-remove"
                onClick={() => removeAttachment(p.file_id)}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}
      <textarea
        value={message}
        onChange={(event) => setMessage(event.target.value)}
        onPaste={handlePaste}
        placeholder="输入你的饮食规划问题，可拖入/粘贴/选择图片"
        rows={5}
      />
      {uploadError && <div className="upload-error">{uploadError}</div>}
      <div className="composer-actions">
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          style={{ display: "none" }}
          onChange={(e) => void handleFileSelect(e.target.files)}
        />
        <button
          type="button"
          className="secondary"
          onClick={() => fileInputRef.current?.click()}
          disabled={isLoading || uploading}
        >
          {uploading ? "上传中..." : "📷 添加图片"}
        </button>
        <button type="submit" disabled={isLoading || uploading}>
          {isLoading ? "处理中..." : "发送"}
        </button>
      </div>
    </form>
  );
}
