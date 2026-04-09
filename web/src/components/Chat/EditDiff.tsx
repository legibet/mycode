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

/** Context lines to show around each edit (matches backend ctx=3). */
const CTX = 3

/**
 * Build diff rows for a single edit, including context-before/after.
 * Collapse long unchanged runs within the diff itself (agents may send
 * broad oldText/newText with lots of unchanged lines).
 */
function buildDiffRows(
  oldText: string | undefined,
  newText: string | undefined,
  oldHighlighted: string[],
  newHighlighted: string[],
): DiffRow[] {
  const changes = diffLines(oldText || '', newText || '')
  let ln = 0 // relative line counter, caller adjusts via startLine
  let oldIdx = 0
  let newIdx = 0
  const rows: DiffRow[] = []

  for (const change of changes) {
    const lines = change.value.replace(/\n$/, '').split('\n')
    if (change.removed) {
      for (const line of lines) {
        rows.push({
          key: `r-${oldIdx}`,
          type: 'removed',
          ln: null,
          html: oldHighlighted[oldIdx++] ?? escapeHtml(line),
        })
      }
    } else if (change.added) {
      for (const line of lines) {
        rows.push({
          key: `a-${newIdx}`,
          type: 'added',
          ln: ln++,
          html: newHighlighted[newIdx++] ?? escapeHtml(line),
        })
      }
    } else {
      for (const line of lines) {
        rows.push({
          key: `c-${ln}-${oldIdx}`,
          type: 'context',
          ln: ln++,
          html: oldHighlighted[oldIdx++] ?? escapeHtml(line),
        })
        newIdx++
      }
    }
  }

  // Collapse long context runs, keeping CTX lines around actual changes.
  const keep = new Uint8Array(rows.length)
  for (let i = 0; i < rows.length; i++) {
    if (rows[i]!.type !== 'context') {
      const lo = Math.max(0, i - CTX)
      const hi = Math.min(rows.length - 1, i + CTX)
      for (let j = lo; j <= hi; j++) keep[j] = 1
    }
  }
  const hasChanges = rows.some((r) => r.type !== 'context')
  if (!hasChanges) return rows // no diff — keep all
  return rows.filter((_, i) => keep[i])
}

// All HTML rendered via dangerouslySetInnerHTML comes from shiki's tokenized
// AST output (only <span> elements with inline styles), not from user input.

export interface EditItem {
  oldText: string
  newText: string
  meta?: EditMeta | null
}

interface EditDiffProps {
  path?: string | undefined
  edits: EditItem[]
}

/**
 * Highlight an edit's old/new text together with its context lines,
 * then split back into context / diff sections.
 */
function highlightEdit(
  highlighter: AppHighlighter,
  lang: ResolvedLanguage,
  oldText: string,
  newText: string,
  ctxBefore: string[],
  ctxAfter: string[],
): {
  oldHighlighted: string[]
  newHighlighted: string[]
  ctxBeforeHtml: string[]
  ctxAfterHtml: string[]
} {
  const ctxBeforeText = ctxBefore.join('\n')
  const ctxAfterText = ctxAfter.join('\n')

  const fullOldText = [ctxBeforeText, oldText || '', ctxAfterText]
    .filter(Boolean)
    .join('\n')
  const fullOldLines = highlight(highlighter, fullOldText, lang)

  const fullNewText = [ctxBeforeText, newText || '', ctxAfterText]
    .filter(Boolean)
    .join('\n')
  const fullNewLines = highlight(highlighter, fullNewText, lang)

  const bc = ctxBefore.length
  const ac = ctxAfter.length

  return {
    oldHighlighted: fullOldLines.slice(bc, fullOldLines.length - ac),
    newHighlighted: fullNewLines.slice(bc, fullNewLines.length - ac),
    ctxBeforeHtml: ctxBefore.map(
      (line, i) => fullOldLines[i] ?? escapeHtml(line),
    ),
    ctxAfterHtml: ctxAfter.map(
      (line, i) =>
        fullOldLines[fullOldLines.length - ac + i] ?? escapeHtml(line),
    ),
  }
}

/**
 * Merge multiple edits into unified diff rows, handling overlapping
 * context regions like standard unified diff hunks.
 *
 * Edits are assumed sorted by start_line (backend guarantees this).
 * Adjacent edits whose context regions overlap or touch are merged
 * into a single continuous hunk; otherwise a separator is inserted.
 */
function buildMergedRows(
  edits: EditItem[],
  highlighter: AppHighlighter,
  lang: ResolvedLanguage,
): DiffRow[] {
  const allRows: DiffRow[] = []

  // Track where the previous edit's display ended (line number)
  let prevDisplayEnd = -Infinity

  for (let i = 0; i < edits.length; i++) {
    const edit = edits[i]!
    const meta = edit.meta ?? null
    const startLine = meta?.start_line ?? 1
    const newLc = meta?.new_line_count ?? (edit.newText.split('\n').length || 1)
    const fullCtxBefore = meta?.context_before ?? []
    const fullCtxAfter = meta?.context_after ?? []

    // Where this edit's display region starts (in file line numbers)
    const displayStart = startLine - fullCtxBefore.length

    // Trim context_before to avoid overlapping with previous edit's range.
    // prevDisplayEnd is exclusive, displayStart is inclusive.
    let ctxBefore = fullCtxBefore
    if (prevDisplayEnd > -Infinity) {
      const overlap = prevDisplayEnd - displayStart
      if (overlap > 0) {
        // Overlapping — drop lines already shown by previous edit
        ctxBefore = fullCtxBefore.slice(overlap)
      } else if (overlap < 0) {
        // Gap between edits — insert separator
        allRows.push({
          key: `sep-${i}`,
          type: 'separator',
          ln: null,
          html: '',
        })
      }
      // overlap === 0: exactly adjacent, keep full ctxBefore, no separator
    }

    // For the last edit in a merged group that's followed by another edit,
    // trim context_after to avoid overlapping with next edit's range.
    let ctxAfter = fullCtxAfter
    if (i < edits.length - 1) {
      const nextMeta = edits[i + 1]!.meta ?? null
      const nextStartLine = nextMeta?.start_line ?? 1
      const nextCtxBefore = nextMeta?.context_before ?? []
      const nextDisplayStart = nextStartLine - nextCtxBefore.length
      const afterStart = startLine + newLc
      const afterEnd = afterStart + fullCtxAfter.length
      if (afterEnd > nextDisplayStart) {
        // Trim context_after to not extend into next edit's territory
        const trimTo = Math.max(0, nextDisplayStart - afterStart)
        ctxAfter = fullCtxAfter.slice(0, trimTo)
      }
    }

    // Highlight with trimmed context
    const { oldHighlighted, newHighlighted, ctxBeforeHtml, ctxAfterHtml } =
      highlightEdit(
        highlighter,
        lang,
        edit.oldText,
        edit.newText,
        ctxBefore,
        ctxAfter,
      )

    // Context-before rows
    let ln = startLine - ctxBefore.length
    for (let j = 0; j < ctxBefore.length; j++) {
      allRows.push({
        key: `e${i}-cb-${ln}`,
        type: 'context',
        ln: ln++,
        html: ctxBeforeHtml[j] ?? escapeHtml(ctxBefore[j]!),
      })
    }

    // Diff rows (with startLine offset applied)
    const diffRows = buildDiffRows(
      edit.oldText,
      edit.newText,
      oldHighlighted,
      newHighlighted,
    )
    for (const row of diffRows) {
      allRows.push({
        ...row,
        key: `e${i}-${row.key}`,
        ln: row.ln !== null ? row.ln + startLine : null,
      })
    }

    // Context-after rows
    let afterLn = startLine + newLc
    for (let j = 0; j < ctxAfter.length; j++) {
      allRows.push({
        key: `e${i}-ca-${afterLn}`,
        type: 'context',
        ln: afterLn++,
        html: ctxAfterHtml[j] ?? escapeHtml(ctxAfter[j]!),
      })
    }

    prevDisplayEnd = startLine + newLc + ctxAfter.length
  }

  return allRows
}

export default function EditDiff({ path, edits }: EditDiffProps) {
  const highlighter = use(highlighterPromise)

  const language = resolveLanguage(getLangFromPath(path))
  const loaded = highlighter.getLoadedLanguages()
  let lang: ResolvedLanguage = loaded.includes(language) ? language : 'text'

  if (lang === 'text' && language !== 'text') {
    const loadResult = loadLang(highlighter, language)
    const resolved = use(loadResult)
    if (resolved) lang = resolved
  }

  const allRows = buildMergedRows(edits, highlighter, lang)
  const hasLineNumbers = edits.some((e) => e.meta != null)

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
            {allRows.map((row) =>
              row.type === 'separator' ? (
                <tr key={row.key}>
                  <td
                    colSpan={hasLineNumbers ? 3 : 2}
                    className="text-center text-muted-foreground/20 select-none py-0.5 text-xs"
                  >
                    ···
                  </td>
                </tr>
              ) : (
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
              ),
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
