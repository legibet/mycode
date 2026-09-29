/**
 * Session search dialog.
 *
 * Searches titles and visible message text of the workspace's sessions via
 * `GET /api/sessions/search`. The query is debounced, the previous request is
 * aborted on every change, and the last results stay on screen while the next
 * query loads.
 */

import { useEffect, useRef, useState } from "react";
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import type { SessionSearchHit, SessionSearchResponse } from "../types";
import { formatShortDate, parseDate } from "../utils/format";

const DEBOUNCE_MS = 250;
// Longest `before` context kept so the match stays visible on one line, even
// with wide CJK glyphs.
const BEFORE_MAX = 24;

interface SessionSearchProps {
  open: boolean;
  onClose: () => void;
  cwd: string;
  activeSessionId: string | undefined;
  onSelect: (id: string) => void;
}

export function SessionSearch({
  open,
  onClose,
  cwd,
  activeSessionId,
  onSelect,
}: SessionSearchProps) {
  const isDesktop = useMediaQuery("(min-width: 640px)");
  const inputRef = useRef<HTMLInputElement | null>(null);

  const handleOpenChange = (next: boolean) => {
    if (!next) onClose();
  };

  // The popup unmounts on close, so the panel's query state resets on reopen.
  const panel = (
    <SearchPanel
      inputRef={inputRef}
      cwd={cwd}
      activeSessionId={activeSessionId}
      onSelect={(id) => {
        onSelect(id);
        onClose();
      }}
    />
  );

  if (isDesktop) {
    return (
      <Dialog open={open} onOpenChange={handleOpenChange}>
        <DialogContent
          showCloseButton={false}
          initialFocus={inputRef}
          className="flex flex-col gap-0 p-0 w-110 max-w-[calc(100vw-2rem)] max-h-[min(32.5rem,100dvh-2rem)] sm:max-w-[calc(100vw-2rem)]"
        >
          <DialogTitle className="sr-only">Search chats</DialogTitle>
          {panel}
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent
        side="bottom"
        showCloseButton={false}
        initialFocus={inputRef}
        className="flex flex-col gap-0 p-0 max-h-[82vh] rounded-t-2xl"
      >
        <SheetTitle className="sr-only">Search chats</SheetTitle>
        <div
          className="flex justify-center pt-2.5 pb-1 shrink-0"
          aria-hidden="true"
        >
          <div className="w-10 h-0.75 rounded-full bg-border/60" />
        </div>
        {panel}
      </SheetContent>
    </Sheet>
  );
}

interface SearchState {
  query: string;
  results: SessionSearchHit[];
  failed: boolean;
}

function SearchPanel({
  inputRef,
  cwd,
  activeSessionId,
  onSelect,
}: {
  inputRef: React.RefObject<HTMLInputElement | null>;
  cwd: string;
  activeSessionId: string | undefined;
  onSelect: (id: string) => void;
}) {
  const [input, setInput] = useState("");
  const [search, setSearch] = useState<SearchState | null>(null);
  const [selected, setSelected] = useState("");
  const query = input.trim();

  useEffect(() => {
    if (!query) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const params = new URLSearchParams({ q: query, cwd });
        const res = await fetch(`/api/sessions/search?${params}`, {
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(`Search failed: ${res.status}`);
        const data = (await res.json()) as SessionSearchResponse;
        if (controller.signal.aborted) return;
        setSearch({ query, results: data.results, failed: false });
      } catch {
        if (controller.signal.aborted) return;
        setSearch({ query, results: [], failed: true });
      }
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, cwd]);

  const loading = Boolean(query) && search?.query !== query;
  const results = query ? (search?.results ?? []) : [];
  const failed = !loading && Boolean(search?.failed);
  // Earlier results stay visible while the next query loads, but dimmed and
  // not selectable, so Enter never opens a hit from a query that is gone.
  const stale = loading && results.length > 0;
  // cmdk keeps a vanished selection after the list is replaced; fall back to
  // the first result so Enter always has a target.
  const value = results.some(({ session }) => session.id === selected)
    ? selected
    : (results[0]?.session.id ?? "");

  let status = "";
  if (!query) status = "Type to search titles and messages";
  else if (failed) status = "Search failed";
  else if (loading && results.length === 0) status = "searching…";

  return (
    <Command
      shouldFilter={false}
      value={value}
      onValueChange={setSelected}
      className="flex-1 min-h-0 rounded-none! bg-transparent"
    >
      <CommandInput
        ref={inputRef}
        value={input}
        onValueChange={setInput}
        placeholder="Search chats in this workspace"
        spellCheck={false}
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        className="text-base sm:text-sm caret-accent placeholder:text-muted-foreground/40"
      />
      <CommandList className="flex-1 min-h-0 max-h-none mt-1 overscroll-contain">
        {status && (
          <div className="px-2 py-6 text-center text-[12px] text-muted-foreground/60">
            {status}
          </div>
        )}
        {query && !loading && !failed && (
          <CommandEmpty className="py-6 text-[12px] text-muted-foreground/60">
            No matching chats
          </CommandEmpty>
        )}
        {results.map(({ session, snippet }) => {
          const date =
            parseDate(session.updated_at) || parseDate(session.created_at);
          const isActive = session.id === activeSessionId;
          return (
            <CommandItem
              key={session.id}
              value={session.id}
              disabled={stale}
              onSelect={() => onSelect(session.id)}
              aria-current={isActive ? "true" : undefined}
              className="flex-col items-stretch gap-0.5 px-3 py-2"
            >
              {isActive && (
                <span
                  className="absolute left-0 top-2 bottom-2 w-0.5 rounded-r bg-accent"
                  aria-hidden="true"
                />
              )}
              <div className="flex items-center gap-2 min-w-0">
                <span className="truncate flex-1 text-[13px] text-foreground">
                  {session.title || "New Chat"}
                </span>
                {date && (
                  <span className="shrink-0 font-mono text-[10px] text-muted-foreground/55">
                    {formatShortDate(date)}
                  </span>
                )}
              </div>
              {snippet && (
                <div className="truncate text-[12px] text-muted-foreground">
                  {snippet.before.length > BEFORE_MAX
                    ? `…${snippet.before.slice(-BEFORE_MAX)}`
                    : snippet.before}
                  <mark className="bg-transparent text-accent">
                    {snippet.match}
                  </mark>
                  {snippet.after}
                </div>
              )}
            </CommandItem>
          );
        })}
      </CommandList>
    </Command>
  );
}
