/** Tool permission prompt panel mounted above the input area. */

import {
  FileText,
  type LucideIcon,
  PenLine,
  SquarePen,
  Terminal,
} from "lucide-react";
import { type KeyboardEvent, memo, useCallback } from "react";
import type { PermissionRequest } from "../../types";

const TOOL_ICON: Record<string, LucideIcon> = {
  bash: Terminal,
  read: FileText,
  write: PenLine,
  edit: SquarePen,
};

interface PermissionPromptProps {
  request: PermissionRequest;
  onDecide: (decision: "allow" | "deny") => void;
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
  const isBash = request.tool_name === "bash";

  return (
    <div className="mx-auto max-w-4xl max-md:max-w-none px-5 max-md:px-3 pt-3 max-md:pt-2 pb-1">
      <div
        key={request.request_id}
        ref={setDialogElement}
        role="dialog"
        aria-label="Tool permission request"
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        className="rounded-lg border border-border bg-card shadow-sm animate-fade-in-up focus:outline-none"
      >
        <div className="flex items-center gap-2 px-3.5 pt-3 pb-2">
          <Icon
            className="size-3.5 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
          <span className="text-sm text-foreground/90">
            Run <span className="font-medium">{request.tool_name}</span>?
          </span>
        </div>

        {request.preview && (
          <div className="px-3.5 pb-2.5">
            <div className="rounded-md bg-code px-3 py-2 font-mono text-[12.5px] leading-normal text-foreground/80 whitespace-pre-wrap break-all max-h-28 overflow-y-auto scrollbar-subtle">
              {isBash && (
                <span className="text-muted-foreground/40 select-none">$ </span>
              )}
              {request.preview}
            </div>
          </div>
        )}

        <div className="flex items-center justify-end gap-1 px-2 pb-2">
          <button
            type="button"
            onClick={() => onDecide("allow")}
            className="h-7 px-3 rounded-md text-[12.5px] font-medium bg-accent text-accent-foreground hover:opacity-90 active:scale-95 transition-[opacity,scale] duration-150"
          >
            Allow
          </button>
          <button
            type="button"
            onClick={() => onDecide("deny")}
            className="h-7 px-3 rounded-md text-[12.5px] text-muted-foreground hover:text-foreground hover:bg-muted/70 active:scale-95 transition-[color,background-color,scale] duration-150"
          >
            Deny
          </button>
        </div>
      </div>
    </div>
  );
});
