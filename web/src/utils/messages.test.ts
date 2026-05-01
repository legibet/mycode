import { describe, expect, it } from 'vitest'

import type { ChatMessage, RenderMessage } from '../types'
import { isCompactMarker } from '../types'
import {
  appendRenderAssistantDelta,
  appendRenderToolUse,
  buildRenderMessages,
  createRenderAssistantMessage,
  createUserMessage,
  updateLatestThinkingDuration,
  updateRenderToolRuntime,
} from './messages'

function expectChat(message: RenderMessage | undefined): ChatMessage {
  if (!message || isCompactMarker(message)) {
    throw new Error('expected a ChatMessage, got compact marker or undefined')
  }
  return message
}

describe('messages', () => {
  it('assigns sourceIndex to render user messages', () => {
    const renderMessages = buildRenderMessages([
      {
        role: 'user',
        content: [{ type: 'text', text: 'first' }],
      },
      {
        role: 'assistant',
        content: [{ type: 'text', text: 'ack' }],
      },
      {
        role: 'user',
        content: [{ type: 'text', text: 'second' }],
      },
    ])

    const first = expectChat(renderMessages[0])
    const third = expectChat(renderMessages[2])

    expect(first.role).toBe('user')
    expect(first.sourceIndex).toBe(0)
    expect(third.role).toBe('user')
    expect(third.sourceIndex).toBe(2)
  })

  it('emits a compact marker entry between real turns', () => {
    const renderMessages = buildRenderMessages([
      {
        role: 'user',
        content: [{ type: 'text', text: 'hello' }],
      },
      {
        role: 'assistant',
        content: [{ type: 'text', text: 'hi' }],
      },
      {
        role: 'compact',
        content: [{ type: 'text', text: 'summary' }],
      },
      {
        role: 'user',
        content: [{ type: 'text', text: 'follow-up' }],
      },
    ])

    expect(renderMessages).toHaveLength(4)
    const marker = renderMessages[2]
    expect(marker && isCompactMarker(marker)).toBe(true)
    if (marker && isCompactMarker(marker)) {
      expect(marker.sourceIndex).toBe(2)
      expect(marker.renderKey).toBe('compact:2')
    }
    expect(expectChat(renderMessages[3]).role).toBe('user')
  })

  it('keeps document blocks in user render messages', () => {
    const renderMessages = buildRenderMessages([
      {
        role: 'user',
        content: [
          { type: 'text', text: 'summarize this' },
          {
            type: 'document',
            data: 'JVBERi0xLjc=',
            mime_type: 'application/pdf',
            name: 'report.pdf',
          },
        ],
      },
    ])

    expect(renderMessages).toEqual([
      {
        role: 'user',
        renderKey: 'user:0',
        sourceIndex: 0,
        content: [
          {
            type: 'text',
            text: 'summarize this',
            renderKey: 'user:0:0',
          },
          {
            type: 'document',
            data: 'JVBERi0xLjc=',
            mime_type: 'application/pdf',
            name: 'report.pdf',
            renderKey: 'user:0:1',
          },
        ],
      },
    ])
  })

  it('wraps text attachments like CLI file references', () => {
    const message = createUserMessage('review this', [
      {
        kind: 'text',
        name: 'main <"v2">.py',
        text: 'print("ok")',
      },
    ])

    expect(message).toEqual({
      role: 'user',
      content: [
        { type: 'text', text: 'review this' },
        {
          type: 'text',
          text: '<file name="main &lt;&quot;v2&quot;&gt;.py">\nprint("ok")\n</file>',
          meta: { attachment: true, path: 'main <"v2">.py' },
        },
      ],
    })
  })

  it('preserves earlier render message references when appending assistant delta', () => {
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
    const updated = appendRenderAssistantDelta(initial, 'text', '!', 1)

    expect(updated[0]).toBe(firstUser)
    expect(updated[1]).not.toBe(initial[1])
    expect(expectChat(updated[1]).content).toEqual([
      {
        type: 'text',
        text: 'world!',
        renderKey: 'assistant:1:0',
      },
    ])
  })

  it('updates only the matching tool block', () => {
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
      1,
      {
        pending: true,
        output: '',
        finalOutput: null,
        metadata: null,
        isError: false,
      },
    )

    const firstUser = initial[0]
    const updated = updateRenderToolRuntime(
      initial,
      'tool-1',
      {
        pending: false,
        output: 'file.txt',
        finalOutput: 'file.txt',
        metadata: null,
        isError: false,
      },
      1,
    )

    expect(updated[0]).toBe(firstUser)
    expect(updated[1]).not.toBe(initial[1])

    const toolBlock = expectChat(updated[1]).content[1]
    expect(toolBlock?.type).toBe('tool_use')
    if (toolBlock?.type !== 'tool_use') {
      throw new Error('Expected tool block')
    }
    expect(toolBlock.runtime).toEqual({
      pending: false,
      output: 'file.txt',
      finalOutput: 'file.txt',
      metadata: null,
      isError: false,
    })
  })

  it('updates the latest thinking block duration', () => {
    const initial = buildRenderMessages([
      {
        role: 'assistant',
        content: [
          {
            type: 'thinking',
            text: 'plan',
            meta: { native: { signature: 'sig' } },
          },
          { type: 'text', text: 'answer' },
        ],
      },
    ])

    const updated = updateLatestThinkingDuration(initial, 1200)

    expect(expectChat(updated[0]).content[0]).toEqual({
      type: 'thinking',
      text: 'plan',
      meta: { native: { signature: 'sig' }, duration_ms: 1200 },
      renderKey: 'assistant:0:0',
    })
  })

  it('keeps incremental assistant keys aligned with canonical source indices', () => {
    const previousMessages = buildRenderMessages([
      {
        role: 'user',
        content: [{ type: 'text', text: 'run ls' }],
      },
      {
        role: 'assistant',
        content: [
          { type: 'text', text: 'Running now.' },
          {
            type: 'tool_use',
            id: 'tool-1',
            name: 'bash',
            input: { command: 'ls' },
          },
        ],
      },
      {
        role: 'user',
        content: [
          {
            type: 'tool_result',
            tool_use_id: 'tool-1',
            output: 'file.txt',
            metadata: null,
            is_error: false,
          },
        ],
      },
      {
        role: 'user',
        content: [{ type: 'text', text: 'what changed?' }],
      },
    ])

    const incremental = appendRenderAssistantDelta(
      [...previousMessages, createRenderAssistantMessage(4)],
      'text',
      'Nothing else changed.',
      4,
    )

    const canonical = buildRenderMessages([
      {
        role: 'user',
        content: [{ type: 'text', text: 'run ls' }],
      },
      {
        role: 'assistant',
        content: [
          { type: 'text', text: 'Running now.' },
          {
            type: 'tool_use',
            id: 'tool-1',
            name: 'bash',
            input: { command: 'ls' },
          },
        ],
      },
      {
        role: 'user',
        content: [
          {
            type: 'tool_result',
            tool_use_id: 'tool-1',
            output: 'file.txt',
            metadata: null,
            is_error: false,
          },
        ],
      },
      {
        role: 'user',
        content: [{ type: 'text', text: 'what changed?' }],
      },
      {
        role: 'assistant',
        content: [{ type: 'text', text: 'Nothing else changed.' }],
      },
    ])

    expect(incremental[3]?.renderKey).toBe(canonical[3]?.renderKey)
    expect(incremental[3]?.sourceIndex).toBe(canonical[3]?.sourceIndex)
    expect(expectChat(incremental[3]).content).toEqual(
      expectChat(canonical[3]).content,
    )
  })
})
