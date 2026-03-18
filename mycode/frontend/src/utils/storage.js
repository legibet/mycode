/**
 * Local storage utilities for config and history persistence.
 */

const STORAGE_KEY = 'mycode_config'
const HISTORY_KEY = 'mycode_cwd_history'

export const DEFAULT_CONFIG = {
  provider: '', // configured alias or raw any-llm provider id; empty = use server default
  model: '',
  cwd: '.',
  apiKey: '',
  apiBase: '',
}

export const MODEL_PRESETS = [
  'anthropic:claude-opus-4-6',
  'anthropic:claude-sonnet-4-6',
  'openai:gpt-5.4',
  'openai:gpt-5-mini',
  'gemini:gemini-1.5-pro',
  'gemini:gemini-2.0-flash',
]

export function loadConfig() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved) return { ...DEFAULT_CONFIG, ...JSON.parse(saved) }
  } catch (e) {
    console.error('Failed to load config:', e)
  }
  return DEFAULT_CONFIG
}

export function saveConfig(config) {
  try {
    // Never persist raw api key to localStorage
    const { apiKey, ...rest } = config
    localStorage.setItem(STORAGE_KEY, JSON.stringify(rest))
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
