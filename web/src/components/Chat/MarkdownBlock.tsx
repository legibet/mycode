/**
 * Markdown renderer with GFM, code highlighting, and LaTeX math support.
 * Math is rendered by remark-math + rehype-katex.
 * \(...\) and \[...\] are normalized to dollar delimiters before parsing.
 */

import { type ComponentPropsWithoutRef, memo } from 'react'
import ReactMarkdown, { type Components, type ExtraProps } from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import 'katex/dist/katex.min.css'
import { CodeBlock } from './CodeBlock'

type MarkdownPreProps = ComponentPropsWithoutRef<'pre'> & ExtraProps
type MarkdownTableProps = ComponentPropsWithoutRef<'table'> & ExtraProps

const REMARK_PLUGINS = [remarkGfm, remarkMath]
const REHYPE_PLUGINS = [rehypeKatex]

const MARKDOWN_COMPONENTS: Components = {
  pre: ({ children }: MarkdownPreProps) => children,
  code: CodeBlock,
  table: ({ children, ...props }: MarkdownTableProps) => (
    <div className="my-4 overflow-x-auto scrollbar-subtle">
      <table {...props}>{children}</table>
    </div>
  ),
}

/**
 * Normalize \(...\) and \[...\] without touching code spans or code blocks.
 */
export function normalizeMathDelimiters(text: string): string {
  let result = ''
  let i = 0
  let fenceChar = ''
  let fenceLength = 0

  while (i < text.length) {
    const lineStart = i === 0 || text[i - 1] === '\n'
    const lineEnd = text.indexOf('\n', i)
    const nextLineEnd = lineEnd === -1 ? text.length : lineEnd + 1

    if (lineStart) {
      const line = text.slice(i, nextLineEnd)
      const fenceMatch = /^( {0,3})(`{3,}|~{3,})/.exec(line)

      if (fenceChar) {
        result += line
        if (
          fenceMatch &&
          fenceMatch[2]?.[0] === fenceChar &&
          fenceMatch[2].length >= fenceLength
        ) {
          fenceChar = ''
          fenceLength = 0
        }
        i = nextLineEnd
        continue
      }

      if (fenceMatch) {
        result += line
        fenceChar = fenceMatch[2]?.[0] || ''
        fenceLength = fenceMatch[2]?.length || 0
        i = nextLineEnd
        continue
      }

      if (line.startsWith('    ') || line.startsWith('\t')) {
        result += line
        i = nextLineEnd
        continue
      }
    }

    if (text[i] === '`') {
      let ticks = 1
      while (text[i + ticks] === '`') ticks += 1
      const delimiter = '`'.repeat(ticks)
      const close = text.indexOf(delimiter, i + ticks)

      if (close !== -1) {
        result += text.slice(i, close + ticks)
        i = close + ticks
        continue
      }
    }

    if (text[i] === '\\' && text[i + 1] === '[') {
      const close = text.indexOf('\\]', i + 2)
      if (close !== -1) {
        result += `$$${text.slice(i + 2, close)}$$`
        i = close + 2
        continue
      }
    }

    if (text[i] === '\\' && text[i + 1] === '(') {
      const close = text.indexOf('\\)', i + 2)
      if (close !== -1) {
        const body = text.slice(i + 2, close)
        if (!body.includes('\n')) {
          result += `$${body}$`
          i = close + 2
          continue
        }
      }
    }

    result += text[i]
    i += 1
  }

  return result
}

export const MarkdownBlock = memo(function MarkdownBlock({
  content,
}: {
  content: string
}) {
  return (
    <div className="prose prose-sm max-w-none dark:prose-invert">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={MARKDOWN_COMPONENTS}
      >
        {normalizeMathDelimiters(content)}
      </ReactMarkdown>
    </div>
  )
})
