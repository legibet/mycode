/**
 * Scrollable message list with auto-scroll.
 * Only auto-scrolls when the user is already near the bottom; away from it,
 * a button jumps back to the latest output.
 * Empty state: blinking cursor terminal prompt.
 */

import { ArrowDown } from "lucide-react";
import {
  memo,
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { CompactMarker } from "@/components/messages/CompactMarker";
import { JobMarker } from "@/components/messages/JobMarker";
import { MessageBubble } from "@/components/messages/MessageBubble";
import { SettleAnchor } from "@/components/messages/SettleAnchor";
import { Button } from "@/components/ui/button";
import { SimpleTooltip } from "@/components/ui/tooltip";
import type { PendingInput, RenderMessage } from "@/types";
import { isChatMessage, isCompactMarker } from "@/types";
import { cn } from "@/utils/cn";
import { createUserMessage } from "@/utils/messages";

const SCROLL_THRESHOLD = 120;
const DRAFT_SESSION_KEY = "__draft__";
const INITIAL_MESSAGE_COUNT = 60;
const LOAD_PREVIOUS_COUNT = 30;
const LOAD_PREVIOUS_THRESHOLD = 160;

interface MessageListProps {
  sessionId?: string | undefined;
  messages: RenderMessage[];
  loading: boolean;
  /** A compact run is active; a pending divider pulses at the tail. */
  compacting: boolean;
  /** Last compact failure, shown as a quiet inline note at the tail. */
  compactError: string | null;
  /** Last rejected send, shown as plain text at the tail. */
  sendError: string | null;
  /** Steers waiting for the running turn's next step boundary. */
  pendingSteers?: PendingInput[];
  onRewindAndSend?: ((rewindTo: number, input: string) => Promise<void>) | undefined;
}

/** The blinking terminal prompt shown while there is nothing to list;
 * `children` go below it. */
export function EmptyState({ children }: { children?: ReactNode }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center p-8 text-center">
      <h1 className="font-display text-3xl tracking-[-0.022em] text-foreground/70">
        mycode
        <span className="ml-0.5 inline-block h-6 w-0.5 animate-cursor-blink bg-primary/60 align-middle" />
      </h1>
      {children && <div className="mt-8">{children}</div>}
    </div>
  );
}

export const MessageList = memo(function MessageList({
  sessionId,
  messages,
  loading,
  compacting,
  compactError,
  sendError,
  pendingSteers = [],
  onRewindAndSend,
}: MessageListProps) {
  const sessionKey = sessionId || DRAFT_SESSION_KEY;
  const contentKey = messages.length === 0 ? "empty" : "ready";

  return (
    <WindowedMessages
      key={`${sessionKey}:${contentKey}`}
      messages={messages}
      loading={loading}
      compacting={compacting}
      compactError={compactError}
      sendError={sendError}
      pendingSteers={pendingSteers}
      onRewindAndSend={onRewindAndSend}
    />
  );
});

type WindowedMessagesProps = Omit<MessageListProps, "sessionId" | "pendingSteers"> & {
  pendingSteers: PendingInput[];
};

function getInitialStartIndex(messageCount: number): number {
  return Math.max(0, messageCount - INITIAL_MESSAGE_COUNT);
}

function isNearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD;
}

function measureMessageShells(container: HTMLElement) {
  const shells = container.querySelectorAll<HTMLElement>(".chat-message-shell");
  for (const shell of shells) {
    shell.style.setProperty(
      "--chat-message-intrinsic-size",
      `${Math.ceil(shell.getBoundingClientRect().height)}px`,
    );
  }
}

interface PrependSnapshot {
  scrollHeight: number;
  scrollTop: number;
}

function WindowedMessages({
  messages,
  loading,
  compacting,
  compactError,
  sendError,
  pendingSteers,
  onRewindAndSend,
}: WindowedMessagesProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const followOutputRef = useRef(true);
  const lastScrollTop = useRef(0);
  const previousOutputVersionRef = useRef("");
  const prependSnapshot = useRef<PrependSnapshot | null>(null);
  const layoutMeasureFrame = useRef<number | null>(null);
  const [layoutOptimized, setLayoutOptimized] = useState(false);
  const [awayFromBottom, setAwayFromBottom] = useState(false);
  const [visibleStartIndex, setVisibleStartIndex] = useState(() =>
    getInitialStartIndex(messages.length),
  );
  const effectiveStartIndex = Math.min(visibleStartIndex, getInitialStartIndex(messages.length));
  const visibleMessages = useMemo(
    () => messages.slice(effectiveStartIndex),
    [effectiveStartIndex, messages],
  );
  const latestMessage = messages.at(-1);
  const latestChat = latestMessage && isChatMessage(latestMessage) ? latestMessage : null;
  const streamingKey =
    loading && latestChat?.role === "assistant"
      ? latestChat.renderKey || `msg-${messages.length - 1}`
      : null;
  const pendingSteerMessages = useMemo(
    () =>
      pendingSteers.map((item) => ({
        id: item.id,
        role: "user" as const,
        blocks: createUserMessage(
          item.submission.text,
          item.attachments,
          item.submission.workspaceFiles,
        ).content.map((block, index) => ({
          ...block,
          renderKey: `${item.id}:${index}`,
        })),
      })),
    [pendingSteers],
  );
  const showPendingCompact = compacting && (!latestMessage || !isCompactMarker(latestMessage));
  const latestOutputBlockCount = latestChat?.content.length ?? 0;
  const latestOutputTextLength =
    latestChat?.content.reduce((total, block) => {
      if (block.type !== "text" && block.type !== "thinking") return total;
      return total + (block.text?.length ?? 0);
    }, 0) ?? 0;
  const outputVersion = `${messages.length}:${latestOutputBlockCount}:${latestOutputTextLength}:${pendingSteers.length}:${compacting}:${compactError ?? ""}:${sendError ?? ""}`;

  const scrollToBottom = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;

    el.scrollTop = el.scrollHeight;
  }, []);

  const scheduleLayoutOptimization = useCallback(() => {
    if (layoutMeasureFrame.current !== null) {
      window.cancelAnimationFrame(layoutMeasureFrame.current);
    }
    layoutMeasureFrame.current = window.requestAnimationFrame(() => {
      layoutMeasureFrame.current = null;
      const el = containerRef.current;
      if (el) measureMessageShells(el);
      setLayoutOptimized(true);
    });
  }, []);

  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;

    // Only scrolling up leaves the bottom on purpose: a smooth jump to the
    // latest output passes through positions away from it.
    const nearBottom = isNearBottom(el);
    if (nearBottom) followOutputRef.current = true;
    else if (el.scrollTop < lastScrollTop.current) {
      followOutputRef.current = false;
    }
    lastScrollTop.current = el.scrollTop;
    setAwayFromBottom(!nearBottom);

    if (el.scrollTop > LOAD_PREVIOUS_THRESHOLD || effectiveStartIndex === 0) {
      return;
    }

    prependSnapshot.current = {
      scrollHeight: el.scrollHeight,
      scrollTop: el.scrollTop,
    };
    setLayoutOptimized(false);
    setVisibleStartIndex(Math.max(0, effectiveStartIndex - LOAD_PREVIOUS_COUNT));
  }, [effectiveStartIndex]);

  const jumpToLatest = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    followOutputRef.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, []);

  useLayoutEffect(() => {
    followOutputRef.current = true;
    scrollToBottom();
    scheduleLayoutOptimization();
  }, [scheduleLayoutOptimization, scrollToBottom]);

  // Expanding work grows the content without a scroll event.
  useEffect(() => {
    const el = containerRef.current;
    const content = contentRef.current;
    if (!el || !content) return;
    const observer = new ResizeObserver(() => setAwayFromBottom(!isNearBottom(el)));
    observer.observe(el);
    observer.observe(content);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    return () => {
      if (layoutMeasureFrame.current !== null) {
        window.cancelAnimationFrame(layoutMeasureFrame.current);
      }
    };
  }, []);

  // No deps: must run after every render — handleScroll arms the snapshot
  // during the same event pass that triggers the prepend re-render.
  useLayoutEffect(() => {
    const snapshot = prependSnapshot.current;
    if (snapshot == null) return;

    const el = containerRef.current;
    if (!el) return;

    el.scrollTop = el.scrollHeight - snapshot.scrollHeight + snapshot.scrollTop;
    prependSnapshot.current = null;
    followOutputRef.current = isNearBottom(el);
    scheduleLayoutOptimization();
  });

  useLayoutEffect(() => {
    if (!layoutOptimized || !followOutputRef.current) return;
    if (prependSnapshot.current != null) return;

    scrollToBottom();
  }, [layoutOptimized, scrollToBottom]);

  useLayoutEffect(() => {
    if (previousOutputVersionRef.current === outputVersion) return;
    previousOutputVersionRef.current = outputVersion;
    if (!followOutputRef.current) return;
    if (prependSnapshot.current != null) return;

    scrollToBottom();
  }, [outputVersion, scrollToBottom]);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="min-h-0 flex-1 scrollbar-gutter-both overflow-y-auto pt-6 pb-4 [overflow-anchor:none]"
      >
        <SettleAnchor
          streamingKey={streamingKey}
          containerRef={containerRef}
          followOutputRef={followOutputRef}
        >
          <div
            ref={contentRef}
            className="mx-auto flex min-h-full max-w-4xl flex-col gap-6 max-md:max-w-none max-md:gap-5"
          >
            {messages.length === 0 && <EmptyState />}
            {visibleMessages.map((message, visibleIndex) => {
              const index = effectiveStartIndex + visibleIndex;
              const renderKey = message.renderKey || `msg-${index}`;
              if (!isChatMessage(message)) {
                return (
                  <div
                    key={renderKey}
                    className="chat-message-shell"
                    data-layout-optimized={layoutOptimized}
                  >
                    {isCompactMarker(message) ? (
                      <CompactMarker />
                    ) : (
                      <JobMarker jobs={message.jobs} />
                    )}
                  </div>
                );
              }
              const isStreamingMessage = renderKey === streamingKey;

              return (
                <div
                  key={renderKey}
                  className="chat-message-shell"
                  data-layout-optimized={layoutOptimized}
                  data-streaming={isStreamingMessage || undefined}
                >
                  <MessageBubble
                    role={message.role}
                    blocks={message.content}
                    sourceIndex={message.sourceIndex}
                    isStreaming={isStreamingMessage}
                    isLoading={loading}
                    model={message.meta?.model}
                    stats={message.stats}
                    interruption={message.interruption}
                    error={message.meta?.error}
                    onRewindAndSend={onRewindAndSend}
                  />
                </div>
              );
            })}
            {/* The freshly-appended marker replaces the pending divider in place. */}
            {showPendingCompact && (
              <div className="chat-message-shell">
                <CompactMarker pending />
              </div>
            )}
            {pendingSteerMessages.map(({ id, role, blocks }) => (
              <MessageBubble key={id} role={role} blocks={blocks} isLoading={false} pending />
            ))}
            {!compacting && compactError && (
              <div
                role="status"
                title={compactError}
                className="flex items-center justify-center px-2 py-1 select-none"
              >
                <span className="text-2xs tracking-[0.18em] text-muted-foreground/50 uppercase">
                  {compactError === "nothing to compact"
                    ? "nothing to compact"
                    : "compaction failed"}
                </span>
              </div>
            )}
            {sendError && (
              <div role="alert" className="chat-message-shell px-5 max-md:px-4">
                <p className="text-xs break-words text-destructive/80">{sendError}</p>
              </div>
            )}
            {(messages.length > 0 ||
              pendingSteers.length > 0 ||
              showPendingCompact ||
              compactError ||
              sendError) && <div className="h-4" />}
          </div>
        </SettleAnchor>
      </div>
      {messages.length > 0 && (
        <SimpleTooltip label="Scroll to latest">
          <Button
            variant="ghost"
            size="icon"
            aria-label="Scroll to latest"
            inert={!awayFromBottom}
            onClick={jumpToLatest}
            className={cn(
              "absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border-border/60 bg-background shadow-sm transition-[color,background-color,opacity,scale] hover:bg-muted",
              !awayFromBottom && "scale-90 opacity-0",
            )}
          >
            <ArrowDown className="size-4" />
          </Button>
        </SimpleTooltip>
      )}
    </div>
  );
}
