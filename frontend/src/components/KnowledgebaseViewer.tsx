import { useMemo } from "react";

import { KnowledgebaseFile } from "../types";

type Props = {
  files: KnowledgebaseFile[];
  activeFileName: string;
  activeContent: string;
  onSelect: (name: string) => void;
};

export function KnowledgebaseViewer({
  files,
  activeFileName,
  activeContent,
  onSelect,
}: Props) {
  const orderedFiles = useMemo(
    () => [...files].sort((left, right) => left.name.localeCompare(right.name)),
    [files],
  );

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>知识库原文</h2>
      </div>
      <div className="knowledgebase-layout">
        <div className="knowledgebase-sidebar">
          {orderedFiles.map((file) => (
            <button
              key={file.name}
              type="button"
              className={file.name === activeFileName ? "kb-active" : ""}
              onClick={() => onSelect(file.name)}
            >
              {file.name}
            </button>
          ))}
        </div>
        <div className="knowledgebase-content">
          {activeFileName ? <h3>{activeFileName}</h3> : null}
          <pre>{activeContent || "选择一份知识库文档查看内容。"}</pre>
        </div>
      </div>
    </section>
  );
}
