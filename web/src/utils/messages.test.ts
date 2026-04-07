import { describe, expect, it } from 'vitest'

import {
  appendRenderAssistantDelta,
  appendRenderToolUse,
  buildRenderMessages,
  createRenderAssistantMessage,
  updateRenderToolRuntime,
} from './messages'

describe('messages', () => {
  it('keeps sourceIndex and synthetic meta for user messages', () => {
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

    const firstMessage = renderMessages[0]
    const thirdMessage = renderMessages[2]

    expect(firstMessage).toBeTruthy()
    expect(thirdMessage).toBeTruthy()
    expect(firstMessage?.role).toBe('user')
    expect(firstMessage?.sourceIndex).toBe(0)
    expect(firstMessage?.meta?.synthetic).toBe(true)
    expect(thirdMessage?.role).toBe('user')
    expect(thirdMessage?.sourceIndex).toBe(2)
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
    expect(updated[1]?.content).toEqual([
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
        modelText: null,
        displayText: null,
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
        modelText: 'file.txt',
        displayText: 'file.txt',
        isError: false,
      },
      1,
    )

    expect(updated[0]).toBe(firstUser)
    expect(updated[1]).not.toBe(initial[1])

    const toolBlock = updated[1]?.content[1]
    expect(toolBlock?.type).toBe('tool_use')
    if (toolBlock?.type !== 'tool_use') {
      throw new Error('Expected tool block')
    }
    expect(toolBlock.runtime).toEqual({
      pending: false,
      output: 'file.txt',
      modelText: 'file.txt',
      displayText: 'file.txt',
      isError: false,
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
            model_text: 'file.txt',
            display_text: 'file.txt',
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
            model_text: 'file.txt',
            display_text: 'file.txt',
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
    expect(incremental[3]?.content).toEqual(canonical[3]?.content)
  })
})
