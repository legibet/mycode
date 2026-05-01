import { describe, expect, it } from 'vitest'

import type { ChatMessage, RenderMessage } from '../types'
import { isCompactMarker } from '../types'
import {
  buildRenderMessages,
  createUserMessage,
  updateLatestThinkingDuration,
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

  it('updates the latest thinking block duration', () => {
    const initial: ChatMessage[] = [
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
    ]

    const updated = updateLatestThinkingDuration(initial, 1200)

    expect(updated[0]?.content[0]).toEqual({
      type: 'thinking',
      text: 'plan',
      meta: { native: { signature: 'sig' }, duration_ms: 1200 },
    })
  })
})
