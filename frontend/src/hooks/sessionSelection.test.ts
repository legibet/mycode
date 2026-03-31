import { describe, expect, it } from 'vitest'

import {
  isCurrentSendRequest,
  isCurrentWorkspaceRequest,
  resolveInitialSessionId,
} from './sessionSelection'

describe('sessionSelection', () => {
  it('prefers the previously active session', () => {
    const sessions = [{ id: 'latest' }, { id: 'previous' }]

    expect(resolveInitialSessionId(sessions, 'previous')).toBe('previous')
  })

  it('falls back to the latest session', () => {
    const sessions = [{ id: 'latest' }, { id: 'older' }]

    expect(resolveInitialSessionId(sessions, 'missing')).toBe('latest')
    expect(resolveInitialSessionId([], 'missing')).toBeNull()
  })

  it('rejects send responses from a previous workspace', () => {
    expect(
      isCurrentSendRequest({
        pendingRequestToken: 3,
        requestToken: 3,
        activeSessionId: 'session-a',
        sessionId: 'session-a',
        activeCwd: '/workspace/new',
        requestCwd: '/workspace/old',
      }),
    ).toBe(false)
  })

  it('accepts matching send request state', () => {
    expect(
      isCurrentSendRequest({
        pendingRequestToken: 3,
        requestToken: 3,
        activeSessionId: 'session-a',
        sessionId: 'session-a',
        activeCwd: '/workspace/a',
        requestCwd: '/workspace/a',
      }),
    ).toBe(true)
  })

  it('rejects stale workspace loads', () => {
    expect(
      isCurrentWorkspaceRequest({
        pendingRequestToken: 4,
        requestToken: 3,
        activeCwd: '/workspace/b',
        requestCwd: '/workspace/a',
      }),
    ).toBe(false)
  })
})
