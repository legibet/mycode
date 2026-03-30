export function resolveInitialSessionId(sessions, preferredSessionId) {
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
}) {
  return (
    pendingRequestToken === requestToken &&
    activeSessionId === sessionId &&
    activeCwd === requestCwd
  )
}
