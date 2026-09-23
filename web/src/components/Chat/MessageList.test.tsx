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

    const scrollContainer = container.firstElementChild
      ?.firstElementChild as HTMLElement;
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

    const scrollContainer = container.firstElementChild
      ?.firstElementChild as HTMLElement;
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

  it("keeps following output that arrives during a smooth jump to the latest", () => {
    scrollHeight = 2_000;
    const props = {
      sessionId: "s",
      loading: true,
      compacting: false,
      compactError: null,
    };
    const { container, rerender } = render(
      <MessageList {...props} messages={history} />,
    );
    flushAnimationFrames();
    const scrollContainer = container.firstElementChild
      ?.firstElementChild as HTMLElement;
    // jsdom fires no scroll events for scrollTop writes and has no scrollTo,
    // so each position is replayed by hand.
    scrollContainer.scrollTo = () => {};
    fireEvent.scroll(scrollContainer);

    scrollContainer.scrollTop = 500;
    fireEvent.scroll(scrollContainer);
    fireEvent.click(screen.getByRole("button", { name: "Scroll to latest" }));
    // The smooth scroll passes through positions still away from the bottom.
    scrollContainer.scrollTop = 900;
    fireEvent.scroll(scrollContainer);

    scrollHeight = 2_600;
    rerender(
      <MessageList
        {...props}
        messages={[
          ...history,
          {
            role: "assistant",
            content: [{ type: "text", text: "More output" }],
            renderKey: "message-80",
            sourceIndex: 80,
          },
        ]}
      />,
    );

    expect(scrollContainer.scrollTop).toBe(2_600);
  });

  it("leaves a reader inside the work in place when the turn stops unfolded", () => {
    scrollHeight = 2_000;
    const turn: RenderMessage = {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: "t1",
          name: "read",
          input: { path: "a.py" },
          renderKey: "t1",
        },
        { type: "text", text: "Partial answer", renderKey: "answer" },
      ],
      renderKey: "message-80",
      sourceIndex: 80,
      meta: { stop_reason: "cancelled" },
    };
    const props = {
      sessionId: "s",
      messages: [...history, turn],
      compacting: false,
      compactError: null,
    };
    const { container, rerender } = render(<MessageList {...props} loading />);
    flushAnimationFrames();
    const scrollContainer = container.firstElementChild
      ?.firstElementChild as HTMLElement;
    fireEvent.scroll(scrollContainer);
    scrollContainer.scrollTop = 500;
    fireEvent.scroll(scrollContainer);
    // The work starts above the container's top: the reader is inside it.
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        return this.hasAttribute("data-work")
          ? new DOMRect(0, -300, 0, 1_000)
          : new DOMRect();
      },
    );

    rerender(<MessageList {...props} loading={false} />);

    expect(scrollContainer.scrollTop).toBe(500);
  });

  it("shows the jump to the latest when content grows below the reader", () => {
    let resize = () => {};
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: () => void) {
          resize = callback;
        }
        observe() {}
        disconnect() {}
      },
    );
    scrollHeight = 400;
    render(
      <MessageList
        sessionId="s"
        messages={history}
        loading={false}
        compacting={false}
        compactError={null}
      />,
    );
    const button = screen.getByLabelText("Scroll to latest");
    expect(button).toHaveAttribute("inert");

    // Expanding work grows the content without scrolling.
    scrollHeight = 1_200;
    act(() => resize());

    expect(button).not.toHaveAttribute("inert");
  });
});
