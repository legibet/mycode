/** Slash command, skill reference, and @path matching for the composer menu. */

export interface SlashCommand {
  name: "/new" | "/clear" | "/compact";
  description: string;
  /** Require a second Enter on a confirm row before executing. */
  confirm?: boolean;
}

export const SLASH_COMMANDS: readonly SlashCommand[] = [
  { name: "/new", description: "New session" },
  { name: "/clear", description: "Clear conversation", confirm: true },
  { name: "/compact", description: "Compact conversation context" },
];

/**
 * Slash menu candidates for the current input. The menu only opens while the
 * whole input is a single slash token ("/", "/c", "/clear"); a "/" anywhere
 * else stays plain text, and unknown commands ("/usr/local") match nothing so
 * they submit as a normal message.
 */
export function matchSlashCommands(input: string): SlashCommand[] {
  if (!/^\/\S*$/.test(input)) return [];
  return SLASH_COMMANDS.filter((command) => command.name.startsWith(input));
}

const SKILL_TOKEN_RE = /(?<!\S)\/([a-zA-Z0-9_-]*)$/;

export interface SkillQuery {
  prefix: string;
  start: number;
}

/** Parse a standalone `/skill-name` token immediately before the cursor. */
export function matchSkillQuery(textBeforeCursor: string): SkillQuery | null {
  const match = SKILL_TOKEN_RE.exec(textBeforeCursor);
  if (!match) return null;
  return { prefix: match[1] ?? "", start: match.index };
}

// A standalone `@token` at the cursor: bare `@src/co` or double-quoted
// `@"my dir/co` (quote stays open while browsing a path with spaces).
const AT_TOKEN_RE = /(?<!\S)@(?:"([^"]*)|([^\s"]*))$/;

export interface AtQuery {
  /** Directory part relative to cwd, e.g. "src/components/" or "". */
  dir: string;
  /** Filename prefix within `dir` to filter by. */
  prefix: string;
  /** Start offset of the `@` in the text, for replacement. */
  start: number;
}

/** Parse the `@` reference immediately before the cursor, if any. */
export function matchAtQuery(textBeforeCursor: string): AtQuery | null {
  const match = AT_TOKEN_RE.exec(textBeforeCursor);
  if (!match) return null;
  const raw = match[1] ?? match[2] ?? "";
  const slash = raw.lastIndexOf("/");
  return {
    dir: slash === -1 ? "" : raw.slice(0, slash + 1),
    prefix: slash === -1 ? raw : raw.slice(slash + 1),
    start: match.index,
  };
}

/**
 * Text inserted when a directory is picked from the @ menu: browsing
 * continues inside the token, so a path with spaces opens a double quote
 * that stays unclosed until a file is picked (which becomes a pill node).
 */
export function dirTokenText(path: string): string {
  return path.includes(" ") ? `@"${path}` : `@${path}`;
}

export interface WorkspaceEntry {
  name: string;
  path: string;
  kind: "directory" | "text" | "image" | "document";
}

export interface WorkspaceFilesResponse {
  entries: WorkspaceEntry[];
  truncated: boolean;
  error: string;
}
