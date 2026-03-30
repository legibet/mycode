import type { SessionSummary } from '../types'

interface CurrentSendRequest {
  pendingRequestToken: number
  requestToken: number
  activeSessionId: string
  sessionId: string
  activeCwd: string
  requestCwd: string
}

export function resolveInitialSessionId(
  sessions: SessionSummary[],
  preferredSessionId: string,
): string | null {
  if (!Array.isArray(sessions) || sessions.length === 0) return null
  if (preferredSessionId) {
    const matchingSession = sessions.find(
      (session) => session.id === preferredSessionId,
    )
    if (matchingSession?.id) return matchingSession.id
  }
  return sessions[0]?.id || null
}

export function isCurrentSendRequest({
  pendingRequestToken,
  requestToken,
  activeSessionId,
  sessionId,
  activeCwd,
  requestCwd,
}: CurrentSendRequest): boolean {
  return (
    pendingRequestToken === requestToken &&
    activeSessionId === sessionId &&
    activeCwd === requestCwd
  )
}
