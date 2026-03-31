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

describe('useChat', () => {
  beforeEach(() => {
    globalThis.localStorage = createLocalStorage()
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/sessions?cwd=')) {
        return new Response(JSON.stringify({ sessions: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      throw new Error(`Unexpected fetch: ${url}`)
    }) as typeof fetch
  })

  it('creates a draft session when the workspace has no saved sessions', async () => {
    const { result } = renderHook(() =>
      useChat({
        provider: '',
        model: '',
        cwd: '/workspace/a',
        apiKey: '',
        apiBase: '',
        reasoningEffort: '',
      }),
    )

    await waitFor(() => {
      expect(result.current.sessionLoading).toBe(false)
      expect(result.current.sessions).toHaveLength(1)
    })

    expect(result.current.activeSession.isDraft).toBe(true)
    expect(result.current.sessions[0]?.id).toBe(result.current.activeSession.id)
    expect(result.current.messages).toEqual([])
    expect(result.current.loading).toBe(false)
  })
})
