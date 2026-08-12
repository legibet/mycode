/** Message block helpers and the render-message projection. */

import type {
  AttachedFile,
  ChatMessage,
  CompactMarkerMessage,
  Cost,
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
  TurnStats,
  WorkspaceFileReference,
} from "../types";
import { isCompactMarker } from "../types";

interface ToolCall {
  id?: string;
  name?: string;
  input?: ToolInput;
}

interface ToolIndexEntry {
  messageIndex: number;
  blockIndex: number;
}

// Shared frozen ref so memo equality holds across tool_use blocks.
const EMPTY_TOOL_INPUT: ToolInput = Object.freeze({}) as ToolInput;

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function getBlocks(message?: ChatMessage | null): MessageBlock[] {
  return Array.isArray(message?.content) ? message.content : [];
}

function cloneBlock(
  block: MessageBlock,
  renderKey: string | null = null,
): MessageBlock {
  const next = { ...block };
  if (next.meta) next.meta = { ...next.meta };
  if (renderKey) next.renderKey = renderKey;
  return next;
}

function createMessage(
  role: ChatMessage["role"],
  content: MessageBlock[] = [],
): ChatMessage {
  return { role, content };
}

function createTextBlock(text: string): TextBlock {
  return { type: "text", text };
}

function escapeHtmlAttribute(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function createAttachedTextBlock(text: string, name: string): TextBlock {
  return {
    type: "text",
    text: `<file name="${escapeHtmlAttribute(name)}">\n${text}\n</file>`,
    meta: { attachment: true, path: name },
  };
}

function createThinkingBlock(text: string): ThinkingBlock {
  return { type: "thinking", text };
}

function createToolUseBlock(toolCall: ToolCall): ToolUseBlock {
  return {
    type: "tool_use",
    id: toolCall?.id || "",
    name: toolCall?.name || "tool",
    input: isObject(toolCall?.input) ? toolCall.input : EMPTY_TOOL_INPUT,
  };
}

function createToolResultBlock(
  toolUseId: string,
  output: string | null,
  metadata: Record<string, unknown> | null,
  isError = false,
): ToolResultBlock {
  return {
    type: "tool_result",
    tool_use_id: toolUseId,
    output,
    metadata,
    is_error: isError,
  };
}

function createImageBlock(
  data: string,
  mimeType: string,
  name?: string,
): MessageBlock {
  const block: MessageBlock = { type: "image", data, mime_type: mimeType };
  if (name) block.name = name;
  return block;
}

function createDocumentBlock(
  data: string,
  mimeType: string,
  name?: string,
): DocumentBlock {
  const block: DocumentBlock = { type: "document", data, mime_type: mimeType };
  if (name) block.name = name;
  return block;
}

function createAttachmentBlock(attachment: AttachedFile): MessageBlock {
  if (attachment.kind === "image") {
    return createImageBlock(
      attachment.data,
      attachment.mime_type,
      attachment.name,
    );
  }
  if (attachment.kind === "document") {
    return createDocumentBlock(
      attachment.data,
      attachment.mime_type,
      attachment.name,
    );
  }
  return createAttachedTextBlock(attachment.text, attachment.name);
}

/**
 * Optimistic block for an inline @ workspace reference. Only the server has
 * the bytes, so text renders as an empty `<file>` snapshot and image/PDF as
 * empty-data blocks that the bubble shows as file cards; a session reload
 * replaces them with the server-persisted real blocks.
 */
function createWorkspaceRefBlock(ref: WorkspaceFileReference): MessageBlock {
  if (ref.kind === "image") {
    return createImageBlock("", "", ref.name);
  }
  if (ref.kind === "document") {
    return createDocumentBlock("", "application/pdf", ref.name);
  }
  return createAttachedTextBlock("", ref.path);
}

export function createUserTextMessage(text: string): ChatMessage {
  return createMessage("user", text ? [createTextBlock(text)] : []);
}

export function createUserMessage(
  text: string,
  attachments: AttachedFile[],
  workspaceFiles: WorkspaceFileReference[] = [],
): ChatMessage {
  const blocks: MessageBlock[] = [];
  if (text) blocks.push(createTextBlock(text));
  for (const ref of workspaceFiles) {
    blocks.push(createWorkspaceRefBlock(ref));
  }
  for (const attachment of attachments) {
    blocks.push(createAttachmentBlock(attachment));
  }
  return createMessage("user", blocks);
}

export function createAssistantMessage(
  content: MessageBlock[] = [],
): ChatMessage {
  return createMessage("assistant", content);
}

function ensureTailAssistant(messages: ChatMessage[]): {
  messages: ChatMessage[];
  index: number;
} {
  const next = [...messages];
  const lastIndex = next.length - 1;
  if (lastIndex >= 0 && next[lastIndex]?.role === "assistant") {
    return { messages: next, index: lastIndex };
  }
  next.push(createAssistantMessage([]));
  return { messages: next, index: next.length - 1 };
}

export function appendAssistantDelta(
  messages: ChatMessage[],
  blockType: "thinking" | "text",
  delta: string,
): ChatMessage[] {
  if (!delta) return messages;

  const { messages: next, index } = ensureTailAssistant(messages);
  const assistant = next[index] ?? createAssistantMessage([]);
  const content = [...getBlocks(assistant)];
  const lastBlock = content[content.length - 1];

  if (lastBlock?.type === blockType) {
    content[content.length - 1] = {
      ...lastBlock,
      text: `${lastBlock.text || ""}${delta}`,
    };
  } else {
    content.push(
      blockType === "thinking"
        ? createThinkingBlock(delta)
        : createTextBlock(delta),
    );
  }

  next[index] = { ...assistant, content };
  return next;
}

export function appendToolUse(
  messages: ChatMessage[],
  toolCall: ToolCall,
): ChatMessage[] {
  // Tail-aware: a backward scan would attach the new tool to the previous
  // turn's assistant when the tail is a tool-result user message or compact.
  const { messages: next, index } = ensureTailAssistant(messages);
  const assistant = next[index];
  if (!assistant) return next;
  next[index] = {
    ...assistant,
    content: [...getBlocks(assistant), createToolUseBlock(toolCall)],
  };
  return next;
}

function isToolResultOnlyUserMessage(message?: ChatMessage): boolean {
  const blocks = getBlocks(message);
  return (
    message?.role === "user" &&
    blocks.length > 0 &&
    blocks.every((block) => block?.type === "tool_result")
  );
}

export function appendToolResult(
  messages: ChatMessage[],
  toolUseId: string,
  output: string | null,
  metadata: Record<string, unknown> | null,
  isError = false,
): ChatMessage[] {
  const block = createToolResultBlock(toolUseId, output, metadata, isError);
  const next = [...messages];
  const lastIndex = next.length - 1;

  if (lastIndex >= 0 && isToolResultOnlyUserMessage(next[lastIndex])) {
    const lastMessage = next[lastIndex];
    if (!lastMessage) return next;
    next[lastIndex] = {
      ...lastMessage,
      content: [...getBlocks(lastMessage), block],
    };
    return next;
  }

  next.push(createMessage("user", [block]));
  return next;
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
    const message = messages[messageIndex];
    if (message?.role !== "assistant") continue;

    const content = getBlocks(message);
    for (let blockIndex = content.length - 1; blockIndex >= 0; blockIndex--) {
      const block = content[blockIndex];
      if (block?.type !== "thinking") continue;

      const next = [...messages];
      const nextContent = [...content];
      nextContent[blockIndex] = {
        ...block,
        meta: {
          ...(isObject(block.meta) ? block.meta : {}),
          duration_ms: durationMs,
        },
      };
      next[messageIndex] = { ...message, content: nextContent };
      return next;
    }
  }

  return messages;
}

export function updateLatestAssistantMeta(
  messages: ChatMessage[],
  patch: Partial<MessageMeta>,
): ChatMessage[] {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message?.role !== "assistant") continue;
    const next = [...messages];
    next[i] = { ...message, meta: { ...(message.meta ?? {}), ...patch } };
    return next;
  }
  return messages;
}

function buildToolRuntime(
  runtime: ToolRuntime | undefined,
  toolResultBlock: ToolResultBlock | null,
): ToolRuntime {
  const output = typeof runtime?.output === "string" ? runtime.output : "";
  const runtimeFinal =
    typeof runtime?.finalOutput === "string" ? runtime.finalOutput : null;
  const persistedOutput =
    typeof toolResultBlock?.output === "string" ? toolResultBlock.output : null;
  const finalOutput = runtimeFinal ?? persistedOutput;
  const runtimeMetadata = isObject(runtime?.metadata) ? runtime.metadata : null;
  const persistedMetadata = isObject(toolResultBlock?.metadata)
    ? toolResultBlock.metadata
    : null;
  const metadata = runtimeMetadata ?? persistedMetadata;
  const isError = Boolean(
    runtime?.isError ||
      toolResultBlock?.is_error ||
      (typeof finalOutput === "string" && finalOutput.startsWith("error:")),
  );

  return {
    pending: Boolean(runtime?.pending),
    output,
    finalOutput,
    metadata,
    isError,
  };
}

function createCompactMarker(sourceIndex: number): CompactMarkerMessage {
  return {
    kind: "compact-marker",
    sourceIndex,
    renderKey: `compact:${sourceIndex}`,
  };
}

const TURN_TOKEN_KEYS = [
  "total_tokens",
  "input_tokens",
  "output_tokens",
  "cache_read_tokens",
  "cache_write_tokens",
  "reasoning_tokens",
] as const;

function readCost(value: unknown): Cost | undefined {
  // biome-ignore lint/complexity/useLiteralKeys: index-signature object boundary
  return isObject(value) && typeof value["total"] === "number"
    ? (value as unknown as Cost)
    : undefined;
}

function addCost(total: Cost | undefined, request: Cost | undefined) {
  if (!request) return total;
  if (!total) return { ...request };

  const totalValue = total.total + request.total;
  if (
    total.input === undefined ||
    total.output === undefined ||
    request.input === undefined ||
    request.output === undefined
  ) {
    return { total: totalValue };
  }

  const result: Cost = {
    input: total.input + request.input,
    output: total.output + request.output,
    total: totalValue,
  };
  if (total.cache_read !== undefined || request.cache_read !== undefined) {
    result.cache_read = (total.cache_read ?? 0) + (request.cache_read ?? 0);
  }
  if (total.cache_write !== undefined || request.cache_write !== undefined) {
    result.cache_write = (total.cache_write ?? 0) + (request.cache_write ?? 0);
  }
  if (total.reasoning !== undefined || request.reasoning !== undefined) {
    result.reasoning = (total.reasoning ?? 0) + (request.reasoning ?? 0);
  }
  return result;
}

/**
 * Fold one raw assistant's meta into the turn stats of its render bubble.
 *
 * Two mutually exclusive shapes feed this:
 * - Streaming: SSE usage events patch turn-CUMULATIVE `turn_usage` /
 *   `turn_cost` onto each raw message, so the latest raw carrying them
 *   replaces everything accumulated before — summing would double-count.
 * - History: each raw carries its persisted per-request `usage` and `cost`,
 *   which sum across the turn.
 *
 * Usage and cost are independent. A new usage record without a context total
 * clears the previous occupancy; a raw with neither usage nor cost contributes
 * nothing.
 */
function foldTurnStats(
  prev: TurnStats | undefined,
  meta: MessageMeta | undefined,
): TurnStats | undefined {
  if (!meta) return prev;
  const contextWindow =
    typeof meta.context_window === "number"
      ? meta.context_window
      : prev?.context_window;

  if (meta.turn_usage !== undefined || meta.turn_cost !== undefined) {
    const stats: TurnStats = {};
    if (contextWindow !== undefined) stats.context_window = contextWindow;
    if (typeof meta.context_tokens === "number") {
      stats.context_tokens = meta.context_tokens;
    }
    for (const key of TURN_TOKEN_KEYS) {
      const value = meta.turn_usage?.[key];
      if (value !== undefined) stats[key] = value;
    }
    const cost = readCost(meta.turn_cost);
    if (cost) stats.cost = cost;
    return stats;
  }

  const usage = isObject(meta.usage) ? meta.usage : null;
  const requestCost = readCost(meta.cost);
  if (!usage && !requestCost) return prev;

  const stats: TurnStats = { ...prev };
  if (contextWindow !== undefined) stats.context_window = contextWindow;
  if (usage) {
    // Context occupancy is the latest request's total, never a sum.
    // biome-ignore lint/complexity/useLiteralKeys: index-signature record requires bracket access
    const totalTokens = usage["total_tokens"];
    delete stats.context_tokens;
    if (typeof totalTokens === "number") {
      stats.context_tokens = totalTokens;
    }
    for (const key of TURN_TOKEN_KEYS) {
      const value = usage[key];
      if (typeof value === "number") stats[key] = (stats[key] ?? 0) + value;
    }
  }
  const cost = addCost(stats.cost, requestCost);
  if (cost) stats.cost = cost;
  return stats;
}

function createRenderAssistantMessage(sourceIndex: number): ChatMessage {
  return {
    role: "assistant",
    content: [],
    renderKey: `assistant:${sourceIndex}`,
    sourceIndex,
  };
}

/**
 * Project rawMessages + toolRuntimeById into the shape the UI consumes.
 */
export function buildRenderMessages(
  messages: ChatMessage[],
  toolRuntimeById: Record<string, ToolRuntime> = {},
): RenderMessage[] {
  if (!Array.isArray(messages)) return [];

  const result: RenderMessage[] = [];
  const toolIndex: Record<string, ToolIndexEntry> = {};
  let currentAssistant: ChatMessage | null = null;
  let turnStats: TurnStats | undefined;
  let turnStatsOwnerIndex: number | null = null;

  const commitTurnStats = () => {
    if (turnStats && turnStatsOwnerIndex !== null) {
      const owner = result[turnStatsOwnerIndex];
      if (owner && !isCompactMarker(owner)) {
        result[turnStatsOwnerIndex] = { ...owner, stats: turnStats };
      }
    }
    turnStats = undefined;
    turnStatsOwnerIndex = null;
  };

  const ensureAssistantRenderMessage = (sourceIndex: number) => {
    if (currentAssistant) return currentAssistant;
    currentAssistant = createRenderAssistantMessage(sourceIndex);
    result.push(currentAssistant);
    return currentAssistant;
  };

  for (const [sourceIndex, message] of messages.entries()) {
    const role = message?.role;
    const blocks = getBlocks(message);

    if (role === "compact") {
      const contextTokens = turnStats?.context_tokens;
      turnStats = foldTurnStats(
        turnStats,
        message?.meta as MessageMeta | undefined,
      );
      // The summary request is billed into the turn, but its total is not the
      // post-compact context occupancy. Keep the last normal request's value.
      if (turnStats) {
        if (contextTokens === undefined) delete turnStats.context_tokens;
        else turnStats.context_tokens = contextTokens;
      }
      result.push(createCompactMarker(sourceIndex));
      currentAssistant = null;
      continue;
    }

    if (role === "user") {
      const userBlocks: MessageBlock[] = [];
      const toolResults: ToolResultBlock[] = [];

      for (const [blockIndex, block] of blocks.entries()) {
        // biome-ignore lint/complexity/useLiteralKeys: index signature requires bracket access
        if (block?.type === "text" && block.meta?.["skill_snapshot"]) {
          continue;
        }
        if (
          (block?.type === "text" && block.text) ||
          block?.type === "image" ||
          block?.type === "document"
        ) {
          userBlocks.push(
            cloneBlock(block, `user:${sourceIndex}:${blockIndex}`),
          );
        } else if (block?.type === "tool_result") {
          toolResults.push(block);
        }
      }

      if (userBlocks.length > 0) {
        commitTurnStats();
        const userMsg: ChatMessage = {
          role: "user",
          content: userBlocks,
          renderKey: `user:${sourceIndex}`,
          sourceIndex,
        };
        if (isObject(message?.meta))
          userMsg.meta = { ...(message.meta as MessageMeta) };
        result.push(userMsg);
        currentAssistant = null;
      }

      if (toolResults.length === 0) continue;

      const assistantMessage = ensureAssistantRenderMessage(sourceIndex);
      let assistantContent = [...getBlocks(assistantMessage)];

      for (const block of toolResults) {
        const toolUseId = block.tool_use_id;
        const runtime = toolUseId ? toolRuntimeById[toolUseId] : undefined;
        const entry = toolUseId ? toolIndex[toolUseId] : undefined;

        if (entry) {
          // Tool result for a tool_use we already projected — splice the
          // runtime/result back onto that tool_use block.
          const target = result[entry.messageIndex];
          if (target && !isCompactMarker(target)) {
            const targetContent = [...getBlocks(target)];
            const targetBlock = targetContent[entry.blockIndex];
            if (targetBlock?.type === "tool_use") {
              targetContent[entry.blockIndex] = {
                ...targetBlock,
                runtime: buildToolRuntime(runtime, block),
              };
              const updatedMessage = { ...target, content: targetContent };
              result[entry.messageIndex] = updatedMessage;
              if (entry.messageIndex === result.length - 1) {
                currentAssistant = updatedMessage;
                assistantContent = targetContent;
              }
            }
          }
          continue;
        }

        // Orphan tool_result (no matching tool_use seen) — surface it as a
        // synthetic tool_use block on the current assistant so the UI still
        // shows it instead of silently dropping.
        const nextBlock: ToolUseBlock = {
          type: "tool_use",
          id: toolUseId || "",
          name: "tool",
          input: EMPTY_TOOL_INPUT,
          runtime: buildToolRuntime(runtime, block),
        };
        const blockIndex = assistantContent.length;
        nextBlock.renderKey =
          toolUseId || `tool-result:${sourceIndex}:${blockIndex}`;
        assistantContent.push(nextBlock);
        currentAssistant = { ...assistantMessage, content: assistantContent };
        result[result.length - 1] = currentAssistant;
        if (toolUseId) {
          toolIndex[toolUseId] = {
            messageIndex: result.length - 1,
            blockIndex,
          };
        }
      }

      continue;
    }

    if (role !== "assistant") continue;

    const assistantMessage = ensureAssistantRenderMessage(sourceIndex);
    const assistantContent = [...getBlocks(assistantMessage)];
    const messageIndex = result.length - 1;

    for (const [sourceBlockIndex, block] of blocks.entries()) {
      if (block?.type === "thinking" && block.text) {
        assistantContent.push(
          cloneBlock(block, `assistant:${sourceIndex}:${sourceBlockIndex}`),
        );
        continue;
      }

      if (block?.type === "text" && block.text) {
        assistantContent.push(
          cloneBlock(block, `assistant:${sourceIndex}:${sourceBlockIndex}`),
        );
        continue;
      }

      if (block?.type !== "tool_use") continue;

      const renderBlock: ToolUseBlock = {
        type: "tool_use",
        id: block.id,
        name: block.name,
        input: isObject(block.input) ? block.input : EMPTY_TOOL_INPUT,
        runtime: buildToolRuntime(
          block.id ? toolRuntimeById[block.id] : undefined,
          null,
        ),
      };
      renderBlock.renderKey =
        block.id || `assistant:${sourceIndex}:${sourceBlockIndex}`;
      if (isObject(block.meta)) {
        renderBlock.meta = { ...block.meta };
      }
      const blockIndex = assistantContent.length;
      assistantContent.push(renderBlock);

      if (block.id) {
        toolIndex[block.id] = { messageIndex, blockIndex };
      }
    }

    // Tool loops collapse multiple raw assistants into one render message;
    // overwrite (or clear) meta from the latest raw call so the bubble never
    // shows stale per-turn fields from earlier iterations. Turn stats fold
    // across all of the turn's raw messages instead.
    const merged: ChatMessage = {
      ...assistantMessage,
      content: assistantContent,
    };
    const rawMeta = message?.meta as MessageMeta | undefined;
    if (rawMeta) {
      merged.meta = { ...rawMeta };
    } else {
      delete merged.meta;
    }
    turnStats = foldTurnStats(turnStats, rawMeta);
    if (turnStats) turnStatsOwnerIndex = messageIndex;
    currentAssistant = merged;
    result[messageIndex] = merged;
  }

  commitTurnStats();
  return result.filter((message, index) => {
    if (isCompactMarker(message)) return true;
    return (
      (Array.isArray(message.content) && message.content.length > 0) ||
      (index === result.length - 1 && message.role === "assistant")
    );
  });
}
