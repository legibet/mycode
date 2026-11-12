/**
 * What a view's cards and markers reach in the session that view shows: a
 * subagent's transcript, and a background command's live log. The main view
 * provides the parent session's links, a transcript its child's.
 */

import { createContext, use } from "react";

export interface SessionLinks {
  openAgent: (childId: string) => void;
  /** Absent in a transcript: a subagent runs no background commands. */
  logUrl?: (jobId: string) => string;
}

export const SessionLinksContext = createContext<SessionLinks | null>(null);

/** null outside a view that provides links. */
export function useSessionLinks(): SessionLinks | null {
  return use(SessionLinksContext);
}
