import type { CSSProperties, Dispatch, ReactNode, SetStateAction } from 'react'

export type Theme = 'light' | 'dark' | 'system'
export type ConnectionState = 'idle' | 'ready' | 'error'
export type ChatStatus = 'idle' | 'ready' | 'offline' | 'generating'
export type ReasoningEffort =
  | ''
  | 'auto'
  | 'none'
  | 'low'
  | 'medium'
  | 'high'
  | 'xhigh'

export interface LocalConfig {
  provider: string
  model: string
  cwd: string
  apiKey: string
  apiBase: string
  reasoningEffort: ReasoningEffort
}

export interface ProviderInfo {
  name: string
  provider: string
  type: string
  models: string[]
  base_url: string
  has_api_key: boolean
  supports_reasoning_effort?: boolean
  reasoning_models?: string[]
  reasoning_effort?: ReasoningEffort | null
  supports_image_input?: boolean
  image_input_models?: string[]
  supports_pdf_input?: boolean
  pdf_input_models?: string[]
}

export interface RemoteConfig {
  providers?: Record<string, ProviderInfo>
  default?: {
    provider: string
    model: string
  }
  default_reasoning_effort?: ReasoningEffort | null
  reasoning_effort_options?: ReasoningEffort[]
  cwd?: string
  workspace_root?: string
  config_paths?: string[]
}

export interface SessionSummary {
  id: string
  title?: string
  isDraft?: boolean
  is_running?: boolean
}

export interface RunInfo {
  id: string
  session_id: string
  status: string
  last_seq: number
  error?: string
}

export interface ToolRuntime {
  pending: boolean
  output: string
  modelText: string | null
  displayText: string | null
  isError: boolean
}

export type ToolInput = Record<string, unknown>

export interface TextBlock {
  type: 'text'
  text: string
  renderKey?: string
  meta?: Record<string, unknown>
}

export interface ThinkingBlock {
  type: 'thinking'
  text: string
  renderKey?: string
  meta?: Record<string, unknown>
}

export interface ToolUseBlock {
  type: 'tool_use'
  id: string
  name: string
  input: ToolInput
  runtime?: ToolRuntime
  renderKey?: string
  meta?: Record<string, unknown>
}

export interface ToolResultBlock {
  type: 'tool_result'
  tool_use_id: string
  model_text: string | null
  display_text: string | null
  is_error: boolean
  renderKey?: string
  meta?: Record<string, unknown>
}

export interface ImageBlock {
  type: 'image'
  data: string
  mime_type: string
  name?: string
  renderKey?: string
  meta?: Record<string, unknown>
}

export interface DocumentBlock {
  type: 'document'
  data: string
  mime_type: string
  name?: string
  renderKey?: string
  meta?: Record<string, unknown>
}

export type MessageBlock =
  | TextBlock
  | ThinkingBlock
  | ToolUseBlock
  | ToolResultBlock
  | ImageBlock
  | DocumentBlock

export interface AttachedImageFile {
  kind: 'image'
  data: string
  mime_type: string
  name: string
  preview: string // object URL for thumbnail display
}

export interface AttachedDocumentFile {
  kind: 'document'
  data: string
  mime_type: 'application/pdf'
  name: string
}

export interface AttachedTextFile {
  kind: 'text'
  text: string
  name: string
}

/** File attached in the input area, pending send. */
export type AttachedFile =
  | AttachedImageFile
  | AttachedDocumentFile
  | AttachedTextFile

export interface MessageMeta {
  synthetic?: boolean
  [key: string]: unknown
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: MessageBlock[]
  meta?: MessageMeta
  renderKey?: string
  sourceIndex?: number
}

export interface StreamEventBase {
  type: string
  seq?: number
}

export interface ReasoningEvent extends StreamEventBase {
  type: 'reasoning'
  delta?: string
}

export interface TextEvent extends StreamEventBase {
  type: 'text'
  delta?: string
}

export interface ToolStartEvent extends StreamEventBase {
  type: 'tool_start'
  tool_call?: {
    id?: string
    name?: string
    input?: ToolInput
  }
}

export interface ToolOutputEvent extends StreamEventBase {
  type: 'tool_output'
  tool_use_id?: string
  output?: string
}

export interface ToolDoneEvent extends StreamEventBase {
  type: 'tool_done'
  tool_use_id?: string
  model_text?: string
  display_text?: string
  is_error?: boolean
}

export interface ErrorEvent extends StreamEventBase {
  type: 'error'
  message?: string
}

export interface CompactEvent extends StreamEventBase {
  type: 'compact'
  message?: string
  compacted_count?: number
}

export type StreamEvent =
  | ReasoningEvent
  | TextEvent
  | ToolStartEvent
  | ToolOutputEvent
  | ToolDoneEvent
  | ErrorEvent
  | CompactEvent

export interface SessionsResponse {
  sessions?: SessionSummary[]
}

export interface SessionResponse {
  session: SessionSummary | null
  messages: ChatMessage[]
  active_run: RunInfo | null
  pending_events: StreamEvent[]
}

export interface ChatResponse {
  run: RunInfo
  session: SessionSummary
}

export interface ChatErrorResponse {
  detail?:
    | string
    | {
        message?: string
        run?: RunInfo
      }
}

export interface WorkspaceEntry {
  name: string
  path: string
}

export interface WorkspaceRootsResponse {
  roots?: string[]
}

export interface WorkspaceBrowseResponse {
  root: string
  path: string
  current: string
  entries: WorkspaceEntry[]
  error: string
}

export interface WorkspaceState {
  roots: string[]
  root: string
  path: string
  current: string
  entries: WorkspaceEntry[]
  loading: boolean
  error: string
}

export interface ThemeContextValue {
  theme: Theme
  resolvedTheme: Exclude<Theme, 'system'>
  setTheme: (theme: Theme) => void
}

export interface AppContentFetchError {
  message: string
}

export type SetString = Dispatch<SetStateAction<string>>

export interface ToolMeta {
  icon: (props: { className?: string; 'aria-hidden'?: boolean }) => ReactNode
  label: string
}

export interface DiffRow {
  key: string
  type: 'context' | 'removed' | 'added' | 'separator'
  ln: number | null
  html: string
}

export interface EditMeta {
  start_line: number
  old_line_count: number
  new_line_count: number
  context_before?: string[]
  context_after?: string[]
}

export type InlineStyle = CSSProperties
