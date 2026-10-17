import type { ReactNode } from "react";

import { cn } from "@/utils/cn";

/** Animates its children's height open and closed; closed content is inert. */
export function Collapse({ open, children }: { open: boolean; children: ReactNode }) {
  return (
    <div
      inert={!open}
      className={cn(
        "grid transition-[grid-template-rows,opacity] duration-300 ease-in-out",
        open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
      )}
    >
      <div className="overflow-hidden">{children}</div>
    </div>
  );
}
