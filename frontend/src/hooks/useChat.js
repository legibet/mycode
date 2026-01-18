import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

const EMPTY_SESSION = { id: 'default', title: 'New chat' }

export function useChat(config) {
  const [messages, setMessages] = useState([])
  const [sessions, setSessions] = useState([])
  const [activeSession, setActiveSession] = useState(EMPTY_SESSION)
  const [loading, setLoading] = useState(false)
  const [sessionLoading, setSessionLoading] = useState(false)
  const [connectionState, setConnectionState] = useState('idle')
  const abortRef = useRef(null)
  const initRef = useRef(false)
  const cwdRef = useRef(config.cwd)

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
    setMessages((prev) => {
      const next = [...prev]
      const last = next[next.length - 1]
      if (!last || last.role !== 'assistant') return prev
      const parts = [...last.parts]

      if (event.type === 'text') {
        const lastPart = parts[parts.length - 1]
        if (lastPart?.type === 'text') {
          parts[parts.length - 1] = { ...lastPart, content: lastPart.content + event.content }
        } else {
          parts.push({ type: 'text', content: event.content })
        }
      } else if (event.type === 'tool_start') {
        parts.push({
          type: 'tool',
          id: event.id,
          name: event.name,
          args: event.args,
          result: '',
          pending: true,
        })
      } else if (event.type === 'tool_done') {
        const index = parts.findIndex((part) => part.type === 'tool' && part.id === event.id)
        if (index !== -1) {
          parts[index] = { ...parts[index], result: event.result, pending: false }
        }
      } else if (event.type === 'tool_output') {
        const index = parts.findIndex((part) => part.type === 'tool' && part.id === event.id)
        if (index !== -1) {
          const output = typeof event.content === 'string' ? event.content : ''
          if (output) {
            const prevResult = typeof parts[index].result === 'string' ? parts[index].result : ''
            const nextResult = prevResult ? `${prevResult}\n${output}` : output
            parts[index] = { ...parts[index], result: nextResult, pending: true }
          }
        }
      } else if (event.type === 'error') {
        const errorText = event.message || event.error || 'Unknown error'
        parts.push({ type: 'text', content: `\n\n**Error:** ${errorText}` })
      }

      next[next.length - 1] = { ...last, parts }
      return next
    })
  }, [])

  const send = useCallback(
    async (input) => {
      if (!input.trim() || loading) return

      setLoading(true)
      setConnectionState('ready')

      setMessages((prev) => [...prev, { role: 'user', parts: [{ type: 'text', content: input }] }])
      setMessages((prev) => [...prev, { role: 'assistant', parts: [] }])

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
        if (e.name !== 'AbortError') {
          setConnectionState('error')
          applyEvent({ type: 'error', message: e.message })
        }
      } finally {
        setLoading(false)
        fetchSessions()
      }
    },
    [activeSession.id, applyEvent, config.apiBase, config.apiKey, config.cwd, config.model, fetchSessions, loading]
  )

  const clear = useCallback(async () => {
    try {
      await fetch(`/api/clear?session_id=${encodeURIComponent(activeSession.id)}`, { method: 'POST' })
      setMessages([])
    } catch (e) {
      console.error('Failed to clear:', e)
    }
  }, [activeSession.id])

  const cancel = useCallback(async () => {
    abortRef.current?.abort()
    setLoading(false)
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
        setMessages(data.messages || [])
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
          setMessages(data.messages || [])
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
    setMessages([])
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
