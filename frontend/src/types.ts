export type StepRecord = {
  step_index: number;
  thought: string | null;
  action_text: string | null;
  tool_name: string | null;
  tool_input: string | null;
  observation: string | null;
  raw_response: string | null;
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

export type ChatResponse = {
  answer: string;
  terminated_reason: string;
  steps: StepRecord[];
  state: AppState;
};

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
