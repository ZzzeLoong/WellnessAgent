export type ToolCall = {
  id: string;
  name: string;
  arguments: string;
};

export type ToolResult = {
  tool_call_id: string;
  name: string;
  status: string; // "success" | "partial" | "error"
  content: string;
};

export type StepRecord = {
  index: number;
  role: string; // "assistant" | "tool"
  thought: string | null;
  tool_calls: ToolCall[];
  tool_results: ToolResult[];
  source: string; // "function_calling" | "json_fallback"
  raw_response: string | null;
};

export type SafetyInfo = {
  action: string; // "pass" | "rewrite" | "block"
  reason?: string;
  hits?: string[];
} | null;

export type SessionSummary = {
  session_id: string;
  created_at: string | null;
  updated_at: string | null;
  message_count: number;
  round_count: number;
};

export type KnowledgebaseFile = {
  name: string;
  stem: string;
  path?: string;
  size_bytes?: number;
  content?: string;
};

export type CurrentProfile = {
  profile_type: string;
  allergies: string[];
  diet_pattern: string;
  goal: string;
  dislikes: string[];
  medical_notes: string[];
  preferred_cuisines: string[];
  cooking_constraints: string[];
  notes: string;
};

export type AppState = {
  user_id: string;
  rag_namespace: string;
  knowledgebase_dir: string;
  memory_summary: string;
  rag_summary: string;
  current_profile: CurrentProfile;
  current_profile_summary: string;
  knowledgebase_files: KnowledgebaseFile[];
};

export type SubAgentBrief = {
  name: string;
  success: boolean;
  summary?: string;
  steps?: number | null;
  duration_ms?: number | null;
  tools_used?: string[] | null;
};

export type OrchestrationInfo = {
  route: string; // "simple" | "composite"
  delegate_to_monolith?: boolean;
  reason?: string;
  subagents?: SubAgentBrief[];
  pending?: ConfirmationInfo | null;
} | null;

export type ConfirmationInfo = {
  confirm_id: string;
  kind: string; // "profile_update" | "safety_risk"
  prompt: string;
  payload?: Record<string, unknown>;
};

export type ConfirmationDecision = {
  confirm_id: string;
  decision: "approve" | "reject" | "modify";
  patch?: Record<string, unknown>;
};

export type ChatResponse = {
  answer: string;
  terminated_reason: string;
  steps: StepRecord[];
  state: AppState;
  session_id?: string;
  trace_id?: string | null;
  safety?: SafetyInfo;
  orchestration?: OrchestrationInfo;
  confirmation?: ConfirmationInfo | null;
};

export type SubAgentStat = {
  calls: number;
  avg_steps: number;
  avg_duration_ms: number;
  fail_rate: number;
};

export type MetricsResponse = {
  turns: number;
  avg_steps_per_turn: number;
  tool_calls: Record<string, number>;
  terminated_reason_dist: Record<string, number>;
  safety_blocks: number;
  circuit_open: number;
  confirm_requests: number;
  confirm_resumes: number;
  route_dist: Record<string, number>;
  subagent_stats: Record<string, SubAgentStat>;
  latency_ms: { p50: number | null; p95: number | null; count: number };
};

export type StreamEventType =
  | "agent_start"
  | "step_start"
  | "thinking"
  | "tool_call_start"
  | "tool_call_finish"
  | "llm_chunk"
  | "safety"
  | "agent_finish"
  | "error"
  | "orchestrator_triage"
  | "subagent_start"
  | "subagent_result"
  | "orchestrator_aggregate"
  | "confirm";

export type ProfilePayload = {
  allergies: string[];
  diet_pattern: string;
  goal: string;
  dislikes: string[];
  medical_notes: string[];
  preferred_cuisines: string[];
  cooking_constraints: string[];
  notes: string;
};

export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
};
