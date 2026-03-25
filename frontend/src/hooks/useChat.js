/**
 * Chat state management hook.
 * Stores canonical raw messages and derives render messages from them.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from 'react'
import {
  appendAssistantDelta,
  appendToolResult,
  appendToolUse,
  buildRenderMessages,
  createAssistantMessage,
  createUserTextMessage,
} from '../utils/messages'

const DEFAULT_SESSION_TITLE = 'New chat'

function createDraftSession() {
  const id =
    globalThis.crypto?.randomUUID?.() ||
    `draft-${Date.now()}-${Math.random().toString(16).slice(2)}`

  return { id, title: DEFAULT_SESSION_TITLE, isDraft: true }
}

function chatReducer(state, action) {
  switch (action.type) {
    case 'set_messages': {
      const rawMessages = Array.isArray(action.messages) ? action.messages : []
      return { rawMessages, toolRuntimeById: {} }
    }

    case 'start_turn': {
      return {
        ...state,
        rawMessages: [
          ...state.rawMessages,
          createUserTextMessage(action.content),
          createAssistantMessage([]),
        ],
      }
    }

    case 'apply_event': {
      const event = action.event || {}
      let rawMessages = [...state.rawMessages]
      const toolRuntimeById = { ...state.toolRuntimeById }

      if (event.type === 'reasoning') {
        rawMessages = appendAssistantDelta(
          rawMessages,
          'thinking',
          event.delta || '',
        )
      } else if (event.type === 'text') {
        rawMessages = appendAssistantDelta(
          rawMessages,
          'text',
          event.delta || '',
        )
      } else if (event.type === 'tool_start') {
        const toolCall = event.tool_call || {}
        rawMessages = appendToolUse(rawMessages, toolCall)
        if (toolCall.id) {
          toolRuntimeById[toolCall.id] = {
            pending: true,
            output: '',
            result: null,
            isError: false,
          }
        }
      } else if (event.type === 'tool_output') {
        const toolUseId = event.tool_use_id || ''
        if (toolUseId) {
          const current = toolRuntimeById[toolUseId] || {
            pending: true,
            output: '',
            result: null,
            isError: false,
          }
          const nextOutput = event.output || ''
          toolRuntimeById[toolUseId] = {
            ...current,
            pending: true,
            output: current.output
              ? `${current.output}\n${nextOutput}`
              : nextOutput,
          }
        }
      } else if (event.type === 'tool_done') {
        const toolUseId = event.tool_use_id || ''
        const result = event.result || ''
        const isError = Boolean(
          event.is_error ||
            (typeof result === 'string' && result.startsWith('error:')),
        )

        if (toolUseId) {
          const current = toolRuntimeById[toolUseId] || {
            pending: false,
            output: '',
            result: null,
            isError: false,
          }
          toolRuntimeById[toolUseId] = {
            ...current,
            pending: false,
            result,
            isError,
          }
          rawMessages = appendToolResult(
            rawMessages,
            toolUseId,
            result,
            isError,
          )
        }
      } else if (event.type === 'error') {
        rawMessages = appendAssistantDelta(
          rawMessages,
          'text',
          `\n\n**Error:** ${event.message || 'Unknown'}`,
        )
      }

      return { rawMessages, toolRuntimeById }
    }
    default:
      return state
  }
}

export function useChat(config) {
  const [chatState, dispatch] = useReducer(chatReducer, {
    rawMessages: [],
    toolRuntimeById: {},
  })
  const [sessions, setSessions] = useState([])
  const [activeSession, setActiveSession] = useState(createDraftSession)
  const [loading, setLoading] = useState(false)
  const [sessionLoading, setSessionLoading] = useState(false)
  const [connectionState, setConnectionState] = useState('idle')
  const initRef = useRef(false)
  const cwdRef = useRef(config.cwd)
  const activeSessionRef = useRef(activeSession)
  const activeSessionIdRef = useRef(activeSession.id)
  const requestTokenRef = useRef(0)
  const pendingRequestTokenRef = useRef(0)
  const streamAbortRef = useRef(null)
  const streamTokenRef = useRef(0)
  const activeRunRef = useRef(null)

  const messages = useMemo(
    () => buildRenderMessages(chatState.rawMessages, chatState.toolRuntimeById),
    [chatState.rawMessages, chatState.toolRuntimeById],
  )

  const status = loading
    ? 'generating'
    : connectionState === 'error'
      ? 'offline'
      : connectionState === 'ready'
        ? 'ready'
        : 'idle'

  const cancelRun = useCallback(async (runId) => {
    if (!runId) return

    try {
      await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
        method: 'POST',
      })
    } catch (e) {
      console.error('Failed to cancel:', e)
    }
  }, [])

  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch(
        `/api/sessions?cwd=${encodeURIComponent(config.cwd)}`,
      )
      if (!res.ok) throw new Error('Failed to load sessions')
      const data = await res.json()
      const savedSessions = data.sessions || []
      const active = activeSessionRef.current
      const sessionsWithDraft =
        active.isDraft &&
        !savedSessions.some((session) => session.id === active.id)
          ? [active, ...savedSessions]
          : savedSessions

      setSessions(sessionsWithDraft)

      const syncedActive = sessionsWithDraft.find(
        (session) => session.id === active.id,
      )
      if (syncedActive && syncedActive !== active) {
        activeSessionRef.current = syncedActive
        activeSessionIdRef.current = syncedActive.id
        setActiveSession(syncedActive)
      }

      return sessionsWithDraft
    } catch (e) {
      console.error('Failed to load sessions:', e)
      return []
    }
  }, [config.cwd])

  const stopStreaming = useCallback(() => {
    streamTokenRef.current += 1
    pendingRequestTokenRef.current = 0
    streamAbortRef.current?.abort()
    streamAbortRef.current = null
    activeRunRef.current = null
    setLoading(false)
    setConnectionState('ready')
  }, [])

  const streamRun = useCallback(
    async (run, sessionId, after = 0) => {
      const runId = run?.id
      if (!runId) return

      streamTokenRef.current += 1
      const token = streamTokenRef.current
      streamAbortRef.current?.abort()

      const controller = new AbortController()
      streamAbortRef.current = controller
      activeRunRef.current = run
      setLoading(true)
      setConnectionState('ready')

      try {
        const res = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/stream?after=${after}`,
          { signal: controller.signal },
        )
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`)

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            const data = line.slice(6)
            if (data === '[DONE]') continue

            try {
              const event = JSON.parse(data)
              if (
                streamTokenRef.current !== token ||
                activeSessionIdRef.current !== sessionId
              ) {
                continue
              }
              dispatch({ type: 'apply_event', event })
            } catch (e) {
              console.error('Parse error:', e)
            }
          }
        }
      } catch (e) {
        if (e.name !== 'AbortError') {
          if (
            streamTokenRef.current === token &&
            activeSessionIdRef.current === sessionId
          ) {
            setConnectionState('error')
            dispatch({
              type: 'apply_event',
              event: {
                type: 'error',
                message: 'Stream disconnected. Reload the session to resume.',
              },
            })
          }
        }
      } finally {
        if (streamTokenRef.current === token) {
          streamAbortRef.current = null
          activeRunRef.current = null

          if (activeSessionIdRef.current === sessionId) {
            setLoading(false)
          }

          fetchSessions()
        }
      }
    },
    [fetchSessions],
  )

  const loadSession = useCallback(
    async (sessionId) => {
      const previousSession = activeSessionRef.current
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`)
      if (!res.ok) throw new Error('Failed to load session')

      const data = await res.json()
      if (!data.session) return null

      setConnectionState('ready')
      activeSessionRef.current = data.session
      activeSessionIdRef.current = data.session.id
      setActiveSession(data.session)
      if (previousSession.isDraft) {
        setSessions((prev) =>
          prev.filter((session) => session.id !== previousSession.id),
        )
      }
      dispatch({ type: 'set_messages', messages: data.messages || [] })

      const pendingEvents = Array.isArray(data.pending_events)
        ? data.pending_events
        : []
      for (const event of pendingEvents) {
        dispatch({ type: 'apply_event', event })
      }

      const run = data.active_run || null
      activeRunRef.current = run

      if (run?.id) {
        const lastSeq = pendingEvents.at(-1)?.seq || 0
        streamRun(run, data.session.id, lastSeq)
      } else {
        setLoading(false)
      }

      return data
    },
    [streamRun],
  )

  const send = useCallback(
    async (input) => {
      const content = input.trim()
      if (!content || loading) return

      const sessionId = activeSession.id
      const previousMessages = chatState.rawMessages
      const requestToken = requestTokenRef.current + 1

      requestTokenRef.current = requestToken
      pendingRequestTokenRef.current = requestToken

      dispatch({ type: 'start_turn', content })
      setLoading(true)
      setConnectionState('ready')

      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: sessionId,
            message: content,
            provider: config.provider || undefined,
            model: config.model || undefined,
            cwd: config.cwd,
            reasoning_effort:
              config.reasoningEffort && config.reasoningEffort !== 'auto'
                ? config.reasoningEffort
                : undefined,
          }),
        })

        const data = await res.json()
        const isCurrentRequest =
          pendingRequestTokenRef.current === requestToken &&
          activeSessionIdRef.current === sessionId

        if (!res.ok) {
          const existingRun = data?.detail?.run
          if (res.status === 409 && existingRun?.id) {
            if (isCurrentRequest) {
              pendingRequestTokenRef.current = 0
              dispatch({ type: 'set_messages', messages: previousMessages })
              streamRun(existingRun, sessionId, existingRun.last_seq || 0)
            }
            return
          }
          throw new Error(data?.detail?.message || 'Failed to start task')
        }

        pendingRequestTokenRef.current = 0

        if (!isCurrentRequest) {
          await cancelRun(data.run?.id)
          return
        }

        streamRun(data.run, sessionId, 0)
      } catch (e) {
        if (
          pendingRequestTokenRef.current === requestToken &&
          activeSessionIdRef.current === sessionId
        ) {
          pendingRequestTokenRef.current = 0
          setLoading(false)
          setConnectionState('error')
          dispatch({
            type: 'apply_event',
            event: { type: 'error', message: e.message },
          })
        }
      }
    },
    [
      activeSession.id,
      cancelRun,
      chatState.rawMessages,
      config,
      loading,
      streamRun,
    ],
  )

  const clear = useCallback(async () => {
    try {
      const res = await fetch(
        `/api/sessions/${encodeURIComponent(activeSession.id)}/clear`,
        { method: 'POST' },
      )
      if (!res.ok) throw new Error('Failed to clear session')
      dispatch({ type: 'set_messages', messages: [] })
    } catch (e) {
      console.error('Failed to clear:', e)
    }
  }, [activeSession.id])

  const cancel = useCallback(() => {
    const runId = activeRunRef.current?.id
    stopStreaming()

    if (!runId) return
    void cancelRun(runId)
  }, [cancelRun, stopStreaming])

  const createSession = useCallback(() => {
    if (sessionLoading) return

    stopStreaming()
    initRef.current = true
    const session = createDraftSession()

    activeSessionRef.current = session
    activeSessionIdRef.current = session.id
    setActiveSession(session)
    dispatch({ type: 'set_messages', messages: [] })
    setSessions((prev) => [session, ...prev.filter((item) => !item.isDraft)])
  }, [sessionLoading, stopStreaming])

  const selectSession = useCallback(
    async (sessionId) => {
      if (!sessionId || sessionId === activeSession.id) return

      stopStreaming()
      initRef.current = true
      setSessionLoading(true)

      try {
        await loadSession(sessionId)
      } catch (e) {
        console.error('Failed to load session:', e)
      } finally {
        setSessionLoading(false)
      }
    },
    [activeSession.id, loadSession, stopStreaming],
  )

  const deleteSession = useCallback(
    async (sessionId) => {
      if (
        !sessionId ||
        sessions.length === 1 ||
        sessionId === activeSession.id
      ) {
        return
      }

      setSessionLoading(true)
      try {
        const res = await fetch(
          `/api/sessions/${encodeURIComponent(sessionId)}`,
          {
            method: 'DELETE',
          },
        )
        if (!res.ok) throw new Error('Failed to delete session')
        setSessions((prev) =>
          prev.filter((session) => session.id !== sessionId),
        )
      } catch (e) {
        console.error('Failed to delete session:', e)
      } finally {
        setSessionLoading(false)
      }
    },
    [activeSession.id, sessions.length],
  )

  const initializeSessions = useCallback(async () => {
    if (initRef.current) return

    initRef.current = true
    try {
      const list = await fetchSessions()
      if (list.length === 0) {
        createSession()
        return
      }
      if (list.some((session) => session.id === activeSession.id)) return
      await loadSession(list[0].id)
    } catch (e) {
      console.error('Failed to initialize sessions:', e)
    }
  }, [activeSession.id, createSession, fetchSessions, loadSession])

  useEffect(() => {
    activeSessionRef.current = activeSession
    activeSessionIdRef.current = activeSession.id
  }, [activeSession])

  useEffect(() => {
    initializeSessions()
  }, [initializeSessions])

  useEffect(() => {
    if (cwdRef.current === config.cwd) return

    stopStreaming()
    cwdRef.current = config.cwd
    initRef.current = false
    dispatch({ type: 'set_messages', messages: [] })
    setSessions([])
    const session = createDraftSession()
    setActiveSession(session)
    activeSessionRef.current = session
    activeSessionIdRef.current = session.id
    initializeSessions()
  }, [config.cwd, initializeSessions, stopStreaming])

  useEffect(() => {
    return () => {
      stopStreaming()
    }
  }, [stopStreaming])

  return {
    messages,
    loading,
    status,
    sessions,
    activeSession,
    sessionLoading,
    send,
    clear,
    cancel,
    createSession,
    selectSession,
    deleteSession,
  }
}
