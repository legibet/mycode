import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'

const EMPTY_SESSION = { id: 'default', title: 'New chat' }
const INITIAL_CHAT_STATE = { messages: [], toolIndex: {} }

function chatReducer(state, action) {
  switch (action.type) {
    case 'set_messages': {
      const messages = Array.isArray(action.messages) ? action.messages : []
      const toolIndex = {}
      for (let msgIndex = 0; msgIndex < messages.length; msgIndex += 1) {
        const msg = messages[msgIndex]
        if (msg?.role !== 'assistant') continue
        const parts = msg.parts || []
        for (let partIndex = 0; partIndex < parts.length; partIndex += 1) {
          const part = parts[partIndex]
          if (part?.type === 'tool' && part.id) {
            toolIndex[part.id] = { messageIndex: msgIndex, partIndex }
          }
        }
      }
      return { messages, toolIndex }
    }
    case 'append_user': {
      const nextMessages = [...state.messages, { role: 'user', parts: [{ type: 'text', content: action.content }] }]
      return { ...state, messages: nextMessages }
    }
    case 'start_assistant': {
      const nextMessages = [...state.messages, { role: 'assistant', parts: [] }]
      return { ...state, messages: nextMessages }
    }
    case 'apply_event': {
      const event = action.event || {}
      const messages = [...state.messages]
      const toolIndex = { ...state.toolIndex }

      // Events always attach to the latest assistant message.
      let assistantIndex = messages.length - 1
      if (assistantIndex < 0 || messages[assistantIndex].role !== 'assistant') {
        messages.push({ role: 'assistant', parts: [] })
        assistantIndex = messages.length - 1
      }

      const assistant = {
        ...messages[assistantIndex],
        parts: [...(messages[assistantIndex].parts || [])],
      }
      messages[assistantIndex] = assistant

      // Locate tool part by id (fallback scan when index is missing).
      const ensureToolEntry = (toolId) => {
        if (!toolId) return null
        if (toolIndex[toolId]) return toolIndex[toolId]
        for (let m = messages.length - 1; m >= 0; m -= 1) {
          const msg = messages[m]
          if (msg.role !== 'assistant') continue
          const parts = msg.parts || []
          for (let p = parts.length - 1; p >= 0; p -= 1) {
            const part = parts[p]
            if (part?.type === 'tool' && part.id === toolId) {
              toolIndex[toolId] = { messageIndex: m, partIndex: p }
              return toolIndex[toolId]
            }
          }
        }
        return null
      }

      if (event.type === 'text') {
        const lastPart = assistant.parts[assistant.parts.length - 1]
        if (lastPart?.type === 'text') {
          assistant.parts[assistant.parts.length - 1] = {
            ...lastPart,
            content: lastPart.content + event.content,
          }
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
        if (event.id) {
          toolIndex[event.id] = { messageIndex: assistantIndex, partIndex }
        }
      } else if (event.type === 'tool_output') {
        const entry = ensureToolEntry(event.id)
        if (entry) {
          const targetMsg = messages[entry.messageIndex]
          const targetParts = [...(targetMsg.parts || [])]
          const targetPart = { ...targetParts[entry.partIndex] }
          const output = typeof event.content === 'string' ? event.content : ''
          if (output) {
            const prevResult = typeof targetPart.result === 'string' ? targetPart.result : ''
            targetPart.result = prevResult ? `${prevResult}\n${output}` : output
          }
          targetPart.pending = true
          targetParts[entry.partIndex] = targetPart
          messages[entry.messageIndex] = { ...targetMsg, parts: targetParts }
        }
      } else if (event.type === 'tool_done') {
        const entry = ensureToolEntry(event.id)
        if (entry) {
          const targetMsg = messages[entry.messageIndex]
          const targetParts = [...(targetMsg.parts || [])]
          targetParts[entry.partIndex] = {
            ...targetParts[entry.partIndex],
            result: event.result,
            pending: false,
          }
          messages[entry.messageIndex] = { ...targetMsg, parts: targetParts }
        }
      } else if (event.type === 'error') {
        const errorText = event.message || event.error || 'Unknown error'
        assistant.parts.push({ type: 'text', content: `\n\n**Error:** ${errorText}` })
      }

      return { messages, toolIndex }
    }
    case 'finalize_pending': {
      const result = action.result
      let changed = false
      const messages = state.messages.map((message) => {
        if (message.role !== 'assistant') return message
        let messageChanged = false
        const parts = (message.parts || []).map((part) => {
          if (part?.type !== 'tool' || !part.pending) return part
          changed = true
          messageChanged = true
          if (result && (!part.result || part.result === '')) {
            return { ...part, pending: false, result }
          }
          return { ...part, pending: false }
        })
        return messageChanged ? { ...message, parts } : message
      })
      return changed ? { ...state, messages } : state
    }
    default:
      return state
  }
}

export function useChat(config) {
  const [chatState, dispatch] = useReducer(chatReducer, INITIAL_CHAT_STATE)
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

  const fetchSessions = useCallback(async () => {
    try {
      const response = await fetch(`/api/sessions?cwd=${encodeURIComponent(config.cwd)}`)
      if (!response.ok) throw new Error('Failed to load sessions')
      const data = await response.json()
      setSessions(data.sessions || [])
      return data.sessions || []
    } catch (e) {
      console.error('Failed to load sessions:', e)
      return []
    }
  }, [config.cwd])

  const applyEvent = useCallback((event) => {
    dispatch({ type: 'apply_event', event })
  }, [])

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
        const response = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: activeSession.id,
            message: input,
            model: config.model,
            cwd: config.cwd,
            api_key: config.apiKey || undefined,
            api_base: config.apiBase || undefined,
          }),
          signal: abortRef.current.signal,
        })

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}: ${response.statusText}`)
        }

        const reader = response.body.getReader()
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
              const event = JSON.parse(data)
              applyEvent(event)
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
          applyEvent({ type: 'error', message: e.message })
          dispatch({ type: 'finalize_pending', result: `error: ${e.message}` })
        }
      } finally {
        if (aborted) {
          dispatch({ type: 'finalize_pending', result: 'error: cancelled' })
        } else {
          dispatch({ type: 'finalize_pending', result: 'error: stream ended' })
        }
        setLoading(false)
        fetchSessions()
      }
    },
    [activeSession.id, applyEvent, config.apiBase, config.apiKey, config.cwd, config.model, fetchSessions, loading]
  )

  const clear = useCallback(async () => {
    try {
      await fetch(`/api/clear?session_id=${encodeURIComponent(activeSession.id)}`, { method: 'POST' })
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
      await fetch(`/api/cancel?session_id=${encodeURIComponent(activeSession.id)}`, { method: 'POST' })
    } catch (e) {
      console.error('Failed to cancel:', e)
    }
  }, [activeSession.id])

  const createSession = useCallback(async () => {
    if (sessionLoading) return
    initRef.current = true
    setSessionLoading(true)
    try {
      const response = await fetch('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: config.model,
          cwd: config.cwd,
          api_base: config.apiBase || undefined,
        }),
      })
      if (!response.ok) throw new Error('Failed to create session')
      const data = await response.json()
      if (data.session) {
        setActiveSession(data.session)
        dispatch({ type: 'set_messages', messages: data.messages || [] })
        setSessions((prev) => [data.session, ...prev.filter((item) => item.id !== data.session.id)])
      }
      await fetchSessions()
    } catch (e) {
      console.error('Failed to create session:', e)
    } finally {
      setSessionLoading(false)
    }
  }, [config.apiBase, config.cwd, config.model, fetchSessions, sessionLoading])

  const selectSession = useCallback(
    async (sessionId) => {
      if (!sessionId || sessionId === activeSession.id) return
      initRef.current = true
      setSessionLoading(true)
      try {
        const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`)
        if (!response.ok) throw new Error('Failed to load session')
        const data = await response.json()
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
    [activeSession.id]
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
      if (list.some((session) => session.id === activeSession.id)) return
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

  const deleteSession = useCallback(
    async (sessionId) => {
      if (!sessionId) return
      if (sessions.length === 1 || sessionId === activeSession.id) return
      setSessionLoading(true)
      try {
        await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
        const nextSessions = sessions.filter((item) => item.id !== sessionId)
        setSessions(nextSessions)
      } catch (e) {
        console.error('Failed to delete session:', e)
      } finally {
        setSessionLoading(false)
      }
    },
    [activeSession.id, sessions]
  )

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
