/**
 * User message: right-aligned compact bubble, attachments above the text,
 * and a hover button that edits the text and resends it from this point.
 */

import { FileText, Pencil } from "lucide-react";
import { type KeyboardEvent, useCallback, useState } from "react";

import { Button } from "@/components/ui/button";
import { SimpleTooltip } from "@/components/ui/tooltip";
import type { DocumentBlock, ImageBlock, MessageBlock, TextBlock } from "@/types";
import { cn } from "@/utils/cn";

interface UserBubbleProps {
  blocks: MessageBlock[];
  sourceIndex: number | undefined;
  isLoading: boolean;
  pending: boolean;
  onRewindAndSend: ((rewindTo: number, input: string) => Promise<void>) | undefined;
}

/** Attachment card in a user bubble: a labelled file reference. */
function FileCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="max-w-xs min-w-32 rounded-lg border border-border/40 bg-muted/50 px-3 py-2 text-sm text-foreground/80">
      <div className="flex items-center gap-2">
        <FileText className="size-4 shrink-0 text-primary/80" />
        <span className="font-medium">{label}</span>
      </div>
      <div className="mt-1 text-xs break-all text-muted-foreground">{value}</div>
    </div>
  );
}

export function UserBubble({
  blocks,
  sourceIndex,
  isLoading,
  pending,
  onRewindAndSend,
}: UserBubbleProps) {
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");

  const texts: string[] = [];
  const textAttachmentBlocks: TextBlock[] = [];
  const imageBlocks: ImageBlock[] = [];
  const documentBlocks: DocumentBlock[] = [];
  for (const block of blocks) {
    if (block.type === "text") {
      if (block.meta?.["attachment"]) textAttachmentBlocks.push(block);
      else texts.push(block.text);
    } else if (block.type === "image") {
      imageBlocks.push(block);
    } else if (block.type === "document") {
      documentBlocks.push(block);
    }
  }
  const textContent = texts.join("\n\n");

  const canEdit =
    !!textContent &&
    imageBlocks.length === 0 &&
    documentBlocks.length === 0 &&
    textAttachmentBlocks.length === 0 &&
    typeof sourceIndex === "number" &&
    !isLoading &&
    onRewindAndSend;

  const setEditElement = useCallback((el: HTMLTextAreaElement | null) => {
    if (!el) return;
    el.focus();
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, []);

  const submitEdit = () => {
    const trimmed = editText.trim();
    if (!trimmed || !onRewindAndSend || typeof sourceIndex !== "number") return;
    setEditing(false);
    void onRewindAndSend(sourceIndex, trimmed);
  };

  const handleEditKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitEdit();
    } else if (e.key === "Escape") {
      // Consumed here, or the App-level Esc handler stops a running turn.
      e.preventDefault();
      setEditing(false);
    }
  };

  if (editing) {
    return (
      <div className="flex justify-end px-5 max-md:px-4">
        <div className="flex w-full max-w-[85%] flex-col gap-2">
          <textarea
            ref={setEditElement}
            name="edit-message"
            aria-label="Edit message"
            value={editText}
            onChange={(e) => {
              setEditText(e.target.value);
              e.target.style.height = "auto";
              e.target.style.height = `${Math.min(e.target.scrollHeight, 300)}px`;
            }}
            onKeyDown={handleEditKeyDown}
            className="max-h-75 w-full resize-none rounded-lg border border-border/60 bg-muted px-3.5 py-2 text-base leading-relaxed text-foreground focus:border-primary/60 focus:outline-none md:text-sm"
          />
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              size="xs"
              className="font-normal"
              onClick={() => setEditing(false)}
            >
              Cancel
            </Button>
            <Button
              variant={editText.trim() ? "default" : "ghost"}
              size="xs"
              className="font-normal"
              onClick={submitEdit}
              disabled={!editText.trim()}
            >
              Send
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="group/user flex justify-end px-5 max-md:px-4">
      {canEdit && (
        <SimpleTooltip label="Edit & resend">
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Edit message"
            onClick={() => {
              setEditText(textContent);
              setEditing(true);
            }}
            className="mr-2 mb-0.5 self-end opacity-0 transition-[color,background-color,opacity,scale] group-hover/user:opacity-100 focus-visible:opacity-100 max-md:opacity-60"
          >
            <Pencil className="size-3.5" />
          </Button>
        </SimpleTooltip>
      )}
      <div className="flex max-w-[85%] flex-col items-end gap-1.5">
        {imageBlocks.length > 0 && (
          <div className="flex flex-wrap justify-end gap-2">
            {imageBlocks.map((block) =>
              block.data ? (
                <img
                  key={block.renderKey}
                  src={`data:${block.mime_type};base64,${block.data}`}
                  alt={block.name ?? "Image"}
                  className="max-h-64 max-w-full rounded-lg"
                />
              ) : (
                // Workspace image before send: no bytes locally, show a card.
                <FileCard key={block.renderKey} label="Image" value={block.name ?? "image"} />
              ),
            )}
          </div>
        )}
        {documentBlocks.length > 0 && (
          <div className="flex flex-wrap justify-end gap-2">
            {documentBlocks.map((block) => (
              <FileCard key={block.renderKey} label="PDF" value={block.name ?? "document.pdf"} />
            ))}
          </div>
        )}
        {textAttachmentBlocks.length > 0 && (
          <div className="flex flex-wrap justify-end gap-2">
            {textAttachmentBlocks.map((block) => {
              const path = block.meta?.["path"];
              return (
                <FileCard
                  key={block.renderKey}
                  label="Text"
                  value={typeof path === "string" ? path : "attached-file"}
                />
              );
            })}
          </div>
        )}
        {textContent && (
          <div className="flex max-w-full items-center gap-2">
            {pending && (
              // The sidebar's running dot: the message is waiting for the step.
              <span
                aria-hidden="true"
                className="size-1.5 shrink-0 animate-breathing rounded-full bg-primary"
              />
            )}
            <div
              title={pending ? "Waiting for the current step" : undefined}
              className={cn(
                "min-w-0 rounded-lg px-3.5 py-2 text-sm leading-relaxed wrap-anywhere whitespace-pre-wrap",
                pending ? "text-foreground/80 shadow-hairline" : "bg-muted text-foreground",
              )}
            >
              {textContent}
              {pending && <span className="sr-only">Waiting for the current step…</span>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
