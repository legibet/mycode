/**
 * Inline divider rendered where the agent compacted the conversation.
 */

export function CompactMarker() {
  return (
    <div className="flex select-none items-center gap-3 px-2 py-1">
      <div className="h-px flex-1 bg-border/40" />
      <span className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground/50">
        compacted
      </span>
      <div className="h-px flex-1 bg-border/40" />
    </div>
  )
}
