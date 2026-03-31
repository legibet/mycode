import { diffLines } from 'diff'
import { use } from 'react'
import type { DiffRow, EditMeta } from '../../types'
import {
  type AppHighlighter,
  codeToHtmlSafely,
  highlighterPromise,
  loadLang,
  type ResolvedLanguage,
  resolveLanguage,
  SHIKI_OPTIONS,
} from '../../utils/highlighter'

const EXT_LANG: Record<string, string> = {
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'jsx',
  ts: 'typescript',
  tsx: 'tsx',
  py: 'python',
  rb: 'ruby',
  rs: 'rust',
  go: 'go',
  java: 'java',
  kt: 'kotlin',
  swift: 'swift',
  c: 'c',
  cpp: 'cpp',
  h: 'c',
  hpp: 'cpp',
  cs: 'csharp',
  php: 'php',
  html: 'html',
  css: 'css',
  scss: 'scss',
  less: 'less',
  json: 'json',
  jsonc: 'jsonc',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'toml',
  md: 'markdown',
  mdx: 'mdx',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  sql: 'sql',
  graphql: 'graphql',
  gql: 'graphql',
  xml: 'xml',
  svg: 'xml',
  vue: 'vue',
  svelte: 'svelte',
}

function getLangFromPath(path?: string): string {
  const ext = path?.split('.').pop()?.toLowerCase()
  return ext ? (EXT_LANG[ext] ?? 'text') : 'text'
}

function escapeHtml(str: string): string {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function splitHtmlLines(html: string): string[] {
  const codeStart = html.indexOf('<code>')
  const codeEnd = html.lastIndexOf('</code>')
  if (codeStart === -1 || codeEnd === -1) return []
  const inner = html.slice(codeStart + 6, codeEnd)
  const marker = '<span class="line">'
  const parts = inner.split(marker)
  return parts.slice(1).map((part) => {
    const lastClose = part.lastIndexOf('</span>')
    return lastClose === -1 ? part : part.slice(0, lastClose)
  })
}

function parseEditResult(result?: string | null): EditMeta | null {
  if (!result || typeof result !== 'string') return null
  try {
    const data = JSON.parse(result) as Partial<EditMeta> & { status?: string }
    if (data.status === 'ok' && typeof data.start_line === 'number') {
      const contextBefore = Array.isArray(data.context_before)
        ? data.context_before.filter(
            (line): line is string => typeof line === 'string',
          )
        : undefined
      const contextAfter = Array.isArray(data.context_after)
        ? data.context_after.filter(
            (line): line is string => typeof line === 'string',
          )
        : undefined

      return {
        status: 'ok',
        start_line: data.start_line,
        ...(contextBefore ? { context_before: contextBefore } : {}),
        ...(contextAfter ? { context_after: contextAfter } : {}),
      }
    }
  } catch {
    /* not JSON, ignore */
  }
  return null
}

function highlight(
  highlighter: AppHighlighter,
  code: string,
  lang: ResolvedLanguage,
): string[] {
  const html = codeToHtmlSafely(highlighter, code, {
    lang,
    ...SHIKI_OPTIONS,
  })
  if (!html) {
    return code.split('\n').map(escapeHtml)
  }
  return splitHtmlLines(html)
}

function buildRows(
  oldText: string | undefined,
  newText: string | undefined,
  oldLines: string[],
  newLines: string[],
  meta: EditMeta | null,
): DiffRow[] {
  const changes = diffLines(oldText || '', newText || '')
  const startLine = meta?.start_line ?? 1
  const ctxBefore = meta?.context_before ?? []
  const ctxAfter = meta?.context_after ?? []

  let ln = startLine - ctxBefore.length
  let oldIdx = 0
  let newIdx = 0
  const rows: DiffRow[] = []

  // Context before (from backend)
  for (let i = 0; i < ctxBefore.length; i++) {
    const line = ctxBefore[i]
    if (line === undefined) continue
    rows.push({
      key: `ctx-before-${ln}`,
      type: 'context',
      ln: ln++,
      html: escapeHtml(line),
    })
  }

  // Diff rows
  for (const change of changes) {
    const lines = change.value.replace(/\n$/, '').split('\n')
    if (change.removed) {
      for (const line of lines) {
        const oldLineIndex = oldIdx
        rows.push({
          key: `removed-${oldLineIndex}`,
          type: 'removed',
          ln: null,
          html: oldLines[oldIdx++] ?? escapeHtml(line),
        })
      }
    } else if (change.added) {
      for (const line of lines) {
        const newLineIndex = newIdx
        rows.push({
          key: `added-${newLineIndex}`,
          type: 'added',
          ln: ln++,
          html: newLines[newIdx++] ?? escapeHtml(line),
        })
      }
    } else {
      for (const line of lines) {
        const oldLineIndex = oldIdx
        rows.push({
          key: `context-${ln}-${oldLineIndex}`,
          type: 'context',
          ln: ln++,
          html: oldLines[oldIdx++] ?? escapeHtml(line),
        })
        newIdx++
      }
    }
  }

  // Context after (from backend)
  for (let i = 0; i < ctxAfter.length; i++) {
    const line = ctxAfter[i]
    if (line === undefined) continue
    rows.push({
      key: `ctx-after-${ln}`,
      type: 'context',
      ln: ln++,
      html: escapeHtml(line),
    })
  }

  return rows
}

// All HTML rendered via dangerouslySetInnerHTML comes from shiki's tokenized
// AST output (only <span> elements with inline styles), not from user input.

interface EditDiffProps {
  path?: string | undefined
  oldText?: string | undefined
  newText?: string | undefined
  result?: string | null | undefined
}

export default function EditDiff({
  path,
  oldText,
  newText,
  result,
}: EditDiffProps) {
  const highlighter = use(highlighterPromise)

  const language = resolveLanguage(getLangFromPath(path))
  const loaded = highlighter.getLoadedLanguages()
  let lang: ResolvedLanguage = loaded.includes(language) ? language : 'text'

  if (lang === 'text' && language !== 'text') {
    const loadResult = loadLang(highlighter, language)
    const resolved = use(loadResult)
    if (resolved) lang = resolved
  }
  const meta = parseEditResult(result)

  // Highlight oldText, newText, and context lines together for proper syntax
  const ctxBeforeText = meta?.context_before?.join('\n') ?? ''
  const ctxAfterText = meta?.context_after?.join('\n') ?? ''

  const fullOldText = [ctxBeforeText, oldText || '', ctxAfterText]
    .filter(Boolean)
    .join('\n')
  const fullOldLines = highlight(highlighter, fullOldText, lang)

  const fullNewText = [ctxBeforeText, newText || '', ctxAfterText]
    .filter(Boolean)
    .join('\n')
  const fullNewLines = highlight(highlighter, fullNewText, lang)

  // Split highlighted lines back into context/diff sections
  const ctxBeforeCount = meta?.context_before?.length ?? 0
  const ctxAfterCount = meta?.context_after?.length ?? 0

  const oldDiffLines = fullOldLines.slice(
    ctxBeforeCount,
    fullOldLines.length - ctxAfterCount,
  )
  const newDiffLines = fullNewLines.slice(
    ctxBeforeCount,
    fullNewLines.length - ctxAfterCount,
  )
  const contextBefore = meta?.context_before ?? []
  const contextAfter = meta?.context_after ?? []

  // Overwrite context_before/after with highlighted versions
  const highlightedMeta = meta
    ? {
        ...meta,
        context_before: contextBefore.map(
          (line, i) => fullOldLines[i] ?? escapeHtml(line),
        ),
        context_after: contextAfter.map(
          (line, i) =>
            fullOldLines[fullOldLines.length - ctxAfterCount + i] ??
            escapeHtml(line),
        ),
      }
    : null

  const rows = buildRows(
    oldText,
    newText,
    oldDiffLines,
    newDiffLines,
    highlightedMeta,
  )

  const hasLineNumbers = meta !== null

  return (
    <div className="rounded-md bg-code overflow-hidden">
      {path && (
        <div className="px-3 pt-2">
          <span className="text-[11px] font-mono text-muted-foreground/30 tracking-wider select-none">
            {path}
          </span>
        </div>
      )}
      <div className="overflow-x-auto scrollbar-subtle">
        <table
          className="w-full border-collapse"
          style={{
            fontFamily: '"DM Mono", "JetBrains Mono", monospace',
            fontSize: '13px',
            lineHeight: '1.5',
          }}
        >
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.key}
                className={
                  row.type === 'removed'
                    ? 'diff-line-removed'
                    : row.type === 'added'
                      ? 'diff-line-added'
                      : ''
                }
              >
                {hasLineNumbers && (
                  <td className="diff-ln select-none w-8 min-w-8 text-right align-top pr-2 text-muted-foreground/20 tabular-nums">
                    {row.ln ?? ''}
                  </td>
                )}
                <td
                  className={`select-none w-5 min-w-5 text-center align-top ${
                    row.type === 'removed'
                      ? 'diff-gutter-removed'
                      : row.type === 'added'
                        ? 'diff-gutter-added'
                        : 'text-transparent'
                  }`}
                >
                  {row.type === 'removed'
                    ? '\u2212'
                    : row.type === 'added'
                      ? '+'
                      : '\u00A0'}
                </td>
                <td className="pr-3 whitespace-pre">
                  <span
                    className="shiki"
                    // biome-ignore lint/security/noDangerouslySetInnerHtml: shiki tokenized AST output
                    dangerouslySetInnerHTML={{ __html: row.html }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
