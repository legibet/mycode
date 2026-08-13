/**
 * Tool execution display.
 * Zero container. Two-tier typography: tool name (sans medium, foreground)
 * vs everything else (mono regular, muted-foreground). Click the row text
 * to toggle. Per-tool bodies (bash / read / write / edit / generic) are
 * preserved.
 */

import {
  FileText,
  Globe,
  type LucideIcon,
  PenLine,
  Search,
  SquarePen,
  Terminal,
} from "lucide-react";
import { lazy, memo, type ReactNode, Suspense, useState } from "react";
import { cn } from "../../utils/cn";

const EditDiff = lazy(() => import("./EditDiff"));

interface EditEntry {
  oldText: string;
  newText: string;
}

// Tool inputs/outputs are JSON from the model; treat fields as `unknown` and
// type-check at the read site instead of trusting the shape with `as`.
type Args = Record<string, unknown> | undefined;
type Meta = Record<string, unknown> | null | undefined;

interface BashArgs {
  command?: unknown;
}
interface PathArgs {
  path?: unknown;
}
interface ReadArgs {
  offset?: unknown;
  limit?: unknown;
}
interface WriteArgs {
  content?: unknown;
}
interface EditArgs {
  edits?: unknown;
}
interface EditMeta {
  patch?: unknown;
  added_lines?: unknown;
  removed_lines?: unknown;
}
interface WebFetchArgs {
  url?: unknown;
}
interface WebSearchArgs {
  query?: unknown;
}
interface WebSearchMeta {
  results?: unknown;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function getEdits(args: Args): EditEntry[] | null {
  const edits = (args as EditArgs | undefined)?.edits;
  return Array.isArray(edits) ? (edits as EditEntry[]) : null;
}

function getEditPatch(metadata: Meta): string | null {
  const patch = (metadata as EditMeta | null | undefined)?.patch;
  return typeof patch === "string" && patch ? patch : null;
}

function getEditStats(
  metadata: Meta,
): { added: number; removed: number } | null {
  const meta = metadata as EditMeta | null | undefined;
  const added = asNumber(meta?.added_lines);
  const removed = asNumber(meta?.removed_lines);
  if (added == null || removed == null) return null;
  return { added, removed };
}

function EditDiffFallback({ edits }: { edits: EditEntry[] }) {
  return (
    <div className="rounded-md bg-code shadow-hairline px-3 py-2 font-mono text-[13px] leading-normal overflow-x-auto scrollbar-subtle whitespace-pre-wrap">
      {edits.map((entry, i) => (
        <div key={i}>
          {i > 0 && (
            <div className="text-center text-muted-foreground/20 select-none text-xs py-0.5">
              ···
            </div>
          )}
          {entry.oldText && (
            <div className="diff-line-removed px-1">{entry.oldText}</div>
          )}
          {entry.newText && (
            <div className="diff-line-added px-1">{entry.newText}</div>
          )}
        </div>
      ))}
    </div>
  );
}

const TOOL_ICON: Record<string, LucideIcon> = {
  bash: Terminal,
  read: FileText,
  write: PenLine,
  edit: SquarePen,
  webfetch: Globe,
  websearch: Search,
};

interface ToolCardProps {
  name: string;
  args?: Record<string, unknown>;
  output?: string | null | undefined;
  finalOutput?: string | null | undefined;
  metadata?: Record<string, unknown> | null | undefined;
  pending?: boolean | undefined;
  isError?: boolean | undefined;
}

// ---------------------------------------------------------------------------
// Shared code surface for text-based tools — one execution reads as one block:
// input on top, output below, split by a hairline rule instead of a gap.
// Diffs are not text: EditDiff owns its own container (see EditDiff.tsx).
// ---------------------------------------------------------------------------

function ToolSurface({ head, body }: { head?: ReactNode; body?: ReactNode }) {
  if (!head && !body) return null;
  return (
    <div className="rounded-md bg-code shadow-hairline overflow-hidden font-mono text-[13px]">
      {head && (
        <div className="px-3 py-2 leading-normal max-h-60 overflow-auto scrollbar-subtle">
          {head}
        </div>
      )}
      {body && (
        <div
          className={cn(
            "px-3 py-2 leading-relaxed text-muted-foreground whitespace-pre-wrap overflow-auto scrollbar-subtle max-h-[240px]",
            head && "border-t border-border/60",
          )}
        >
          {body}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers for trigger-line previews and collapsed suffixes
// ---------------------------------------------------------------------------

function getPreview(name: string, args: Args): string {
  if (!args) return "";
  switch (name) {
    case "bash":
      return asString((args as BashArgs).command);
    case "read":
    case "write":
    case "edit":
      return asString((args as PathArgs).path);
    case "webfetch":
      return asString((args as WebFetchArgs).url);
    case "websearch":
      return asString((args as WebSearchArgs).query);
    default: {
      const values: string[] = [];
      for (const [key, value] of Object.entries(args)) {
        if (key === "content" || key === "prompt") continue;
        values.push(typeof value === "object" ? "…" : String(value));
      }
      return values.join(" ");
    }
  }
}

function getReadHint(args: Args): string {
  const a = args as ReadArgs | undefined;
  const offset = asNumber(a?.offset);
  const limit = asNumber(a?.limit);
  if (offset != null && limit != null) return `:${offset}-${offset + limit}`;
  if (offset != null) return `:${offset}`;
  if (limit != null) return `:1-${limit}`;
  return "";
}

function getWriteHint(args: Args): string {
  const content = asString((args as WriteArgs | undefined)?.content);
  if (!content) return "";
  return `${content.split("\n").length} lines`;
}

function CollapsedSuffix({
  name,
  args,
  metadata,
}: {
  name: string;
  args: Args;
  metadata: Meta;
}) {
  if (name === "edit") {
    const stats = getEditStats(metadata);
    if (!stats || (stats.added === 0 && stats.removed === 0)) return null;
    return (
      <span className="shrink-0 text-[12px] font-mono tabular-nums">
        <span className="text-diff-added">+{stats.added}</span>
        <span className="text-diff-removed ml-1">−{stats.removed}</span>
      </span>
    );
  }

  if (name === "websearch") {
    const results = asNumber(
      (metadata as WebSearchMeta | null | undefined)?.results,
    );
    if (results == null) return null;
    return (
      <span className="shrink-0 text-[12px] font-mono tabular-nums text-muted-foreground/60">
        {results} results
      </span>
    );
  }

  const hint =
    name === "read"
      ? getReadHint(args)
      : name === "write"
        ? getWriteHint(args)
        : "";
  if (!hint) return null;
  return (
    <span className="shrink-0 text-[12px] font-mono tabular-nums text-muted-foreground/60">
      {hint}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Expanded body components — one per tool type
// ---------------------------------------------------------------------------

function BashBody({ args, display }: { args: Args; display: string }) {
  const command = asString((args as BashArgs | undefined)?.command);

  return (
    <ToolSurface
      head={
        command && (
          <>
            <span className="text-muted-foreground/40 select-none">$ </span>
            <span className="text-foreground/75 whitespace-pre-wrap break-all">
              {command}
            </span>
          </>
        )
      }
      body={display}
    />
  );
}

function WriteBody({
  args,
  display,
  isError,
}: {
  args: Args;
  display: string;
  isError: boolean;
}) {
  const content = asString((args as WriteArgs | undefined)?.content);

  return (
    <ToolSurface
      head={
        content && (
          <span className="text-foreground/75 whitespace-pre-wrap">
            {content}
          </span>
        )
      }
      body={isError ? display : null}
    />
  );
}

function EditBody({
  args,
  metadata,
  display,
  isError,
}: {
  args: Args;
  metadata: Meta;
  display: string;
  isError: boolean;
}) {
  const edits = getEdits(args);
  if (edits?.length) {
    const patch = getEditPatch(metadata);
    return (
      <div className="space-y-2">
        {patch ? (
          <Suspense fallback={<EditDiffFallback edits={edits} />}>
            <EditDiff patch={patch} />
          </Suspense>
        ) : (
          <EditDiffFallback edits={edits} />
        )}
        {isError && <ToolSurface body={display} />}
      </div>
    );
  }

  return (
    <ToolSurface
      head={args && Object.keys(args).length > 0 && <GenericArgs args={args} />}
      body={display}
    />
  );
}

function GenericArgs({ args }: { args: Record<string, unknown> }) {
  return Object.entries(args).map(([key, value]) => (
    <div key={key}>
      <span className="text-accent/80">{key}: </span>
      <span className="text-foreground/75 break-all whitespace-pre-wrap">
        {typeof value === "object"
          ? JSON.stringify(value, null, 2)
          : String(value)}
      </span>
    </div>
  ));
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export const ToolCard = memo(function ToolCard({
  name,
  args,
  output,
  finalOutput,
  metadata,
  pending,
  isError,
}: ToolCardProps) {
  const display =
    typeof finalOutput === "string"
      ? finalOutput
      : typeof output === "string"
        ? output
        : "";
  const resolvedIsError =
    Boolean(isError) ||
    (typeof finalOutput === "string" && finalOutput.startsWith("error:"));
  const [expanded, setExpanded] = useState(false);

  const status = pending ? "pending" : resolvedIsError ? "error" : "success";

  const Icon = TOOL_ICON[name] ?? Terminal;
  const preview = getPreview(name, args);

  const body =
    name === "bash" ? (
      <BashBody args={args} display={display} />
    ) : name === "read" || name === "webfetch" || name === "websearch" ? (
      <ToolSurface body={display} />
    ) : name === "write" ? (
      <WriteBody args={args} display={display} isError={resolvedIsError} />
    ) : name === "edit" ? (
      <EditBody
        args={args}
        metadata={metadata}
        display={display}
        isError={resolvedIsError}
      />
    ) : (
      <ToolSurface
        head={
          args && Object.keys(args).length > 0 && <GenericArgs args={args} />
        }
        body={display}
      />
    );

  return (
    <div className="group/tool">
      <button
        type="button"
        className="flex w-full items-center gap-1.5 select-none cursor-pointer text-left"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <Icon
          className="size-3.5 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />

        <span
          className={cn(
            "text-[13px] shrink-0 tracking-tight transition-colors duration-200",
            status === "error"
              ? "text-destructive/90 group-hover/tool:text-destructive"
              : "text-foreground/90 group-hover/tool:text-foreground",
            status === "pending" && "animate-thinking",
          )}
        >
          {name}
        </span>

        {preview && (
          <span className="min-w-0 text-[13px] font-mono text-muted-foreground/60 truncate">
            {preview}
          </span>
        )}

        <CollapsedSuffix name={name} args={args} metadata={metadata} />
      </button>

      <div
        data-expanded={expanded}
        className={cn(
          "chat-collapsible-body grid transition-[grid-template-rows,opacity] duration-250 ease-out-strong",
          expanded
            ? "grid-rows-[1fr] opacity-100"
            : "grid-rows-[0fr] opacity-0",
        )}
      >
        <div className="overflow-hidden">
          <div className="mt-2 ml-5">{body}</div>
        </div>
      </div>
    </div>
  );
});
