/**
 * Message format utilities.
 * Transforms between provider format (OpenAI-style) and UI format.
 */

/**
 * Parse tool arguments from JSON string.
 */
function parseToolArgs(raw) {
  if (!raw) return {}
  try {
    return JSON.parse(raw)
  } catch {
    return {}
  }
}

/**
 * Transform provider messages to UI format.
 *
 * Provider format (OpenAI-style):
 *   { role: 'user', content: '...' }
 *   { role: 'assistant', content: '...', tool_calls: [...] }
 *   { role: 'tool', tool_call_id: '...', content: '...' }
 *
 * UI format:
 *   { role: 'user', parts: [{ type: 'text', content: '...' }] }
 *   { role: 'assistant', parts: [{ type: 'text', content: '...' }, { type: 'tool', ... }] }
 */
export function transformMessages(messages) {
  if (!Array.isArray(messages)) return []

  const result = []
  let currentAssistant = null
  const toolIndex = {} // tool_call_id -> part index in current assistant

  for (const msg of messages) {
    const role = msg.role

    if (role === 'system') continue

    if (role === 'user') {
      result.push({
        role: 'user',
        parts: [{ type: 'text', content: msg.content || '' }],
      })
      currentAssistant = null
      continue
    }

    if (role === 'assistant') {
      currentAssistant = { role: 'assistant', parts: [] }
      result.push(currentAssistant)

      // Add text content if present
      if (msg.content) {
        currentAssistant.parts.push({ type: 'text', content: msg.content })
      }

      // Add tool calls
      const toolCalls = msg.tool_calls || []
      for (const tc of toolCalls) {
        const toolId = tc.id
        const fn = tc.function || {}
        const part = {
          type: 'tool',
          id: toolId,
          name: fn.name || 'unknown',
          args: parseToolArgs(fn.arguments),
          result: '',
          pending: false,
        }
        toolIndex[toolId] = currentAssistant.parts.length
        currentAssistant.parts.push(part)
      }
      continue
    }

    if (role === 'tool') {
      // Ensure we have an assistant message to attach to
      if (!currentAssistant) {
        currentAssistant = { role: 'assistant', parts: [] }
        result.push(currentAssistant)
      }

      const toolCallId = msg.tool_call_id
      const content = msg.content || ''

      if (toolCallId && toolIndex[toolCallId] !== undefined) {
        // Update existing tool part with result
        const partIndex = toolIndex[toolCallId]
        currentAssistant.parts[partIndex] = {
          ...currentAssistant.parts[partIndex],
          result: content,
        }
      } else {
        // Orphan tool result - create a placeholder
        currentAssistant.parts.push({
          type: 'tool',
          id: toolCallId,
          name: 'tool',
          args: {},
          result: content,
          pending: false,
        })
      }
    }
  }

  return result
}

/**
 * Build a tool index from UI messages for quick lookup.
 */
export function buildToolIndex(messages) {
  const index = {}
  for (let msgIndex = 0; msgIndex < messages.length; msgIndex++) {
    const msg = messages[msgIndex]
    if (msg?.role !== 'assistant') continue
    const parts = msg.parts || []
    for (let partIndex = 0; partIndex < parts.length; partIndex++) {
      const part = parts[partIndex]
      if (part?.type === 'tool' && part.id) {
        index[part.id] = { messageIndex: msgIndex, partIndex }
      }
    }
  }
  return index
}
