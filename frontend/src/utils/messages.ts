/**
 * Canonical message helpers shared by history hydration and live streaming.
 * Frontend state stays close to the backend block-based conversation model.
 */

import type {
  ChatMessage,
  MessageBlock,
  MessageMeta,
  TextBlock,
  ThinkingBlock,
  ToolInput,
  ToolResultBlock,
  ToolRuntime,
  ToolUseBlock,
} from '../types'

interface ToolCall {
  id?: string
  name?: string
  input?: ToolInput
}

interface ToolIndexEntry {
  messageIndex: number
  blockIndex: number
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function getBlocks(message?: ChatMessage | null): MessageBlock[] {
  return Array.isArray(message?.content) ? message.content : []
}

function cloneBlock(block: MessageBlock, renderKey: string | null = null) {
  const next = { ...block }
  if (isObject(block?.meta)) next.meta = { ...block.meta }
  if ('input' in next && isObject(next.input)) next.input = { ...next.input }
  if (renderKey) next.renderKey = renderKey
  return next
}

function createMessage(
  role: ChatMessage['role'],
  content: MessageBlock[] = [],
  renderKey: string | null = null,
): ChatMessage {
  const message: ChatMessage = { role, content }
  if (renderKey) message.renderKey = renderKey
  return message
}

function createTextBlock(text: string): TextBlock {
  return { type: 'text', text }
}

function createThinkingBlock(text: string): ThinkingBlock {
  return { type: 'thinking', text }
}

function createToolUseBlock(toolCall: ToolCall): ToolUseBlock {
  return {
    type: 'tool_use',
    id: toolCall?.id || '',
    name: toolCall?.name || 'tool',
    input: isObject(toolCall?.input) ? { ...toolCall.input } : {},
  }
}

function createToolResultBlock(
  toolUseId: string,
  modelText: string | null,
  displayText: string | null,
  isError = false,
): ToolResultBlock {
  return {
    type: 'tool_result',
    tool_use_id: toolUseId,
    model_text: modelText,
    display_text: displayText,
    is_error: isError,
  }
}

export function createUserTextMessage(text: string): ChatMessage {
  return createMessage('user', text ? [createTextBlock(text)] : [])
}

export function createAssistantMessage(
  content: MessageBlock[] = [],
): ChatMessage {
  return createMessage('assistant', content)
}

function ensureTailAssistant(messages) {
  const next = [...messages]
  const lastIndex = next.length - 1
  if (lastIndex >= 0 && next[lastIndex]?.role === 'assistant') {
    return { messages: next, index: lastIndex }
  }

  next.push(createAssistantMessage([]))
  return { messages: next, index: next.length - 1 }
}

function findLatestAssistantIndex(messages) {
  for (let index = messages.length - 1; index >= 0; index--) {
    if (messages[index]?.role === 'assistant') return index
  }
  return -1
}

export function appendAssistantDelta(
  messages: ChatMessage[],
  blockType: 'thinking' | 'text',
  delta: string,
): ChatMessage[] {
  if (!delta) return messages

  const { messages: next, index } = ensureTailAssistant(messages)
  const assistant = next[index]
  const content = [...getBlocks(assistant)]
  const lastBlock = content[content.length - 1]

  if (lastBlock?.type === blockType) {
    content[content.length - 1] = {
      ...lastBlock,
      text: `${lastBlock.text || ''}${delta}`,
    }
  } else {
    content.push(
      blockType === 'thinking'
        ? createThinkingBlock(delta)
        : createTextBlock(delta),
    )
  }

  next[index] = { ...assistant, content }
  return next
}

export function appendToolUse(
  messages: ChatMessage[],
  toolCall: ToolCall,
): ChatMessage[] {
  const next = [...messages]
  let index = findLatestAssistantIndex(next)

  if (index === -1) {
    next.push(createAssistantMessage([]))
    index = next.length - 1
  }

  const assistant = next[index]
  next[index] = {
    ...assistant,
    content: [...getBlocks(assistant), createToolUseBlock(toolCall)],
  }
  return next
}

function isToolResultOnlyUserMessage(message?: ChatMessage) {
  const blocks = getBlocks(message)
  return (
    message?.role === 'user' &&
    blocks.length > 0 &&
    blocks.every((block) => block?.type === 'tool_result')
  )
}

export function appendToolResult(
  messages: ChatMessage[],
  toolUseId: string,
  modelText: string | null,
  displayText: string | null,
  isError = false,
): ChatMessage[] {
  const block = createToolResultBlock(
    toolUseId,
    modelText,
    displayText,
    isError,
  )
  const next = [...messages]
  const lastIndex = next.length - 1

  if (lastIndex >= 0 && isToolResultOnlyUserMessage(next[lastIndex])) {
    const lastMessage = next[lastIndex]
    next[lastIndex] = {
      ...lastMessage,
      content: [...getBlocks(lastMessage), block],
    }
    return next
  }

  next.push(createMessage('user', [block]))
  return next
}

function buildToolRuntime(
  runtime: ToolRuntime | undefined,
  toolResultBlock: ToolResultBlock | null,
): ToolRuntime {
  const output = typeof runtime?.output === 'string' ? runtime.output : ''
  const hasRuntimeModelText = typeof runtime?.modelText === 'string'
  const persistedModelText =
    typeof toolResultBlock?.model_text === 'string'
      ? toolResultBlock.model_text
      : null
  const hasRuntimeDisplayText = typeof runtime?.displayText === 'string'
  const persistedDisplayText =
    typeof toolResultBlock?.display_text === 'string'
      ? toolResultBlock.display_text
      : null
  const modelText = hasRuntimeModelText ? runtime.modelText : persistedModelText
  const displayText = hasRuntimeDisplayText
    ? runtime.displayText
    : (persistedDisplayText ?? modelText)
  const isError = Boolean(
    runtime?.isError ||
      toolResultBlock?.is_error ||
      (typeof modelText === 'string' && modelText.startsWith('error:')),
  )

  return {
    pending: Boolean(runtime?.pending),
    output,
    modelText,
    displayText,
    isError,
  }
}

function updateRenderToolMessage(
  result: ChatMessage[],
  entry: ToolIndexEntry,
  runtime: ToolRuntime | undefined,
  toolResultBlock: ToolResultBlock | null,
) {
  const targetMessage = result[entry.messageIndex]
  const content = [...getBlocks(targetMessage)]
  const targetBlock = content[entry.blockIndex]
  if (targetBlock?.type !== 'tool_use') {
    return targetMessage
  }

  content[entry.blockIndex] = {
    ...targetBlock,
    runtime: buildToolRuntime(runtime, toolResultBlock),
  }

  const updatedMessage = { ...targetMessage, content }
  result[entry.messageIndex] = updatedMessage
  return updatedMessage
}

/**
 * Derive renderable chat messages from canonical persisted messages plus
 * ephemeral tool runtime state.
 */
export function buildRenderMessages(
  messages: ChatMessage[],
  toolRuntimeById: Record<string, ToolRuntime> = {},
): ChatMessage[] {
  if (!Array.isArray(messages)) return []

  const result: ChatMessage[] = []
  const toolIndex: Record<string, ToolIndexEntry> = {}
  let currentAssistant: ChatMessage | null = null

  const ensureAssistantRenderMessage = (renderKey: string) => {
    if (currentAssistant) return currentAssistant
    currentAssistant = createMessage('assistant', [], renderKey)
    result.push(currentAssistant)
    return currentAssistant
  }

  for (const [sourceIndex, message] of messages.entries()) {
    const role = message?.role
    const blocks = getBlocks(message)

    if (role === 'user') {
      const textBlocks = blocks
        .filter((block) => block?.type === 'text' && block.text)
        .map((block, blockIndex) =>
          cloneBlock(block, `user:${sourceIndex}:${blockIndex}`),
        )

      if (textBlocks.length > 0) {
        const userMsg = createMessage('user', textBlocks, `user:${sourceIndex}`)
        if (isObject(message?.meta))
          userMsg.meta = { ...(message.meta as MessageMeta) }
        userMsg.sourceIndex = sourceIndex
        result.push(userMsg)
        currentAssistant = null
      }

      const toolResults = blocks.filter(
        (block) => block?.type === 'tool_result',
      )
      if (toolResults.length === 0) continue

      const assistantMessage = ensureAssistantRenderMessage(
        `assistant:${sourceIndex}`,
      )
      let assistantContent = [...getBlocks(assistantMessage)]

      for (const block of toolResults) {
        const toolUseId = block.tool_use_id
        const runtime = toolUseId ? toolRuntimeById[toolUseId] : undefined
        const entry = toolUseId ? toolIndex[toolUseId] : undefined

        if (entry) {
          const updatedMessage = updateRenderToolMessage(
            result,
            entry,
            runtime,
            block,
          )
          if (entry.messageIndex === result.length - 1) {
            currentAssistant = updatedMessage
            assistantContent = [...getBlocks(updatedMessage)]
          }
          continue
        }

        // Keep tool results visually attached to the assistant tool block even
        // though they are persisted as a separate user message.
        const nextBlock: ToolUseBlock = {
          type: 'tool_use',
          id: toolUseId || '',
          name: 'tool',
          input: {},
          runtime: buildToolRuntime(runtime, block),
        }
        const blockIndex = assistantContent.length
        nextBlock.renderKey =
          toolUseId || `tool-result:${sourceIndex}:${blockIndex}`
        assistantContent.push(nextBlock)
        currentAssistant = { ...assistantMessage, content: assistantContent }
        result[result.length - 1] = currentAssistant
        if (toolUseId) {
          toolIndex[toolUseId] = { messageIndex: result.length - 1, blockIndex }
        }
      }

      continue
    }

    if (role !== 'assistant') continue

    const assistantMessage = ensureAssistantRenderMessage(
      `assistant:${sourceIndex}`,
    )
    const assistantContent = [...getBlocks(assistantMessage)]
    const messageIndex = result.length - 1

    for (const [sourceBlockIndex, block] of blocks.entries()) {
      if (block?.type === 'thinking' && block.text) {
        assistantContent.push(
          cloneBlock(block, `assistant:${sourceIndex}:${sourceBlockIndex}`),
        )
        continue
      }

      if (block?.type === 'text' && block.text) {
        assistantContent.push(
          cloneBlock(block, `assistant:${sourceIndex}:${sourceBlockIndex}`),
        )
        continue
      }

      if (block?.type !== 'tool_use') continue

      const renderBlock = {
        ...cloneBlock(
          block,
          block.id || `assistant:${sourceIndex}:${sourceBlockIndex}`,
        ),
        input: isObject(block.input) ? { ...block.input } : {},
        runtime: buildToolRuntime(
          block.id ? toolRuntimeById[block.id] : undefined,
          null,
        ),
      }
      const blockIndex = assistantContent.length
      assistantContent.push(renderBlock)

      if (block.id) {
        toolIndex[block.id] = { messageIndex, blockIndex }
      }
    }

    currentAssistant = { ...assistantMessage, content: assistantContent }
    result[messageIndex] = currentAssistant
  }

  return result.filter(
    (message, index) =>
      (Array.isArray(message.content) && message.content.length > 0) ||
      (index === result.length - 1 && message.role === 'assistant'),
  )
}
