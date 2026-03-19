/**
 * Message format utilities.
 * Transforms native Messages API history into UI message parts.
 */

export function transformMessages(messages) {
  if (!Array.isArray(messages)) return []

  const result = []
  let currentAssistant = null
  const toolIndex = {}

  for (const message of messages) {
    const role = message?.role
    const blocks = Array.isArray(message?.content) ? message.content : []

    if (role === 'user') {
      const textParts = blocks
        .filter((block) => block?.type === 'text' && block.text)
        .map((block) => ({ type: 'text', content: block.text }))

      if (textParts.length > 0) {
        result.push({ role: 'user', parts: textParts })
        currentAssistant = null
      }

      const toolResults = blocks.filter(
        (block) => block?.type === 'tool_result',
      )
      if (toolResults.length === 0) continue

      if (!currentAssistant) {
        currentAssistant = { role: 'assistant', parts: [] }
        result.push(currentAssistant)
      }

      for (const block of toolResults) {
        const toolUseId = block.tool_use_id
        const resultText = block.content || ''
        const partIndex = toolUseId ? toolIndex[toolUseId] : undefined

        if (partIndex === undefined) {
          currentAssistant.parts.push({
            type: 'tool',
            id: toolUseId,
            name: 'tool',
            args: {},
            result: resultText,
            pending: false,
          })
          continue
        }

        currentAssistant.parts[partIndex] = {
          ...currentAssistant.parts[partIndex],
          result: resultText,
          pending: false,
        }
      }

      continue
    }

    if (role !== 'assistant') continue

    // Merge consecutive assistant turns (no user text in between)
    if (!currentAssistant) {
      currentAssistant = { role: 'assistant', parts: [] }
      result.push(currentAssistant)
    }

    for (const block of blocks) {
      if (block?.type === 'thinking' && block.text) {
        currentAssistant.parts.push({ type: 'reasoning', content: block.text })
        continue
      }

      if (block?.type === 'text' && block.text) {
        currentAssistant.parts.push({ type: 'text', content: block.text })
        continue
      }

      if (block?.type !== 'tool_use') continue

      const partIndex = currentAssistant.parts.length
      currentAssistant.parts.push({
        type: 'tool',
        id: block.id,
        name: block.name || 'tool',
        args: typeof block.input === 'object' && block.input ? block.input : {},
        result: '',
        pending: false,
      })

      if (block.id) {
        toolIndex[block.id] = partIndex
      }
    }
  }

  return result.filter(
    (message) => Array.isArray(message.parts) && message.parts.length > 0,
  )
}

export function buildToolIndex(messages) {
  const index = {}

  for (let messageIndex = 0; messageIndex < messages.length; messageIndex++) {
    const message = messages[messageIndex]
    if (message?.role !== 'assistant') continue

    const parts = message.parts || []
    for (let partIndex = 0; partIndex < parts.length; partIndex++) {
      const part = parts[partIndex]
      if (part?.type === 'tool' && part.id) {
        index[part.id] = { messageIndex, partIndex }
      }
    }
  }

  return index
}
