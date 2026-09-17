import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RenderMessage } from "../../types";
import { MessageList } from "./MessageList";

const history: RenderMessage[] = Array.from({ length: 80 }, (_, index) => ({
  role: index % 2 === 0 ? "user" : "assistant",
  content: [
    {
      type: "text",
      text: index === 79 ? "Newest message" : `Message ${index + 1}`,
    },
  ],
  renderKey: `message-${index}`,
  sourceIndex: index,
}));

describe("MessageList", () => {
  let scrollHeight = 0;
  let scrollHeightForElement: ((element: HTMLElement) => number) | null = null;
  let nextFrameId = 1;
  let animationFrames = new Map<number, FrameRequestCallback>();

  beforeEach(() => {
    scrollHeight = 0;
    scrollHeightForElement = null;
    nextFrameId = 1;
    animationFrames = new Map();

    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(
      function (this: HTMLElement) {
        return scrollHeightForElement?.(this) ?? scrollHeight;
      },
    );
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(400);
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      const id = nextFrameId++;
      animationFrames.set(id, callback);
      return id;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => {
      animationFrames.delete(id);
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function flushAnimationFrames() {
    const callbacks = Array.from(animationFrames.values());
    animationFrames.clear();
    act(() => {
      for (const callback of callbacks) callback(0);
    });
  }

  it("shows the newest message after a long session finishes loading", () => {
    const props = {
      sessionId: "long-session",
      loading: false,
      compacting: false,
      compactError: null,
    };
    const { container, rerender } = render(
      <MessageList {...props} messages={[]} />,
    );

    flushAnimationFrames();

    scrollHeight = 1_000;
    rerender(<MessageList {...props} messages={history} />);
    expect(screen.getByText("Newest message")).toBeInTheDocument();

    scrollHeight = 2_400;
    flushAnimationFrames();

    const scrollContainer = container.firstElementChild as HTMLElement;
    expect(scrollContainer.scrollTop).toBe(scrollContainer.scrollHeight);
  });

  it("preserves the viewport when older messages are prepended", () => {
    const messageHeight = 40;
    scrollHeightForElement = (element) =>
      element.querySelectorAll(".chat-message-shell").length * messageHeight;

    const { container } = render(
      <MessageList
        sessionId="long-session"
        messages={history}
        loading={false}
        compacting={false}
        compactError={null}
      />,
    );
    flushAnimationFrames();

    const scrollContainer = container.firstElementChild as HTMLElement;
    const renderedBefore = container.querySelectorAll(
      ".chat-message-shell",
    ).length;
    const scrollHeightBefore = scrollContainer.scrollHeight;

    scrollContainer.scrollTop = 100;
    fireEvent.scroll(scrollContainer);

    const renderedAfter = container.querySelectorAll(
      ".chat-message-shell",
    ).length;
    expect(renderedAfter).toBeGreaterThan(renderedBefore);
    expect(scrollContainer.scrollTop).toBe(
      100 + scrollContainer.scrollHeight - scrollHeightBefore,
    );
  });
});
