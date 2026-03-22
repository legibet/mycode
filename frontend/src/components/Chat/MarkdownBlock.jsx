/**
 * Markdown renderer with GFM support and code highlighting.
 */

import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { CodeBlock } from './CodeBlock'

const REMARK_PLUGINS = [remarkGfm]
const MARKDOWN_COMPONENTS = {
  pre: ({ children }) => <>{children}</>,
  code: CodeBlock,
}

export function MarkdownBlock({ content }) {
  return (
    <div className="prose prose-sm max-w-none dark:prose-invert">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        components={MARKDOWN_COMPONENTS}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
