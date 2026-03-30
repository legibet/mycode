import { diffLines } from 'diff'
import { use } from 'react'
import {
  codeToHtmlSafely,
  highlighterPromise,
  loadLang,
  resolveLanguage,
  SHIKI_OPTIONS,
} from '../../utils/highlighter'

const EXT_LANG = {
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

function getLangFromPath(path) {
  const ext = path?.split('.').pop()?.toLowerCase()
  return EXT_LANG[ext] || 'text'
}

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function splitHtmlLines(html) {
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

function parseEditResult(result) {
  if (!result || typeof result !== 'string') return null
  try {
    const data = JSON.parse(result)
    if (data.status === 'ok' && typeof data.start_line === 'number') return data
  } catch {
    /* not JSON, ignore */
  }
  return null
}

function highlight(highlighter, code, opts) {
  const html = codeToHtmlSafely(highlighter, code, opts)
  if (!html) {
    return code.split('\n').map(escapeHtml)
  }
  return splitHtmlLines(html)
}

function buildRows(oldText, newText, oldLines, newLines, meta) {
  const changes = diffLines(oldText || '', newText || '')
  const startLine = meta?.start_line ?? 1
  const ctxBefore = meta?.context_before ?? []
  const ctxAfter = meta?.context_after ?? []

  let ln = startLine - ctxBefore.length
  let oldIdx = 0
  let newIdx = 0
  const rows = []

  // Context before (from backend)
  for (let i = 0; i < ctxBefore.length; i++) {
    rows.push({
      key: `ctx-before-${ln}`,
      type: 'context',
      ln: ln++,
      html: escapeHtml(ctxBefore[i]),
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
    rows.push({
      key: `ctx-after-${ln}`,
      type: 'context',
      ln: ln++,
      html: escapeHtml(ctxAfter[i]),
    })
  }

  return rows
}

// All HTML rendered via dangerouslySetInnerHTML comes from shiki's tokenized
// AST output (only <span> elements with inline styles), not from user input.

export default function EditDiff({ path, oldText, newText, result }) {
  const highlighter = use(highlighterPromise)

  const language = resolveLanguage(getLangFromPath(path))
  const loaded = highlighter.getLoadedLanguages()
  let lang = loaded.includes(language) ? language : 'text'

  if (lang === 'text' && language !== 'text') {
    const loadResult = loadLang(highlighter, language)
    if (loadResult instanceof Promise) {
      const resolved = use(loadResult)
      if (resolved) lang = resolved
    }
  }

  const opts = { lang, ...SHIKI_OPTIONS }
  const meta = parseEditResult(result)

  // Highlight oldText, newText, and context lines together for proper syntax
  const ctxBeforeText = meta?.context_before?.join('\n') ?? ''
  const ctxAfterText = meta?.context_after?.join('\n') ?? ''

  const fullOldText = [ctxBeforeText, oldText || '', ctxAfterText]
    .filter(Boolean)
    .join('\n')
  const fullOldLines = highlight(highlighter, fullOldText, opts)

  const fullNewText = [ctxBeforeText, newText || '', ctxAfterText]
    .filter(Boolean)
    .join('\n')
  const fullNewLines = highlight(highlighter, fullNewText, opts)

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

  // Overwrite context_before/after with highlighted versions
  const highlightedMeta = meta
    ? {
        ...meta,
        context_before: meta.context_before?.map(
          (_, i) => fullOldLines[i] ?? escapeHtml(meta.context_before[i]),
        ),
        context_after: meta.context_after?.map(
          (_, i) =>
            fullOldLines[fullOldLines.length - ctxAfterCount + i] ??
            escapeHtml(meta.context_after[i]),
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
      <div className="overflow-x-auto">
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
