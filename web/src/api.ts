/**
 * The HTTP contract with the server (`docs/api.md`): wire types, JSON and
 * error reading, and the run stream's SSE lines.
 */

import type {
  ChatMessage,
  Cost,
  PermissionRequest,
  ReasoningEffort,
  SessionSummary,
  ToolInput,
} from "@/types";

// ─── config and settings ────────────────────────────────────────────────────

export interface ProviderInfo {
  name: string;
  models: string[];
  /** Efforts each model accepts; empty when effort is not configurable. */
  reasoning_efforts: Record<string, ReasoningEffort[]>;
  image_input_models: string[];
  pdf_input_models: string[];
}

export interface SkillInfo {
  name: string;
  description: string;
}

/** `GET /api/config`. */
export interface RemoteConfig {
  providers?: Record<string, ProviderInfo>;
  default?: {
    provider: string;
    model: string;
  };
  cwd?: string;
  cwd_exists?: boolean;
  project?: string;
  config_paths?: string[];
  /** Effective for this cwd; 0 disables automatic compaction. */
  compact_threshold?: number;
  skills?: SkillInfo[];
  setup_error?: { message: string } | null;
}

export type PermissionLevel = "readonly" | "safe" | "standard" | "yolo";
export type PermissionMode = "ask" | "deny";

/** A single provider entry. GET returns ``models`` as a string[] (with
 * per-model metadata overrides surfaced separately under ``model_overrides``);
 * PUT accepts either a list or the dict form so the UI can round-trip those
 * overrides without exposing them in the form. */
export interface GlobalProviderEntry {
  type?: string;
  models?: string[] | Record<string, Record<string, unknown>>;
  api_key?: string | null;
  api_key_saved?: boolean;
  base_url?: string;
  /** Opt-in for a generic openai_chat endpoint that accepts the standard
   * top-level reasoning_effort. Ignored for other provider types. */
  supports_reasoning_effort?: boolean;
  model_overrides?: Record<string, Record<string, unknown>>;
}

export type WebFetchProvider = "local" | "tavily" | "exa";
export type WebSearchProvider = "off" | "tavily" | "exa";

export interface WebProviderEntry {
  api_key?: string | null;
  api_key_saved?: boolean;
}

export interface GlobalConfig {
  compact_threshold?: number | false | null;
  permission?: PermissionLevel | { level?: PermissionLevel; mode?: PermissionMode };
  providers?: Record<string, GlobalProviderEntry>;
  web?: {
    fetch?: WebFetchProvider;
    search?: WebSearchProvider;
    tavily?: WebProviderEntry;
    exa?: WebProviderEntry;
  };
}

/** `GET /api/settings` and `PUT /api/settings`. */
export interface SettingsResponse {
  path: string;
  exists: boolean;
  config: GlobalConfig;
  options: {
    provider_types: string[];
    permission_levels: PermissionLevel[];
    permission_modes: PermissionMode[];
  };
  env: Record<string, boolean>;
  provider_type_env_vars: Record<string, string[]>;
  provider_type_default_models: Record<string, string[]>;
}

// ─── runs and sessions ──────────────────────────────────────────────────────

export type RunKind = "chat" | "compact";

export interface RunInfo {
  id: string;
  session_id: string;
  kind: RunKind;
  status: "running" | "completed" | "failed" | "cancelled";
  last_seq: number;
  error?: string;
}

/** A running background command, as `GET /api/sessions/{id}` and `/api/events` describe it. */
export interface BackgroundJobInfo {
  tool_use_id: string;
  label: string;
  pid: number;
  started_at: string;
}

/** One event of `GET /api/events`: a run or a background command started or ended. */
export type ServerEvent =
  | { type: "run_started" | "run_finished"; session_id: string; run: RunInfo }
  | {
      type: "job_started" | "job_finished";
      session_id: string;
      job: BackgroundJobInfo;
    };

/** One event of a run, without the stream's `seq`. Field optionality follows
 * the SSE contract: the server omits `None` fields, which only `tool_done`'s
 * `metadata` and the `usage` fields can be. */
export type RunEvent =
  | { type: "reasoning"; delta: string }
  | { type: "reasoning_done"; duration_ms: number }
  | { type: "text"; delta: string }
  | { type: "tool_start"; tool_call: { id: string; name: string; input: ToolInput } }
  | { type: "tool_output"; tool_use_id: string; output: string }
  | {
      type: "tool_done";
      tool_use_id: string;
      output: string;
      is_error: boolean;
      metadata?: Record<string, unknown>;
    }
  | { type: "error"; message: string }
  | { type: "cancelled" }
  | { type: "compact"; trigger: "auto" | "manual" }
  | {
      type: "user_message";
      /** Merged steers (`meta.steer`) or the merged queue; `meta.input_ids`
       * lists the delivered items. */
      message: ChatMessage;
    }
  | ({ type: "permission_request" } & PermissionRequest)
  | { type: "permission_resolved"; request_id: string; decision: "allow" | "deny" }
  | {
      type: "usage";
      context_tokens?: number;
      context_window?: number;
      model?: string;
      /** Turn-cumulative token counts. */
      turn_usage?: Record<string, number>;
      /** Turn-cumulative cost. */
      turn_cost?: Cost;
      /** Elapsed time from the turn's user message to the record this event follows. */
      turn_duration_ms?: number;
      /** Pre-run session totals + turn totals, composed by the server. */
      session_usage?: Record<string, number>;
      session_cost?: Cost;
    };

/** One event of `GET /api/runs/{id}/stream`, or of a snapshot's `pending_events`. */
export type StreamEvent = RunEvent & { seq: number };

/** An undelivered message in a session snapshot; the server sets its id. */
export type PendingMessage = ChatMessage & { meta: { input_id: string } };

export interface SessionsResponse {
  sessions: SessionSummary[];
}

/** `GET /api/sessions/{id}`. */
export interface SessionResponse {
  /** null when the session does not exist. */
  session: SessionSummary | null;
  messages: ChatMessage[];
  session_usage?: Record<string, number> | null;
  session_cost?: Cost | null;
  active_run: RunInfo | null;
  pending_events: StreamEvent[];
  /** The active run's undelivered steers and queued messages. */
  pending?: { steers: PendingMessage[]; queue: PendingMessage[] };
  /** Background commands still running in this session. */
  jobs?: BackgroundJobInfo[];
}

export interface SessionSearchHit {
  session: SessionSummary;
  /** Text around the first message match; null when only the title matched. */
  snippet: { before: string; match: string; after: string } | null;
}

export interface SessionSearchResponse {
  results: SessionSearchHit[];
}

export interface ChatResponse {
  run: RunInfo;
  session: SessionSummary;
  /** The sent message as the run received it: results of background
   * commands that waited for it ride ahead of its own blocks. */
  message: ChatMessage;
}

/** Compact start, steer, queue, and queue removal. */
export interface RunResponse {
  run: RunInfo;
}

// ─── workspaces ─────────────────────────────────────────────────────────────

export interface WorkspaceRootsResponse {
  roots: string[];
}

/** A directory in `GET /api/workspaces/browse`. */
export interface WorkspaceDirEntry {
  name: string;
  path: string;
}

export interface WorkspaceBrowseResponse {
  root: string;
  path: string;
  current: string;
  entries: WorkspaceDirEntry[];
  error: string;
}

/** An `@` completion candidate from `GET /api/workspaces/files`. */
export interface WorkspaceFileEntry {
  name: string;
  path: string;
  kind: "directory" | "text" | "image" | "document";
}

export interface WorkspaceFilesResponse {
  entries: WorkspaceFileEntry[];
  truncated: boolean;
  error: string;
}

// ─── requests ───────────────────────────────────────────────────────────────

/** A non-OK response: the server's message and, for a busy session, its running run. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly run: RunInfo | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface ValidationErrorItem {
  msg?: string;
}

/**
 * Read a non-OK response's FastAPI `detail`: a string, `{message, run}` (a
 * busy or locked session), or a validation error list. A body that is not
 * JSON leaves `fallback` as the message.
 */
export async function readApiError(response: Response, fallback: string): Promise<ApiError> {
  let detail: unknown;
  try {
    detail = ((await response.json()) as { detail?: unknown } | null)?.detail;
  } catch {}
  let message = "";
  let run: RunInfo | null = null;
  if (typeof detail === "string") {
    message = detail;
  } else if (Array.isArray(detail)) {
    message = (detail as ValidationErrorItem[]).find((item) => item.msg)?.msg ?? "";
  } else if (detail && typeof detail === "object") {
    const body = detail as { message?: string; run?: RunInfo };
    message = body.message ?? "";
    run = body.run ?? null;
  }
  return new ApiError(message || fallback, response.status, run);
}

/** Fetch JSON; a non-OK response throws an `ApiError` with the server's message. */
export async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    throw await readApiError(response, `Request failed (${response.status})`);
  }
  return (await response.json()) as T;
}

/** The marker `readSse` yields for the stream's final `data: [DONE]` line. */
export const SSE_DONE = "[DONE]";

/** Parse a run stream's `data:` lines into events, then `SSE_DONE`. */
export async function* readSse(response: Response): AsyncGenerator<StreamEvent | typeof SSE_DONE> {
  if (!response.body) throw new Error("Response body is empty");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const data = line.slice(6);
      if (data === SSE_DONE) {
        yield SSE_DONE;
        continue;
      }
      let event: StreamEvent;
      try {
        event = JSON.parse(data) as StreamEvent;
      } catch (e) {
        console.error("Parse error:", e);
        continue;
      }
      yield event;
    }
  }
}
