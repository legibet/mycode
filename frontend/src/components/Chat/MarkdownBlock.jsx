/**
 * Markdown renderer with GFM support and code highlighting.
 */

import 'katex/dist/katex.min.css'
import renderMathInElement from 'katex/contrib/auto-render'
import { useLayoutEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { CodeBlock } from './CodeBlock'

const REMARK_PLUGINS = [remarkGfm]
const MATH_DELIMITERS = [
  { left: '$$', right: '$$', display: true },
  { left: '$', right: '$', display: false },
  { left: '\\(', right: '\\)', display: false },
  { left: '\\[', right: '\\]', display: true },
]

const MARKDOWN_COMPONENTS = {
  code: CodeBlock,
}

function PlainMarkdown({ content }) {
  return (
    <ReactMarkdown
      remarkPlugins={REMARK_PLUGINS}
      components={MARKDOWN_COMPONENTS}
    >
      {content}
    </ReactMarkdown>
  )
}

function RenderedMarkdown({ content }) {
  const contentRef = useRef(null)

  useLayoutEffect(() => {
    if (!contentRef.current) return

    renderMathInElement(contentRef.current, {
      delimiters: MATH_DELIMITERS,
      throwOnError: false,
    })
  }, [])

  return (
    <div ref={contentRef}>
      <PlainMarkdown content={content} />
    </div>
  )
}

export function MarkdownBlock({ content, isStreaming = false }) {
  return (
    <div className="prose prose-sm max-w-none dark:prose-invert">
      {isStreaming ? (
        <PlainMarkdown content={content} />
      ) : (
        // KaTeX mutates the DOM, so render it only once after streaming settles.
        <RenderedMarkdown key={content} content={content} />
      )}
    </div>
  )
}
