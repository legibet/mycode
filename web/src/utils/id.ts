/**
 * Client-side unique id for UI state (React keys, draft sessions).
 * `crypto.randomUUID` is unavailable in non-secure contexts (plain HTTP),
 * so fall back to a timestamp + random suffix.
 */
export function randomId(): string {
  return (
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  );
}
