/**
 * Markdown renderer with GFM support and code highlighting.
 */

import 'katex/dist/katex.min.css'
import renderMathInElement from 'katex/contrib/auto-render'
import { useEffect, useRef } from 'react'
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

function RenderedMarkdown({ content }) {
  const contentRef = useRef(null)

  useEffect(() => {
    if (!contentRef.current) return

    renderMathInElement(contentRef.current, {
      delimiters: MATH_DELIMITERS,
      throwOnError: false,
    })
  }, [])

  return (
    <div ref={contentRef}>
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        components={MARKDOWN_COMPONENTS}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}

export function MarkdownBlock({ content }) {
  return (
    <div className="prose prose-sm max-w-none dark:prose-invert">
      {/* KaTeX mutates the rendered DOM, so remount the markdown subtree when content changes. */}
      <RenderedMarkdown key={content} content={content} />
    </div>
  )
}
