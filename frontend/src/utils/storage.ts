/**
 * Local storage utilities for config and history persistence.
 */

const STORAGE_KEY = 'mycode_config'
const HISTORY_KEY = 'mycode_cwd_history'
const ACTIVE_SESSIONS_KEY = 'mycode_active_sessions'
const SCHEMA_VERSION = 1

const DEFAULT_CONFIG = {
  provider: '', // configured alias or raw provider id; empty = use server default
  model: '',
  cwd: '.',
  apiKey: '',
  apiBase: '',
  reasoningEffort: '', // empty = use server/config default
}

export function loadConfig() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved) {
      const parsed = JSON.parse(saved)
      if (parsed._v !== SCHEMA_VERSION) return DEFAULT_CONFIG
      // The web UI no longer exposes per-request auth/base overrides.
      // Drop any stale browser-side values so they cannot shadow backend config.
      const { apiKey: _apiKey, apiBase: _apiBase, ...rest } = parsed
      return { ...DEFAULT_CONFIG, ...rest }
    }
  } catch (e) {
    console.error('Failed to load config:', e)
  }
  return DEFAULT_CONFIG
}

export function saveConfig(config) {
  try {
    // Keep browser config aligned with the visible settings only.
    const { apiKey, apiBase, ...rest } = config
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ ...rest, _v: SCHEMA_VERSION }),
    )
  } catch (e) {
    console.error('Failed to save config:', e)
  }
}

export function loadHistory() {
  try {
    const saved = localStorage.getItem(HISTORY_KEY)
    if (saved) return JSON.parse(saved)
  } catch (e) {
    console.error('Failed to load history:', e)
  }
  return []
}

export function saveHistory(history) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, 6)))
  } catch (e) {
    console.error('Failed to save history:', e)
  }
}

export function addHistory(history, value) {
  if (!value) return history
  const cleaned = value.trim()
  if (!cleaned) return history
  const next = [cleaned, ...history.filter((item) => item !== cleaned)]
  return next.slice(0, 6)
}

function normalizeCwdKey(cwd) {
  if (typeof cwd !== 'string') return '.'
  const value = cwd.trim()
  return value || '.'
}

function loadActiveSessionMap() {
  try {
    const saved = localStorage.getItem(ACTIVE_SESSIONS_KEY)
    if (!saved) return {}
    const parsed = JSON.parse(saved)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch (e) {
    console.error('Failed to load active sessions:', e)
    return {}
  }
}

function saveActiveSessionMap(activeSessions) {
  try {
    const entries = Object.entries(activeSessions).filter(
      ([cwd, sessionId]) =>
        typeof cwd === 'string' &&
        cwd &&
        typeof sessionId === 'string' &&
        sessionId,
    )
    if (entries.length === 0) {
      localStorage.removeItem(ACTIVE_SESSIONS_KEY)
      return
    }
    localStorage.setItem(
      ACTIVE_SESSIONS_KEY,
      JSON.stringify(Object.fromEntries(entries)),
    )
  } catch (e) {
    console.error('Failed to save active sessions:', e)
  }
}

export function loadActiveSession(cwd) {
  const activeSessions = loadActiveSessionMap()
  const sessionId = activeSessions[normalizeCwdKey(cwd)]
  return typeof sessionId === 'string' ? sessionId : ''
}

export function saveActiveSession(cwd, sessionId) {
  if (typeof sessionId !== 'string' || !sessionId) return
  const activeSessions = loadActiveSessionMap()
  activeSessions[normalizeCwdKey(cwd)] = sessionId
  saveActiveSessionMap(activeSessions)
}
