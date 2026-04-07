/**
 * Chat input area with optional image/PDF attachment.
 */

import { ArrowUp, FileText, Paperclip, Square, X } from 'lucide-react'
import {
  type ChangeEvent,
  type DragEvent,
  type KeyboardEvent,
  memo,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react'
import type { AttachedFile, SetString } from '../../types'
import { cn } from '../../utils/cn'

interface InputAreaProps {
  input: string
  setInput: SetString
  loading: boolean
  onSend: () => void
  onCancel: () => void
  supportsImages?: boolean
  supportsDocuments?: boolean
  files?: AttachedFile[]
  onAttachFiles?: (files: AttachedFile[]) => void
  onRemoveFile?: (index: number) => void
}

function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = reader.result as string
      resolve(result.split(',')[1] ?? '')
    }
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

async function processFiles(
  files: File[],
  {
    supportsImages,
    supportsDocuments,
  }: { supportsImages: boolean; supportsDocuments: boolean },
): Promise<AttachedFile[]> {
  const acceptedFiles = files.filter((file) => {
    if (supportsImages && file.type.startsWith('image/')) return true
    return (
      supportsDocuments &&
      (file.type === 'application/pdf' ||
        file.name.toLowerCase().endsWith('.pdf'))
    )
  })
  if (!acceptedFiles.length) return []
  return Promise.all(
    acceptedFiles.map(async (file) => {
      const mimeType =
        file.type === 'application/pdf' ||
        file.name.toLowerCase().endsWith('.pdf')
          ? 'application/pdf'
          : file.type
      if (mimeType === 'application/pdf') {
        return {
          kind: 'document' as const,
          data: await readFileAsBase64(file),
          mime_type: 'application/pdf' as const,
          name: file.name,
        }
      }
      return {
        kind: 'image' as const,
        data: await readFileAsBase64(file),
        mime_type: mimeType,
        name: file.name,
        preview: URL.createObjectURL(file),
      }
    }),
  )
}

export const InputArea = memo(function InputArea({
  input,
  setInput,
  loading,
  onSend,
  onCancel,
  supportsImages = false,
  supportsDocuments = false,
  files = [],
  onAttachFiles,
  onRemoveFile,
}: InputAreaProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [dragging, setDragging] = useState(false)
  const dragCounterRef = useRef(0)

  useEffect(() => {
    if (!input && textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }, [input])

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (!loading) onSend()
    }
  }

  const attachFiles = useCallback(
    async (files: File[]) => {
      const nextFiles = await processFiles(files, {
        supportsImages,
        supportsDocuments,
      })
      if (nextFiles.length) onAttachFiles?.(nextFiles)
    },
    [onAttachFiles, supportsDocuments, supportsImages],
  )

  const handleFileChange = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? [])
    e.target.value = ''
    await attachFiles(files)
  }

  const handleDragEnter = (e: DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dragCounterRef.current++
    if (dragCounterRef.current === 1) setDragging(true)
  }

  const handleDragLeave = (e: DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dragCounterRef.current--
    if (dragCounterRef.current === 0) setDragging(false)
  }

  const handleDragOver = (e: DragEvent) => {
    e.preventDefault()
  }

  const handleDrop = async (e: DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dragCounterRef.current = 0
    setDragging(false)
    if (!supportsImages && !supportsDocuments) return
    await attachFiles(Array.from(e.dataTransfer.files))
  }

  const hasAttachments = supportsImages || supportsDocuments
  const hasInput = input.trim().length > 0 || files.length > 0
  const accept = [
    supportsImages ? 'image/*' : null,
    supportsDocuments ? '.pdf,application/pdf' : null,
  ]
    .filter(Boolean)
    .join(',')

  return (
    <div className="mx-auto max-w-4xl max-md:max-w-none px-5 max-md:px-3 py-3 max-md:py-2">
      {/* biome-ignore lint/a11y/noStaticElementInteractions: drag-and-drop drop target */}
      <div
        role="presentation"
        className={cn(
          'relative rounded-xl bg-card border shadow-sm transition duration-200',
          'focus-within:shadow-md focus-within:border-border/50',
          dragging && hasAttachments
            ? 'border-accent/50 bg-accent/5'
            : 'border-border/25',
        )}
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
      >
        {files.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-3 pt-2.5 pb-1">
            {files.map((file, i) => (
              <div
                key={`${file.kind}:${file.name}:${i}`}
                className="relative group/thumb flex-shrink-0"
              >
                {file.kind === 'image' ? (
                  <img
                    src={file.preview}
                    alt={file.name}
                    className="h-14 w-14 rounded-lg object-cover border border-border/30"
                  />
                ) : (
                  <div className="h-14 min-w-28 rounded-lg border border-border/30 bg-muted/30 px-3 flex items-center gap-2 text-xs text-foreground/80">
                    <FileText className="h-4 w-4 shrink-0 text-accent/80" />
                    <span className="line-clamp-2 break-all">{file.name}</span>
                  </div>
                )}
                <button
                  type="button"
                  onClick={() => onRemoveFile?.(i)}
                  aria-label={`Remove ${file.name}`}
                  className="absolute -top-1 -right-1 h-4 w-4 bg-foreground text-background rounded-full flex items-center justify-center opacity-0 group-hover/thumb:opacity-100 max-md:opacity-100 transition-opacity"
                >
                  <X className="h-2.5 w-2.5" />
                </button>
              </div>
            ))}
          </div>
        )}

        <textarea
          ref={textareaRef}
          rows={1}
          name="message"
          aria-label="Message"
          value={input}
          onChange={(e) => {
            setInput(e.target.value)
            e.target.style.height = 'auto'
            e.target.style.height = `${Math.min(e.target.scrollHeight, 200)}px`
          }}
          onKeyDown={handleKeyDown}
          placeholder="Message…"
          className={cn(
            'block w-full resize-none bg-transparent py-3 max-md:py-2.5 pr-14 text-base md:text-sm leading-relaxed text-foreground placeholder:text-muted-foreground/40 focus-visible:outline-none max-h-[200px]',
            hasAttachments ? 'pl-12 max-md:pl-11' : 'px-4',
          )}
        />

        {dragging && hasAttachments && (
          <div className="absolute inset-0 flex items-center justify-center rounded-xl bg-accent/5 pointer-events-none z-10">
            <span className="text-sm text-accent font-medium">
              Drop file here
            </span>
          </div>
        )}

        {hasAttachments && (
          <>
            <input
              ref={fileInputRef}
              type="file"
              accept={accept}
              multiple
              className="hidden"
              onChange={handleFileChange}
            />
            <div className="absolute bottom-0 left-2.5 max-md:left-2 h-[calc(100%-1px)] flex items-center">
              <button
                type="button"
                aria-label="Attach file"
                disabled={loading}
                onClick={() => fileInputRef.current?.click()}
                className={cn(
                  'h-8 w-8 flex items-center justify-center rounded-lg transition duration-150',
                  loading
                    ? 'text-muted-foreground/20'
                    : 'text-muted-foreground/40 hover:text-muted-foreground/70 hover:bg-muted/50 active:scale-95',
                )}
                title="Attach file"
              >
                <Paperclip className="h-4 w-4" />
              </button>
            </div>
          </>
        )}

        <div className="absolute bottom-0 right-2.5 max-md:right-2 h-[calc(100%-1px)] flex items-center">
          {loading ? (
            <button
              type="button"
              aria-label="Stop generating"
              onClick={onCancel}
              className="h-8 w-8 flex items-center justify-center rounded-lg text-destructive/70 hover:text-destructive hover:bg-destructive/10 active:scale-95 transition"
              title="Stop"
            >
              <Square className="h-3.5 w-3.5 fill-current" />
            </button>
          ) : (
            <button
              type="button"
              aria-label="Send message"
              onClick={onSend}
              disabled={!hasInput}
              className={cn(
                'h-8 w-8 flex items-center justify-center rounded-lg transition duration-150',
                hasInput
                  ? 'bg-foreground text-background hover:opacity-90 active:scale-95'
                  : 'text-muted-foreground/40',
              )}
              title="Send"
            >
              <ArrowUp className="h-4 w-4" strokeWidth={2.5} />
            </button>
          )}
        </div>
      </div>
    </div>
  )
})
