/**
 * Chat input area with text/image/PDF attachment.
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

// File pickers only understand MIME types and extensions, so keep the text
// allowlist explicit here.
const TEXT_FILE_ACCEPT = [
  'text/*',
  '.txt',
  '.md',
  '.mdx',
  '.rst',
  '.json',
  '.jsonl',
  '.yaml',
  '.yml',
  '.toml',
  '.ini',
  '.cfg',
  '.conf',
  '.xml',
  '.html',
  '.htm',
  '.css',
  '.scss',
  '.sass',
  '.less',
  '.js',
  '.jsx',
  '.mjs',
  '.cjs',
  '.ts',
  '.tsx',
  '.mts',
  '.cts',
  '.py',
  '.rb',
  '.php',
  '.go',
  '.rs',
  '.java',
  '.kt',
  '.swift',
  '.c',
  '.cc',
  '.cpp',
  '.cxx',
  '.h',
  '.hh',
  '.hpp',
  '.m',
  '.mm',
  '.sh',
  '.bash',
  '.zsh',
  '.fish',
  '.ps1',
  '.sql',
  '.graphql',
  '.gql',
  '.proto',
  '.csv',
  '.tsv',
  '.log',
  '.env',
  '.gitignore',
  '.gitattributes',
  '.editorconfig',
  '.npmrc',
  '.yarnrc',
  '.pnpmrc',
].join(',')

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

async function readFileAsUtf8(file: File): Promise<string | null> {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(
      await file.arrayBuffer(),
    )
  } catch {
    return null
  }
}

async function processFiles(
  files: File[],
  {
    supportsImages,
    supportsDocuments,
  }: { supportsImages: boolean; supportsDocuments: boolean },
): Promise<AttachedFile[]> {
  const attachedFiles = await Promise.all(
    files.map(async (file) => {
      if (file.type.startsWith('image/')) {
        if (!supportsImages) return null
        return {
          kind: 'image' as const,
          data: await readFileAsBase64(file),
          mime_type: file.type,
          name: file.name,
          preview: URL.createObjectURL(file),
        }
      }

      const isPdfFile =
        file.type === 'application/pdf' ||
        file.name.toLowerCase().endsWith('.pdf')
      if (isPdfFile) {
        if (!supportsDocuments) return null
        return {
          kind: 'document' as const,
          data: await readFileAsBase64(file),
          mime_type: 'application/pdf' as const,
          name: file.name,
        }
      }

      const text = await readFileAsUtf8(file)
      if (text === null) return null
      return { kind: 'text' as const, text, name: file.name }
    }),
  )
  return attachedFiles.filter((file) => file !== null)
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
    await attachFiles(Array.from(e.dataTransfer.files))
  }

  const hasInput = input.trim().length > 0 || files.length > 0
  const accept = [
    TEXT_FILE_ACCEPT,
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
          dragging ? 'border-accent/50 bg-accent/5' : 'border-border/25',
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
                    <div className="min-w-0">
                      <div className="text-[10px] uppercase tracking-[0.16em] text-muted-foreground/70">
                        {file.kind === 'document' ? 'PDF' : 'Text'}
                      </div>
                      <div className="line-clamp-2 break-all">{file.name}</div>
                    </div>
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

        <input
          ref={fileInputRef}
          type="file"
          accept={accept}
          multiple
          className="hidden"
          onChange={handleFileChange}
        />

        <div className="relative">
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
            className="block w-full resize-none bg-transparent py-3 max-md:py-2.5 pr-14 pl-12 max-md:pl-11 text-base md:text-sm leading-relaxed text-foreground placeholder:text-muted-foreground/40 focus-visible:outline-none max-h-[200px]"
          />

          <div className="absolute inset-y-0 left-2.5 max-md:left-2 flex items-center">
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

          <div className="absolute inset-y-0 right-2.5 max-md:right-2 flex items-center">
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

        {dragging && (
          <div className="absolute inset-0 flex items-center justify-center rounded-xl bg-accent/5 pointer-events-none z-10">
            <span className="text-sm text-accent font-medium">
              Drop file here
            </span>
          </div>
        )}
      </div>
    </div>
  )
})
