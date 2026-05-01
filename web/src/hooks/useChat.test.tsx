import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatMessage, RenderMessage } from '../types'
import { isCompactMarker } from '../types'
import { loadActiveSession, saveActiveSession } from '../utils/storage'
import { useChat } from './useChat'

function expectChat(message: RenderMessage | undefined): ChatMessage {
  if (!message || isCompactMarker(message)) {
    throw new Error('expected a ChatMessage, got compact marker or undefined')
  }
  return message
}

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

  it('sends document data without keeping it in UI messages', async () => {
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
      provider: 'xiaomi',
      model: 'mimo-v2.5-pro',
    })

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false)
    })

    await result.current.send('summarize', [
      {
        kind: 'document',
        name: 'report.pdf',
        mime_type: 'application/pdf',
        data: 'large-pdf-base64',
      },
    ])

    const chatCall = fetchMock.mock.calls.find(([url]) => url === '/api/chat')
    expect(JSON.parse(String(chatCall?.[1]?.body)).input[1]).toEqual({
      type: 'document',
      data: 'large-pdf-base64',
      mime_type: 'application/pdf',
      name: 'report.pdf',
    })

    await waitFor(() => {
      const userMessage = result.current.messages.find(
        (message): message is ChatMessage =>
          !isCompactMarker(message) && message.role === 'user',
      )
      expect(userMessage?.content[1]).toEqual({
        type: 'document',
        data: '',
        mime_type: 'application/pdf',
        name: 'report.pdf',
        renderKey: 'user:0:1',
      })
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

    expect(expectChat(result.current.messages[1]).content[0]).toEqual({
      type: 'text',
      text: 'looks good',
      renderKey: 'assistant:1:0',
    })
  })

  it('deletes the latest active session and loads the previous session in history', async () => {
    saveActiveSession('/workspace/a', 'session-2')

    mockFetch({
      '/api/sessions?cwd=': createJsonResponse({
        sessions: [
          { id: 'session-2', title: 'Second' },
          { id: 'session-1', title: 'First' },
        ],
      }),
      '/api/sessions/session-1': createJsonResponse({
        session: { id: 'session-1', title: 'First' },
        messages: [
          {
            role: 'assistant',
            content: [{ type: 'text', text: 'first session' }],
          },
        ],
        active_run: null,
        pending_events: [],
      }),
      '/api/sessions/session-2': (init?: RequestInit) =>
        init?.method === 'DELETE'
          ? new Response(null, { status: 200 })
          : createJsonResponse({
              session: { id: 'session-2', title: 'Second' },
              messages: [
                {
                  role: 'assistant',
                  content: [{ type: 'text', text: 'second session' }],
                },
              ],
              active_run: null,
              pending_events: [],
            }),
    })

    const { result } = renderChatHook()

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false)
      expect(result.current.activeSession.id).toBe('session-2')
    })

    await result.current.deleteSession('session-2')

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false)
      expect(result.current.activeSession.id).toBe('session-1')
      expect(result.current.sessions.map((session) => session.id)).toEqual([
        'session-1',
      ])
      expect(expectChat(result.current.messages[0]).content[0]).toEqual({
        type: 'text',
        text: 'first session',
        renderKey: 'assistant:0:0',
      })
    })

    expect(loadActiveSession('/workspace/a')).toBe('session-1')
  })

  it('replays pending permission requests and clears them on decide', async () => {
    globalThis.localStorage.setItem(
      'mycode.activeSessions',
      JSON.stringify({ '/workspace/a': 'session-3' }),
    )
    const decideCalls: string[] = []
    const encoder = new TextEncoder()
    let streamController!: ReadableStreamDefaultController<Uint8Array>
    const liveStream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller
      },
    })
    mockFetch({
      '/api/sessions?cwd=': createJsonResponse({
        sessions: [{ id: 'session-3', title: 'Awaiting' }],
      }),
      '/api/sessions/session-3': createJsonResponse({
        session: { id: 'session-3', title: 'Awaiting' },
        messages: [],
        active_run: {
          id: 'run-3',
          session_id: 'session-3',
          status: 'running',
          last_seq: 0,
        },
        pending_events: [],
      }),
      '/api/runs/run-3/stream?after=0': new Response(liveStream, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }),
      '/api/runs/run-3/decide': (init?: RequestInit) => {
        decideCalls.push(String(init?.body ?? ''))
        streamController.enqueue(
          encoder.encode(
            'data: {"type":"permission_resolved","seq":2,"request_id":"req-1","decision":"allow"}\n\n',
          ),
        )
        return new Response(JSON.stringify({ status: 'ok' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      },
    })

    const { result } = renderChatHook()

    await waitFor(() => {
      expect(result.current.activeSession.id).toBe('session-3')
    })

    streamController.enqueue(
      encoder.encode(
        'data: {"type":"permission_request","seq":1,"request_id":"req-1","tool_use_id":"call-1","tool_name":"bash","preview":"pnpm install"}\n\n',
      ),
    )

    await waitFor(() => {
      expect(result.current.pendingPermission?.request_id).toBe('req-1')
    })
    expect(result.current.pendingPermission?.preview).toBe('pnpm install')

    await result.current.decidePermission('allow')

    await waitFor(() => {
      expect(result.current.pendingPermission).toBeNull()
    })
    expect(decideCalls).toEqual([
      JSON.stringify({ request_id: 'req-1', decision: 'allow' }),
    ])

    streamController.close()
  })

  it('keeps the permission prompt visible when decide POST fails', async () => {
    globalThis.localStorage.setItem(
      'mycode.activeSessions',
      JSON.stringify({ '/workspace/a': 'session-4' }),
    )
    const encoder = new TextEncoder()
    let streamController!: ReadableStreamDefaultController<Uint8Array>
    const liveStream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller
      },
    })
    mockFetch({
      '/api/sessions?cwd=': createJsonResponse({
        sessions: [{ id: 'session-4', title: 'Awaiting' }],
      }),
      '/api/sessions/session-4': createJsonResponse({
        session: { id: 'session-4', title: 'Awaiting' },
        messages: [],
        active_run: {
          id: 'run-4',
          session_id: 'session-4',
          status: 'running',
          last_seq: 0,
        },
        pending_events: [],
      }),
      '/api/runs/run-4/stream?after=0': new Response(liveStream, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }),
      '/api/runs/run-4/decide': new Response('boom', { status: 500 }),
    })

    const { result } = renderChatHook()

    await waitFor(() => {
      expect(result.current.activeSession.id).toBe('session-4')
    })

    streamController.enqueue(
      encoder.encode(
        'data: {"type":"permission_request","seq":1,"request_id":"req-1","tool_use_id":"call-1","tool_name":"bash","preview":"pnpm install"}\n\n',
      ),
    )

    await waitFor(() => {
      expect(result.current.pendingPermission?.request_id).toBe('req-1')
    })

    await result.current.decidePermission('allow')

    expect(result.current.pendingPermission?.request_id).toBe('req-1')

    streamController.close()
  })

  it('deletes the last active session and falls back to a draft', async () => {
    saveActiveSession('/workspace/a', 'session-1')

    mockFetch({
      '/api/sessions?cwd=': createJsonResponse({
        sessions: [{ id: 'session-1', title: 'Only Session' }],
      }),
      '/api/sessions/session-1': (init?: RequestInit) =>
        init?.method === 'DELETE'
          ? new Response(null, { status: 200 })
          : createJsonResponse({
              session: { id: 'session-1', title: 'Only Session' },
              messages: [
                {
                  role: 'assistant',
                  content: [{ type: 'text', text: 'only session' }],
                },
              ],
              active_run: null,
              pending_events: [],
            }),
    })

    const { result } = renderChatHook()

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false)
      expect(result.current.activeSession.id).toBe('session-1')
    })

    await result.current.deleteSession('session-1')

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false)
      expect(result.current.activeSession.isDraft).toBe(true)
      expect(result.current.sessions).toHaveLength(1)
      expect(result.current.sessions[0]?.id).toBe(
        result.current.activeSession.id,
      )
      expect(result.current.messages).toEqual([])
    })

    expect(loadActiveSession('/workspace/a')).toBe('')
  })
})
