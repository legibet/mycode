/**
 * Syntax-highlighted code block.
 * Language label and copy button float over code. No border.
 */

import { Check, Copy } from "lucide-react";
import type { ComponentPropsWithoutRef } from "react";
import { useEffect, useRef, useState } from "react";
import type { ExtraProps } from "react-markdown";

import { Button } from "@/components/ui/button";
import { SimpleTooltip } from "@/components/ui/tooltip";

import { copyText } from "../../utils/clipboard";
import { cn } from "../../utils/cn";
import HighlightedCode from "./HighlightedCode";

const LANGUAGE_RE = /language-([a-z0-9+#-]+)/i;
type CodeBlockProps = ComponentPropsWithoutRef<"code"> & ExtraProps;

export function CodeBlock({ className, children, ...props }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const resetCopiedTimeoutRef = useRef<number | null>(null);

  const match = LANGUAGE_RE.exec(className || "");
  const language = match?.[1] ?? "";
  const rawContent = typeof children === "string" ? children : "";
  const codeContent = rawContent.replace(/\n$/, "");

  const isInline = !match && !rawContent.endsWith("\n");

  useEffect(() => {
    return () => {
      if (resetCopiedTimeoutRef.current !== null) {
        window.clearTimeout(resetCopiedTimeoutRef.current);
      }
    };
  }, []);

  const handleCopy = async () => {
    try {
      await copyText(codeContent);
      setCopied(true);
      if (resetCopiedTimeoutRef.current !== null) {
        window.clearTimeout(resetCopiedTimeoutRef.current);
      }
      resetCopiedTimeoutRef.current = window.setTimeout(() => {
        setCopied(false);
        resetCopiedTimeoutRef.current = null;
      }, 2000);
    } catch {
      /* ignore */
    }
  };

  if (isInline) {
    return (
      <code
        className={cn(
          "rounded-sm bg-code px-1.5 py-0.5 font-mono text-ui font-medium text-primary",
          className,
        )}
        {...props}
      >
        {children}
      </code>
    );
  }

  return (
    <div data-code-block className="group/code relative my-3 rounded-md bg-code shadow-hairline">
      {language && (
        <span className="pointer-events-none absolute top-1.5 left-3 z-10 font-mono text-2xs tracking-wider text-muted-foreground/50 uppercase select-none">
          {language}
        </span>
      )}

      <SimpleTooltip label="Copy">
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Copy code"
          onClick={handleCopy}
          className={cn(
            "absolute top-1 right-1 z-10 transition-[color,background-color,opacity,scale]",
            copied
              ? "text-primary opacity-100 hover:text-primary"
              : "opacity-0 group-hover/code:opacity-100 focus-visible:opacity-100 max-md:opacity-60",
          )}
        >
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
        </Button>
      </SimpleTooltip>

      <div className="scrollbar-subtle overflow-x-auto">
        <div className={cn("px-3 pb-2.5", language ? "pt-6" : "pt-2")}>
          <HighlightedCode language={language} code={codeContent} />
        </div>
      </div>
    </div>
  );
}
