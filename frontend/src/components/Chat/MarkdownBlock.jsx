/**
 * Markdown renderer with GFM support and code highlighting.
 */

import 'katex/dist/katex.min.css'
import ReactMarkdown from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import { normalizeMarkdownMath } from '../../utils/markdown'
import { CodeBlock } from './CodeBlock'

const REMARK_PLUGINS = [remarkGfm, remarkMath]
const REHYPE_PLUGINS = [rehypeKatex]

function isMathClassName(className) {
  return typeof className === 'string' && className.includes('language-math')
}

function MarkdownPre({ children, ...props }) {
  const child = Array.isArray(children) ? children[0] : children
  if (isMathClassName(child?.props?.className)) {
    return (
      <pre className="math-pre" {...props}>
        {children}
      </pre>
    )
  }

  return <>{children}</>
}

function MarkdownCode({ className, children, ...props }) {
  if (isMathClassName(className)) {
    return (
      <code className={className} {...props}>
        {children}
      </code>
    )
  }

  return (
    <CodeBlock className={className} {...props}>
      {children}
    </CodeBlock>
  )
}

const MARKDOWN_COMPONENTS = {
  pre: MarkdownPre,
  code: MarkdownCode,
}

export function MarkdownBlock({ content }) {
  return (
    <div className="prose prose-sm max-w-none dark:prose-invert">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={MARKDOWN_COMPONENTS}
      >
        {normalizeMarkdownMath(content)}
      </ReactMarkdown>
    </div>
  )
}
