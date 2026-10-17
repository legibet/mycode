/**
 * Chat input area: Lexical composer plus upload attachments (image/PDF/text).
 * Inline @workspace references live inside the composer; this component owns
 * the upload strip, drag-and-drop, and the bottom action row.
 */

import { ArrowUp, CircleAlert, FileText, Paperclip, Square, X } from "lucide-react";
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

import type { BackgroundJobInfo, RemoteConfig, SkillInfo } from "@/api";
import { Composer, type ComposerHandle } from "@/components/composer/Composer";
import { InFlightCard } from "@/components/composer/InFlightCard";
import { EffortTrigger, ModelTrigger } from "@/components/composer/InputPills";
import { SessionStats } from "@/components/composer/SessionStats";
import {
  readUploads,
  type UnsupportedMedia,
  unsupportedMedia,
  uploadAccept,
} from "@/components/composer/uploads";
import { Button } from "@/components/ui/button";
import { SimpleTooltip } from "@/components/ui/tooltip";
import type {
  AttachedFile,
  ComposerSubmission,
  LocalConfig,
  PendingInput,
  UsageTotals,
} from "@/types";
import { cn } from "@/utils/cn";
import type { SlashCommand } from "@/utils/completion";
import { isMac } from "@/utils/platform";
import { addPromptHistory, loadPromptHistory, savePromptHistory } from "@/utils/storage";

const EMPTY_SKILLS: SkillInfo[] = [];

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
  onSubmit: (submission: ComposerSubmission, toQueue: boolean) => Promise<boolean>;
  onCancel: () => void;
  /** Messages queued for the running chat's next turn. */
  queued?: PendingInput[];
  onSteerQueued?: (id: string) => void;
  /** Called only while the composer is empty. */
  onEditQueued?: (id: string) => void;
  onRemoveQueued?: (id: string) => void;
  /** Background commands running in the session. */
  jobs?: BackgroundJobInfo[];
  onStopJob?: (toolUseId: string) => void;
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
  kind: UnsupportedMedia | "edit";
  blocking: boolean;
}

// Full and compact wording; a non-blocking notice uses the compact one.
const NOTICE_TEXT: Record<InputNotice["kind"], [full: string, compact: string]> = {
  edit: ["Clear the composer to edit", "Clear the composer to edit"],
  mixed: ["Remove attachments or switch model", "Attachments unsupported"],
  image: ["Remove image or switch model", "Image unsupported"],
  document: ["Remove PDF or switch model", "PDF unsupported"],
};

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
  jobs = [],
  onStopJob,
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
  const [promptHistory, setPromptHistory] = useState(() => loadPromptHistory(config.cwd));

  const disabled = disabledProp || Boolean(disabledReason);

  useImperativeHandle(ref, () => ({
    prepend: (submissions) => composerRef.current?.prepend(submissions),
  }));
  const hasImageUpload = files.some((file) => file.kind === "image");
  const hasDocumentUpload = files.some((file) => file.kind === "document");

  const showInputNotice = useCallback((notice: InputNotice | null, timeoutMs?: number) => {
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
  }, []);

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
    async (submission: ComposerSubmission, toQueue: boolean): Promise<boolean> => {
      if (disabled || compacting) return false;
      if (!submission.text.trim() && submission.workspaceFiles.length === 0 && files.length === 0) {
        return false;
      }
      const unsupported = unsupportedMedia(
        !supportsImages &&
          (hasImageUpload || submission.workspaceFiles.some((ref) => ref.kind === "image")),
        !supportsDocuments &&
          (hasDocumentUpload || submission.workspaceFiles.some((ref) => ref.kind === "document")),
      );
      if (unsupported) {
        showInputNotice({ kind: unsupported, blocking: true });
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
      const { attachments, unsupported } = await readUploads(incoming, {
        supportsImages,
        supportsDocuments,
      });
      showInputNotice(unsupported ? { kind: unsupported, blocking: false } : null, 2500);
      if (attachments.length) onAttachFiles?.(attachments);
    },
    [disabled, onAttachFiles, showInputNotice, supportsDocuments, supportsImages],
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

  const [fullNoticeText, compactNoticeText] = inputNotice ? NOTICE_TEXT[inputNotice.kind] : [];
  const noticeText = inputNotice?.blocking ? fullNoticeText : compactNoticeText;

  return (
    <div className="mx-auto max-w-4xl px-5 max-md:max-w-none max-md:px-3 max-md:pb-2">
      <InFlightCard
        jobs={jobs}
        onStopJob={onStopJob}
        queued={queued}
        onSteerQueued={onSteerQueued}
        onEditQueued={handleEditQueued}
        onRemoveQueued={onRemoveQueued}
      />
      <div
        role="presentation"
        className={cn(
          "relative rounded-lg bg-card transition-[background-color,box-shadow] duration-200",
          "focus-within:shadow-card-focus",
          dragging ? "bg-primary/5 shadow-card-focus" : "shadow-card",
        )}
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
      >
        {disabledReason && (
          <div className="border-b border-border/30 px-3.5 py-2 text-2xs leading-relaxed text-muted-foreground">
            {disabledReason}
          </div>
        )}

        {files.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-3 pt-2.5 pb-1">
            {files.map((file) => (
              <div key={file.id} className="group/thumb relative shrink-0">
                {file.kind === "image" ? (
                  <img
                    src={file.preview}
                    alt={file.name}
                    className="size-14 rounded-lg border border-border/30 object-cover"
                  />
                ) : (
                  <div className="flex h-14 min-w-28 items-center gap-2 rounded-lg border border-border/30 bg-muted/30 px-3 text-xs text-foreground/80">
                    <FileText className="size-4 shrink-0 text-primary/80" />
                    <div className="min-w-0">
                      <div className="text-2xs tracking-[0.16em] text-muted-foreground/70 uppercase">
                        {file.kind === "document" ? "PDF" : "Text"}
                      </div>
                      <div className="line-clamp-2 break-all">{file.name}</div>
                    </div>
                  </div>
                )}
                <Button
                  size="icon-xs"
                  onClick={() => {
                    showInputNotice(null);
                    onRemoveFile?.(file.id);
                  }}
                  aria-label={`Remove ${file.name}`}
                  className="absolute -top-1 -right-1 size-4 rounded-full border-0 bg-foreground text-background opacity-0 transition-[background-color,opacity,scale] group-hover/thumb:opacity-100 hover:bg-foreground/85 focus-visible:opacity-100 max-md:opacity-100"
                >
                  <X className="size-2.5" />
                </Button>
              </div>
            ))}
          </div>
        )}

        <input
          ref={fileInputRef}
          type="file"
          accept={uploadAccept({ supportsImages, supportsDocuments })}
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
          <SimpleTooltip label="Attach file">
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Attach file"
              disabled={disabled || compacting}
              onClick={() => fileInputRef.current?.click()}
            >
              <Paperclip className="size-3.5" />
            </Button>
          </SimpleTooltip>

          <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-hidden">
            <div className={cn("min-w-0", inputNotice && "max-w-[45%] max-md:max-w-24")}>
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
                className="flex min-w-0 flex-1 items-center gap-1 px-1.5 text-xs leading-4 text-destructive/80"
              >
                <CircleAlert className="size-3 shrink-0" />
                {inputNotice.blocking ? (
                  <>
                    <span className="truncate max-md:hidden">{noticeText}</span>
                    <span className="hidden truncate max-md:inline">{compactNoticeText}</span>
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
            <SimpleTooltip label="Stop · Esc">
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Stop generating"
                onClick={onCancel}
                className="text-destructive/70 hover:bg-destructive/10 hover:text-destructive"
              >
                <Square className="size-3 fill-current" />
              </Button>
            </SimpleTooltip>
          ) : (
            <SimpleTooltip
              label={loading ? `Steer · Enter, queue · ${isMac ? "⌘" : "Ctrl+"}Enter` : "Send"}
            >
              <Button
                variant={canSend ? "default" : "secondary"}
                size="icon-sm"
                aria-label="Send message"
                onClick={(e) => composerRef.current?.submit(e.metaKey || e.ctrlKey)}
                disabled={!canSend}
              >
                <ArrowUp className="size-3.5" strokeWidth={2.5} />
              </Button>
            </SimpleTooltip>
          )}
        </div>

        {dragging && (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-lg bg-primary/5">
            <span className="text-sm font-medium text-primary">Drop file here</span>
          </div>
        )}
      </div>
    </div>
  );
});
