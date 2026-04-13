import { AppState } from "../types";

type Props = {
  state: AppState | null;
};

export function StatePanel({ state }: Props) {
  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Memory / RAG 状态</h2>
      </div>
      {!state ? (
        <p className="muted">状态尚未加载。</p>
      ) : (
        <div className="state-grid">
          <div>
            <strong>User</strong>
            <pre>{state.user_id}</pre>
          </div>
          <div>
            <strong>Namespace</strong>
            <pre>{state.rag_namespace}</pre>
          </div>
          <div className="state-block">
            <strong>Current Profile</strong>
            <pre>{state.current_profile_summary}</pre>
          </div>
          <div className="state-block">
            <strong>Memory Summary</strong>
            <pre>{state.memory_summary}</pre>
          </div>
          <div className="state-block">
            <strong>Current Profile JSON</strong>
            <pre>{JSON.stringify(state.current_profile, null, 2)}</pre>
          </div>
          <div className="state-block">
            <strong>RAG Summary</strong>
            <pre>{state.rag_summary}</pre>
          </div>
        </div>
      )}
    </section>
  );
}
