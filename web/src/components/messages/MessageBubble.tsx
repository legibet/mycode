/**
 * Message display.
 * No role labels — layout conveys who is speaking.
 * User: right-aligned compact bubble (UserBubble).
 * Assistant: left-aligned, full-width, content-first.
 */

import { Check, Copy } from "lucide-react";
import { Component, memo, type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { MarkdownBlock } from "@/components/markdown/MarkdownBlock";
import { blockListsEqual, turnStatsEqual } from "@/components/messages/bubbleEquality";
import { CompactMarker } from "@/components/messages/CompactMarker";
import { ReasoningBlock } from "@/components/messages/ReasoningBlock";
import { hasUsageRows, StatsPopover, StatsText, UsageGrid } from "@/components/messages/StatsCard";
import { ToolCard } from "@/components/messages/ToolCard";
import { UserBubble } from "@/components/messages/UserBubble";
import { WorkSection } from "@/components/messages/WorkSection";
import { Button } from "@/components/ui/button";
import { SimpleTooltip } from "@/components/ui/tooltip";
import type { ChatMessage, Interruption, MessageBlock, TurnStats } from "@/types";
import { copyText } from "@/utils/clipboard";
import { cn } from "@/utils/cn";
import { formatCost } from "@/utils/format";
import { splitTurn } from "@/utils/messages";

interface MessageBubbleProps {
  role: ChatMessage["role"];
  blocks: MessageBlock[];
  sourceIndex?: number | undefined;
  isStreaming?: boolean | undefined;
  isLoading: boolean;
  model?: string | undefined;
  stats?: TurnStats | undefined;
  /** How the turn ended before its final response. */
  interruption?: Interruption | undefined;
  /** The run error that ended the turn, shown as plain text. */
  error?: string | undefined;
  /** A steer handed to the running turn and not yet delivered. */
  pending?: boolean | undefined;
  onRewindAndSend?: ((rewindTo: number, input: string) => Promise<void>) | undefined;
}

function messageBubblePropsEqual(prev: MessageBubbleProps, next: MessageBubbleProps): boolean {
  return (
    prev.role === next.role &&
    prev.sourceIndex === next.sourceIndex &&
    prev.isStreaming === next.isStreaming &&
    // Only the user bubble reads it: editing waits for the run to end.
    (prev.role !== "user" || prev.isLoading === next.isLoading) &&
    prev.model === next.model &&
    prev.interruption === next.interruption &&
    prev.error === next.error &&
    prev.pending === next.pending &&
    prev.onRewindAndSend === next.onRewindAndSend &&
    turnStatsEqual(prev.stats, next.stats) &&
    blockListsEqual(prev.blocks, next.blocks)
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
  interruption,
  error,
  pending = false,
  onRewindAndSend,
}: MessageBubbleProps) {
  if (role === "user") {
    return (
      <UserBubble
        blocks={blocks}
        sourceIndex={sourceIndex}
        isLoading={isLoading}
        pending={pending}
        onRewindAndSend={onRewindAndSend}
      />
    );
  }
  return (
    <AssistantBubble
      blocks={blocks}
      isStreaming={isStreaming}
      model={model}
      stats={stats}
      interruption={interruption}
      error={error}
    />
  );
}, messageBubblePropsEqual);

class RenderErrorBoundary extends Component<
  { children: ReactNode; fallback: ReactNode },
  { hasError: boolean }
> {
  state = { hasError: false };

  static getDerivedStateFromError() {
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

/** Per-turn footer: `model · $0.0164`, this turn's token breakdown in a card.
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
  if (!hasUsageRows(stats)) return <StatsText>{visible}</StatsText>;

  return (
    <StatsPopover trigger={visible}>
      <UsageGrid usage={stats} />
    </StatsPopover>
  );
}

function AssistantBubble({
  blocks,
  isStreaming,
  model,
  stats,
  interruption,
  error,
}: Pick<
  MessageBubbleProps,
  "blocks" | "isStreaming" | "model" | "stats" | "interruption" | "error"
>) {
  const [copied, setCopied] = useState(false);
  const resetCopiedTimeoutRef = useRef<number | null>(null);

  const { work, answer, afterAnswer } = useMemo(() => splitTurn(blocks), [blocks]);
  // The work folds once the turn ends, however it ends.
  const folded =
    !isStreaming &&
    [...work, ...afterAnswer].some(
      (block) => block.type === "tool_use" || block.type === "compact",
    );

  // Copy takes the visible text: a folded turn copies only its answer.
  const copyBlocks = folded ? answer : blocks;
  const textContent = useMemo(
    () => copyBlocks.flatMap((block) => (block.type === "text" ? [block.text] : [])).join("\n\n"),
    [copyBlocks],
  );

  useEffect(() => {
    return () => {
      if (resetCopiedTimeoutRef.current !== null) {
        window.clearTimeout(resetCopiedTimeoutRef.current);
      }
    };
  }, []);

  const handleCopy = async () => {
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
  };

  const renderBlock = (block: MessageBlock) => {
    if (block.type === "thinking") {
      const renderKey = block.renderKey || `thinking:${block.text || "block"}`;
      const rawDuration = block.meta?.["duration_ms"];
      const durationMs = typeof rawDuration === "number" ? rawDuration : undefined;
      return (
        <RenderErrorBoundary key={renderKey} fallback={renderErrorFallback}>
          <ReasoningBlock
            content={block.text}
            durationMs={durationMs}
            isStreaming={isStreaming && durationMs == null && block === blocks.at(-1)}
          />
        </RenderErrorBoundary>
      );
    }
    if (block.type === "text") {
      const renderKey = block.renderKey || `text:${block.text || "block"}`;
      return (
        <RenderErrorBoundary key={renderKey} fallback={renderErrorFallback}>
          <MarkdownBlock content={block.text} streaming={isStreaming && block === blocks.at(-1)} />
        </RenderErrorBoundary>
      );
    }
    if (block.type === "tool_use") {
      const renderKey = block.renderKey || block.id || `tool:${block.name || "tool"}`;
      return (
        <RenderErrorBoundary key={renderKey} fallback={renderErrorFallback}>
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
    if (block.type === "compact") {
      return <CompactMarker key={block.renderKey} />;
    }
    return null;
  };

  return (
    <div className="group/msg relative px-5 max-md:px-4">
      <div className="flex flex-col gap-3 text-sm leading-relaxed text-foreground/90">
        {/* The work keeps its place from its first block on, so folding
            never remounts it or the answer. */}
        {(work.length > 0 || folded) && (
          <WorkSection
            blocks={work}
            folded={folded}
            interruption={interruption}
            durationMs={stats?.duration_ms}
          >
            {(folded ? [...work, ...afterAnswer] : work).map(renderBlock)}
          </WorkSection>
        )}
        {(folded ? answer : [...answer, ...afterAnswer]).map(renderBlock)}
        {/* A folded turn reports a stop in its summary row. */}
        {interruption === "cancelled" && !isStreaming && !folded && (
          <p className="text-xs text-muted-foreground">Stopped</p>
        )}
        {error && <p className="text-xs break-words text-destructive/80">{error}</p>}

        {isStreaming && (
          <span className="ml-0.5 inline-block h-4 w-[1.5px] bg-foreground/40 align-middle" />
        )}
      </div>

      {textContent && !isStreaming && (
        <div className="mt-2 flex items-center gap-2 opacity-0 transition-opacity duration-150 group-hover/msg:opacity-100 focus-within:opacity-100 max-md:opacity-60">
          <SimpleTooltip label="Copy">
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label="Copy to clipboard"
              onClick={handleCopy}
              className={cn(copied && "text-primary hover:text-primary")}
            >
              {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
            </Button>
          </SimpleTooltip>
          <TurnStatsFooter model={model} stats={stats} />
        </div>
      )}
    </div>
  );
}
