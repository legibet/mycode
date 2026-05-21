/**
 * Scrollable message list with auto-scroll.
 * Only auto-scrolls when the user is already near the bottom.
 * Empty state: blinking cursor terminal prompt.
 */

import {
  memo,
  type ReactNode,
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { RenderMessage } from "../../types";
import { isCompactMarker } from "../../types";
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
  onRewindAndSend?:
    | ((rewindTo: number, input: string) => Promise<void>)
    | undefined;
  emptyStateFooter?: ReactNode;
}

export const MessageList = memo(function MessageList({
  sessionId,
  messages,
  loading,
  onRewindAndSend,
  emptyStateFooter,
}: MessageListProps) {
  const sessionKey = sessionId || DRAFT_SESSION_KEY;

  if (messages.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center p-8 text-center">
        <h1 className="font-display text-3xl tracking-[-0.022em] text-foreground/70">
          mycode
          <span className="inline-block w-0.5 h-6 bg-accent/60 ml-0.5 align-middle animate-cursor-blink" />
        </h1>
        {emptyStateFooter && <div className="mt-8">{emptyStateFooter}</div>}
      </div>
    );
  }

  return (
    <WindowedMessages
      key={sessionKey}
      messages={messages}
      loading={loading}
      onRewindAndSend={onRewindAndSend}
    />
  );
});

interface WindowedMessagesProps {
  messages: RenderMessage[];
  loading: boolean;
  onRewindAndSend?:
    | ((rewindTo: number, input: string) => Promise<void>)
    | undefined;
}

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

function WindowedMessages({
  messages,
  loading,
  onRewindAndSend,
}: WindowedMessagesProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const stickToBottom = useRef(true);
  const didHandleInitialMessages = useRef(false);
  const previousMessageCount = useRef(messages.length);
  const prependSnapshot = useRef<PrependSnapshot | null>(null);
  const layoutMeasureFrame = useRef<number | null>(null);
  const [layoutOptimized, setLayoutOptimized] = useState(false);
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

  const updateStickToBottom = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    stickToBottom.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD;
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

    stickToBottom.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD;

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
  }, [effectiveStartIndex]);

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    el.scrollTop = el.scrollHeight;
    stickToBottom.current = true;
    scheduleLayoutOptimization();
  }, [scheduleLayoutOptimization]);

  useLayoutEffect(() => {
    return () => {
      if (layoutMeasureFrame.current !== null) {
        window.cancelAnimationFrame(layoutMeasureFrame.current);
      }
    };
  }, []);

  useLayoutEffect(() => {
    const snapshot = prependSnapshot.current;
    if (snapshot == null) return;

    const el = containerRef.current;
    if (!el) return;

    el.scrollTop = el.scrollHeight - snapshot.scrollHeight + snapshot.scrollTop;
    prependSnapshot.current = null;
    scheduleLayoutOptimization();
  }, [scheduleLayoutOptimization]);

  useLayoutEffect(() => {
    if (!layoutOptimized || !stickToBottom.current) return;

    const el = containerRef.current;
    if (!el) return;

    el.scrollTop = el.scrollHeight;
    updateStickToBottom();
  }, [layoutOptimized, updateStickToBottom]);

  useLayoutEffect(() => {
    const previousCount = previousMessageCount.current;
    previousMessageCount.current = messages.length;

    if (!didHandleInitialMessages.current) {
      didHandleInitialMessages.current = true;
      return;
    }

    if (!stickToBottom.current) return;

    const el = containerRef.current;
    if (!el) return;

    el.scrollTo({
      top: el.scrollHeight,
      behavior:
        loading || messages.length === previousCount ? "auto" : "smooth",
    });
    updateStickToBottom();
  }, [loading, messages, updateStickToBottom]);

  return (
    <div
      ref={containerRef}
      onScroll={handleScroll}
      className="flex-1 overflow-y-auto pb-4 pt-6 [overflow-anchor:none]"
    >
      <div className="mx-auto max-w-4xl max-md:max-w-none flex flex-col gap-6 max-md:gap-5">
        {visibleMessages.map((message, visibleIndex) => {
          const index = effectiveStartIndex + visibleIndex;
          const renderKey = message.renderKey || `msg-${index}`;

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
            >
              <MessageBubble
                role={message.role}
                blocks={message.content}
                sourceIndex={message.sourceIndex}
                isStreaming={
                  loading &&
                  index === messages.length - 1 &&
                  message.role === "assistant"
                }
                isLoading={loading}
                totalTokens={message.meta?.total_tokens}
                model={message.meta?.model}
                contextWindow={message.meta?.context_window}
                onRewindAndSend={onRewindAndSend}
              />
            </div>
          );
        })}
        <div className="h-4" />
      </div>
    </div>
  );
}
