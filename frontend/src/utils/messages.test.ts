import assert from 'node:assert/strict'
import test from 'node:test'

import { buildRenderMessages } from './messages'

test('buildRenderMessages keeps sourceIndex and synthetic meta for user messages', () => {
  const renderMessages = buildRenderMessages([
    {
      role: 'user',
      content: [{ type: 'text', text: 'summary' }],
      meta: { synthetic: true },
    },
    {
      role: 'assistant',
      content: [{ type: 'text', text: 'ack' }],
    },
    {
      role: 'user',
      content: [{ type: 'text', text: 'real prompt' }],
    },
  ])

  assert.equal(renderMessages[0].role, 'user')
  assert.equal(renderMessages[0].sourceIndex, 0)
  assert.equal(renderMessages[0].meta?.synthetic, true)
  assert.equal(renderMessages[2].role, 'user')
  assert.equal(renderMessages[2].sourceIndex, 2)
})
