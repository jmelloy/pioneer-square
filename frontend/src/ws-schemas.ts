import { z } from 'zod'
import type { WSInbound } from './types'

// ─── Primitive enums ──────────────────────────────────────────────────────────

const agentStateZ = z.enum(['idle', 'thinking', 'working', 'busy', 'error', 'offline'])
const agentActivityZ = z.enum([
  'reading',
  'editing',
  'running',
  'searching',
  'fetching',
  'thinking',
  'planning',
])
const taskStateZ = z.enum([
  'pending',
  'planning',
  'working',
  'awaiting-review',
  'done',
  'failed',
  'followup',
  'cancelled',
])
const logLevelZ = z.enum(['info', 'worker', 'auth', 'claude', 'thinking'])
const logDetailZ = z
  .object({
    toolType: z.enum(['tool_use', 'tool_result', 'thinking']).optional(),
    name: z.string().optional(),
    input: z.union([z.record(z.unknown()), z.string()]).optional(),
    output: z.string().optional(),
    fullText: z.string().optional(),
    summary: z.string().optional(),
  })
  .passthrough()

// ─── Per-variant schemas ──────────────────────────────────────────────────────

// passthrough() on chat so extra server fields survive into the ChatMessage store.
const chatWSZ = z
  .object({
    type: z.literal('chat'),
    from: z.string(),
    to: z.string().optional(),
    content: z.string(),
    prUrl: z.string().nullable().optional(),
    createdAt: z.string().optional(),
    created_at: z.string().optional(),
    role: z.string().optional(),
    toolId: z.string().optional(),
    toolName: z.string().optional(),
    from_agent: z.string().optional(),
  })
  .passthrough()

const guildUpdatedWSZ = z.object({
  type: z.literal('guild-updated'),
  id: z.string(),
  name: z.string().optional(),
})

const agentJoinedWSZ = z.object({
  type: z.literal('agent-joined'),
  agentId: z.string(),
  agentName: z.string().optional(),
  agentType: z.string().optional(),
  workerId: z.string().nullable().optional(),
  workerName: z.string().optional(),
  state: agentStateZ.optional(),
  taskId: z.string().nullable().optional(),
  joinedAt: z.string().optional(),
})

const agentStateWSZ = z.object({
  type: z.literal('agent-state'),
  agentId: z.string(),
  workerId: z.string().nullable().optional(),
  taskId: z.string().nullable().optional(),
  state: agentStateZ,
  activity: agentActivityZ.nullable().optional(),
})

const terminalOutputWSZ = z.object({
  type: z.literal('terminal-output'),
  agentId: z.string().optional(),
  workerId: z.string().optional(),
  taskId: z.string().optional(),
  line: z.string(),
  timestamp: z.string().optional(),
  detail: logDetailZ.nullable().optional(),
  level: logLevelZ.nullable().optional(),
})

const taskCreatedWSZ = z.object({
  type: z.literal('task-created'),
  taskId: z.string(),
  name: z.string().optional(),
  description: z.string().optional(),
  phase: z.string().optional(),
  state: taskStateZ,
  createdAt: z.string().optional(),
})

const taskAssignedWSZ = z.object({
  type: z.literal('task-assigned'),
  taskId: z.string(),
  workerId: z.string(),
  name: z.string().optional(),
  description: z.string().optional(),
  phase: z.string().optional(),
  parentTaskId: z.string().nullable().optional(),
})

const taskUpdateWSZ = z.object({
  type: z.literal('task-update'),
  taskId: z.string(),
  agentId: z.string().nullable().optional(),
  state: taskStateZ.optional(),
  branch: z.string().optional(),
  prUrl: z.string().optional(),
  finishedAt: z.string().optional(),
  worktreePath: z.string().optional(),
  deletedAt: z.string().nullable().optional(),
})

const taskCompleteWSZ = z.object({
  type: z.literal('task-complete'),
  taskId: z.string(),
  workerId: z.string().optional(),
  agentId: z.string().nullable().optional(),
  branch: z.string().optional(),
  prUrl: z.string().nullable().optional(),
})

const taskFollowupDoneWSZ = z.object({
  type: z.literal('task-followup-done'),
  taskId: z.string(),
  agentId: z.string().nullable().optional(),
})

const needsInputWSZ = z.object({
  type: z.literal('needs-input'),
  workerId: z.string(),
  description: z.string().optional(),
})

const claudeAuthRequiredWSZ = z.object({
  type: z.literal('claude-auth-required'),
  workerId: z.string(),
  url: z.string(),
})

const claudeAuthSuccessWSZ = z.object({
  type: z.literal('claude-auth-success'),
  workerId: z.string(),
})

const claudeAuthClearedWSZ = z.object({
  type: z.literal('claude-auth-cleared'),
  workerId: z.string(),
})

const foremanPollStatusWSZ = z.object({
  type: z.literal('foreman-poll-status'),
  nextCheckIn: z.number().optional(),
})

const claudeUsageWSZ = z.object({
  type: z.literal('claude-usage'),
  taskId: z.string().nullable(),
  workerId: z.string().nullable().optional(),
  sessionId: z.string().nullable().optional(),
  model: z.string().nullable().optional(),
  repo: z.string().nullable().optional(),
  reporter: z.string().nullable().optional(),
  apiCalls: z.number(),
  summedInputTokens: z.number(),
  summedOutputTokens: z.number(),
  summedCacheReadTokens: z.number(),
  summedCacheCreationTokens: z.number(),
  reportedInputTokens: z.number().nullable().optional(),
  reportedOutputTokens: z.number().nullable().optional(),
  reportedCacheReadTokens: z.number().nullable().optional(),
  reportedCacheCreationTokens: z.number().nullable().optional(),
  costUsd: z.number().nullable().optional(),
  numTurns: z.number().nullable().optional(),
  stopReason: z.string().nullable().optional(),
})

// ─── Discriminated union ──────────────────────────────────────────────────────

const wsMessageSchema = z.discriminatedUnion('type', [
  chatWSZ,
  guildUpdatedWSZ,
  agentJoinedWSZ,
  agentStateWSZ,
  terminalOutputWSZ,
  taskCreatedWSZ,
  taskAssignedWSZ,
  taskUpdateWSZ,
  taskCompleteWSZ,
  taskFollowupDoneWSZ,
  needsInputWSZ,
  claudeAuthRequiredWSZ,
  claudeAuthSuccessWSZ,
  claudeAuthClearedWSZ,
  foremanPollStatusWSZ,
  claudeUsageWSZ,
])

// ─── Public API ───────────────────────────────────────────────────────────────

/**
 * Parse and validate a raw WebSocket frame against the WSInbound union.
 * Returns the typed payload on success, null for non-JSON or unrecognised-type frames.
 */
export function parseWsMessage(raw: string): WSInbound | null {
  let json: unknown
  try {
    json = JSON.parse(raw)
  } catch {
    console.warn('Dropping non-JSON WS frame')
    return null
  }
  const result = wsMessageSchema.safeParse(json)
  if (result.success) return result.data as WSInbound
  console.warn(
    'Unrecognised WS message — dropped:',
    typeof json === 'object' && json !== null ? (json as Record<string, unknown>).type : json,
  )
  return null
}

/** TypeScript exhaustiveness sentinel: call in `default` branches of WSInbound switches. */
export function assertNever(x: never): never {
  throw new Error(`Unhandled WS message type: ${(x as { type: string }).type}`)
}
