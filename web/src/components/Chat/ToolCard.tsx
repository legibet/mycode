/**
 * Tool execution display.
 * Zero container. Two-tier typography: tool name (sans medium, foreground)
 * vs everything else (mono regular, muted-foreground). Click the row text
 * to toggle. Per-tool bodies (bash / read / write / edit / generic) are
 * preserved.
 */

import { Terminal } from "lucide-react";
import { memo, type ReactNode, useMemo, useState } from "react";

import { Collapse } from "@/components/ui/collapse";

import { cn } from "../../utils/cn";
import { type HunkContext, hunksFromEdits, parsePatch } from "../../utils/diff";
import { EditDiff } from "./EditDiff";
import { TOOL_ICON } from "./toolIcons";

// Tool inputs/outputs are JSON from the model; treat fields as `unknown` and
// type-check at the read site instead of trusting the shape with `as`.
type Args = Record<string, unknown> | undefined;
type Meta = Record<string, unknown> | null | undefined;

interface BashArgs {
  command?: unknown;
}
interface BashMeta {
  background?: unknown;
  /** The started job's state, added by the projection: running, or its exit code. */
  running?: unknown;
  exit_code?: unknown;
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
interface EditEntry {
  oldText?: unknown;
  newText?: unknown;
}
interface HighlightContextEntry {
  old?: unknown;
  new?: unknown;
}
interface EditMeta {
  patch?: unknown;
  highlight_context?: unknown;
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

function getEdits(args: Args): { oldText: string; newText: string }[] {
  const edits = (args as EditArgs | undefined)?.edits;
  if (!Array.isArray(edits)) return [];
  return edits.map((edit: EditEntry | null | undefined) => ({
    oldText: asString(edit?.oldText),
    newText: asString(edit?.newText),
  }));
}

function getEditPatch(metadata: Meta): string | null {
  const patch = (metadata as EditMeta | null | undefined)?.patch;
  return typeof patch === "string" && patch ? patch : null;
}

function getHighlightContext(metadata: Meta): HunkContext[] {
  const context = (metadata as EditMeta | null | undefined)?.highlight_context;
  if (!Array.isArray(context)) return [];
  return context.map((entry: HighlightContextEntry | null | undefined) => ({
    old: asString(entry?.old),
    new: asString(entry?.new),
  }));
}

function getEditStats(metadata: Meta): { added: number; removed: number } | null {
  const meta = metadata as EditMeta | null | undefined;
  const added = asNumber(meta?.added_lines);
  const removed = asNumber(meta?.removed_lines);
  if (added == null || removed == null) return null;
  return { added, removed };
}

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
// one fill, no internal rule. Input and output are told apart by text colour,
// the way a terminal does it; a rule has to out-shout the fill to be seen, and
// a second fill turns one surface into a titlebar plus a content pane.
// Diffs get their own surface (EditDiff.tsx): tinted rows run edge to edge.
// ---------------------------------------------------------------------------

function ToolSurface({ head, body }: { head?: ReactNode; body?: ReactNode }) {
  if (!head && !body) return null;
  return (
    <div className="overflow-hidden rounded-md bg-code font-mono text-ui shadow-hairline">
      {head && (
        <div className="scrollbar-subtle max-h-60 overflow-auto px-3 py-2 leading-normal">
          {head}
        </div>
      )}
      {body && (
        <div className="scrollbar-subtle max-h-60 overflow-auto px-3 py-2 leading-relaxed whitespace-pre-wrap text-muted-foreground">
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
        if (typeof value === "string") values.push(value);
        else values.push(typeof value === "object" ? "…" : JSON.stringify(value));
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

/**
 * A status beside a mono preview. Sans type on a muted fill keeps it apart
 * from the command text, which the muted mono suffixes blend into.
 */
export function StatusPill({
  tone = "muted",
  pulse = false,
  children,
}: {
  tone?: "muted" | "error";
  pulse?: boolean;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "shrink-0 rounded px-1.5 py-px text-2xs leading-4 tabular-nums",
        tone === "error" ? "bg-destructive/10 text-destructive" : "bg-muted text-muted-foreground",
        pulse && "animate-thinking",
      )}
    >
      {children}
    </span>
  );
}

function CollapsedSuffix({ name, args, metadata }: { name: string; args: Args; metadata: Meta }) {
  if (name === "edit") {
    const stats = getEditStats(metadata);
    if (!stats || (stats.added === 0 && stats.removed === 0)) return null;
    return (
      <span className="shrink-0 font-mono text-xs tabular-nums">
        <span className="text-diff-added">+{stats.added}</span>
        <span className="ml-1 text-diff-removed">−{stats.removed}</span>
      </span>
    );
  }

  if (name === "bash") {
    const meta = metadata as BashMeta | null | undefined;
    if (meta?.background !== true) return null;
    const exitCode = asNumber(meta.exit_code);
    return (
      <StatusPill tone={exitCode ? "error" : "muted"} pulse={meta.running === true}>
        {exitCode != null
          ? `background · exit ${exitCode}`
          : meta.running === true
            ? "background · running"
            : "background"}
      </StatusPill>
    );
  }

  if (name === "websearch") {
    const results = asNumber((metadata as WebSearchMeta | null | undefined)?.results);
    if (results == null) return null;
    return (
      <span className="shrink-0 font-mono text-xs text-muted-foreground/60 tabular-nums">
        {results} results
      </span>
    );
  }

  const hint = name === "read" ? getReadHint(args) : name === "write" ? getWriteHint(args) : "";
  if (!hint) return null;
  return (
    <span className="shrink-0 font-mono text-xs text-muted-foreground/60 tabular-nums">{hint}</span>
  );
}

// ---------------------------------------------------------------------------
// Expanded body components — one per tool type
// ---------------------------------------------------------------------------

export function BashBody({ args, display }: { args: Args; display: string }) {
  const command = asString((args as BashArgs | undefined)?.command);

  return (
    <ToolSurface
      head={
        command && (
          <>
            <span className="text-muted-foreground/40 select-none">$ </span>
            <span className="break-all whitespace-pre-wrap text-foreground">{command}</span>
          </>
        )
      }
      body={display}
    />
  );
}

function WriteBody({ args, display, isError }: { args: Args; display: string; isError: boolean }) {
  const content = asString((args as WriteArgs | undefined)?.content);

  return (
    <ToolSurface
      head={content && <span className="whitespace-pre-wrap text-foreground/75">{content}</span>}
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
  // The patch exists once the edit has applied; until then (or on failure)
  // show the requested replacements.
  const hunks = useMemo(() => {
    const patch = getEditPatch(metadata);
    return patch
      ? parsePatch(patch, getHighlightContext(metadata))
      : hunksFromEdits(getEdits(args));
  }, [args, metadata]);

  if (hunks.length) {
    return (
      <div className="flex flex-col gap-2">
        <EditDiff path={asString((args as PathArgs | undefined)?.path)} hunks={hunks} />
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
      <span className="text-primary/80">{key}: </span>
      <span className="break-all whitespace-pre-wrap text-foreground/75">
        {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
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
    typeof finalOutput === "string" ? finalOutput : typeof output === "string" ? output : "";
  const resolvedIsError =
    Boolean(isError) || (typeof finalOutput === "string" && finalOutput.startsWith("error:"));
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
      <EditBody args={args} metadata={metadata} display={display} isError={resolvedIsError} />
    ) : (
      <ToolSurface
        head={args && Object.keys(args).length > 0 && <GenericArgs args={args} />}
        body={display}
      />
    );

  return (
    <div className="group/tool">
      <button
        type="button"
        className="flex w-full cursor-pointer items-center gap-1.5 rounded-sm text-left select-none focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset"
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />

        <span
          className={cn(
            "shrink-0 text-ui tracking-tight transition-colors duration-200",
            status === "error"
              ? "text-destructive/90 group-hover/tool:text-destructive"
              : "text-foreground/90 group-hover/tool:text-foreground",
            status === "pending" && "animate-thinking",
          )}
        >
          {name}
        </span>

        {preview && (
          <span className="min-w-0 truncate font-mono text-ui text-muted-foreground/60">
            {preview}
          </span>
        )}

        <CollapsedSuffix name={name} args={args} metadata={metadata} />
      </button>

      <Collapse open={expanded}>
        {/* Plain indent: the body is already a bounded surface, so a rail
            beside it would just be a second parallel line. */}
        <div className="mt-2 ml-5">{body}</div>
      </Collapse>
    </div>
  );
});
