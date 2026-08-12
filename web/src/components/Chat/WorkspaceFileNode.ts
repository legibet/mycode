/**
 * Atomic `@path` workspace file reference inside the composer.
 *
 * Token mode makes the node an immutable unit: the caret can't enter it and
 * Backspace/Delete removes it whole. Its text content is always `@<path>`, so
 * plain-text serialization (getTextContent, copy) naturally yields the same
 * prompt the CLI would see.
 */

import {
  $applyNodeReplacement,
  type EditorConfig,
  type LexicalNode,
  type LexicalUpdateJSON,
  type NodeKey,
  type SerializedTextNode,
  type Spread,
  TextNode,
} from "lexical";
import type { WorkspaceFileReference } from "../../types";

const PILL_CLASS =
  "rounded bg-accent/15 text-accent px-0.5 font-mono text-[0.92em]";

export type SerializedWorkspaceFileNode = Spread<
  WorkspaceFileReference,
  SerializedTextNode
>;

export class WorkspaceFileNode extends TextNode {
  __path: string;
  __kind: WorkspaceFileReference["kind"];
  __fileName: string;

  constructor(
    text = "",
    path = "",
    kind: WorkspaceFileReference["kind"] = "text",
    fileName = "",
    key?: NodeKey,
  ) {
    super(text, key);
    this.__path = path;
    this.__kind = kind;
    this.__fileName = fileName;
  }

  static getType(): string {
    return "workspace-file";
  }

  static clone(node: WorkspaceFileNode): WorkspaceFileNode {
    return new WorkspaceFileNode(
      node.__text,
      node.__path,
      node.__kind,
      node.__fileName,
      node.__key,
    );
  }

  static importJSON(serialized: SerializedTextNode): WorkspaceFileNode {
    return new WorkspaceFileNode().updateFromJSON(serialized);
  }

  override updateFromJSON(
    serialized: LexicalUpdateJSON<SerializedTextNode> &
      Partial<WorkspaceFileReference>,
  ): this {
    const self = super.updateFromJSON(serialized);
    if (typeof serialized.path === "string") self.__path = serialized.path;
    if (typeof serialized.kind === "string") self.__kind = serialized.kind;
    if (typeof serialized.name === "string") self.__fileName = serialized.name;
    return self;
  }

  override exportJSON(): SerializedWorkspaceFileNode {
    return {
      ...super.exportJSON(),
      path: this.__path,
      kind: this.__kind,
      name: this.__fileName,
    };
  }

  override createDOM(config: EditorConfig): HTMLElement {
    const dom = super.createDOM(config);
    dom.className = PILL_CLASS;
    dom.setAttribute("data-workspace-file", this.__path);
    return dom;
  }

  override updateDOM(
    prevNode: this,
    dom: HTMLElement,
    config: EditorConfig,
  ): boolean {
    const updated = super.updateDOM(prevNode, dom, config);
    if (prevNode.__path !== this.__path) {
      dom.setAttribute("data-workspace-file", this.__path);
    }
    return updated;
  }

  getReference(): WorkspaceFileReference {
    const self = this.getLatest();
    return { path: self.__path, kind: self.__kind, name: self.__fileName };
  }
}

export function $createWorkspaceFileNode(
  ref: WorkspaceFileReference,
): WorkspaceFileNode {
  const node = new WorkspaceFileNode(
    `@${ref.path}`,
    ref.path,
    ref.kind,
    ref.name,
  );
  node.setMode("token");
  return $applyNodeReplacement(node);
}

export function $isWorkspaceFileNode(
  node: LexicalNode | null | undefined,
): node is WorkspaceFileNode {
  return node instanceof WorkspaceFileNode;
}
