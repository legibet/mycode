import { beforeEach, describe, expect, it } from 'vitest'

import { loadActiveSession, saveActiveSession } from './storage'

function createLocalStorage() {
  const store = new Map()

  return {
    get length() {
      return store.size
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null
    },
    getItem(key: string) {
      return store.has(key) ? store.get(key) : null
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

describe('storage', () => {
  beforeEach(() => {
    globalThis.localStorage = createLocalStorage()
  })

  it('stores active sessions per workspace', () => {
    saveActiveSession('/workspace/a', 'session-a')
    saveActiveSession('/workspace/b', 'session-b')

    expect(loadActiveSession('/workspace/a')).toBe('session-a')
    expect(loadActiveSession('/workspace/b')).toBe('session-b')
  })

  it('returns empty for workspaces without a saved session', () => {
    saveActiveSession('/workspace/a', 'session-a')

    expect(loadActiveSession('/workspace/b')).toBe('')
  })
})
