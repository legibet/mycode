/**
 * Lexical editor operations behind the composer. Each runs inside
 * editor.read() or editor.update().
 */

import {
  $createLineBreakNode,
  $createParagraphNode,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isElementNode,
  $isRangeSelection,
  $isTextNode,
  type LexicalNode,
} from "lexical";

import {
  $createWorkspaceFileNode,
  $isWorkspaceFileNode,
} from "@/components/composer/WorkspaceFileNode";
import type { ComposerSubmission, WorkspaceFileReference } from "@/types";
import {
  type AtQuery,
  dirTokenText,
  matchAtQuery,
  matchSkillQuery,
  type SkillQuery,
  type WorkspaceEntry,
} from "@/utils/completion";

/** Editor-selection context the menus are derived from. */
export interface EditorContext {
  rootText: string;
  atQuery: AtQuery | null;
  skillQuery: SkillQuery | null;
}

export function $readEditorContext(): EditorContext {
  const rootText = $getRoot().getTextContent();
  const selection = $getSelection();
  let atQuery: AtQuery | null = null;
  let skillQuery: SkillQuery | null = null;
  if ($isRangeSelection(selection) && selection.isCollapsed()) {
    const anchorNode = selection.anchor.getNode();
    if ($isTextNode(anchorNode) && !$isWorkspaceFileNode(anchorNode)) {
      const textBeforeCursor = anchorNode.getTextContent().slice(0, selection.anchor.offset);
      atQuery = matchAtQuery(textBeforeCursor);
      skillQuery = matchSkillQuery(textBeforeCursor);
    }
  }
  return { rootText, atQuery, skillQuery };
}

/** Equal as far as the menus can tell; completion re-reads token offsets. */
export function sameEditorContext(a: EditorContext, b: EditorContext): boolean {
  return (
    a.rootText === b.rootText &&
    (a.atQuery === null) === (b.atQuery === null) &&
    a.atQuery?.dir === b.atQuery?.dir &&
    a.atQuery?.prefix === b.atQuery?.prefix &&
    (a.skillQuery === null) === (b.skillQuery === null) &&
    a.skillQuery?.prefix === b.skillQuery?.prefix
  );
}

export function $buildSubmission(): ComposerSubmission {
  const root = $getRoot();
  return {
    text: root.getTextContent(),
    workspaceFiles: root
      .getAllTextNodes()
      .filter($isWorkspaceFileNode)
      .map((node) => node.getReference()),
  };
}

/**
 * Replace the @token before the caret with completed text (directory: browsing
 * continues) or a pill node (file). No-op if the token vanished.
 */
export function $completeAtToken(entry: WorkspaceEntry): void {
  const selection = $getSelection();
  if (!$isRangeSelection(selection) || !selection.isCollapsed()) return;
  const anchorNode = selection.anchor.getNode();
  if (!$isTextNode(anchorNode) || $isWorkspaceFileNode(anchorNode)) return;
  const offset = selection.anchor.offset;
  const query = matchAtQuery(anchorNode.getTextContent().slice(0, offset));
  if (!query) return;

  if (entry.kind === "directory") {
    // Swap the token text in place; the caret lands after it and the menu
    // keeps browsing.
    anchorNode.spliceText(query.start, offset - query.start, dirTokenText(entry.path), true);
    return;
  }

  // Isolate the @token into its own node, replace it with a pill, and add a
  // trailing space for the caret to rest on.
  let tokenNode = anchorNode;
  if (query.start > 0) {
    const [, rest] = anchorNode.splitText(query.start);
    if (!rest) return;
    tokenNode = rest;
  }
  const tokenLength = offset - query.start;
  if (tokenNode.getTextContent().length > tokenLength) {
    const [head] = tokenNode.splitText(tokenLength);
    if (!head) return;
    tokenNode = head;
  }
  const pill = $createWorkspaceFileNode({
    path: entry.path,
    name: entry.name,
    kind: entry.kind,
  });
  tokenNode.replace(pill);
  const space = $createTextNode(" ");
  pill.insertAfter(space);
  space.select(1, 1);
}

export function $completeSkillToken(name: string): void {
  const selection = $getSelection();
  if (!$isRangeSelection(selection) || !selection.isCollapsed()) return;
  const anchorNode = selection.anchor.getNode();
  if (!$isTextNode(anchorNode) || $isWorkspaceFileNode(anchorNode)) return;
  const offset = selection.anchor.offset;
  const query = matchSkillQuery(anchorNode.getTextContent().slice(0, offset));
  if (!query) return;
  anchorNode.spliceText(query.start, offset - query.start, `/${name} `, true);
}

/** Replace the editor content with plain text, caret at the end. */
export function $setPlainText(text: string): void {
  const root = $getRoot();
  root.clear();
  const paragraph = $createParagraphNode();
  text.split("\n").forEach((line, index) => {
    if (index > 0) paragraph.append($createLineBreakNode());
    if (line) paragraph.append($createTextNode(line));
  });
  root.append(paragraph);
  paragraph.selectEnd();
}

/** Text with its `@path` pills rebuilt from the references it was sent with. */
function $submissionNodes({ text, workspaceFiles }: ComposerSubmission): LexicalNode[] {
  const nodes: LexicalNode[] = [];
  text.split("\n").forEach((line, index) => {
    if (index > 0) nodes.push($createLineBreakNode());
    let rest = line;
    while (rest) {
      // The earliest pill; the longer path wins where two start together.
      let pill: { index: number; ref: WorkspaceFileReference } | null = null;
      for (const ref of workspaceFiles) {
        const at = rest.indexOf(`@${ref.path}`);
        if (at < 0) continue;
        if (
          !pill ||
          at < pill.index ||
          (at === pill.index && ref.path.length > pill.ref.path.length)
        ) {
          pill = { index: at, ref };
        }
      }
      if (!pill) {
        nodes.push($createTextNode(rest));
        break;
      }
      if (pill.index > 0) nodes.push($createTextNode(rest.slice(0, pill.index)));
      nodes.push($createWorkspaceFileNode(pill.ref));
      rest = rest.slice(pill.index + pill.ref.path.length + 1);
    }
  });
  return nodes;
}

export function $prependSubmissions(submissions: ComposerSubmission[]): void {
  const nodes: LexicalNode[] = [];
  const blankLine = () => [$createLineBreakNode(), $createLineBreakNode()];
  for (const submission of submissions) {
    if (!submission.text) continue;
    if (nodes.length) nodes.push(...blankLine());
    nodes.push(...$submissionNodes(submission));
  }
  if (!nodes.length) return;
  const root = $getRoot();
  if (root.getTextContent()) nodes.push(...blankLine());
  const first = root.getFirstChild();
  if ($isElementNode(first)) {
    first.splice(0, 0, nodes);
  } else {
    root.append($createParagraphNode().append(...nodes));
  }
}
