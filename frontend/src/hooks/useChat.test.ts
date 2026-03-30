import assert from 'node:assert/strict'
import test from 'node:test'

import {
  isCurrentSendRequest,
  resolveInitialSessionId,
} from './sessionSelection'

test('resolveInitialSessionId prefers the previously active session', () => {
  const sessions = [{ id: 'latest' }, { id: 'previous' }]

  assert.equal(resolveInitialSessionId(sessions, 'previous'), 'previous')
})

test('resolveInitialSessionId falls back to the latest session', () => {
  const sessions = [{ id: 'latest' }, { id: 'older' }]

  assert.equal(resolveInitialSessionId(sessions, 'missing'), 'latest')
  assert.equal(resolveInitialSessionId([], 'missing'), null)
})

test('isCurrentSendRequest rejects responses from a previous workspace', () => {
  assert.equal(
    isCurrentSendRequest({
      pendingRequestToken: 3,
      requestToken: 3,
      activeSessionId: 'session-a',
      sessionId: 'session-a',
      activeCwd: '/workspace/new',
      requestCwd: '/workspace/old',
    }),
    false,
  )
})

test('isCurrentSendRequest accepts matching request state', () => {
  assert.equal(
    isCurrentSendRequest({
      pendingRequestToken: 3,
      requestToken: 3,
      activeSessionId: 'session-a',
      sessionId: 'session-a',
      activeCwd: '/workspace/a',
      requestCwd: '/workspace/a',
    }),
    true,
  )
})
