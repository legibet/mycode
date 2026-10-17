/**
 * Value equality for MessageBubble's memo. buildRenderMessages() rebuilds
 * every block and stats object on each projection, so identity alone would
 * re-render every bubble on every streamed delta.
 */

import type { MessageBlock, TurnStats } from "@/types";

function blocksEqual(prev: MessageBlock, next: MessageBlock): boolean {
  if (prev === next) return true;

  if (prev.type === "text" && next.type === "text") {
    return (
      prev.text === next.text &&
      prev.meta?.["attachment"] === next.meta?.["attachment"] &&
      prev.meta?.["path"] === next.meta?.["path"]
    );
  }

  if (prev.type === "thinking" && next.type === "thinking") {
    return prev.text === next.text && prev.meta?.["duration_ms"] === next.meta?.["duration_ms"];
  }

  // The projection keeps `input` and runtime `metadata` references stable.
  if (prev.type === "tool_use" && next.type === "tool_use") {
    return (
      prev.id === next.id &&
      prev.name === next.name &&
      prev.input === next.input &&
      prev.runtime?.pending === next.runtime?.pending &&
      prev.runtime?.output === next.runtime?.output &&
      prev.runtime?.finalOutput === next.runtime?.finalOutput &&
      prev.runtime?.metadata === next.runtime?.metadata &&
      prev.runtime?.isError === next.runtime?.isError
    );
  }

  if (
    (prev.type === "image" && next.type === "image") ||
    (prev.type === "document" && next.type === "document")
  ) {
    return prev.data === next.data && prev.mime_type === next.mime_type && prev.name === next.name;
  }

  return prev.type === "compact" && next.type === "compact";
}

export function blockListsEqual(prev: MessageBlock[], next: MessageBlock[]): boolean {
  if (prev === next) return true;
  if (prev.length !== next.length) return false;
  return prev.every((block, index) => {
    const nextBlock = next[index];
    return nextBlock !== undefined && blocksEqual(block, nextBlock);
  });
}

export function turnStatsEqual(prev: TurnStats | undefined, next: TurnStats | undefined): boolean {
  if (prev === next) return true;
  if (!prev || !next) return false;
  return (
    prev.input_tokens === next.input_tokens &&
    prev.total_tokens === next.total_tokens &&
    prev.output_tokens === next.output_tokens &&
    prev.cache_read_tokens === next.cache_read_tokens &&
    prev.cache_write_tokens === next.cache_write_tokens &&
    prev.reasoning_tokens === next.reasoning_tokens &&
    prev.context_tokens === next.context_tokens &&
    prev.context_window === next.context_window &&
    prev.cost?.total === next.cost?.total &&
    prev.cost?.input === next.cost?.input &&
    prev.cost?.cache_read === next.cost?.cache_read &&
    prev.cost?.cache_write === next.cost?.cache_write &&
    prev.cost?.output === next.cost?.output &&
    prev.cost?.reasoning === next.cost?.reasoning &&
    prev.duration_ms === next.duration_ms
  );
}
