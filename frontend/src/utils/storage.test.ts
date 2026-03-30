import assert from 'node:assert/strict'
import test from 'node:test'

import { loadActiveSession, saveActiveSession } from './storage'

function createLocalStorage() {
  const store = new Map()

  return {
    get length() {
      return store.size
    },
    key(index) {
      return Array.from(store.keys())[index] ?? null
    },
    getItem(key) {
      return store.has(key) ? store.get(key) : null
    },
    setItem(key, value) {
      store.set(key, String(value))
    },
    removeItem(key) {
      store.delete(key)
    },
    clear() {
      store.clear()
    },
  }
}

test.beforeEach(() => {
  globalThis.localStorage = createLocalStorage()
})

test('active sessions are stored per workspace', () => {
  saveActiveSession('/workspace/a', 'session-a')
  saveActiveSession('/workspace/b', 'session-b')

  assert.equal(loadActiveSession('/workspace/a'), 'session-a')
  assert.equal(loadActiveSession('/workspace/b'), 'session-b')
})

test('loadActiveSession returns empty for workspaces without a saved session', () => {
  saveActiveSession('/workspace/a', 'session-a')

  assert.equal(loadActiveSession('/workspace/b'), '')
})
