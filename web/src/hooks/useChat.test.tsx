import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChat } from './useChat'

function createLocalStorage() {
  const store = new Map<string, string>()

  return {
    get length() {
      return store.size
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null
    },
    getItem(key: string) {
      return store.has(key) ? (store.get(key) ?? null) : null
    },
    setItem(key: string, value: string) {
      store.set(key, String(value))
    },
    removeItem(key: string) {
      store.delete(key)
    },
    clear() {
      store.clear()
    },
  }
}

function createJsonResponse(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderChatHook(overrides?: Partial<Parameters<typeof useChat>[0]>) {
  return renderHook(() =>
    useChat({
      provider: '',
      model: '',
      cwd: '/workspace/a',
      apiKey: '',
      apiBase: '',
      reasoningEffort: '',
      ...overrides,
    }),
  )
}

function mockFetch(
  routes: Record<string, Response | ((init?: RequestInit) => Response)>,
) {
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      for (const [prefix, response] of Object.entries(routes)) {
        if (!url.startsWith(prefix)) continue
        if (typeof response === 'function') {
          return response(init)
        }
        return response.clone()
      }
      throw new Error(`Unexpected fetch: ${url}`)
    },
  )
  globalThis.fetch = fetchMock as typeof fetch
  return fetchMock
}

describe('useChat', () => {
  beforeEach(() => {
    globalThis.localStorage = createLocalStorage()
    mockFetch({
      '/api/sessions?cwd=': createJsonResponse({ sessions: [] }),
    })
  })

  it('creates a draft session when the workspace has no saved sessions', async () => {
    const { result } = renderChatHook()

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false)
      expect(result.current.sessions).toHaveLength(1)
    })

    expect(result.current.activeSession.isDraft).toBe(true)
    expect(result.current.sessions[0]?.id).toBe(result.current.activeSession.id)
    expect(result.current.messages).toEqual([])
    expect(result.current.loading).toBe(false)
  })

  it('sends text attachments as attachment text blocks', async () => {
    const fetchMock = mockFetch({
      '/api/sessions?cwd=': createJsonResponse({ sessions: [] }),
      '/api/chat': createJsonResponse({
        run: {
          id: 'run-1',
          session_id: 'draft-1',
          status: 'running',
          last_seq: 0,
        },
        session: { id: 'draft-1', title: 'Draft' },
      }),
      '/api/runs/run-1/stream?after=0': new Response('data: [DONE]\n\n', {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }),
    })

    const { result } = renderChatHook({
      provider: 'anthropic',
      model: 'claude-sonnet-4-6',
    })

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false)
    })

    await result.current.send('check this', [
      { kind: 'text', name: 'main.py', text: 'print("ok")' },
    ])

    const chatCall = fetchMock.mock.calls.find(([url]) => url === '/api/chat')
    expect(chatCall).toBeTruthy()
    expect(JSON.parse(String(chatCall?.[1]?.body))).toEqual({
      session_id: result.current.activeSession.id,
      provider: 'anthropic',
      model: 'claude-sonnet-4-6',
      cwd: '/workspace/a',
      input: [
        { type: 'text', text: 'check this' },
        {
          type: 'text',
          text: 'print("ok")',
          name: 'main.py',
          is_attachment: true,
        },
      ],
    })
  })

  it('keeps assistant replies when loading a persisted session with text attachments', async () => {
    globalThis.localStorage.setItem(
      'mycode.activeSessions',
      JSON.stringify({ '/workspace/a': 'session-1' }),
    )
    mockFetch({
      '/api/sessions?cwd=': createJsonResponse({
        sessions: [{ id: 'session-1', title: 'Persisted' }],
      }),
      '/api/sessions/session-1': createJsonResponse({
        session: { id: 'session-1', title: 'Persisted' },
        messages: [
          {
            role: 'user',
            content: [
              { type: 'text', text: 'check this' },
              {
                type: 'text',
                text: '<file name="main.py">\nprint("ok")\n</file>',
                meta: { attachment: true, path: 'main.py' },
              },
            ],
          },
          {
            role: 'assistant',
            content: [{ type: 'text', text: 'looks good' }],
          },
        ],
        active_run: null,
        pending_events: [],
      }),
    })

    const { result } = renderChatHook()

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false)
      expect(result.current.messages).toHaveLength(2)
    })

    expect(result.current.messages[1]).toEqual({
      role: 'assistant',
      renderKey: 'assistant:1',
      sourceIndex: 1,
      content: [
        {
          type: 'text',
          text: 'looks good',
          renderKey: 'assistant:1:0',
        },
      ],
    })
  })

  it('rebuilds assistant replies from active-run pending events after refresh', async () => {
    globalThis.localStorage.setItem(
      'mycode.activeSessions',
      JSON.stringify({ '/workspace/a': 'session-2' }),
    )
    mockFetch({
      '/api/sessions?cwd=': createJsonResponse({
        sessions: [{ id: 'session-2', title: 'Running' }],
      }),
      '/api/sessions/session-2': createJsonResponse({
        session: { id: 'session-2', title: 'Running' },
        messages: [
          {
            role: 'user',
            content: [
              {
                type: 'text',
                text: '<file name="main.py">\nprint("ok")\n</file>',
                meta: { attachment: true, path: 'main.py' },
              },
            ],
          },
        ],
        active_run: {
          id: 'run-2',
          session_id: 'session-2',
          status: 'running',
          last_seq: 1,
        },
        pending_events: [{ type: 'text', delta: 'looks good', seq: 1 }],
      }),
      '/api/runs/run-2/stream?after=1': new Response('data: [DONE]\n\n', {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }),
    })

    const { result } = renderChatHook()

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false)
      expect(result.current.messages).toHaveLength(2)
    })

    expect(result.current.messages[1]?.content[0]).toEqual({
      type: 'text',
      text: 'looks good',
      renderKey: 'assistant:1:0',
    })
  })
})
