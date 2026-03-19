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

const EMPTY_SESSION = { id: 'default', title: 'New chat' }

function hasPersistedToolResult(messages, toolUseId) {
  if (!toolUseId) return false

  return messages.some(
    (message) =>
      Array.isArray(message?.content) &&
      message.content.some(
        (block) =>
          block?.type === 'tool_result' && block.tool_use_id === toolUseId,
      ),
  )
}

function chatReducer(state, action) {
  switch (action.type) {
    case 'set_messages': {
      const rawMessages = Array.isArray(action.messages) ? action.messages : []
      return { rawMessages, toolRuntimeById: {} }
    }

    case 'append_user': {
      return {
        ...state,
        rawMessages: [
          ...state.rawMessages,
          createUserTextMessage(action.content),
        ],
      }
    }

    case 'start_assistant': {
      return {
        ...state,
        rawMessages: [...state.rawMessages, createAssistantMessage([])],
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

    case 'finalize_pending': {
      const fallbackResult = action.result || ''
      let changed = false
      let rawMessages = [...state.rawMessages]
      const toolRuntimeById = { ...state.toolRuntimeById }

      for (const [toolUseId, runtime] of Object.entries(toolRuntimeById)) {
        if (!runtime?.pending) continue

        const result = runtime.result || fallbackResult
        toolRuntimeById[toolUseId] = {
          ...runtime,
          pending: false,
          result,
          isError: true,
        }
        if (!hasPersistedToolResult(rawMessages, toolUseId)) {
          rawMessages = appendToolResult(rawMessages, toolUseId, result, true)
        }
        changed = true
      }

      return changed ? { rawMessages, toolRuntimeById } : state
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
  const [activeSession, setActiveSession] = useState(EMPTY_SESSION)
  const [loading, setLoading] = useState(false)
  const [sessionLoading, setSessionLoading] = useState(false)
  const [connectionState, setConnectionState] = useState('idle')
  const abortRef = useRef(null)
  const initRef = useRef(false)
  const cwdRef = useRef(config.cwd)

  const messages = useMemo(
    () => buildRenderMessages(chatState.rawMessages, chatState.toolRuntimeById),
    [chatState.rawMessages, chatState.toolRuntimeById],
  )

  const status = useMemo(() => {
    if (loading) return 'generating'
    if (connectionState === 'error') return 'offline'
    if (connectionState === 'ready') return 'ready'
    return 'idle'
  }, [loading, connectionState])

  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch(
        `/api/sessions?cwd=${encodeURIComponent(config.cwd)}`,
      )
      if (!res.ok) throw new Error('Failed to load sessions')
      const data = await res.json()
      setSessions(data.sessions || [])
      return data.sessions || []
    } catch (e) {
      console.error('Failed to load sessions:', e)
      return []
    }
  }, [config.cwd])

  const send = useCallback(
    async (input) => {
      if (!input.trim() || loading) return

      setLoading(true)
      setConnectionState('ready')
      dispatch({ type: 'append_user', content: input })
      dispatch({ type: 'start_assistant' })

      let aborted = false
      try {
        abortRef.current = new AbortController()
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: activeSession.id,
            message: input,
            provider: config.provider || undefined,
            model: config.model || undefined,
            cwd: config.cwd,
            api_key: config.apiKey || undefined,
            api_base: config.apiBase || undefined,
          }),
          signal: abortRef.current.signal,
        })

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
              dispatch({ type: 'apply_event', event: JSON.parse(data) })
            } catch (e) {
              console.error('Parse error:', e)
            }
          }
        }
      } catch (e) {
        if (e.name === 'AbortError') {
          aborted = true
        } else {
          setConnectionState('error')
          dispatch({
            type: 'apply_event',
            event: { type: 'error', message: e.message },
          })
          dispatch({ type: 'finalize_pending', result: `error: ${e.message}` })
        }
      } finally {
        dispatch({
          type: 'finalize_pending',
          result: aborted ? 'error: cancelled' : 'error: stream ended',
        })
        setLoading(false)
        fetchSessions()
      }
    },
    [activeSession.id, config, fetchSessions, loading],
  )

  const clear = useCallback(async () => {
    try {
      await fetch(
        `/api/sessions/${encodeURIComponent(activeSession.id)}/clear`,
        { method: 'POST' },
      )
      dispatch({ type: 'set_messages', messages: [] })
    } catch (e) {
      console.error('Failed to clear:', e)
    }
  }, [activeSession.id])

  const cancel = useCallback(async () => {
    abortRef.current?.abort()
    setLoading(false)
    dispatch({ type: 'finalize_pending', result: 'error: cancelled' })
    try {
      await fetch(
        `/api/cancel?session_id=${encodeURIComponent(activeSession.id)}`,
        { method: 'POST' },
      )
    } catch (e) {
      console.error('Failed to cancel:', e)
    }
  }, [activeSession.id])

  const createSession = useCallback(async () => {
    if (sessionLoading) return
    initRef.current = true
    setSessionLoading(true)
    try {
      const res = await fetch('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: config.provider || undefined,
          model: config.model || undefined,
          cwd: config.cwd,
          api_base: config.apiBase || undefined,
        }),
      })
      if (!res.ok) throw new Error('Failed to create session')
      const data = await res.json()
      if (data.session) {
        setActiveSession(data.session)
        dispatch({ type: 'set_messages', messages: [] })
        setSessions((prev) => [
          data.session,
          ...prev.filter((s) => s.id !== data.session.id),
        ])
      }
      await fetchSessions()
    } catch (e) {
      console.error('Failed to create session:', e)
    } finally {
      setSessionLoading(false)
    }
  }, [config, fetchSessions, sessionLoading])

  const selectSession = useCallback(
    async (sessionId) => {
      if (!sessionId || sessionId === activeSession.id) return
      initRef.current = true
      setSessionLoading(true)
      try {
        const res = await fetch(
          `/api/sessions/${encodeURIComponent(sessionId)}`,
        )
        if (!res.ok) throw new Error('Failed to load session')
        const data = await res.json()
        if (data.session) {
          setActiveSession(data.session)
          dispatch({ type: 'set_messages', messages: data.messages || [] })
        }
      } catch (e) {
        console.error('Failed to load session:', e)
      } finally {
        setSessionLoading(false)
      }
    },
    [activeSession.id],
  )

  const deleteSession = useCallback(
    async (sessionId) => {
      if (!sessionId || sessions.length === 1 || sessionId === activeSession.id)
        return
      setSessionLoading(true)
      try {
        await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
          method: 'DELETE',
        })
        setSessions((prev) => prev.filter((s) => s.id !== sessionId))
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
        await createSession()
        return
      }
      if (list.some((s) => s.id === activeSession.id)) return
      await selectSession(list[0].id)
    } catch (e) {
      console.error('Failed to initialize sessions:', e)
    }
  }, [activeSession.id, createSession, fetchSessions, selectSession])

  useEffect(() => {
    initializeSessions()
  }, [initializeSessions])

  useEffect(() => {
    if (cwdRef.current === config.cwd) return
    cwdRef.current = config.cwd
    initRef.current = false
    dispatch({ type: 'set_messages', messages: [] })
    setSessions([])
    setActiveSession(EMPTY_SESSION)
    initializeSessions()
  }, [config.cwd, initializeSessions])

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
