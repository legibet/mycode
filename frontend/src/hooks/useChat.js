import { useMemo, useRef, useState } from 'react'

export function useChat(config) {
  const [messages, setMessages] = useState([])
  const [loading, setLoading] = useState(false)
  const [connectionState, setConnectionState] = useState('idle')
  const abortRef = useRef(null)

  const status = useMemo(() => {
    if (loading) return 'generating'
    if (connectionState === 'error') return 'offline'
    if (connectionState === 'ready') return 'ready'
    return 'idle'
  }, [loading, connectionState])

  const applyEvent = (event) => {
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
      } else if (event.type === 'error') {
        const errorText = event.message || event.error || 'Unknown error'
        parts.push({ type: 'text', content: `\n\n**Error:** ${errorText}` })
      }

      next[next.length - 1] = { ...last, parts }
      return next
    })
  }

  const send = async (input) => {
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
    }
  }

  const clear = async () => {
    try {
      await fetch('/api/clear', { method: 'POST' })
      setMessages([])
    } catch (e) {
      console.error('Failed to clear:', e)
    }
  }

  const cancel = () => {
    abortRef.current?.abort()
    setLoading(false)
  }

  return {
    messages,
    loading,
    status,
    send,
    clear,
    cancel,
  }
}
