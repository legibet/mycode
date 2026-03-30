import assert from 'node:assert/strict'
import test from 'node:test'

import {
  appendRenderAssistantDelta,
  appendRenderToolUse,
  buildRenderMessages,
  updateRenderToolRuntime,
} from './messages'

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

  assert.ok(renderMessages[0])
  assert.ok(renderMessages[2])
  assert.equal(renderMessages[0].role, 'user')
  assert.equal(renderMessages[0].sourceIndex, 0)
  assert.equal(renderMessages[0].meta?.synthetic, true)
  assert.equal(renderMessages[2].role, 'user')
  assert.equal(renderMessages[2].sourceIndex, 2)
})

test('appendRenderAssistantDelta preserves earlier render message references', () => {
  const initial = buildRenderMessages([
    {
      role: 'user',
      content: [{ type: 'text', text: 'hello' }],
    },
    {
      role: 'assistant',
      content: [{ type: 'text', text: 'world' }],
    },
  ])

  const firstUser = initial[0]
  const updated = appendRenderAssistantDelta(initial, 'text', '!')

  assert.equal(updated[0], firstUser)
  assert.notEqual(updated[1], initial[1])
  assert.deepEqual(updated[1]?.content, [
    {
      type: 'text',
      text: 'world!',
      renderKey: 'assistant:1:0',
    },
  ])
})

test('updateRenderToolRuntime updates only the matching tool block', () => {
  const initial = appendRenderToolUse(
    buildRenderMessages([
      {
        role: 'user',
        content: [{ type: 'text', text: 'run ls' }],
      },
      {
        role: 'assistant',
        content: [{ type: 'text', text: 'running' }],
      },
    ]),
    {
      id: 'tool-1',
      name: 'bash',
      input: { command: 'ls' },
    },
    {
      pending: true,
      output: '',
      modelText: null,
      displayText: null,
      isError: false,
    },
  )

  const firstUser = initial[0]
  const updated = updateRenderToolRuntime(initial, 'tool-1', {
    pending: false,
    output: 'file.txt',
    modelText: 'file.txt',
    displayText: 'file.txt',
    isError: false,
  })

  assert.equal(updated[0], firstUser)
  assert.notEqual(updated[1], initial[1])

  const toolBlock = updated[1]?.content[1]
  assert.equal(toolBlock?.type, 'tool_use')
  if (toolBlock?.type !== 'tool_use') {
    throw new Error('Expected tool block')
  }
  assert.deepEqual(toolBlock.runtime, {
    pending: false,
    output: 'file.txt',
    modelText: 'file.txt',
    displayText: 'file.txt',
    isError: false,
  })
})
