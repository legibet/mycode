import { act, render, screen } from "@testing-library/react";
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
  let nextFrameId = 1;
  let animationFrames = new Map<number, FrameRequestCallback>();

  beforeEach(() => {
    scrollHeight = 0;
    nextFrameId = 1;
    animationFrames = new Map();

    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(
      () => scrollHeight,
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
});
