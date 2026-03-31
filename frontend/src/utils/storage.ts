/**
 * Local storage utilities for config and history persistence.
 */

import type { LocalConfig } from '../types'
import { isReasoningEffort } from './config'

const STORAGE_KEY = 'mycode_config'
const HISTORY_KEY = 'mycode_cwd_history'
const ACTIVE_SESSIONS_KEY = 'mycode_active_sessions'
const SCHEMA_VERSION = 1

const DEFAULT_CONFIG: LocalConfig = {
  provider: '', // configured alias or raw provider id; empty = use server default
  model: '',
  cwd: '.',
  apiKey: '',
  apiBase: '',
  reasoningEffort: '', // empty = use server/config default
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function getString(
  record: Record<string, unknown>,
  key: string,
  fallback: string,
): string {
  return typeof record[key] === 'string' ? record[key] : fallback
}

function normalizeStoredConfig(record: Record<string, unknown>): LocalConfig {
  const reasoningEffort = record['reasoningEffort']

  return {
    provider: getString(record, 'provider', DEFAULT_CONFIG.provider),
    model: getString(record, 'model', DEFAULT_CONFIG.model),
    cwd: getString(record, 'cwd', DEFAULT_CONFIG.cwd),
    apiKey: DEFAULT_CONFIG.apiKey,
    apiBase: DEFAULT_CONFIG.apiBase,
    reasoningEffort: isReasoningEffort(reasoningEffort)
      ? reasoningEffort
      : DEFAULT_CONFIG.reasoningEffort,
  }
}

export function loadConfig(): LocalConfig {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved) {
      const parsed = JSON.parse(saved) as unknown
      if (!isRecord(parsed) || parsed['_v'] !== SCHEMA_VERSION) {
        return DEFAULT_CONFIG
      }
      // The web UI no longer exposes per-request auth/base overrides.
      // Drop any stale browser-side values so they cannot shadow backend config.
      return normalizeStoredConfig(parsed)
    }
  } catch (e) {
    console.error('Failed to load config:', e)
  }
  return DEFAULT_CONFIG
}

export function saveConfig(config: LocalConfig): void {
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

export function loadHistory(): string[] {
  try {
    const saved = localStorage.getItem(HISTORY_KEY)
    if (saved) {
      const parsed = JSON.parse(saved) as unknown
      return Array.isArray(parsed)
        ? parsed.filter((item): item is string => typeof item === 'string')
        : []
    }
  } catch (e) {
    console.error('Failed to load history:', e)
  }
  return []
}

export function saveHistory(history: string[]): void {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, 6)))
  } catch (e) {
    console.error('Failed to save history:', e)
  }
}

export function addHistory(history: string[], value: string): string[] {
  if (!value) return history
  const cleaned = value.trim()
  if (!cleaned) return history
  const next = [cleaned, ...history.filter((item) => item !== cleaned)]
  return next.slice(0, 6)
}

function normalizeCwdKey(cwd: string): string {
  if (typeof cwd !== 'string') return '.'
  const value = cwd.trim()
  return value || '.'
}

function loadActiveSessionMap(): Record<string, string> {
  try {
    const saved = localStorage.getItem(ACTIVE_SESSIONS_KEY)
    if (!saved) return {}
    const parsed = JSON.parse(saved) as unknown
    if (!isRecord(parsed)) return {}
    return Object.fromEntries(
      Object.entries(parsed).filter(
        (entry): entry is [string, string] =>
          typeof entry[0] === 'string' && typeof entry[1] === 'string',
      ),
    )
  } catch (e) {
    console.error('Failed to load active sessions:', e)
    return {}
  }
}

function saveActiveSessionMap(activeSessions: Record<string, string>): void {
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

export function loadActiveSession(cwd: string): string {
  const activeSessions = loadActiveSessionMap()
  const sessionId = activeSessions[normalizeCwdKey(cwd)]
  return typeof sessionId === 'string' ? sessionId : ''
}

export function saveActiveSession(cwd: string, sessionId: string): void {
  if (typeof sessionId !== 'string' || !sessionId) return
  const activeSessions = loadActiveSessionMap()
  activeSessions[normalizeCwdKey(cwd)] = sessionId
  saveActiveSessionMap(activeSessions)
}
