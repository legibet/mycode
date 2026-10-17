/**
 * Keeps the reader in place while a just-finished turn folds its work. The
 * fold commits with the end of streaming, or mid-run when a delivered steer
 * starts a new segment and the previous assistant stops streaming, so the
 * reader's position has to be read before React updates the DOM, which only
 * getSnapshotBeforeUpdate can do. The pin is re-applied on each resize until
 * the fold's transitions end or the user scrolls.
 */

import { Component, type ReactNode, type RefObject } from "react";

interface SettleSnapshot {
  /** Work of the turn segment that just stopped streaming. */
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
  /** Render key of the streaming assistant; null when none streams. */
  streamingKey: string | null;
  containerRef: RefObject<HTMLDivElement | null>;
  followOutputRef: RefObject<boolean>;
  children: ReactNode;
}

export class SettleAnchor extends Component<SettleAnchorProps> {
  private stopSettle: (() => void) | null = null;

  getSnapshotBeforeUpdate(prevProps: SettleAnchorProps): SettleSnapshot | null {
    const el = this.props.containerRef.current;
    if (
      !el ||
      prevProps.streamingKey === null ||
      prevProps.streamingKey === this.props.streamingKey
    ) {
      return null;
    }
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
