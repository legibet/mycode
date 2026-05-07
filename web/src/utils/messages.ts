/** Message block helpers and the render-message projection. */

import type {
  AttachedFile,
  ChatMessage,
  CompactMarkerMessage,
  DocumentBlock,
  MessageBlock,
  MessageMeta,
  RenderMessage,
  TextBlock,
  ThinkingBlock,
  ToolInput,
  ToolResultBlock,
  ToolRuntime,
  ToolUseBlock,
} from '../types'
import { isCompactMarker } from '../types'

interface ToolCall {
  id?: string
  name?: string
  input?: ToolInput
}

interface ToolIndexEntry {
  messageIndex: number
  blockIndex: number
}

// Shared frozen ref so memo equality holds across tool_use blocks.
const EMPTY_TOOL_INPUT: ToolInput = Object.freeze({}) as ToolInput

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function getBlocks(message?: ChatMessage | null): MessageBlock[] {
  return Array.isArray(message?.content) ? message.content : []
}

function cloneBlock(
  block: MessageBlock,
  renderKey: string | null = null,
): MessageBlock {
  const next = { ...block }
  if (next.meta) next.meta = { ...next.meta }
  if (renderKey) next.renderKey = renderKey
  return next
}

function createMessage(
  role: ChatMessage['role'],
  content: MessageBlock[] = [],
): ChatMessage {
  return { role, content }
}

function createTextBlock(text: string): TextBlock {
  return { type: 'text', text }
}

function escapeHtmlAttribute(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
}

function createAttachedTextBlock(text: string, name: string): TextBlock {
  return {
    type: 'text',
    text: `<file name="${escapeHtmlAttribute(name)}">\n${text}\n</file>`,
    meta: { attachment: true, path: name },
  }
}

function createThinkingBlock(text: string): ThinkingBlock {
  return { type: 'thinking', text }
}

function createToolUseBlock(toolCall: ToolCall): ToolUseBlock {
  return {
    type: 'tool_use',
    id: toolCall?.id || '',
    name: toolCall?.name || 'tool',
    input: isObject(toolCall?.input) ? toolCall.input : EMPTY_TOOL_INPUT,
  }
}

function createToolResultBlock(
  toolUseId: string,
  output: string | null,
  metadata: Record<string, unknown> | null,
  isError = false,
): ToolResultBlock {
  return {
    type: 'tool_result',
    tool_use_id: toolUseId,
    output,
    metadata,
    is_error: isError,
  }
}

function createImageBlock(
  data: string,
  mimeType: string,
  name?: string,
): MessageBlock {
  const block: MessageBlock = { type: 'image', data, mime_type: mimeType }
  if (name) block.name = name
  return block
}

function createDocumentBlock(
  data: string,
  mimeType: string,
  name?: string,
): DocumentBlock {
  const block: DocumentBlock = { type: 'document', data, mime_type: mimeType }
  if (name) block.name = name
  return block
}

function createAttachmentBlock(attachment: AttachedFile): MessageBlock {
  if (attachment.kind === 'image') {
    return createImageBlock(
      attachment.data,
      attachment.mime_type,
      attachment.name,
    )
  }
  if (attachment.kind === 'document') {
    return createDocumentBlock(
      attachment.data,
      attachment.mime_type,
      attachment.name,
    )
  }
  return createAttachedTextBlock(attachment.text, attachment.name)
}

export function createUserTextMessage(text: string): ChatMessage {
  return createMessage('user', text ? [createTextBlock(text)] : [])
}

export function createUserMessage(
  text: string,
  attachments: AttachedFile[],
): ChatMessage {
  const blocks: MessageBlock[] = []
  if (text) blocks.push(createTextBlock(text))
  for (const attachment of attachments) {
    blocks.push(createAttachmentBlock(attachment))
  }
  return createMessage('user', blocks)
}

export function createAssistantMessage(
  content: MessageBlock[] = [],
): ChatMessage {
  return createMessage('assistant', content)
}

function ensureTailAssistant(messages: ChatMessage[]): {
  messages: ChatMessage[]
  index: number
} {
  const next = [...messages]
  const lastIndex = next.length - 1
  if (lastIndex >= 0 && next[lastIndex]?.role === 'assistant') {
    return { messages: next, index: lastIndex }
  }
  next.push(createAssistantMessage([]))
  return { messages: next, index: next.length - 1 }
}

export function appendAssistantDelta(
  messages: ChatMessage[],
  blockType: 'thinking' | 'text',
  delta: string,
): ChatMessage[] {
  if (!delta) return messages

  const { messages: next, index } = ensureTailAssistant(messages)
  const assistant = next[index] ?? createAssistantMessage([])
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
  // Tail-aware: a backward scan would attach the new tool to the previous
  // turn's assistant when the tail is a tool-result user message or compact.
  const { messages: next, index } = ensureTailAssistant(messages)
  const assistant = next[index]
  if (!assistant) return next
  next[index] = {
    ...assistant,
    content: [...getBlocks(assistant), createToolUseBlock(toolCall)],
  }
  return next
}

function isToolResultOnlyUserMessage(message?: ChatMessage): boolean {
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
  output: string | null,
  metadata: Record<string, unknown> | null,
  isError = false,
): ChatMessage[] {
  const block = createToolResultBlock(toolUseId, output, metadata, isError)
  const next = [...messages]
  const lastIndex = next.length - 1

  if (lastIndex >= 0 && isToolResultOnlyUserMessage(next[lastIndex])) {
    const lastMessage = next[lastIndex]
    if (!lastMessage) return next
    next[lastIndex] = {
      ...lastMessage,
      content: [...getBlocks(lastMessage), block],
    }
    return next
  }

  next.push(createMessage('user', [block]))
  return next
}

export function updateLatestThinkingDuration(
  messages: ChatMessage[],
  durationMs: number,
): ChatMessage[] {
  for (
    let messageIndex = messages.length - 1;
    messageIndex >= 0;
    messageIndex--
  ) {
    const message = messages[messageIndex]
    if (message?.role !== 'assistant') continue

    const content = getBlocks(message)
    for (let blockIndex = content.length - 1; blockIndex >= 0; blockIndex--) {
      const block = content[blockIndex]
      if (block?.type !== 'thinking') continue

      const next = [...messages]
      const nextContent = [...content]
      nextContent[blockIndex] = {
        ...block,
        meta: {
          ...(isObject(block.meta) ? block.meta : {}),
          duration_ms: durationMs,
        },
      }
      next[messageIndex] = { ...message, content: nextContent }
      return next
    }
  }

  return messages
}

export function updateLatestAssistantMeta(
  messages: ChatMessage[],
  patch: Partial<MessageMeta>,
): ChatMessage[] {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i]
    if (message?.role !== 'assistant') continue
    const next = [...messages]
    next[i] = { ...message, meta: { ...(message.meta ?? {}), ...patch } }
    return next
  }
  return messages
}

function buildToolRuntime(
  runtime: ToolRuntime | undefined,
  toolResultBlock: ToolResultBlock | null,
): ToolRuntime {
  const output = typeof runtime?.output === 'string' ? runtime.output : ''
  const runtimeFinal =
    typeof runtime?.finalOutput === 'string' ? runtime.finalOutput : null
  const persistedOutput =
    typeof toolResultBlock?.output === 'string' ? toolResultBlock.output : null
  const finalOutput = runtimeFinal ?? persistedOutput
  const runtimeMetadata = isObject(runtime?.metadata) ? runtime.metadata : null
  const persistedMetadata = isObject(toolResultBlock?.metadata)
    ? toolResultBlock.metadata
    : null
  const metadata = runtimeMetadata ?? persistedMetadata
  const isError = Boolean(
    runtime?.isError ||
      toolResultBlock?.is_error ||
      (typeof finalOutput === 'string' && finalOutput.startsWith('error:')),
  )

  return {
    pending: Boolean(runtime?.pending),
    output,
    finalOutput,
    metadata,
    isError,
  }
}

function createCompactMarker(sourceIndex: number): CompactMarkerMessage {
  return {
    kind: 'compact-marker',
    sourceIndex,
    renderKey: `compact:${sourceIndex}`,
  }
}

function createRenderAssistantMessage(sourceIndex: number): ChatMessage {
  return {
    role: 'assistant',
    content: [],
    renderKey: `assistant:${sourceIndex}`,
    sourceIndex,
  }
}

/**
 * Project rawMessages + toolRuntimeById into the shape the UI consumes.
 */
export function buildRenderMessages(
  messages: ChatMessage[],
  toolRuntimeById: Record<string, ToolRuntime> = {},
): RenderMessage[] {
  if (!Array.isArray(messages)) return []

  const result: RenderMessage[] = []
  const toolIndex: Record<string, ToolIndexEntry> = {}
  let currentAssistant: ChatMessage | null = null

  const ensureAssistantRenderMessage = (sourceIndex: number) => {
    if (currentAssistant) return currentAssistant
    currentAssistant = createRenderAssistantMessage(sourceIndex)
    result.push(currentAssistant)
    return currentAssistant
  }

  for (const [sourceIndex, message] of messages.entries()) {
    const role = message?.role
    const blocks = getBlocks(message)

    if (role === 'compact') {
      result.push(createCompactMarker(sourceIndex))
      currentAssistant = null
      continue
    }

    if (role === 'user') {
      const userBlocks: MessageBlock[] = []
      const toolResults: ToolResultBlock[] = []

      for (const [blockIndex, block] of blocks.entries()) {
        if (
          (block?.type === 'text' && block.text) ||
          block?.type === 'image' ||
          block?.type === 'document'
        ) {
          userBlocks.push(
            cloneBlock(block, `user:${sourceIndex}:${blockIndex}`),
          )
        } else if (block?.type === 'tool_result') {
          toolResults.push(block)
        }
      }

      if (userBlocks.length > 0) {
        const userMsg: ChatMessage = {
          role: 'user',
          content: userBlocks,
          renderKey: `user:${sourceIndex}`,
          sourceIndex,
        }
        if (isObject(message?.meta))
          userMsg.meta = { ...(message.meta as MessageMeta) }
        result.push(userMsg)
        currentAssistant = null
      }

      if (toolResults.length === 0) continue

      const assistantMessage = ensureAssistantRenderMessage(sourceIndex)
      let assistantContent = [...getBlocks(assistantMessage)]

      for (const block of toolResults) {
        const toolUseId = block.tool_use_id
        const runtime = toolUseId ? toolRuntimeById[toolUseId] : undefined
        const entry = toolUseId ? toolIndex[toolUseId] : undefined

        if (entry) {
          // Tool result for a tool_use we already projected — splice the
          // runtime/result back onto that tool_use block.
          const target = result[entry.messageIndex]
          if (target && !isCompactMarker(target)) {
            const targetContent = [...getBlocks(target)]
            const targetBlock = targetContent[entry.blockIndex]
            if (targetBlock?.type === 'tool_use') {
              targetContent[entry.blockIndex] = {
                ...targetBlock,
                runtime: buildToolRuntime(runtime, block),
              }
              const updatedMessage = { ...target, content: targetContent }
              result[entry.messageIndex] = updatedMessage
              if (entry.messageIndex === result.length - 1) {
                currentAssistant = updatedMessage
                assistantContent = targetContent
              }
            }
          }
          continue
        }

        // Orphan tool_result (no matching tool_use seen) — surface it as a
        // synthetic tool_use block on the current assistant so the UI still
        // shows it instead of silently dropping.
        const nextBlock: ToolUseBlock = {
          type: 'tool_use',
          id: toolUseId || '',
          name: 'tool',
          input: EMPTY_TOOL_INPUT,
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

    const assistantMessage = ensureAssistantRenderMessage(sourceIndex)
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

      const renderBlock: ToolUseBlock = {
        type: 'tool_use',
        id: block.id,
        name: block.name,
        input: isObject(block.input) ? block.input : EMPTY_TOOL_INPUT,
        runtime: buildToolRuntime(
          block.id ? toolRuntimeById[block.id] : undefined,
          null,
        ),
      }
      renderBlock.renderKey =
        block.id || `assistant:${sourceIndex}:${sourceBlockIndex}`
      if (isObject(block.meta)) {
        renderBlock.meta = { ...block.meta }
      }
      const blockIndex = assistantContent.length
      assistantContent.push(renderBlock)

      if (block.id) {
        toolIndex[block.id] = { messageIndex, blockIndex }
      }
    }

    // Tool loops collapse multiple raw assistants into one render message;
    // overwrite (or clear) meta from the latest raw call so the bubble never
    // shows stale per-turn fields from earlier iterations.
    const merged: ChatMessage = {
      ...assistantMessage,
      content: assistantContent,
    }
    const rawMeta = message?.meta as MessageMeta | undefined
    if (rawMeta) {
      merged.meta = { ...rawMeta }
    } else {
      delete merged.meta
    }
    currentAssistant = merged
    result[messageIndex] = merged
  }

  return result.filter((message, index) => {
    if (isCompactMarker(message)) return true
    return (
      (Array.isArray(message.content) && message.content.length > 0) ||
      (index === result.length - 1 && message.role === 'assistant')
    )
  })
}
