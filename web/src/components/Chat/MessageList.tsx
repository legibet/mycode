/**
 * Scrollable message list with auto-scroll.
 * Only auto-scrolls when the user is already near the bottom; away from it,
 * a button jumps back to the latest output.
 * Empty state: blinking cursor terminal prompt.
 */

import { ArrowDown } from "lucide-react";
import {
  Component,
  memo,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { RenderMessage } from "../../types";
import { isCompactMarker } from "../../types";
import { cn } from "../../utils/cn";
import { isInterrupted } from "../../utils/messages";
import { CompactMarker } from "./CompactMarker";
import { MessageBubble } from "./MessageBubble";

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
  onRewindAndSend?:
    | ((rewindTo: number, input: string) => Promise<void>)
    | undefined;
  emptyStateFooter?: ReactNode;
}

export const MessageList = memo(function MessageList({
  sessionId,
  messages,
  loading,
  compacting,
  compactError,
  onRewindAndSend,
  emptyStateFooter,
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
      onRewindAndSend={onRewindAndSend}
      emptyStateFooter={emptyStateFooter}
    />
  );
});

type WindowedMessagesProps = Omit<MessageListProps, "sessionId">;

function getInitialStartIndex(messageCount: number): number {
  return Math.max(0, messageCount - INITIAL_MESSAGE_COUNT);
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

interface SettleSnapshot {
  /** Work of the turn that just stopped streaming. */
  work: HTMLElement;
  /**
   * The work edge to hold, at an offset from the container's top: the bottom
   * at its old offset for a reader below the work, the top at 0 for a reader
   * inside it, so the summary row lands where they were. Null for a reader
   * above it, where nothing moves.
   */
  pin: { edge: "top" | "bottom"; offset: number } | null;
}

interface SettleAnchorProps {
  loading: boolean;
  containerRef: RefObject<HTMLDivElement | null>;
  followOutputRef: RefObject<boolean>;
  children: ReactNode;
}

/**
 * Keeps the reader in place while a just-finished turn folds its work. The
 * fold commits with the end of streaming, so the reader's position has to be
 * read before React updates the DOM, which only getSnapshotBeforeUpdate can
 * do. The pin is re-applied on each resize until the fold's transitions end
 * or the user scrolls.
 */
class SettleAnchor extends Component<SettleAnchorProps> {
  private stopSettle: (() => void) | null = null;

  getSnapshotBeforeUpdate(prevProps: SettleAnchorProps): SettleSnapshot | null {
    const el = this.props.containerRef.current;
    if (!el || !prevProps.loading || this.props.loading) return null;
    const work = el.querySelector<HTMLElement>("[data-streaming] [data-work]");
    if (!work) return null;
    const readerTop = el.getBoundingClientRect().top;
    const { top, bottom } = work.getBoundingClientRect();
    if (bottom <= readerTop) {
      return { work, pin: { edge: "bottom", offset: bottom - readerTop } };
    }
    if (top < readerTop) return { work, pin: { edge: "top", offset: 0 } };
    return { work, pin: null };
  }

  componentDidUpdate(
    _prevProps: SettleAnchorProps,
    _prevState: unknown,
    snapshot: SettleSnapshot | null,
  ) {
    const el = this.props.containerRef.current;
    if (!el || !snapshot?.work.isConnected) return;
    const { work, pin } = snapshot;
    // A turn that stopped or failed stays open, so nothing moves.
    if (work.getAttribute("data-work") !== "folded") return;
    const { followOutputRef } = this.props;

    const hold = () => {
      if (followOutputRef.current) {
        el.scrollTop = el.scrollHeight;
        return;
      }
      if (!pin) return;
      const rect = work.getBoundingClientRect();
      const edge = pin.edge === "top" ? rect.top : rect.bottom;
      const delta = edge - el.getBoundingClientRect().top - pin.offset;
      if (delta !== 0) el.scrollTop += delta;
    };
    this.stopSettle?.();
    hold();

    // Wait for transitions only: a looping animation in the work never ends.
    const transitions =
      work
        .getAnimations?.({ subtree: true })
        .filter((animation) => animation instanceof CSSTransition) ?? [];
    if (transitions.length === 0) return;

    const observer = new ResizeObserver(hold);
    observer.observe(work);
    const input = new AbortController();
    const stop = () => {
      observer.disconnect();
      input.abort();
      if (this.stopSettle === stop) this.stopSettle = null;
    };
    this.stopSettle = stop;
    void Promise.allSettled(transitions.map((t) => t.finished)).then(stop);
    // Scroll input ends the pin; its own scrollTop writes are not input.
    const { signal } = input;
    for (const type of ["wheel", "touchstart", "pointerdown"]) {
      el.addEventListener(type, stop, { passive: true, signal });
    }
    el.ownerDocument.addEventListener("keydown", stop, { signal });
  }

  componentWillUnmount() {
    this.stopSettle?.();
  }

  render() {
    return this.props.children;
  }
}

function WindowedMessages({
  messages,
  loading,
  compacting,
  compactError,
  onRewindAndSend,
  emptyStateFooter,
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
  const effectiveStartIndex = Math.min(
    visibleStartIndex,
    getInitialStartIndex(messages.length),
  );
  const visibleMessages = useMemo(
    () => messages.slice(effectiveStartIndex),
    [effectiveStartIndex, messages],
  );
  const latestMessage = messages.at(-1);
  const showPendingCompact =
    compacting && (!latestMessage || !isCompactMarker(latestMessage));
  const latestOutputBlockCount =
    !latestMessage || isCompactMarker(latestMessage)
      ? 0
      : latestMessage.content.length;
  const latestOutputTextLength =
    !latestMessage || isCompactMarker(latestMessage)
      ? 0
      : latestMessage.content.reduce((total, block) => {
          if (block.type !== "text" && block.type !== "thinking") return total;
          return total + (block.text?.length ?? 0);
        }, 0);
  const outputVersion = `${messages.length}:${latestOutputBlockCount}:${latestOutputTextLength}:${compacting}:${compactError ?? ""}`;

  const isNearBottom = useCallback((el: HTMLElement) => {
    return el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD;
  }, []);

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
    setVisibleStartIndex(
      Math.max(0, effectiveStartIndex - LOAD_PREVIOUS_COUNT),
    );
  }, [effectiveStartIndex, isNearBottom]);

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
    const observer = new ResizeObserver(() =>
      setAwayFromBottom(!isNearBottom(el)),
    );
    observer.observe(el);
    observer.observe(content);
    return () => observer.disconnect();
  }, [isNearBottom]);

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
        className="min-h-0 flex-1 overflow-y-auto pb-4 pt-6 [overflow-anchor:none] scrollbar-gutter-both"
      >
        <SettleAnchor
          loading={loading}
          containerRef={containerRef}
          followOutputRef={followOutputRef}
        >
          <div
            ref={contentRef}
            className="mx-auto flex min-h-full max-w-4xl flex-col gap-6 max-md:max-w-none max-md:gap-5"
          >
            {messages.length === 0 && (
              <div className="flex flex-1 flex-col items-center justify-center p-8 text-center">
                <h1 className="font-display text-3xl tracking-[-0.022em] text-foreground/70">
                  mycode
                  <span className="ml-0.5 inline-block h-6 w-0.5 animate-cursor-blink bg-accent/60 align-middle" />
                </h1>
                {emptyStateFooter && (
                  <div className="mt-8">{emptyStateFooter}</div>
                )}
              </div>
            )}
            {visibleMessages.map((message, visibleIndex) => {
              const index = effectiveStartIndex + visibleIndex;
              const renderKey = message.renderKey || `msg-${index}`;
              const isStreamingMessage =
                loading &&
                index === messages.length - 1 &&
                !isCompactMarker(message) &&
                message.role === "assistant";

              if (isCompactMarker(message)) {
                return (
                  <div
                    key={renderKey}
                    className="chat-message-shell"
                    data-layout-optimized={layoutOptimized}
                  >
                    <CompactMarker />
                  </div>
                );
              }

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
                    interrupted={isInterrupted(message.meta)}
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
            {!compacting && compactError && (
              <div
                role="status"
                title={compactError}
                className="flex select-none items-center justify-center px-2 py-1"
              >
                <span className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground/50">
                  {compactError === "nothing to compact"
                    ? "nothing to compact"
                    : "compaction failed"}
                </span>
              </div>
            )}
            {(messages.length > 0 || showPendingCompact || compactError) && (
              <div className="h-4" />
            )}
          </div>
        </SettleAnchor>
      </div>
      {messages.length > 0 && (
        <button
          type="button"
          aria-label="Scroll to latest"
          title="Scroll to latest"
          inert={!awayFromBottom}
          onClick={jumpToLatest}
          className={cn(
            "absolute bottom-3 left-1/2 flex size-8 -translate-x-1/2 items-center justify-center rounded-full border border-border/60 bg-background text-muted-foreground shadow-sm transition-[color,opacity,scale] duration-150 hover:text-foreground active:scale-95",
            !awayFromBottom && "scale-90 opacity-0",
          )}
        >
          <ArrowDown className="size-4" />
        </button>
      )}
    </div>
  );
}
