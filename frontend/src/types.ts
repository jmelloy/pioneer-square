export type AgentState =
  | 'idle'
  | 'thinking'
  | 'working'
  | 'busy'
  | 'error'
  | 'offline'
  | 'awaiting-review'

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
  toolType?: 'tool_use' | 'tool_result'
  name?: string
  input?: Record<string, any>
  output?: string
  [key: string]: any
}

export interface LogEntry {
  line: string
  timestamp: string
  detail?: LogDetail | null
}

export interface Agent {
  id: string
  name: string
  type: AgentType
  workerId: string | null
  state: AgentState
  activity?: AgentActivity | null
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
  deleted_at?: string | null
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
    state: AgentState
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
  [key: string]: any
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
  [key: string]: any
}

export interface GitHubRepo {
  full_name: string
  language?: string
  [key: string]: any
}

export type WSMessage = Record<string, any>
