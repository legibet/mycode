/**
 * Readers for tool inputs and result metadata. Both are JSON from the model
 * or the tool; fields are `unknown` and type-checked where they are read.
 */

import type { AgentOutcome } from "@/types";
import { type DiffHunk, hunksFromEdits, parsePatch } from "@/utils/diff";
import { formatCost, formatDuration } from "@/utils/format";

export type ToolArgs = Record<string, unknown> | undefined;
export type ToolMeta = Record<string, unknown> | null | undefined;

export function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function asNumber(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

/** The row's one-line summary of what the call acts on. */
export function getPreview(name: string, args: ToolArgs): string {
  if (!args) return "";
  switch (name) {
    case "bash":
      return asString(args["command"]);
    case "read":
    case "write":
    case "edit":
      return asString(args["path"]);
    case "webfetch":
      return asString(args["url"]);
    case "websearch":
      return asString(args["query"]);
    case "agent":
      return asString(args["description"]);
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

/** `:offset-end` for a partial read. */
export function getReadHint(args: ToolArgs): string {
  const offset = asNumber(args?.["offset"]);
  const limit = asNumber(args?.["limit"]);
  if (offset != null && limit != null) return `:${offset}-${offset + limit}`;
  if (offset != null) return `:${offset}`;
  if (limit != null) return `:1-${limit}`;
  return "";
}

export function getWriteHint(args: ToolArgs): string {
  const content = asString(args?.["content"]);
  if (!content) return "";
  return `${content.split("\n").length} lines`;
}

export function getEditStats(metadata: ToolMeta): { added: number; removed: number } | null {
  const added = asNumber(metadata?.["added_lines"]);
  const removed = asNumber(metadata?.["removed_lines"]);
  if (added == null || removed == null) return null;
  return { added, removed };
}

/**
 * The patch exists once the edit has applied; until then (or on failure) the
 * diff shows the requested replacements.
 */
export function getEditHunks(args: ToolArgs, metadata: ToolMeta): DiffHunk[] {
  const patch = metadata?.["patch"];
  if (typeof patch === "string" && patch) {
    const context = metadata?.["highlight_context"];
    const contexts = Array.isArray(context)
      ? context.map((entry: Record<string, unknown> | null | undefined) => ({
          old: asString(entry?.["old"]),
          new: asString(entry?.["new"]),
        }))
      : [];
    return parsePatch(patch, contexts);
  }
  const edits = args?.["edits"];
  return hunksFromEdits(
    Array.isArray(edits)
      ? edits.map((edit: Record<string, unknown> | null | undefined) => ({
          oldText: asString(edit?.["oldText"]),
          newText: asString(edit?.["newText"]),
        }))
      : [],
  );
}

/** A background start's job output, which the projection adds once the job has finished. */
export function getJobOutput(metadata: ToolMeta): string | null {
  const output = metadata?.["job_output"];
  return typeof output === "string" ? output : null;
}

// The paragraph a blocking agent result ends with, for the model only.
const AGENT_NOTICE = /\n*\[Agent [^\n]*\]\s*$/;

/** A blocking agent call's final reply: its output without the closing notice. */
export function getAgentReply(output: string): string {
  return output.replace(AGENT_NOTICE, "");
}

/** `12 tools · 45s · $0.03` for a completed call, led by `failed` or `stopped` otherwise. */
export function formatAgentOutcome(outcome: AgentOutcome): string {
  const tools = `${outcome.tool_calls} ${outcome.tool_calls === 1 ? "tool" : "tools"}`;
  const parts = [tools, formatDuration(outcome.duration_ms)];
  if (outcome.cost) parts.push(formatCost(outcome.cost.total));
  if (outcome.status === "failed") parts.unshift("failed");
  if (outcome.status === "cancelled") parts.unshift("stopped");
  return parts.join(" · ");
}
