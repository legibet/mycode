/**
 * Controlled listbox rendered above the input area for slash / @ completion.
 * The textarea keeps focus and drives all keyboard state; this component only
 * renders items and reports pointer interactions.
 */

import { useEffect } from "react";

import { cn } from "../../utils/cn";

export interface CompletionItem {
  id: string;
  label: string;
  hint?: string;
  /** Shown but not selectable (e.g. image file the model can't accept). */
  disabled?: boolean;
}

export function completionItemDomId(menuId: string, index: number): string {
  return `${menuId}-item-${index}`;
}

interface CompletionMenuProps {
  menuId: string;
  items: CompletionItem[];
  activeIndex: number;
  /** Muted status row below the list (e.g. "N more matches…"). */
  footer?: string | undefined;
  onSelect: (index: number) => void;
  onHighlight: (index: number) => void;
}

export function CompletionMenu({
  menuId,
  items,
  activeIndex,
  footer,
  onSelect,
  onHighlight,
}: CompletionMenuProps) {
  useEffect(() => {
    document
      .getElementById(completionItemDomId(menuId, activeIndex))
      ?.scrollIntoView({ block: "nearest" });
  }, [menuId, activeIndex]);

  return (
    <div className="absolute inset-x-0 bottom-full z-20 mb-2">
      <div
        id={menuId}
        role="listbox"
        aria-label="Completions"
        className="max-h-60 overflow-y-auto rounded-md bg-popover p-1 text-popover-foreground shadow-overlay"
      >
        {items.map((item, index) => (
          // Options in an aria-activedescendant listbox; the textarea keeps focus and handles keys.
          // oxlint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/interactive-supports-focus
          <div
            key={item.id}
            id={completionItemDomId(menuId, index)}
            role="option"
            aria-selected={index === activeIndex}
            aria-disabled={item.disabled || undefined}
            className={cn(
              "grid cursor-pointer grid-cols-[8rem_minmax(0,1fr)] items-baseline gap-2 rounded-sm px-2 py-1.5 select-none md:grid-cols-[12rem_minmax(0,1fr)]",
              index === activeIndex && "bg-muted",
              item.disabled && "cursor-default opacity-50",
            )}
            // Keep the textarea focused (and the soft keyboard open) when
            // tapping an item.
            onPointerDown={(e) => e.preventDefault()}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => onSelect(index)}
            onPointerMove={() => onHighlight(index)}
          >
            <span className="truncate font-mono text-[13px] text-foreground">{item.label}</span>
            {item.hint && (
              <span className="max-w-xl truncate text-left text-xs text-muted-foreground">
                {item.hint}
              </span>
            )}
          </div>
        ))}
        {footer && <div className="px-2 py-1.5 text-xs text-muted-foreground">{footer}</div>}
      </div>
    </div>
  );
}
