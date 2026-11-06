/**
 * 跨 Agent 会话反馈 PG 存储：持久化 👍/👎，撤回/删会话时同步删除。
 */
import { agentPgQuery, isAgentPgConfigured } from './agentPgClient'

export type AgentFeedbackAgent = 'manager' | 'db' | 'rag' | 'admin'

export type SessionFeedbackUpsert = {
  agent: AgentFeedbackAgent
  sessionId: string
  feedbackKey: string
  score: number
  turnId?: number | null
  userMessageIndex?: number | null
  runId?: string | null
  question?: string | null
  comment?: string | null
  artifact?: Record<string, unknown> | null
}

export type SessionFeedbackRow = {
  feedbackKey: string
  turnId: number | null
  userMessageIndex: number | null
  runId: string | null
  score: number
  question: string | null
  comment: string | null
  updatedAt: string
}

const AGENT_SET = new Set<AgentFeedbackAgent>(['manager', 'db', 'rag', 'admin'])

function normAgent(v: string): AgentFeedbackAgent | null {
  const a = String(v || '').trim().toLowerCase() as AgentFeedbackAgent
  return AGENT_SET.has(a) ? a : null
}

function normSessionId(v: string): string {
  return String(v || '').trim().slice(0, 120)
}

function normKey(v: string): string {
  return String(v || '').trim().slice(0, 120)
}

function normScore(score: number, agent: AgentFeedbackAgent): number | null {
  const n = Number(score)
  if (!Number.isFinite(n)) return null
  if (agent === 'manager') {
    if (n === 0 || n === 1) return n
    return null
  }
  if (n === 1 || n === -1) return n
  return null
}

export function turnFeedbackKey(turnId: number): string {
  return `turn:${turnId}`
}

export async function upsertSessionFeedback(input: SessionFeedbackUpsert): Promise<boolean> {
  if (!isAgentPgConfigured()) return false
  const agent = normAgent(input.agent)
  const sessionId = normSessionId(input.sessionId)
  const feedbackKey = normKey(input.feedbackKey)
  const score = agent ? normScore(input.score, agent) : null
  if (!agent || !sessionId || !feedbackKey || score == null) return false

  const res = await agentPgQuery(
    `INSERT INTO agent_session_feedback
       (agent, session_id, feedback_key, turn_id, user_message_index, run_id, score, question, comment, artifact, updated_at)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, NOW())
     ON CONFLICT (agent, session_id, feedback_key) DO UPDATE SET
       turn_id = COALESCE(EXCLUDED.turn_id, agent_session_feedback.turn_id),
       user_message_index = COALESCE(EXCLUDED.user_message_index, agent_session_feedback.user_message_index),
       run_id = COALESCE(EXCLUDED.run_id, agent_session_feedback.run_id),
       score = EXCLUDED.score,
       question = COALESCE(EXCLUDED.question, agent_session_feedback.question),
       comment = COALESCE(EXCLUDED.comment, agent_session_feedback.comment),
       artifact = CASE
         WHEN EXCLUDED.artifact = '{}'::jsonb THEN agent_session_feedback.artifact
         ELSE EXCLUDED.artifact
       END,
       updated_at = NOW()`,
    [
      agent,
      sessionId,
      feedbackKey,
      input.turnId ?? null,
      input.userMessageIndex ?? null,
      input.runId ? String(input.runId).slice(0, 80) : null,
      score,
      input.question ? String(input.question).slice(0, 4000) : null,
      input.comment ? String(input.comment).slice(0, 2000) : null,
      JSON.stringify(input.artifact && typeof input.artifact === 'object' ? input.artifact : {})
    ]
  )
  return Boolean(res)
}

export async function listSessionFeedback(
  agent: AgentFeedbackAgent,
  sessionId: string
): Promise<SessionFeedbackRow[]> {
  if (!isAgentPgConfigured()) return []
  const a = normAgent(agent)
  const sid = normSessionId(sessionId)
  if (!a || !sid) return []

  const res = await agentPgQuery<{
    feedback_key: string
    turn_id: number | null
    user_message_index: number | null
    run_id: string | null
    score: number
    question: string | null
    comment: string | null
    updated_at: Date | string
  }>(
    `SELECT feedback_key, turn_id, user_message_index, run_id, score, question, comment, updated_at
     FROM agent_session_feedback
     WHERE agent = $1 AND session_id = $2
     ORDER BY updated_at ASC`,
    [a, sid]
  )
  if (!res?.rows?.length) return []
  return res.rows.map((r) => ({
    feedbackKey: String(r.feedback_key),
    turnId: r.turn_id ?? null,
    userMessageIndex: r.user_message_index ?? null,
    runId: r.run_id ? String(r.run_id) : null,
    score: Number(r.score),
    question: r.question ?? null,
    comment: r.comment ?? null,
    updatedAt: r.updated_at instanceof Date ? r.updated_at.toISOString() : String(r.updated_at)
  }))
}

export async function deleteSessionFeedbackFromTurn(
  agent: AgentFeedbackAgent,
  sessionId: string,
  fromTurnId: number
): Promise<number> {
  if (!isAgentPgConfigured()) return 0
  const a = normAgent(agent)
  const sid = normSessionId(sessionId)
  if (!a || !sid || !Number.isFinite(fromTurnId)) return 0

  const res = await agentPgQuery(
    `DELETE FROM agent_session_feedback
     WHERE agent = $1 AND session_id = $2
       AND (
         (turn_id IS NOT NULL AND turn_id >= $3)
         OR (
           feedback_key LIKE 'turn:%'
           AND substring(feedback_key from 6) ~ '^[0-9]+$'
           AND CAST(substring(feedback_key from 6) AS INTEGER) >= $3
         )
       )`,
    [a, sid, Math.floor(fromTurnId)]
  )
  return res?.rowCount ?? 0
}

export async function deleteSessionFeedbackFromUserIndex(
  agent: AgentFeedbackAgent,
  sessionId: string,
  fromUserIndex: number
): Promise<number> {
  if (!isAgentPgConfigured()) return 0
  const a = normAgent(agent)
  const sid = normSessionId(sessionId)
  if (!a || !sid || !Number.isFinite(fromUserIndex)) return 0

  const res = await agentPgQuery(
    `DELETE FROM agent_session_feedback
     WHERE agent = $1 AND session_id = $2
       AND user_message_index IS NOT NULL
       AND user_message_index >= $3`,
    [a, sid, Math.floor(fromUserIndex)]
  )
  return res?.rowCount ?? 0
}

export async function deleteSessionFeedbackByRunIds(
  agent: AgentFeedbackAgent,
  sessionId: string,
  runIds: string[]
): Promise<number> {
  if (!isAgentPgConfigured() || !runIds.length) return 0
  const a = normAgent(agent)
  const sid = normSessionId(sessionId)
  if (!a || !sid) return 0
  const keys = [...new Set(runIds.map((r) => normKey(r)).filter(Boolean))]
  if (!keys.length) return 0

  const res = await agentPgQuery(
    `DELETE FROM agent_session_feedback
     WHERE agent = $1 AND session_id = $2
       AND (feedback_key = ANY($3::text[]) OR run_id = ANY($3::text[]))`,
    [a, sid, keys]
  )
  return res?.rowCount ?? 0
}

export async function deleteAllSessionFeedback(
  agent: AgentFeedbackAgent,
  sessionId: string
): Promise<number> {
  if (!isAgentPgConfigured()) return 0
  const a = normAgent(agent)
  const sid = normSessionId(sessionId)
  if (!a || !sid) return 0

  const res = await agentPgQuery(
    `DELETE FROM agent_session_feedback WHERE agent = $1 AND session_id = $2`,
    [a, sid]
  )
  return res?.rowCount ?? 0
}
