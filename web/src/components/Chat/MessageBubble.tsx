/**
 * Message display.
 * No role labels — layout conveys who is speaking.
 * User: right-aligned compact bubble with hover edit button.
 * Assistant: left-aligned, full-width, content-first.
 */

import { Check, Copy, FileText, Pencil } from "lucide-react";
import {
  Component,
  Fragment,
  type KeyboardEvent,
  memo,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  ChatMessage,
  DocumentBlock,
  ImageBlock,
  MessageBlock,
  TextBlock,
  TurnStats,
} from "../../types";
import { copyText } from "../../utils/clipboard";
import { cn } from "../../utils/cn";
import { formatCost } from "../../utils/format";
import { MarkdownBlock } from "./MarkdownBlock";
import { ReasoningBlock } from "./ReasoningBlock";
import { StatsHover } from "./StatsCard";
import { ToolCard } from "./ToolCard";

interface MessageBubbleProps {
  role: ChatMessage["role"];
  blocks: MessageBlock[];
  sourceIndex?: number | undefined;
  isStreaming?: boolean | undefined;
  isLoading: boolean;
  model?: string | undefined;
  stats?: TurnStats | undefined;
  onRewindAndSend?:
    | ((rewindTo: number, input: string) => Promise<void>)
    | undefined;
}

interface RenderErrorBoundaryProps {
  children: ReactNode;
  fallback: ReactNode;
}

interface RenderErrorBoundaryState {
  hasError: boolean;
}

interface AttachmentMeta {
  attachment?: boolean;
  path?: string;
}

function getDurationMs(block: MessageBlock): number | undefined {
  // biome-ignore lint/complexity/useLiteralKeys: index signature requires bracket access
  const durationMs = block.meta?.["duration_ms"];
  return typeof durationMs === "number" ? durationMs : undefined;
}

function getAttachmentMeta(block: MessageBlock): AttachmentMeta | undefined {
  return block.meta as AttachmentMeta | undefined;
}

function blocksEqual(prev: MessageBlock, next: MessageBlock): boolean {
  if (prev === next) return true;
  if (prev.type !== next.type) return false;

  if (prev.type === "text" && next.type === "text") {
    return (
      prev.text === next.text &&
      getAttachmentMeta(prev)?.attachment ===
        getAttachmentMeta(next)?.attachment &&
      getAttachmentMeta(prev)?.path === getAttachmentMeta(next)?.path
    );
  }

  if (prev.type === "thinking" && next.type === "thinking") {
    return (
      prev.text === next.text && getDurationMs(prev) === getDurationMs(next)
    );
  }

  if (prev.type === "tool_use" && next.type === "tool_use") {
    const prevRuntime = prev.runtime;
    const nextRuntime = next.runtime;
    return (
      prev.id === next.id &&
      prev.name === next.name &&
      prev.input === next.input &&
      prevRuntime?.pending === nextRuntime?.pending &&
      prevRuntime?.output === nextRuntime?.output &&
      prevRuntime?.finalOutput === nextRuntime?.finalOutput &&
      prevRuntime?.metadata === nextRuntime?.metadata &&
      prevRuntime?.isError === nextRuntime?.isError
    );
  }

  if (prev.type === "image" && next.type === "image") {
    return (
      prev.data === next.data &&
      prev.mime_type === next.mime_type &&
      prev.name === next.name
    );
  }

  if (prev.type === "document" && next.type === "document") {
    return (
      prev.data === next.data &&
      prev.mime_type === next.mime_type &&
      prev.name === next.name
    );
  }

  if (prev.type === "tool_result" && next.type === "tool_result") {
    return (
      prev.tool_use_id === next.tool_use_id &&
      prev.output === next.output &&
      prev.metadata === next.metadata &&
      prev.is_error === next.is_error
    );
  }

  return false;
}

function blockListsEqual(prev: MessageBlock[], next: MessageBlock[]): boolean {
  if (prev === next) return true;
  if (prev.length !== next.length) return false;
  for (let i = 0; i < prev.length; i++) {
    const prevBlock = prev[i];
    const nextBlock = next[i];
    if (!prevBlock || !nextBlock || !blocksEqual(prevBlock, nextBlock)) {
      return false;
    }
  }
  return true;
}

// Stats objects are rebuilt on every projection, so compare by value.
function turnStatsEqual(
  prev: TurnStats | undefined,
  next: TurnStats | undefined,
): boolean {
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
    prev.cost?.reasoning === next.cost?.reasoning
  );
}

function messageBubblePropsEqual(
  prev: MessageBubbleProps,
  next: MessageBubbleProps,
): boolean {
  if (
    prev.role !== next.role ||
    prev.sourceIndex !== next.sourceIndex ||
    prev.isStreaming !== next.isStreaming ||
    prev.model !== next.model ||
    !turnStatsEqual(prev.stats, next.stats) ||
    prev.onRewindAndSend !== next.onRewindAndSend
  ) {
    return false;
  }

  if (prev.role === "user" && prev.isLoading !== next.isLoading) {
    return false;
  }

  return blockListsEqual(prev.blocks, next.blocks);
}

class RenderErrorBoundary extends Component<
  RenderErrorBoundaryProps,
  RenderErrorBoundaryState
> {
  state: RenderErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): RenderErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error) {
    console.error("Chat block render failed:", error);
  }

  render() {
    return this.state.hasError ? this.props.fallback : this.props.children;
  }
}

const renderErrorFallback = (
  <div className="rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive/80">
    Failed to render this block.
  </div>
);

/** Per-turn footer: `model · $0.0164`, this turn's token breakdown on hover.
 * Session-state numbers (context %, session total) live at the composer. */
function TurnStatsFooter({
  model,
  stats,
}: {
  model?: string | undefined;
  stats?: TurnStats | undefined;
}) {
  const cost = stats?.cost ? formatCost(stats.cost.total) : null;
  const visible = [model, cost].filter(Boolean).join(" · ");
  if (!visible) return null;

  const detailedCost =
    stats?.cost?.input !== undefined && stats.cost.output !== undefined;
  const tokenRows: { label: string; tokens: number; cost?: number }[] = [];
  const pushRow = (
    label: string,
    tokens: number | undefined,
    rowCost?: number,
  ) => {
    if (tokens === undefined) return;
    const row: { label: string; tokens: number; cost?: number } = {
      label,
      tokens,
    };
    if (rowCost !== undefined) row.cost = rowCost;
    tokenRows.push(row);
  };
  const inputTokens =
    stats?.input_tokens !== undefined
      ? stats.input_tokens -
        (stats.cache_read_tokens ?? 0) -
        (stats.cache_write_tokens ?? 0)
      : undefined;
  pushRow("Input", inputTokens, stats?.cost?.input);
  if (stats?.cache_read_tokens) {
    pushRow("Cache read", stats.cache_read_tokens, stats.cost?.cache_read);
  }
  if (stats?.cache_write_tokens) {
    pushRow("Cache write", stats.cache_write_tokens, stats.cost?.cache_write);
  }
  pushRow(
    "Output",
    stats?.output_tokens,
    stats?.cost
      ? (stats.cost.output ?? 0) + (stats.cost.reasoning ?? 0)
      : undefined,
  );

  const totalTokens =
    stats?.total_tokens ??
    (stats?.input_tokens !== undefined && stats.output_tokens !== undefined
      ? stats.input_tokens + stats.output_tokens
      : undefined);

  if (tokenRows.length === 0 && totalTokens === undefined) {
    return (
      <span className="cursor-default text-xs tabular-nums text-muted-foreground/50">
        {visible}
      </span>
    );
  }

  return (
    <StatsHover trigger={visible}>
      <span
        className={cn(
          "grid items-baseline gap-x-5 gap-y-1",
          detailedCost
            ? "grid-cols-[max-content_max-content_max-content]"
            : "grid-cols-[max-content_max-content]",
        )}
      >
        {tokenRows.map((row) => (
          <Fragment key={row.label}>
            <span className="text-muted-foreground">{row.label}</span>
            <span className="text-right tabular-nums">
              {row.tokens.toLocaleString()}
            </span>
            {detailedCost ? (
              <span className="text-right tabular-nums">
                {formatCost(row.cost ?? 0)}
              </span>
            ) : null}
          </Fragment>
        ))}
        {totalTokens !== undefined ? (
          <span className="col-span-full mt-1 grid grid-cols-subgrid items-baseline border-t border-border/50 pt-1.5 font-medium">
            <span>Total</span>
            <span className="text-right tabular-nums">
              {totalTokens.toLocaleString()}
            </span>
            {detailedCost ? (
              <span className="text-right tabular-nums">
                {formatCost(stats.cost?.total ?? 0)}
              </span>
            ) : null}
          </span>
        ) : null}
      </span>
    </StatsHover>
  );
}

/** Attachment card in a user bubble: a labelled file reference. */
function FileCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-32 max-w-xs rounded-lg border border-border/40 bg-muted/50 px-3 py-2 text-sm text-foreground/80">
      <div className="flex items-center gap-2">
        <FileText className="size-4 shrink-0 text-accent/80" />
        <span className="font-medium">{label}</span>
      </div>
      <div className="mt-1 break-all text-xs text-muted-foreground">
        {value}
      </div>
    </div>
  );
}

export const MessageBubble = memo(function MessageBubble({
  role,
  blocks,
  sourceIndex,
  isStreaming,
  isLoading,
  model,
  stats,
  onRewindAndSend,
}: MessageBubbleProps) {
  const isUser = role === "user";
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const resetCopiedTimeoutRef = useRef<number | null>(null);

  const { textContent, textAttachmentBlocks, imageBlocks, documentBlocks } =
    useMemo(() => {
      const visibleText: string[] = [];
      const textAttachmentBlocks: TextBlock[] = [];
      const imageBlocks: ImageBlock[] = [];
      const documentBlocks: DocumentBlock[] = [];
      for (const block of blocks) {
        if (!block) continue;
        if (block.type === "text") {
          if (getAttachmentMeta(block)?.attachment) {
            textAttachmentBlocks.push(block);
          } else {
            visibleText.push(block.text);
          }
        } else if (block.type === "image") {
          imageBlocks.push(block);
        } else if (block.type === "document") {
          documentBlocks.push(block);
        }
      }
      return {
        textContent: visibleText.join("\n\n"),
        textAttachmentBlocks,
        imageBlocks,
        documentBlocks,
      };
    }, [blocks]);

  useEffect(() => {
    return () => {
      if (resetCopiedTimeoutRef.current !== null) {
        window.clearTimeout(resetCopiedTimeoutRef.current);
      }
    };
  }, []);

  const handleCopy = useCallback(async () => {
    if (!textContent) return;
    try {
      await copyText(textContent);
      setCopied(true);
      if (resetCopiedTimeoutRef.current !== null) {
        window.clearTimeout(resetCopiedTimeoutRef.current);
      }
      resetCopiedTimeoutRef.current = window.setTimeout(() => {
        setCopied(false);
        resetCopiedTimeoutRef.current = null;
      }, 2000);
    } catch {
      /* ignore */
    }
  }, [textContent]);

  const canEdit =
    isUser &&
    !!textContent &&
    imageBlocks.length === 0 &&
    documentBlocks.length === 0 &&
    textAttachmentBlocks.length === 0 &&
    typeof sourceIndex === "number" &&
    !isLoading &&
    onRewindAndSend;

  const startEdit = useCallback(() => {
    setEditText(textContent);
    setEditing(true);
  }, [textContent]);

  const setEditElement = useCallback((el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.focus();
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, []);

  const submitEdit = useCallback(() => {
    const trimmed = editText.trim();
    if (!trimmed || !onRewindAndSend || typeof sourceIndex !== "number") return;
    setEditing(false);
    onRewindAndSend(sourceIndex, trimmed);
  }, [editText, onRewindAndSend, sourceIndex]);

  const cancelEdit = useCallback(() => {
    setEditing(false);
  }, []);

  const handleEditKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submitEdit();
      } else if (e.key === "Escape") {
        cancelEdit();
      }
    },
    [submitEdit, cancelEdit],
  );

  if (isUser) {
    if (editing) {
      return (
        <div className="flex justify-end px-5 max-md:px-4">
          <div className="max-w-[85%] w-full flex flex-col gap-2">
            <textarea
              ref={setEditElement}
              name="edit-message"
              aria-label="Edit message"
              value={editText}
              onChange={(e) => {
                setEditText(e.target.value);
                e.target.style.height = "auto";
                e.target.style.height = `${Math.min(e.target.scrollHeight, 300)}px`;
              }}
              onKeyDown={handleEditKeyDown}
              className="w-full resize-none rounded-lg bg-muted px-3.5 py-2 text-base md:text-sm leading-relaxed text-foreground border border-border/60 focus:outline-none focus:border-accent/60 max-h-75"
            />
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={cancelEdit}
                className="px-3 py-1 text-xs rounded-lg text-muted-foreground hover:text-foreground active:scale-95 transition-[color,scale] duration-150"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={submitEdit}
                disabled={!editText.trim()}
                className={cn(
                  "px-3 py-1 text-xs rounded-lg transition-[color,background-color,filter,scale] duration-150",
                  editText.trim()
                    ? "bg-accent text-accent-foreground hover:brightness-105 hover:saturate-[.9] active:scale-95"
                    : "text-muted-foreground/40",
                )}
              >
                Send
              </button>
            </div>
          </div>
        </div>
      );
    }

    return (
      <div className="group/user flex justify-end px-5 max-md:px-4">
        {canEdit && (
          <button
            type="button"
            aria-label="Edit message"
            onClick={startEdit}
            className="self-end mr-2 mb-0.5 opacity-0 group-hover/user:opacity-100 max-md:opacity-60 active:scale-95 transition-[color,opacity,scale] duration-150 size-6 flex items-center justify-center rounded-sm text-muted-foreground/50 hover:text-foreground"
            title="Edit & resend"
          >
            <Pencil className="size-3" />
          </button>
        )}
        <div className="max-w-[85%] flex flex-col gap-1.5 items-end">
          {imageBlocks.length > 0 && (
            <div className="flex flex-wrap gap-2 justify-end">
              {imageBlocks.map((block) =>
                block.data ? (
                  <img
                    key={block.renderKey}
                    src={`data:${block.mime_type};base64,${block.data}`}
                    alt={block.name ?? "Image"}
                    className="max-h-64 max-w-full rounded-lg"
                  />
                ) : (
                  // Workspace image before send: no bytes locally, show a card.
                  <FileCard
                    key={block.renderKey}
                    label="Image"
                    value={block.name ?? "image"}
                  />
                ),
              )}
            </div>
          )}
          {documentBlocks.length > 0 && (
            <div className="flex flex-wrap gap-2 justify-end">
              {documentBlocks.map((block) => (
                <FileCard
                  key={block.renderKey}
                  label="PDF"
                  value={block.name ?? "document.pdf"}
                />
              ))}
            </div>
          )}
          {textAttachmentBlocks.length > 0 && (
            <div className="flex flex-wrap gap-2 justify-end">
              {textAttachmentBlocks.map((block) => {
                const path = getAttachmentMeta(block)?.path;
                return (
                  <FileCard
                    key={block.renderKey}
                    label="Text"
                    value={typeof path === "string" ? path : "attached-file"}
                  />
                );
              })}
            </div>
          )}
          {textContent && (
            <div className="rounded-lg bg-muted px-3.5 py-2 text-sm leading-relaxed text-foreground whitespace-pre-wrap wrap-anywhere">
              {textContent}
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="group/msg relative px-5 max-md:px-4">
      <div className="flex flex-col gap-3 text-foreground/90 leading-relaxed text-sm">
        {blocks.map((block, blockIndex) => {
          if (block.type === "thinking") {
            const renderKey =
              block.renderKey || `thinking:${block.text || "block"}`;
            const durationMs = getDurationMs(block);
            return (
              <RenderErrorBoundary
                key={renderKey}
                fallback={renderErrorFallback}
              >
                <ReasoningBlock
                  content={block.text}
                  durationMs={durationMs}
                  isStreaming={
                    isStreaming &&
                    durationMs == null &&
                    blockIndex === blocks.length - 1
                  }
                />
              </RenderErrorBoundary>
            );
          }
          if (block.type === "text") {
            const renderKey =
              block.renderKey || `text:${block.text || "block"}`;
            return (
              <RenderErrorBoundary
                key={renderKey}
                fallback={renderErrorFallback}
              >
                <MarkdownBlock content={block.text} />
              </RenderErrorBoundary>
            );
          }
          if (block.type === "tool_use") {
            const renderKey =
              block.renderKey || block.id || `tool:${block.name || "tool"}`;
            return (
              <RenderErrorBoundary
                key={renderKey}
                fallback={renderErrorFallback}
              >
                <ToolCard
                  name={block.name}
                  args={block.input}
                  output={block.runtime?.output}
                  finalOutput={block.runtime?.finalOutput}
                  metadata={block.runtime?.metadata}
                  pending={block.runtime?.pending}
                  isError={block.runtime?.isError}
                />
              </RenderErrorBoundary>
            );
          }
          return null;
        })}

        {isStreaming && (
          <span className="inline-block w-[1.5px] h-4 bg-foreground/40 ml-0.5 align-middle" />
        )}
      </div>

      {!isUser && textContent && !isStreaming && (
        <div className="mt-2 flex items-center gap-2 max-md:opacity-60 opacity-0 group-hover/msg:opacity-100 transition-opacity duration-150">
          <button
            type="button"
            aria-label="Copy to clipboard"
            onClick={handleCopy}
            className={cn(
              "flex items-center justify-center size-6 rounded-sm active:scale-95 transition-[color,scale] duration-150",
              copied
                ? "text-accent"
                : "text-muted-foreground/50 hover:text-foreground",
            )}
            title="Copy"
          >
            {copied ? (
              <Check className="size-3.5" />
            ) : (
              <Copy className="size-3.5" />
            )}
          </button>
          {(model || stats) && <TurnStatsFooter model={model} stats={stats} />}
        </div>
      )}
    </div>
  );
}, messageBubblePropsEqual);
