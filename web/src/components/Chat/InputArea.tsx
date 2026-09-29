/**
 * Chat input area: Lexical composer plus upload attachments (image/PDF/text).
 * Inline @workspace references live inside the composer; this component owns
 * the upload strip, drag-and-drop, and the bottom action row.
 */

import {
  ArrowUp,
  ArrowUpToLine,
  CircleAlert,
  CornerDownRight,
  FileText,
  Paperclip,
  Pencil,
  Square,
  Trash2,
  X,
} from "lucide-react";
import {
  type ChangeEvent,
  type DragEvent,
  memo,
  type Ref,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import type {
  AttachedFile,
  ComposerSubmission,
  LocalConfig,
  PendingInput,
  RemoteConfig,
  SkillInfo,
  UsageTotals,
} from "../../types";
import { cn } from "../../utils/cn";
import type { SlashCommand } from "../../utils/completion";
import { formatCost } from "../../utils/format";
import { randomId } from "../../utils/id";
import { isMac } from "../../utils/platform";
import {
  addPromptHistory,
  loadPromptHistory,
  savePromptHistory,
} from "../../utils/storage";
import { Composer, type ComposerHandle } from "./Composer";
import { EffortTrigger, ModelTrigger } from "./InputPills";
import { hasUsageRows, StatsPopover, StatsText, UsageGrid } from "./StatsCard";

const EMPTY_SKILLS: SkillInfo[] = [];

const QUEUED_ACTION_CLASS =
  "flex h-6 items-center justify-center rounded-md hover:bg-background/70 hover:text-foreground active:scale-95 transition-[color,background-color,scale] duration-150";

// File pickers only understand MIME types and extensions, so keep the text
// allowlist explicit here.
const TEXT_FILE_ACCEPT = [
  "text/*",
  ".txt",
  ".md",
  ".mdx",
  ".rst",
  ".json",
  ".jsonl",
  ".yaml",
  ".yml",
  ".toml",
  ".ini",
  ".cfg",
  ".conf",
  ".xml",
  ".html",
  ".htm",
  ".css",
  ".scss",
  ".sass",
  ".less",
  ".js",
  ".jsx",
  ".mjs",
  ".cjs",
  ".ts",
  ".tsx",
  ".mts",
  ".cts",
  ".py",
  ".rb",
  ".php",
  ".go",
  ".rs",
  ".java",
  ".kt",
  ".swift",
  ".c",
  ".cc",
  ".cpp",
  ".cxx",
  ".h",
  ".hh",
  ".hpp",
  ".m",
  ".mm",
  ".sh",
  ".bash",
  ".zsh",
  ".fish",
  ".ps1",
  ".sql",
  ".graphql",
  ".gql",
  ".proto",
  ".csv",
  ".tsv",
  ".log",
  ".env",
  ".gitignore",
  ".gitattributes",
  ".editorconfig",
  ".npmrc",
  ".yarnrc",
  ".pnpmrc",
].join(",");

export interface InputAreaHandle {
  /** Put submissions back ahead of the draft. */
  prepend: (submissions: ComposerSubmission[]) => void;
}

interface InputAreaProps {
  ref?: Ref<InputAreaHandle>;
  /** A run is active: an empty composer offers Stop. */
  loading: boolean;
  /** A compact run is active: nothing can be sent. */
  compacting?: boolean;
  /** `toQueue` is set by ⌘/Ctrl+Enter or ⌘/Ctrl+click. */
  onSubmit: (
    submission: ComposerSubmission,
    toQueue: boolean,
  ) => Promise<boolean>;
  onCancel: () => void;
  /** Messages queued for the running chat's next turn. */
  queued?: PendingInput[];
  onSteerQueued?: (id: string) => void;
  /** Called only while the composer is empty. */
  onEditQueued?: (id: string) => void;
  onRemoveQueued?: (id: string) => void;
  supportsImages?: boolean;
  supportsDocuments?: boolean;
  files?: AttachedFile[];
  onAttachFiles?: (files: AttachedFile[]) => void;
  onRemoveFile?: (id: string) => void;
  config: LocalConfig;
  remoteConfig: RemoteConfig | null;
  onUpdateConfig: (config: LocalConfig) => void;
  onSlashCommand?: (name: SlashCommand["name"]) => void;
  disabledReason?: string | undefined;
  disabled?: boolean | undefined;
  /** Session state cluster: context occupancy and cumulative usage/cost.
   * Unknown parts are hidden. */
  sessionUsage?: UsageTotals | null;
  currentContext?: { tokens: number; window: number } | null;
}

interface InputNotice {
  /** `edit`: a queued message cannot go back into a composer with content. */
  kind: "image" | "document" | "mixed" | "edit";
  blocking: boolean;
}

function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      resolve(result.split(",")[1] ?? "");
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function readFileAsUtf8(file: File): Promise<string | null> {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(
      await file.arrayBuffer(),
    );
  } catch {
    return null;
  }
}

async function processFiles(
  files: File[],
  {
    supportsImages,
    supportsDocuments,
  }: { supportsImages: boolean; supportsDocuments: boolean },
): Promise<{
  attachments: AttachedFile[];
  unsupported: Array<"image" | "document">;
}> {
  const unsupported = new Set<"image" | "document">();
  const attachedFiles = await Promise.all(
    files.map(async (file) => {
      if (file.type.startsWith("image/")) {
        if (!supportsImages) {
          unsupported.add("image");
          return null;
        }
        return {
          id: randomId(),
          kind: "image" as const,
          data: await readFileAsBase64(file),
          mime_type: file.type,
          name: file.name,
          preview: URL.createObjectURL(file),
        };
      }

      const isPdfFile =
        file.type === "application/pdf" ||
        file.name.toLowerCase().endsWith(".pdf");
      if (isPdfFile) {
        if (!supportsDocuments) {
          unsupported.add("document");
          return null;
        }
        return {
          id: randomId(),
          kind: "document" as const,
          data: await readFileAsBase64(file),
          mime_type: "application/pdf" as const,
          name: file.name,
        };
      }

      const text = await readFileAsUtf8(file);
      if (text === null) return null;
      return {
        id: randomId(),
        kind: "text" as const,
        text,
        name: file.name,
      };
    }),
  );
  return {
    attachments: attachedFiles.filter((file) => file !== null),
    unsupported: [...unsupported],
  };
}

/** Session state cluster: `27% · $0.42`, context and session usage in a card.
 * Mobile keeps only the context percentage; the card still has the cost. */
function SessionStats({
  currentContext,
  sessionUsage,
  compactThreshold,
}: {
  currentContext: { tokens: number; window: number } | null;
  sessionUsage: UsageTotals | null;
  compactThreshold: number | undefined;
}) {
  const percent = currentContext
    ? Math.round((currentContext.tokens / currentContext.window) * 100)
    : null;
  const cost = sessionUsage?.cost ? formatCost(sessionUsage.cost.total) : null;
  if (percent === null && !cost) return null;

  // Warn shortly before auto-compact, or near the window when it is off.
  const warn =
    percent !== null && percent >= Math.round((compactThreshold || 1) * 90);
  const trigger = (
    <>
      {percent !== null && (
        <span className={warn ? "text-destructive/70" : undefined}>
          {percent}%
        </span>
      )}
      {cost && (
        <span className={percent !== null ? "max-md:hidden" : undefined}>
          {percent !== null ? ` · ${cost}` : cost}
        </span>
      )}
    </>
  );

  const summary: { label: string; value: string }[] = [];
  if (currentContext) {
    summary.push({
      label: "Context",
      value: `${currentContext.tokens.toLocaleString()} / ${currentContext.window.toLocaleString()}`,
    });
  }
  // Share of all input served from cache; only when the provider reports caching.
  const cacheRead = sessionUsage?.cache_read_tokens ?? 0;
  const cacheWrite = sessionUsage?.cache_write_tokens ?? 0;
  if (sessionUsage?.input_tokens && (cacheRead || cacheWrite)) {
    summary.push({
      label: "Cache hit",
      value: `${Math.round((cacheRead / sessionUsage.input_tokens) * 100)}%`,
    });
  }

  return (
    <span className="mr-2 shrink-0">
      {summary.length > 0 || hasUsageRows(sessionUsage) ? (
        <StatsPopover trigger={trigger} align="end">
          <UsageGrid summary={summary} usage={sessionUsage} />
        </StatsPopover>
      ) : (
        <StatsText>{trigger}</StatsText>
      )}
    </span>
  );
}

export const InputArea = memo(function InputArea({
  ref,
  loading,
  compacting = false,
  onSubmit,
  onCancel,
  queued = [],
  onSteerQueued,
  onEditQueued,
  onRemoveQueued,
  supportsImages = false,
  supportsDocuments = false,
  files = [],
  onAttachFiles,
  onRemoveFile,
  config,
  remoteConfig,
  onUpdateConfig,
  onSlashCommand,
  disabledReason,
  disabled: disabledProp = false,
  sessionUsage = null,
  currentContext = null,
}: InputAreaProps) {
  const composerRef = useRef<ComposerHandle | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const noticeTimerRef = useRef<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const dragCounterRef = useRef(0);
  const [hasContent, setHasContent] = useState(false);
  const [inputNotice, setInputNotice] = useState<InputNotice | null>(null);
  const [promptHistory, setPromptHistory] = useState(() =>
    loadPromptHistory(config.cwd),
  );

  const disabled = disabledProp || Boolean(disabledReason);

  useImperativeHandle(ref, () => ({
    prepend: (submissions) => composerRef.current?.prepend(submissions),
  }));
  const hasImageUpload = files.some((file) => file.kind === "image");
  const hasDocumentUpload = files.some((file) => file.kind === "document");

  const showInputNotice = useCallback(
    (notice: InputNotice | null, timeoutMs?: number) => {
      if (noticeTimerRef.current !== null) {
        window.clearTimeout(noticeTimerRef.current);
        noticeTimerRef.current = null;
      }
      setInputNotice(notice);
      if (notice && timeoutMs) {
        noticeTimerRef.current = window.setTimeout(() => {
          setInputNotice(null);
          noticeTimerRef.current = null;
        }, timeoutMs);
      }
    },
    [],
  );

  useEffect(
    () => () => {
      if (noticeTimerRef.current !== null) {
        window.clearTimeout(noticeTimerRef.current);
      }
    },
    [],
  );

  // Composer calls this on Enter; the send button routes through the same path.
  const handleSubmission = useCallback(
    async (
      submission: ComposerSubmission,
      toQueue: boolean,
    ): Promise<boolean> => {
      if (disabled || compacting) return false;
      if (
        !submission.text.trim() &&
        submission.workspaceFiles.length === 0 &&
        files.length === 0
      ) {
        return false;
      }
      const unsupportedImage =
        !supportsImages &&
        (hasImageUpload ||
          submission.workspaceFiles.some((ref) => ref.kind === "image"));
      const unsupportedDocument =
        !supportsDocuments &&
        (hasDocumentUpload ||
          submission.workspaceFiles.some((ref) => ref.kind === "document"));
      if (unsupportedImage || unsupportedDocument) {
        showInputNotice({
          kind:
            unsupportedImage && unsupportedDocument
              ? "mixed"
              : unsupportedImage
                ? "image"
                : "document",
          blocking: true,
        });
        return false;
      }
      showInputNotice(null);
      const accepted = await onSubmit(submission, toQueue);
      if (accepted) {
        const next = addPromptHistory(promptHistory, submission.text);
        if (next !== promptHistory) {
          setPromptHistory(next);
          savePromptHistory(config.cwd, next);
        }
      }
      return accepted;
    },
    [
      config.cwd,
      promptHistory,
      compacting,
      disabled,
      files.length,
      hasImageUpload,
      hasDocumentUpload,
      supportsImages,
      supportsDocuments,
      onSubmit,
      showInputNotice,
    ],
  );

  const handleHasContentChange = useCallback(
    (next: boolean) => {
      setHasContent(next);
      showInputNotice(null);
    },
    [showInputNotice],
  );

  const handleConfigUpdate = useCallback(
    (next: LocalConfig) => {
      showInputNotice(null);
      onUpdateConfig(next);
    },
    [onUpdateConfig, showInputNotice],
  );

  const attachFiles = useCallback(
    async (incoming: File[]) => {
      if (disabled) return;
      const result = await processFiles(incoming, {
        supportsImages,
        supportsDocuments,
      });
      if (result.unsupported.length) {
        showInputNotice(
          {
            kind:
              result.unsupported.length > 1
                ? "mixed"
                : result.unsupported.includes("image")
                  ? "image"
                  : "document",
            blocking: false,
          },
          2500,
        );
      } else {
        showInputNotice(null);
      }
      if (result.attachments.length) onAttachFiles?.(result.attachments);
    },
    [
      disabled,
      onAttachFiles,
      showInputNotice,
      supportsDocuments,
      supportsImages,
    ],
  );

  const handlePasteFiles = useCallback(
    (incoming: File[]) => {
      void attachFiles(incoming);
    },
    [attachFiles],
  );

  const handleFileChange = async (e: ChangeEvent<HTMLInputElement>) => {
    const incoming = Array.from(e.target.files ?? []);
    e.target.value = "";
    await attachFiles(incoming);
  };

  const handleDragEnter = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current++;
    if (dragCounterRef.current === 1) setDragging(true);
  };

  const handleDragLeave = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current--;
    if (dragCounterRef.current === 0) setDragging(false);
  };

  const handleDragOver = (e: DragEvent) => {
    e.preventDefault();
  };

  const handleDrop = async (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current = 0;
    setDragging(false);
    await attachFiles(Array.from(e.dataTransfer.files));
  };

  const hasInput = hasContent || files.length > 0;
  const canSend = hasInput && !disabled && !compacting;

  const handleEditQueued = (id: string) => {
    if (hasInput) {
      showInputNotice({ kind: "edit", blocking: false }, 2500);
      return;
    }
    onEditQueued?.(id);
  };

  // Full and compact wording per notice; a non-blocking notice uses the compact one.
  const [fullNoticeText, compactNoticeText] = !inputNotice
    ? ["", ""]
    : inputNotice.kind === "edit"
      ? ["Clear the composer to edit", "Clear the composer to edit"]
      : inputNotice.kind === "mixed"
        ? ["Remove attachments or switch model", "Attachments unsupported"]
        : inputNotice.kind === "image"
          ? ["Remove image or switch model", "Image unsupported"]
          : ["Remove PDF or switch model", "PDF unsupported"];
  const noticeText = inputNotice?.blocking ? fullNoticeText : compactNoticeText;
  const accept = [
    TEXT_FILE_ACCEPT,
    supportsImages ? "image/*" : null,
    supportsDocuments ? ".pdf,application/pdf" : null,
  ]
    .filter(Boolean)
    .join(",");

  return (
    <div className="mx-auto max-w-4xl max-md:max-w-none px-5 max-md:px-3 max-md:pb-2">
      {queued.length > 0 && (
        // The next turn: a card behind the composer, showing above its top edge.
        <ul
          aria-label="Queued messages"
          className="mx-4 -mb-3 divide-y divide-border/40 rounded-t-lg bg-muted pb-3 shadow-hairline"
        >
          {queued.map((item) => {
            const attachmentCount =
              item.attachments.length + item.submission.workspaceFiles.length;
            return (
              <li
                key={item.id}
                className="flex items-center gap-2.5 px-3.5 py-2 text-sm leading-5"
              >
                <CornerDownRight
                  aria-hidden="true"
                  className="size-3.5 shrink-0 text-muted-foreground/70"
                />
                <span
                  className="min-w-0 flex-1 truncate text-foreground"
                  title={item.submission.text}
                >
                  {item.submission.text.split("\n", 1)[0]}
                </span>
                {attachmentCount > 0 && (
                  <span
                    className="flex shrink-0 items-center gap-0.5 text-xs text-muted-foreground"
                    title={`${attachmentCount} attachment${attachmentCount === 1 ? "" : "s"}`}
                  >
                    <Paperclip className="size-3" />
                    {attachmentCount}
                  </span>
                )}
                <span className="flex shrink-0 items-center gap-0.5 text-muted-foreground">
                  <button
                    type="button"
                    title="Steer the current turn"
                    onClick={() => onSteerQueued?.(item.id)}
                    className={cn(QUEUED_ACTION_CLASS, "gap-1 px-1.5 text-xs")}
                  >
                    <ArrowUpToLine className="size-3.5" />
                    Steer
                  </button>
                  {!item.partial && (
                    <button
                      type="button"
                      aria-label="Edit"
                      title="Edit"
                      onClick={() => handleEditQueued(item.id)}
                      className={cn(QUEUED_ACTION_CLASS, "w-6")}
                    >
                      <Pencil className="size-3.5" />
                    </button>
                  )}
                  <button
                    type="button"
                    aria-label="Remove"
                    title="Remove"
                    onClick={() => onRemoveQueued?.(item.id)}
                    className={cn(QUEUED_ACTION_CLASS, "w-6")}
                  >
                    <Trash2 className="size-3.5" />
                  </button>
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {/* biome-ignore lint/a11y/noStaticElementInteractions: drag-and-drop drop target */}
      <div
        role="presentation"
        className={cn(
          "relative rounded-lg bg-card transition-[background-color,box-shadow] duration-200",
          "focus-within:shadow-card-accent",
          dragging ? "shadow-card-accent bg-accent/5" : "shadow-card",
        )}
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
      >
        {disabledReason && (
          <div className="border-b border-border/30 px-3.5 py-2 text-[11px] leading-relaxed text-muted-foreground">
            {disabledReason}
          </div>
        )}

        {files.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-3 pt-2.5 pb-1">
            {files.map((file) => (
              <div key={file.id} className="relative group/thumb shrink-0">
                {file.kind === "image" ? (
                  <img
                    src={file.preview}
                    alt={file.name}
                    className="size-14 rounded-lg object-cover border border-border/30"
                  />
                ) : (
                  <div className="h-14 min-w-28 rounded-lg border border-border/30 bg-muted/30 px-3 flex items-center gap-2 text-xs text-foreground/80">
                    <FileText className="size-4 shrink-0 text-accent/80" />
                    <div className="min-w-0">
                      <div className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground/70">
                        {file.kind === "document" ? "PDF" : "Text"}
                      </div>
                      <div className="line-clamp-2 break-all">{file.name}</div>
                    </div>
                  </div>
                )}
                <button
                  type="button"
                  onClick={() => {
                    showInputNotice(null);
                    onRemoveFile?.(file.id);
                  }}
                  aria-label={`Remove ${file.name}`}
                  className="absolute -top-1 -right-1 size-4 bg-foreground text-background rounded-full flex items-center justify-center opacity-0 group-hover/thumb:opacity-100 max-md:opacity-100 transition-opacity"
                >
                  <X className="size-2.5" />
                </button>
              </div>
            ))}
          </div>
        )}

        <input
          ref={fileInputRef}
          type="file"
          accept={accept}
          multiple
          aria-label="Attach files"
          className="hidden"
          onChange={handleFileChange}
        />

        <Composer
          ref={composerRef}
          disabled={disabled}
          placeholder={disabledReason || "Message…"}
          loading={loading}
          cwd={config.cwd}
          supportsImages={supportsImages}
          supportsDocuments={supportsDocuments}
          skills={remoteConfig?.skills ?? EMPTY_SKILLS}
          hasUploads={files.length > 0}
          history={promptHistory}
          onSubmit={handleSubmission}
          onSlashCommand={onSlashCommand}
          onPasteFiles={handlePasteFiles}
          onHasContentChange={handleHasContentChange}
        />

        {/* Bottom row: attach + model · effort + send */}
        <div className="flex items-center gap-1 px-1.5 pb-1.5">
          <button
            type="button"
            aria-label="Attach file"
            disabled={disabled || compacting}
            onClick={() => fileInputRef.current?.click()}
            className={cn(
              "size-7 flex items-center justify-center rounded-md transition-colors shrink-0",
              disabled || compacting
                ? "text-muted-foreground/20"
                : "text-muted-foreground/60 hover:text-foreground hover:bg-muted/70",
            )}
            title="Attach file"
          >
            <Paperclip className="size-3.5" />
          </button>

          <div className="flex items-center gap-0.5 min-w-0 flex-1 overflow-hidden">
            <div
              className={cn(
                "min-w-0",
                inputNotice && "max-w-[45%] max-md:max-w-24",
              )}
            >
              <ModelTrigger
                config={config}
                remoteConfig={remoteConfig}
                onUpdateConfig={handleConfigUpdate}
              />
            </div>
            {inputNotice ? (
              <div
                role="status"
                aria-live="polite"
                title={noticeText}
                className="flex min-w-0 flex-1 items-center gap-1 px-1.5 text-[12px] leading-4 text-destructive/80"
              >
                <CircleAlert className="size-3 shrink-0" />
                {inputNotice.blocking ? (
                  <>
                    <span className="truncate max-md:hidden">{noticeText}</span>
                    <span className="hidden truncate max-md:inline">
                      {compactNoticeText}
                    </span>
                  </>
                ) : (
                  <span className="truncate">{compactNoticeText}</span>
                )}
              </div>
            ) : (
              <EffortTrigger
                config={config}
                remoteConfig={remoteConfig}
                onUpdateConfig={handleConfigUpdate}
              />
            )}
          </div>

          <SessionStats
            currentContext={currentContext}
            sessionUsage={sessionUsage}
            compactThreshold={remoteConfig?.compact_threshold}
          />

          {loading && !canSend ? (
            <button
              type="button"
              aria-label="Stop generating"
              onClick={onCancel}
              className="size-7 flex items-center justify-center rounded-md text-destructive/70 hover:text-destructive hover:bg-destructive/10 active:scale-95 transition-[color,background-color,scale] duration-150 shrink-0"
              title="Stop · Esc"
            >
              <Square className="size-3 fill-current" />
            </button>
          ) : (
            <button
              type="button"
              aria-label="Send message"
              onClick={(e) =>
                composerRef.current?.submit(e.metaKey || e.ctrlKey)
              }
              disabled={!canSend}
              className={cn(
                "size-7 flex items-center justify-center rounded-md transition-[color,background-color,filter,scale] duration-150 shrink-0",
                canSend
                  ? "bg-accent text-accent-foreground hover:brightness-105 hover:saturate-[.9] active:scale-95"
                  : "text-muted-foreground/30 bg-muted/40",
              )}
              title={
                loading
                  ? `Steer · Enter, queue · ${isMac ? "⌘" : "Ctrl+"}Enter`
                  : "Send"
              }
            >
              <ArrowUp className="size-3.5" strokeWidth={2.5} />
            </button>
          )}
        </div>

        {dragging && (
          <div className="absolute inset-0 flex items-center justify-center rounded-lg bg-accent/5 pointer-events-none z-10">
            <span className="text-sm text-accent font-medium">
              Drop file here
            </span>
          </div>
        )}
      </div>
    </div>
  );
});
