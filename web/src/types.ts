import type { CSSProperties, Dispatch, SetStateAction } from "react";

export type Theme = "light" | "dark" | "system";
export type ReasoningEffort =
  | ""
  | "auto"
  | "none"
  | "low"
  | "medium"
  | "high"
  | "xhigh";

export interface LocalConfig {
  provider: string;
  model: string;
  cwd: string;
  reasoningEffort: ReasoningEffort;
}

export interface ProviderInfo {
  name: string;
  provider: string;
  type: string;
  models: string[];
  base_url: string;
  has_api_key: boolean;
  supports_reasoning_effort?: boolean;
  reasoning_models?: string[];
  reasoning_effort?: ReasoningEffort | null;
  supports_image_input?: boolean;
  image_input_models?: string[];
  supports_pdf_input?: boolean;
  pdf_input_models?: string[];
}

export interface RemoteConfig {
  providers?: Record<string, ProviderInfo>;
  default?: {
    provider: string;
    model: string;
  };
  default_reasoning_effort?: ReasoningEffort | null;
  reasoning_effort_options?: ReasoningEffort[];
  cwd?: string;
  cwd_exists?: boolean;
  project?: string;
  config_paths?: string[];
  setup_error?: { message: string } | null;
}

export interface SessionSummary {
  id: string;
  title?: string;
  isDraft?: boolean;
  is_running?: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface RunInfo {
  id: string;
  session_id: string;
  status: string;
  last_seq: number;
  error?: string;
}

export interface ToolRuntime {
  pending: boolean;
  output: string;
  finalOutput: string | null;
  metadata: Record<string, unknown> | null;
  isError: boolean;
}

export type ToolInput = Record<string, unknown>;

export interface TextBlock {
  type: "text";
  text: string;
  renderKey?: string;
  meta?: Record<string, unknown>;
}

export interface ThinkingBlock {
  type: "thinking";
  text: string;
  renderKey?: string;
  meta?: Record<string, unknown>;
}

export interface ToolUseBlock {
  type: "tool_use";
  id: string;
  name: string;
  input: ToolInput;
  runtime?: ToolRuntime;
  renderKey?: string;
  meta?: Record<string, unknown>;
}

export interface ToolResultBlock {
  type: "tool_result";
  tool_use_id: string;
  output: string | null;
  metadata: Record<string, unknown> | null;
  is_error: boolean;
  renderKey?: string;
  meta?: Record<string, unknown>;
}

export interface ImageBlock {
  type: "image";
  data: string;
  mime_type: string;
  name?: string;
  renderKey?: string;
  meta?: Record<string, unknown>;
}

export interface DocumentBlock {
  type: "document";
  data: string;
  mime_type: string;
  name?: string;
  renderKey?: string;
  meta?: Record<string, unknown>;
}

export type MessageBlock =
  | TextBlock
  | ThinkingBlock
  | ToolUseBlock
  | ToolResultBlock
  | ImageBlock
  | DocumentBlock;

interface AttachedImageFile {
  id: string;
  kind: "image";
  data: string;
  mime_type: string;
  name: string;
  preview: string; // object URL for thumbnail display
}

interface AttachedDocumentFile {
  id: string;
  kind: "document";
  data: string;
  mime_type: "application/pdf";
  name: string;
}

interface AttachedTextFile {
  id: string;
  kind: "text";
  text: string;
  name: string;
}

/** File attached in the input area, pending send. */
export type AttachedFile =
  | AttachedImageFile
  | AttachedDocumentFile
  | AttachedTextFile;

export interface MessageMeta {
  total_tokens?: number;
  model?: string;
  provider?: string;
  context_window?: number;
  [key: string]: unknown;
}

export interface ChatMessage {
  role: "user" | "assistant" | "system" | "compact";
  content: MessageBlock[];
  meta?: MessageMeta;
  renderKey?: string;
  sourceIndex?: number;
}

export interface CompactMarkerMessage {
  kind: "compact-marker";
  sourceIndex: number;
  renderKey: string;
}

export type RenderMessage = ChatMessage | CompactMarkerMessage;

export function isCompactMarker(
  message: RenderMessage,
): message is CompactMarkerMessage {
  return "kind" in message && message.kind === "compact-marker";
}

interface StreamEventBase {
  type: string;
  seq?: number;
}

interface ReasoningEvent extends StreamEventBase {
  type: "reasoning";
  delta?: string;
}

interface ReasoningDoneEvent extends StreamEventBase {
  type: "reasoning_done";
  duration_ms?: number;
}

interface TextEvent extends StreamEventBase {
  type: "text";
  delta?: string;
}

interface ToolStartEvent extends StreamEventBase {
  type: "tool_start";
  tool_call?: {
    id?: string;
    name?: string;
    input?: ToolInput;
  };
}

interface ToolOutputEvent extends StreamEventBase {
  type: "tool_output";
  tool_use_id?: string;
  output?: string;
}

interface ToolDoneEvent extends StreamEventBase {
  type: "tool_done";
  tool_use_id?: string;
  output?: string;
  metadata?: Record<string, unknown>;
  is_error?: boolean;
}

interface ErrorEvent extends StreamEventBase {
  type: "error";
  message?: string;
}

interface CompactEvent extends StreamEventBase {
  type: "compact";
}

interface PermissionRequestEvent extends StreamEventBase {
  type: "permission_request";
  request_id: string;
  tool_use_id: string;
  tool_name: string;
  preview: string;
}

interface PermissionResolvedEvent extends StreamEventBase {
  type: "permission_resolved";
  request_id: string;
  decision: "allow" | "deny";
}

interface UsageEvent extends StreamEventBase {
  type: "usage";
  total_tokens?: number;
  context_window?: number;
  model?: string;
  provider?: string;
}

export type StreamEvent =
  | ReasoningEvent
  | ReasoningDoneEvent
  | TextEvent
  | ToolStartEvent
  | ToolOutputEvent
  | ToolDoneEvent
  | ErrorEvent
  | CompactEvent
  | PermissionRequestEvent
  | PermissionResolvedEvent
  | UsageEvent;

export interface PermissionRequest {
  request_id: string;
  tool_use_id: string;
  tool_name: string;
  preview: string;
}

export interface SessionsResponse {
  sessions?: SessionSummary[];
}

export interface SessionResponse {
  session: SessionSummary | null;
  messages: ChatMessage[];
  active_run: RunInfo | null;
  pending_events: StreamEvent[];
}

export interface ChatResponse {
  run: RunInfo;
  session: SessionSummary;
}

export interface ValidationErrorDetail {
  loc?: (string | number)[];
  msg?: string;
  type?: string;
}

export interface ChatErrorResponse {
  detail?:
    | string
    | ValidationErrorDetail[]
    | {
        message?: string;
        run?: RunInfo;
      };
}

export interface WorkspaceEntry {
  name: string;
  path: string;
}

export interface WorkspaceRootsResponse {
  roots?: string[];
}

export interface WorkspaceBrowseResponse {
  root: string;
  path: string;
  current: string;
  entries: WorkspaceEntry[];
  error: string;
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
  reasoning_effort?: string | null;
  model_overrides?: Record<string, Record<string, unknown>>;
}

export interface GlobalConfig {
  default?: {
    provider?: string;
    model?: string;
    reasoning_effort?: string | null;
    compact_threshold?: number | false | null;
  };
  permission?:
    | PermissionLevel
    | { level?: PermissionLevel; mode?: PermissionMode };
  providers?: Record<string, GlobalProviderEntry>;
}

export interface SettingsResponse {
  path: string;
  exists: boolean;
  config: GlobalConfig;
  options: {
    provider_types: string[];
    permission_levels: PermissionLevel[];
    permission_modes: PermissionMode[];
    reasoning_efforts: ReasoningEffort[];
  };
  env: Record<string, boolean>;
  provider_type_env_vars: Record<string, string[]>;
  provider_type_default_models: Record<string, string[]>;
}

export interface ThemeContextValue {
  theme: Theme;
  resolvedTheme: Exclude<Theme, "system">;
  setTheme: (theme: Theme) => void;
}

export type SetString = Dispatch<SetStateAction<string>>;

export type InlineStyle = CSSProperties;
