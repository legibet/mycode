/**
 * Tool permission prompt panel mounted above the input area.
 *
 * Per-tool differences live in the icon, the title sentence and how the
 * payload is typeset; the shell never changes.
 */

import {
  CornerDownLeft,
  FileText,
  Globe,
  type LucideIcon,
  PenLine,
  Search,
  SquarePen,
  Terminal,
} from "lucide-react";
import { type KeyboardEvent, memo, useCallback } from "react";
import type { PermissionRequest } from "../../types";
import { cn } from "../../utils/cn";

const TOOL_ICON: Record<string, LucideIcon> = {
  bash: Terminal,
  read: FileText,
  write: PenLine,
  edit: SquarePen,
  webfetch: Globe,
  websearch: Search,
};

const TOOL_TITLE: Record<string, string> = {
  bash: "Run this command?",
  read: "Read this file?",
  write: "Write this file?",
  edit: "Edit this file?",
  webfetch: "Fetch this page?",
  websearch: "Run this search?",
};

interface PermissionPromptProps {
  request: PermissionRequest;
  onDecide: (decision: "allow" | "deny") => void;
}

/**
 * A tray holding the exact thing being approved. Fill only, no hairline: this
 * is the one place a code surface sits inside an already-outlined card, and two
 * nested borders is what makes a panel read as a box in a box. Elsewhere
 * (CodeBlock, ToolSurface, EditDiff) it sits bare on the message background and
 * keeps its hairline.
 *
 * A command is code and can run long. A query is prose, not code. Anything else
 * is one identifier (path, URL, tool name) that has to wrap whole, since an
 * ellipsis would eat the part the decision turns on.
 */
function Payload({ toolName, preview }: { toolName: string; preview: string }) {
  const isCommand = toolName === "bash";
  const isQuery = toolName === "websearch";

  return (
    <div
      className={cn(
        "rounded-md bg-code px-3 py-2 leading-normal text-foreground/80",
        isQuery ? "text-[13px]" : "font-mono text-[12.5px]",
        isCommand &&
          "max-h-24 overflow-y-auto scrollbar-subtle whitespace-pre-wrap break-words",
        !isCommand && !isQuery && "break-all",
      )}
    >
      {isCommand && (
        <span className="select-none text-muted-foreground/40">$ </span>
      )}
      {isQuery ? `“${preview}”` : preview}
    </div>
  );
}

export const PermissionPrompt = memo(function PermissionPrompt({
  request,
  onDecide,
}: PermissionPromptProps) {
  const setDialogElement = useCallback((el: HTMLDivElement | null) => {
    el?.focus();
  }, []);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      onDecide("allow");
    } else if (event.key === "Escape") {
      event.preventDefault();
      onDecide("deny");
    }
  };

  const Icon = TOOL_ICON[request.tool_name] ?? Terminal;
  const title = TOOL_TITLE[request.tool_name] ?? "Run this tool?";

  return (
    <div className="mx-auto max-w-4xl max-md:max-w-none px-5 max-md:px-3 pt-3 max-md:pt-2 pb-1">
      <div
        key={request.request_id}
        ref={setDialogElement}
        role="dialog"
        aria-label="Tool permission request"
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        className="flex flex-col gap-3 rounded-lg bg-card px-3.5 py-3 shadow-card animate-fade-in-up focus:outline-none"
      >
        <div className="flex items-center gap-2">
          {/* Gives the header row enough weight to hold its end of the panel,
              and says which tool is asking. */}
          <span className="grid size-6 shrink-0 place-items-center rounded-md bg-accent/10 text-accent">
            <Icon className="size-3.5" aria-hidden="true" />
          </span>
          <span className="text-sm text-foreground/90">{title}</span>
        </div>

        {request.preview && (
          <Payload toolName={request.tool_name} preview={request.preview} />
        )}

        <div className="flex items-center justify-end gap-1">
          <button
            type="button"
            onClick={() => onDecide("allow")}
            className="inline-flex h-7 items-center gap-1.5 rounded-md bg-accent px-3 text-[12.5px] font-medium text-accent-foreground transition-[filter,scale] duration-150 hover:brightness-105 hover:saturate-[.9] active:scale-95"
          >
            Allow
            <CornerDownLeft className="size-3 opacity-70" aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={() => onDecide("deny")}
            className="inline-flex h-7 items-center gap-1.5 rounded-md bg-muted/70 px-3 text-[12.5px] text-muted-foreground transition-[color,background-color,scale] duration-150 hover:bg-muted hover:text-foreground active:scale-95"
          >
            Deny
            {/* No color of its own: it inherits the button's, so the hint
                lightens together with the label on hover. */}
            <span
              className="text-[10px] uppercase tracking-wider"
              aria-hidden="true"
            >
              esc
            </span>
          </button>
        </div>
      </div>
    </div>
  );
});
