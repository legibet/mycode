/**
 * Chat state management hook.
 * Handles messages, sessions, and SSE streaming.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { buildToolIndex, transformMessages } from '../utils/messages'

const EMPTY_SESSION = { id: 'default', title: 'New chat' }

/**
 * Chat state reducer - handles message updates from SSE events.
 */
function chatReducer(state, action) {
  switch (action.type) {
    case 'set_messages': {
      const messages = Array.isArray(action.messages) ? action.messages : []
      return { messages, toolIndex: buildToolIndex(messages) }
    }

    case 'append_user': {
      const messages = [...state.messages, { role: 'user', parts: [{ type: 'text', content: action.content }] }]
      return { ...state, messages }
    }

    case 'start_assistant': {
      const messages = [...state.messages, { role: 'assistant', parts: [] }]
      return { ...state, messages }
    }

    case 'apply_event': {
      const event = action.event || {}
      const messages = [...state.messages]
      const toolIndex = { ...state.toolIndex }

      // Get or create latest assistant message
      let assistantIndex = messages.length - 1
      if (assistantIndex < 0 || messages[assistantIndex].role !== 'assistant') {
        messages.push({ role: 'assistant', parts: [] })
        assistantIndex = messages.length - 1
      }

      const assistant = { ...messages[assistantIndex], parts: [...messages[assistantIndex].parts] }
      messages[assistantIndex] = assistant

      // Find tool part by id
      const findToolEntry = (toolId) => {
        if (!toolId) return null
        if (toolIndex[toolId]) return toolIndex[toolId]
        // Fallback scan
        for (let m = messages.length - 1; m >= 0; m--) {
          const msg = messages[m]
          if (msg.role !== 'assistant') continue
          for (let p = (msg.parts || []).length - 1; p >= 0; p--) {
            if (msg.parts[p]?.type === 'tool' && msg.parts[p].id === toolId) {
              toolIndex[toolId] = { messageIndex: m, partIndex: p }
              return toolIndex[toolId]
            }
          }
        }
        return null
      }

      // Handle event types
      if (event.type === 'reasoning') {
        const lastPart = assistant.parts[assistant.parts.length - 1]
        if (lastPart?.type === 'reasoning') {
          assistant.parts[assistant.parts.length - 1] = { ...lastPart, content: lastPart.content + event.content }
        } else {
          assistant.parts.push({ type: 'reasoning', content: event.content })
        }
      } else if (event.type === 'text') {
        const lastPart = assistant.parts[assistant.parts.length - 1]
        if (lastPart?.type === 'text') {
          assistant.parts[assistant.parts.length - 1] = { ...lastPart, content: lastPart.content + event.content }
        } else {
          assistant.parts.push({ type: 'text', content: event.content })
        }
      } else if (event.type === 'tool_start') {
        const partIndex = assistant.parts.length
        assistant.parts.push({
          type: 'tool',
          id: event.id,
          name: event.name,
          args: event.args,
          result: '',
          pending: true,
        })
        if (event.id) toolIndex[event.id] = { messageIndex: assistantIndex, partIndex }
      } else if (event.type === 'tool_output') {
        const entry = findToolEntry(event.id)
        if (entry) {
          const targetMsg = messages[entry.messageIndex]
          const targetParts = [...targetMsg.parts]
          const part = { ...targetParts[entry.partIndex] }
          const output = event.content || ''
          part.result = part.result ? `${part.result}\n${output}` : output
          part.pending = true
          targetParts[entry.partIndex] = part
          messages[entry.messageIndex] = { ...targetMsg, parts: targetParts }
        }
      } else if (event.type === 'tool_done') {
        const entry = findToolEntry(event.id)
        if (entry) {
          const targetMsg = messages[entry.messageIndex]
          const targetParts = [...targetMsg.parts]
          targetParts[entry.partIndex] = { ...targetParts[entry.partIndex], result: event.result, pending: false }
          messages[entry.messageIndex] = { ...targetMsg, parts: targetParts }
        }
      } else if (event.type === 'error') {
        assistant.parts.push({ type: 'text', content: `\n\n**Error:** ${event.message || event.error || 'Unknown'}` })
      }

      return { messages, toolIndex }
    }

    case 'finalize_pending': {
      const result = action.result
      let changed = false
      const messages = state.messages.map((msg) => {
        if (msg.role !== 'assistant') return msg
        let msgChanged = false
        const parts = (msg.parts || []).map((part) => {
          if (part?.type !== 'tool' || !part.pending) return part
          changed = true
          msgChanged = true
          return { ...part, pending: false, result: part.result || result || '' }
        })
        return msgChanged ? { ...msg, parts } : msg
      })
      return changed ? { ...state, messages } : state
    }

    default:
      return state
  }
}

export function useChat(config) {
  const [chatState, dispatch] = useReducer(chatReducer, { messages: [], toolIndex: {} })
  const [sessions, setSessions] = useState([])
  const [activeSession, setActiveSession] = useState(EMPTY_SESSION)
  const [loading, setLoading] = useState(false)
  const [sessionLoading, setSessionLoading] = useState(false)
  const [connectionState, setConnectionState] = useState('idle')
  const abortRef = useRef(null)
  const initRef = useRef(false)
  const cwdRef = useRef(config.cwd)

  const messages = chatState.messages

  const status = useMemo(() => {
    if (loading) return 'generating'
    if (connectionState === 'error') return 'offline'
    if (connectionState === 'ready') return 'ready'
    return 'idle'
  }, [loading, connectionState])

  // Fetch sessions list
  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch(`/api/sessions?cwd=${encodeURIComponent(config.cwd)}`)
      if (!res.ok) throw new Error('Failed to load sessions')
      const data = await res.json()
      setSessions(data.sessions || [])
      return data.sessions || []
    } catch (e) {
      console.error('Failed to load sessions:', e)
      return []
    }
  }, [config.cwd])

  // Send message and stream response
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
          buffer = lines.pop()

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
          dispatch({ type: 'apply_event', event: { type: 'error', message: e.message } })
          dispatch({ type: 'finalize_pending', result: `error: ${e.message}` })
        }
      } finally {
        dispatch({ type: 'finalize_pending', result: aborted ? 'error: cancelled' : 'error: stream ended' })
        setLoading(false)
        fetchSessions()
      }
    },
    [activeSession.id, config, fetchSessions, loading]
  )

  // Clear current session
  const clear = useCallback(async () => {
    try {
      await fetch(`/api/sessions/${encodeURIComponent(activeSession.id)}/clear`, { method: 'POST' })
      dispatch({ type: 'set_messages', messages: [] })
    } catch (e) {
      console.error('Failed to clear:', e)
    }
  }, [activeSession.id])

  // Cancel ongoing request
  const cancel = useCallback(async () => {
    abortRef.current?.abort()
    setLoading(false)
    dispatch({ type: 'finalize_pending', result: 'error: cancelled' })
    try {
      await fetch(`/api/cancel?session_id=${encodeURIComponent(activeSession.id)}`, { method: 'POST' })
    } catch (e) {
      console.error('Failed to cancel:', e)
    }
  }, [activeSession.id])

  // Create new session
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
        setSessions((prev) => [data.session, ...prev.filter((s) => s.id !== data.session.id)])
      }
      await fetchSessions()
    } catch (e) {
      console.error('Failed to create session:', e)
    } finally {
      setSessionLoading(false)
    }
  }, [config, fetchSessions, sessionLoading])

  // Select existing session
  const selectSession = useCallback(
    async (sessionId) => {
      if (!sessionId || sessionId === activeSession.id) return
      initRef.current = true
      setSessionLoading(true)
      try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`)
        if (!res.ok) throw new Error('Failed to load session')
        const data = await res.json()
        if (data.session) {
          setActiveSession(data.session)
          // Transform provider format to UI format
          const uiMessages = transformMessages(data.messages || [])
          dispatch({ type: 'set_messages', messages: uiMessages })
        }
      } catch (e) {
        console.error('Failed to load session:', e)
      } finally {
        setSessionLoading(false)
      }
    },
    [activeSession.id]
  )

  // Delete session
  const deleteSession = useCallback(
    async (sessionId) => {
      if (!sessionId || sessions.length === 1 || sessionId === activeSession.id) return
      setSessionLoading(true)
      try {
        await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
        setSessions((prev) => prev.filter((s) => s.id !== sessionId))
      } catch (e) {
        console.error('Failed to delete session:', e)
      } finally {
        setSessionLoading(false)
      }
    },
    [activeSession.id, sessions.length]
  )

  // Initialize sessions on mount
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

  // Reset on cwd change
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
