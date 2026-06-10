export type AgentState = 'idle' | 'thinking' | 'working' | 'busy' | 'error' | 'offline'

export type AgentActivity =
  | 'reading'
  | 'editing'
  | 'running'
  | 'searching'
  | 'fetching'
  | 'thinking'
  | 'planning'

export type AgentType = 'foreman' | 'worker' | string

export interface LogDetail {
  toolType?: 'tool_use' | 'tool_result' | 'thinking'
  name?: string
  input?: Record<string, unknown> | string
  output?: string
  fullText?: string
  summary?: string
  [key: string]: unknown
}

// Semantic type of a terminal-output line, set by the producer (worker) so the
// frontend can style logs without parsing text prefixes. Keep in sync with the
// LEVEL_* constants in worker/pioneer_worker/worker.py.
//   info     — default agent/Claude output (rendered as markdown)
//   worker   — worker-level status / lifecycle line
//   auth     — Claude login / credential flow
//   claude   — Claude runner framing (start / exit / stderr)
//   thinking — extended-thinking text
export type LogLevel = 'info' | 'worker' | 'auth' | 'claude' | 'thinking'

export interface LogEntry {
  line: string
  timestamp: string
  detail?: LogDetail | null
  level?: LogLevel | null
}

export interface Agent {
  id: string
  name: string
  type: AgentType
  workerId: string | null
  workerName?: string
  state: AgentState
  activity?: AgentActivity | null
  // ID of the task this agent is currently executing (set when state is
  // working/thinking/busy/error, null when idle/offline). Source of truth for
  // mapping a task row to its agent on the factory floor.
  taskId?: string | null
  logs: LogEntry[]
  joinedAt: string
}

export interface Worker {
  id: string
  name: string
  state: AgentState
}

export type TaskState =
  | 'pending'
  | 'planning'
  | 'working'
  | 'awaiting-review'
  | 'done'
  | 'failed'
  | 'followup'
  | 'cancelled'

export interface Task {
  id: string
  name?: string
  description?: string
  phase?: string
  state: TaskState
  worker_id?: string
  parent_task_id?: string | null
  branch?: string
  pr_url?: string
  worktree_path?: string
  created_at?: string
  finished_at?: string
  finalized_at?: string | null
}

export interface Guild {
  id: string
  name?: string
  primary_repo?: string | null
  agents?: Array<{
    id: string
    name: string
    type: string
    worker_id?: string | null
    worker_name?: string | null
    state: AgentState
    current_task_id?: string | null
    joined_at?: string
  }>
  messages?: ChatMessage[]
  created_at?: string
  agent_count?: number
}

export interface ChatMessage {
  type: string
  from: string
  to?: string
  content: string
  prUrl?: string | null
  createdAt?: string
  created_at?: string
  role?: 'tool_use' | 'tool_result' | string
  toolId?: string
  toolName?: string
  toolInput?: Record<string, unknown>
  isError?: boolean
  from_agent?: string
  [key: string]: unknown
}

export interface AuthUser {
  id?: string | null
  login: string
  name?: string
  avatar_url?: string
}

export interface GitHubLabel {
  id: number
  name: string
  color: string
}

export interface GitHubIssue {
  id: number
  number: number
  title: string
  labels: GitHubLabel[]
  created_at: string
  pull_request?: unknown
  repo: string
  [key: string]: unknown
}

export interface GitHubRepo {
  full_name: string
  language?: string
  [key: string]: unknown
}

// ── WebSocket protocol ──────────────────────────────────────────────
// Discriminated unions for the messages we actually inspect. Variants
// declare only the fields we read; an open `UnknownWS` fallback keeps
// forward-compat with backend additions we haven't typed yet.

export interface ChatWS {
  type: 'chat'
  from: string
  to?: string
  content: string
  prUrl?: string | null
  createdAt?: string
  created_at?: string
  role?: 'tool_use' | 'tool_result' | string
  toolId?: string
  toolName?: string
  from_agent?: string
}

export interface GuildUpdatedWS {
  type: 'guild-updated'
  id: string
  name?: string
}

export interface AgentJoinedWS {
  type: 'agent-joined'
  agentId: string
  agentName?: string
  agentType?: string
  workerId?: string | null
  workerName?: string
  state?: AgentState
  taskId?: string | null
  joinedAt?: string
}

export interface AgentStateWS {
  type: 'agent-state'
  agentId: string
  workerId?: string | null
  taskId?: string | null
  state: AgentState
  activity?: AgentActivity | null
}

export interface TerminalOutputWS {
  type: 'terminal-output'
  agentId?: string
  workerId?: string
  taskId?: string
  line: string
  timestamp?: string
  detail?: LogDetail | null
  level?: LogLevel | null
}

export interface TaskCreatedWS {
  type: 'task-created'
  taskId: string
  name?: string
  description?: string
  phase?: string
  state: TaskState
  createdAt?: string
}

export interface TaskAssignedWS {
  type: 'task-assigned'
  taskId: string
  workerId: string
  name?: string
  description?: string
  tool?: string
  model?: string | null
  provider?: string | null
  phase?: string
  parentTaskId?: string | null
}

export interface TaskUpdateWS {
  type: 'task-update'
  taskId: string
  agentId?: string | null
  state?: TaskState
  branch?: string
  prUrl?: string
  finishedAt?: string
  worktreePath?: string
  finalizedAt?: string | null
}

export interface TaskCompleteWS {
  type: 'task-complete'
  taskId: string
  workerId?: string
  agentId?: string | null
  branch?: string
  prUrl?: string | null
}

export interface TaskFollowupDoneWS {
  type: 'task-followup-done'
  taskId: string
  agentId?: string | null
}

export interface NeedsInputWS {
  type: 'needs-input'
  workerId: string
  description?: string
}

export interface ClaudeAuthRequiredWS {
  type: 'claude-auth-required'
  workerId: string
  url: string
}

export interface ClaudeAuthSuccessWS {
  type: 'claude-auth-success'
  workerId: string
}

export interface ClaudeAuthClearedWS {
  type: 'claude-auth-cleared'
  workerId: string
}

export interface ForemanPollStatusWS {
  type: 'foreman-poll-status'
  nextCheckIn?: number
}

export interface ClaudeUsageWS {
  type: 'claude-usage'
  taskId: string | null
  workerId?: string | null
  sessionId?: string | null
  model?: string | null
  repo?: string | null
  reporter?: string | null
  apiCalls: number
  summedInputTokens: number
  summedOutputTokens: number
  summedCacheReadTokens: number
  summedCacheCreationTokens: number
  reportedInputTokens?: number | null
  reportedOutputTokens?: number | null
  reportedCacheReadTokens?: number | null
  reportedCacheCreationTokens?: number | null
  costUsd?: number | null
  numTurns?: number | null
  stopReason?: string | null
}

export type WSInbound =
  | ChatWS
  | GuildUpdatedWS
  | AgentJoinedWS
  | AgentStateWS
  | TerminalOutputWS
  | TaskCreatedWS
  | TaskAssignedWS
  | TaskUpdateWS
  | TaskCompleteWS
  | TaskFollowupDoneWS
  | NeedsInputWS
  | ClaudeAuthRequiredWS
  | ClaudeAuthSuccessWS
  | ClaudeAuthClearedWS
  | ForemanPollStatusWS
  | ClaudeUsageWS

// Outbound: producer-side; we accept any object with a `type`.
export interface WSOutbound {
  type: string
  [key: string]: unknown
}

// Backwards-compatible alias used by older callers.
export type WSMessage = WSInbound
