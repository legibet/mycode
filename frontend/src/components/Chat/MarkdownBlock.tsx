/**
 * Markdown renderer with GFM support and code highlighting.
 * KaTeX is lazy-loaded only when math content is detected.
 */

import { memo, useLayoutEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { CodeBlock } from './CodeBlock'

const REMARK_PLUGINS = [remarkGfm]
const MATH_PATTERN =
  /(\$\$[\s\S]+?\$\$|\$[^\n$]+?\$|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\])/
const MATH_DELIMITERS = [
  { left: '$$', right: '$$', display: true },
  { left: '$', right: '$', display: false },
  { left: '\\(', right: '\\)', display: false },
  { left: '\\[', right: '\\]', display: true },
]

const MARKDOWN_COMPONENTS = {
  pre: ({ children }) => children,
  code: CodeBlock,
}

let katexCssLoaded = false
function ensureKatexCss() {
  if (katexCssLoaded) return
  katexCssLoaded = true
  import('katex/dist/katex.min.css')
}

interface MarkdownContentProps {
  content: string
}

function PlainMarkdown({ content }: MarkdownContentProps) {
  return (
    <ReactMarkdown
      remarkPlugins={REMARK_PLUGINS}
      components={MARKDOWN_COMPONENTS}
    >
      {content}
    </ReactMarkdown>
  )
}

function RenderedMarkdown({ content }: MarkdownContentProps) {
  const contentRef = useRef<HTMLDivElement | null>(null)
  const hasMath = MATH_PATTERN.test(content)

  useLayoutEffect(() => {
    if (!contentRef.current || !hasMath) return

    ensureKatexCss()
    import('katex/contrib/auto-render').then(
      ({ default: renderMathInElement }) => {
        if (!contentRef.current) return
        renderMathInElement(contentRef.current, {
          delimiters: MATH_DELIMITERS,
          throwOnError: false,
        })
      },
    )
  }, [hasMath])

  return (
    <div ref={contentRef}>
      <PlainMarkdown content={content} />
    </div>
  )
}

export const MarkdownBlock = memo(function MarkdownBlock({
  content,
  isStreaming = false,
}: {
  content: string
  isStreaming?: boolean
}) {
  return (
    <div className="prose prose-sm max-w-none dark:prose-invert">
      {isStreaming ? (
        <PlainMarkdown content={content} />
      ) : (
        <RenderedMarkdown key={content} content={content} />
      )}
    </div>
  )
})
