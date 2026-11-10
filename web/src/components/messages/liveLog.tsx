/**
 * A background command's log, streamed from the log route (`docs/api.md`):
 * followed while the job runs, ending when it exits.
 */

import { useEffect, useState } from "react";

import { readApiError } from "@/api";

type LiveLog =
  | { status: "streaming" | "done"; text: string }
  | { status: "missing" }
  | { status: "error"; message: string };

const RECONNECT_DELAY_MS = 1000;
// The tail the tool itself returns: a running log can outgrow the page.
const TEXT_BUDGET = 50 * 1024;

function keepTail(text: string): string {
  if (text.length <= TEXT_BUDGET) return text;
  // Cut at a line start, so the first line shown is whole.
  const cut = text.indexOf("\n", text.length - TEXT_BUDGET);
  return cut === -1 ? text.slice(-TEXT_BUDGET) : text.slice(cut + 1);
}

/**
 * Stream the log at `url` while it is set; null disconnects and keeps the
 * text read so far. A dropped connection resumes after the bytes already
 * read; one decoder spans the connections, so a character split across them
 * still decodes.
 */
export function useLiveLog(url: string | null): LiveLog {
  const [log, setLog] = useState<LiveLog>({ status: "streaming", text: "" });
  // A new connection keeps the text read so far until new bytes arrive, but
  // not an earlier connection's notice.
  const [connectedUrl, setConnectedUrl] = useState(url);
  if (url !== connectedUrl) {
    setConnectedUrl(url);
    if (url && !("text" in log)) setLog({ status: "streaming", text: "" });
  }

  useEffect(() => {
    if (!url) return;
    const controller = new AbortController();
    const { signal } = controller;
    const decoder = new TextDecoder();
    let received = 0;
    let text = "";

    void (async () => {
      while (!signal.aborted) {
        try {
          const res = await fetch(`${url}?after=${received}`, { signal });
          if (res.status === 404) {
            setLog({ status: "missing" });
            return;
          }
          if (!res.ok || !res.body) {
            const error = await readApiError(res, `Failed to load the log (${res.status})`);
            setLog({ status: "error", message: error.message });
            return;
          }
          const reader = res.body.getReader();
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            received += value.byteLength;
            text = keepTail(text + decoder.decode(value, { stream: true }));
            setLog({ status: "streaming", text });
          }
          text = keepTail(text + decoder.decode());
          setLog({ status: "done", text });
          return;
        } catch {
          if (signal.aborted) return;
          await new Promise((resolve) => window.setTimeout(resolve, RECONNECT_DELAY_MS));
        }
      }
    })();
    return () => controller.abort();
  }, [url]);

  return log;
}

/** The log's text, or a quiet line saying why there is none. */
export function LiveLogText({ log }: { log: LiveLog }) {
  if ((log.status === "streaming" || log.status === "done") && log.text) return log.text;
  const notice =
    log.status === "missing"
      ? "No log for this command."
      : log.status === "error"
        ? log.message
        : log.status === "done"
          ? "No output."
          : "Waiting for output…";
  return <span className="font-sans text-xs text-muted-foreground/70">{notice}</span>;
}
