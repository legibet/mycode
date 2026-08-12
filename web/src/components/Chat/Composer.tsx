/**
 * Lexical-based prompt composer: plain text plus atomic @workspace-file pills.
 *
 * The EditorState is the single source of truth for the message text and the
 * inline workspace references; the app only receives a ComposerSubmission on
 * submit. Slash commands and @ completion share the CompletionMenu listbox,
 * with all keyboard handling registered as Lexical commands so IME, history,
 * and selection behavior stay framework-managed.
 */

import { LexicalComposer } from "@lexical/react/LexicalComposer";
import { useLexicalComposerContext } from "@lexical/react/LexicalComposerContext";
import { ContentEditable } from "@lexical/react/LexicalContentEditable";
import { LexicalErrorBoundary } from "@lexical/react/LexicalErrorBoundary";
import { HistoryPlugin } from "@lexical/react/LexicalHistoryPlugin";
import { PlainTextPlugin } from "@lexical/react/LexicalPlainTextPlugin";
import { mergeRegister } from "@lexical/utils";
import {
  $createParagraphNode,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isRangeSelection,
  $isTextNode,
  COMMAND_PRIORITY_HIGH,
  KEY_ARROW_DOWN_COMMAND,
  KEY_ARROW_UP_COMMAND,
  KEY_ENTER_COMMAND,
  KEY_ESCAPE_COMMAND,
  KEY_TAB_COMMAND,
  PASTE_COMMAND,
} from "lexical";
import {
  memo,
  type Ref,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { useWorkspaceFiles } from "../../hooks/useWorkspaceFiles";
import type { ComposerSubmission, SkillInfo } from "../../types";
import { cn } from "../../utils/cn";
import {
  type AtQuery,
  dirTokenText,
  matchAtQuery,
  matchSkillQuery,
  matchSlashCommands,
  type SkillQuery,
  type SlashCommand,
  type WorkspaceEntry,
} from "../../utils/completion";
import {
  type CompletionItem,
  CompletionMenu,
  completionItemDomId,
} from "./CompletionMenu";
import {
  $createWorkspaceFileNode,
  $isWorkspaceFileNode,
  WorkspaceFileNode,
} from "./WorkspaceFileNode";

const MENU_ID = "composer-completion-menu";

export interface ComposerHandle {
  submit: () => void;
}

interface ComposerProps {
  ref?: Ref<ComposerHandle>;
  disabled: boolean;
  placeholder: string;
  loading: boolean;
  cwd: string;
  supportsImages: boolean;
  supportsDocuments: boolean;
  skills: SkillInfo[];
  hasUploads: boolean;
  /** Return false to reject the submission (composer keeps its content). */
  onSubmit: (submission: ComposerSubmission) => Promise<boolean>;
  onSlashCommand?: ((name: SlashCommand["name"]) => void) | undefined;
  onPasteFiles: (files: File[]) => void;
  onHasContentChange: (hasContent: boolean) => void;
}

/** Editor-selection context the menus are derived from. */
interface EditorContext {
  rootText: string;
  atQuery: AtQuery | null;
  skillQuery: SkillQuery | null;
}

function $readEditorContext(): EditorContext {
  const rootText = $getRoot().getTextContent();
  const selection = $getSelection();
  let atQuery: AtQuery | null = null;
  let skillQuery: SkillQuery | null = null;
  if ($isRangeSelection(selection) && selection.isCollapsed()) {
    const anchorNode = selection.anchor.getNode();
    if ($isTextNode(anchorNode) && !$isWorkspaceFileNode(anchorNode)) {
      const textBeforeCursor = anchorNode
        .getTextContent()
        .slice(0, selection.anchor.offset);
      atQuery = matchAtQuery(textBeforeCursor);
      skillQuery = matchSkillQuery(textBeforeCursor);
    }
  }
  return { rootText, atQuery, skillQuery };
}

function $buildSubmission(): ComposerSubmission {
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
 * continues) or a pill node (file). No-op if the token vanished. Must run
 * inside editor.update().
 */
function $completeAtToken(entry: WorkspaceEntry): void {
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
    anchorNode.spliceText(
      query.start,
      offset - query.start,
      dirTokenText(entry.path),
      true,
    );
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

function $completeSkillToken(name: string): void {
  const selection = $getSelection();
  if (!$isRangeSelection(selection) || !selection.isCollapsed()) return;
  const anchorNode = selection.anchor.getNode();
  if (!$isTextNode(anchorNode) || $isWorkspaceFileNode(anchorNode)) return;
  const offset = selection.anchor.offset;
  const query = matchSkillQuery(anchorNode.getTextContent().slice(0, offset));
  if (!query) return;
  anchorNode.spliceText(query.start, offset - query.start, `/${name} `, true);
}

function ComposerInner({
  disabled,
  placeholder,
  loading,
  cwd,
  supportsImages,
  supportsDocuments,
  skills,
  hasUploads,
  onSubmit,
  onSlashCommand,
  onPasteFiles,
  onHasContentChange,
  handleRef,
}: Omit<ComposerProps, "ref"> & {
  handleRef?: Ref<ComposerHandle> | undefined;
}) {
  const [editor] = useLexicalComposerContext();
  const [context, setContext] = useState<EditorContext>({
    rootText: "",
    atQuery: null,
    skillQuery: null,
  });
  const [activeIndex, setActiveIndex] = useState(0);
  const submittingRef = useRef(false);
  // Both remember the root text they were set for, so any edit invalidates them.
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);
  const [confirmingFor, setConfirmingFor] = useState<{
    command: SlashCommand;
    rootText: string;
  } | null>(null);

  useEffect(() => {
    editor.setEditable(!disabled);
  }, [editor, disabled]);

  useEffect(() => {
    return editor.registerUpdateListener(({ editorState }) => {
      const next = editorState.read($readEditorContext);
      setContext((prev) =>
        prev.rootText === next.rootText &&
        prev.atQuery?.dir === next.atQuery?.dir &&
        prev.atQuery?.prefix === next.atQuery?.prefix &&
        (prev.atQuery === null) === (next.atQuery === null) &&
        prev.skillQuery?.prefix === next.skillQuery?.prefix &&
        (prev.skillQuery === null) === (next.skillQuery === null)
          ? prev
          : next,
      );
      onHasContentChange(next.rootText.trim().length > 0);
    });
  }, [editor, onHasContentChange]);

  const slashCandidates = useMemo(
    () =>
      !onSlashCommand || loading || disabled || hasUploads
        ? []
        : matchSlashCommands(context.rootText),
    [onSlashCommand, loading, disabled, hasUploads, context.rootText],
  );

  const skillCandidates = useMemo(() => {
    const query = context.skillQuery;
    if (loading || disabled || !query) return [];
    return skills.filter((skill) => skill.name.startsWith(query.prefix));
  }, [context.skillQuery, disabled, loading, skills]);

  const confirming =
    confirmingFor !== null && confirmingFor.rootText === context.rootText
      ? confirmingFor.command
      : null;

  const atQuery =
    loading ||
    disabled ||
    slashCandidates.length > 0 ||
    skillCandidates.length > 0 ||
    confirming
      ? null
      : context.atQuery;
  const workspaceFiles = useWorkspaceFiles(
    cwd,
    atQuery?.dir ?? "",
    atQuery?.prefix ?? "",
    atQuery !== null,
  );

  const { menuItems, menuFooter } = useMemo((): {
    menuItems: CompletionItem[];
    menuFooter?: string;
  } => {
    if (confirming) {
      return {
        menuItems: [
          {
            id: `${confirming.name}-confirm`,
            label: confirming.name,
            hint: "Enter again to confirm · Esc to cancel",
          },
        ],
      };
    }
    if (slashCandidates.length > 0 || skillCandidates.length > 0) {
      return {
        menuItems: [
          ...slashCandidates.map((command) => ({
            id: command.name,
            label: command.name,
            hint: command.description,
          })),
          ...skillCandidates.map((skill) => ({
            id: `skill:${skill.name}`,
            label: `/${skill.name}`,
            hint: skill.description,
          })),
        ],
      };
    }
    if (atQuery) {
      const items = workspaceFiles.entries.map((entry): CompletionItem => {
        if (entry.kind === "directory") {
          return { id: entry.path, label: `${entry.name}/`, hint: "dir" };
        }
        const unsupported =
          (entry.kind === "image" && !supportsImages) ||
          (entry.kind === "document" && !supportsDocuments);
        const kindLabel =
          entry.kind === "image"
            ? "image"
            : entry.kind === "document"
              ? "pdf"
              : "file";
        return {
          id: entry.path,
          label: entry.name,
          hint: unsupported
            ? `${kindLabel} · not supported by model`
            : kindLabel,
          disabled: unsupported,
        };
      });
      let footer: string | undefined;
      if (workspaceFiles.loading && items.length === 0) footer = "Loading…";
      else if (workspaceFiles.truncated) footer = "More matches, keep typing…";
      return { menuItems: items, ...(footer ? { menuFooter: footer } : {}) };
    }
    return { menuItems: [] };
  }, [
    confirming,
    slashCandidates,
    skillCandidates,
    atQuery,
    workspaceFiles,
    supportsImages,
    supportsDocuments,
  ]);

  const menuOpen =
    (menuItems.length > 0 || Boolean(menuFooter)) &&
    dismissedFor !== context.rootText;
  const menuIndex = Math.min(activeIndex, Math.max(menuItems.length - 1, 0));

  const submit = async () => {
    if (submittingRef.current) return;
    const submission = editor.getEditorState().read($buildSubmission);
    submittingRef.current = true;
    try {
      if (!(await onSubmit(submission))) return;
      editor.update(() => {
        $getRoot().clear();
      });
    } finally {
      submittingRef.current = false;
    }
  };

  const selectMenuItem = (index: number) => {
    if (confirming) {
      onSlashCommand?.(confirming.name);
      setConfirmingFor(null);
      editor.update(() => {
        $getRoot().clear();
      });
      return;
    }
    const command = slashCandidates[index];
    if (command) {
      if (command.confirm) {
        // Normalize a partial token ("/c") to the full name for the confirm row.
        editor.update(() => {
          const root = $getRoot();
          root.clear();
          const paragraph = $createParagraphNode();
          paragraph.append($createTextNode(command.name));
          root.append(paragraph);
          paragraph.selectEnd();
        });
        setConfirmingFor({ command, rootText: command.name });
        return;
      }
      onSlashCommand?.(command.name);
      editor.update(() => {
        $getRoot().clear();
      });
      return;
    }
    const skill = skillCandidates[index - slashCandidates.length];
    if (skill) {
      editor.update(() => $completeSkillToken(skill.name));
      setActiveIndex(0);
      return;
    }
    if (atQuery) {
      if (menuItems[index]?.disabled) return;
      const entry = workspaceFiles.entries[index];
      if (!entry) return;
      editor.update(() => $completeAtToken(entry));
      setActiveIndex(0);
    }
  };

  // Keyboard handling is registered once; the closures read live state via
  // this ref, refreshed every render.
  const keyState = {
    menuOpen,
    menuItems,
    menuIndex,
    rootText: context.rootText,
    selectMenuItem,
    submit,
  };
  const keyStateRef = useRef(keyState);
  keyStateRef.current = keyState;

  useEffect(() => {
    const withMenu = (
      event: KeyboardEvent | null,
      action: (state: typeof keyStateRef.current) => void,
    ): boolean => {
      const state = keyStateRef.current;
      if (!state.menuOpen || state.menuItems.length === 0) return false;
      if (event?.isComposing) return false;
      event?.preventDefault();
      action(state);
      return true;
    };

    return mergeRegister(
      editor.registerCommand(
        KEY_ARROW_DOWN_COMMAND,
        (event) =>
          withMenu(event, (state) =>
            setActiveIndex(
              Math.min(state.menuIndex + 1, state.menuItems.length - 1),
            ),
          ),
        COMMAND_PRIORITY_HIGH,
      ),
      editor.registerCommand(
        KEY_ARROW_UP_COMMAND,
        (event) =>
          withMenu(event, (state) =>
            setActiveIndex(Math.max(state.menuIndex - 1, 0)),
          ),
        COMMAND_PRIORITY_HIGH,
      ),
      editor.registerCommand(
        KEY_TAB_COMMAND,
        (event) =>
          withMenu(event, (state) => state.selectMenuItem(state.menuIndex)),
        COMMAND_PRIORITY_HIGH,
      ),
      editor.registerCommand(
        KEY_ESCAPE_COMMAND,
        (event) => {
          const state = keyStateRef.current;
          if (!state.menuOpen) return false;
          event?.preventDefault();
          setDismissedFor(state.rootText);
          setConfirmingFor(null);
          return true;
        },
        COMMAND_PRIORITY_HIGH,
      ),
      editor.registerCommand(
        KEY_ENTER_COMMAND,
        (event) => {
          if (event?.isComposing) return false;
          const state = keyStateRef.current;
          if (state.menuOpen && state.menuItems.length > 0) {
            event?.preventDefault();
            state.selectMenuItem(state.menuIndex);
            return true;
          }
          if (event?.shiftKey) return false;
          event?.preventDefault();
          void state.submit();
          return true;
        },
        COMMAND_PRIORITY_HIGH,
      ),
      editor.registerCommand(
        PASTE_COMMAND,
        (event) => {
          if (!(event instanceof ClipboardEvent)) return false;
          const files = Array.from(event.clipboardData?.files ?? []);
          if (files.length === 0) return false;
          event.preventDefault();
          onPasteFiles(files);
          return true;
        },
        COMMAND_PRIORITY_HIGH,
      ),
    );
  }, [editor, onPasteFiles]);

  useImperativeHandle(handleRef, () => ({ submit: () => void submit() }));

  return (
    <>
      <div className="relative">
        <PlainTextPlugin
          contentEditable={
            <ContentEditable
              aria-label="Message"
              aria-autocomplete="list"
              aria-controls={menuOpen ? MENU_ID : undefined}
              aria-activedescendant={
                menuOpen && menuItems.length > 0
                  ? completionItemDomId(MENU_ID, menuIndex)
                  : undefined
              }
              className={cn(
                "block w-full bg-transparent px-3.5 pt-4 pb-1.5 max-md:pt-3.5 text-base md:text-sm leading-relaxed focus-visible:outline-none max-h-50 overflow-y-auto whitespace-pre-wrap wrap-anywhere",
                disabled ? "text-muted-foreground/50" : "text-foreground",
              )}
            />
          }
          placeholder={
            <div className="pointer-events-none absolute top-4 max-md:top-3.5 left-3.5 text-base md:text-sm leading-relaxed text-muted-foreground/40">
              {placeholder}
            </div>
          }
          ErrorBoundary={LexicalErrorBoundary}
        />
      </div>
      {menuOpen && (
        <CompletionMenu
          menuId={MENU_ID}
          items={menuItems}
          activeIndex={menuIndex}
          footer={menuFooter}
          onSelect={selectMenuItem}
          onHighlight={setActiveIndex}
        />
      )}
    </>
  );
}

// Seeds the editor once; `editable` is kept in sync with `disabled` by an
// effect inside ComposerInner.
const INITIAL_CONFIG = {
  namespace: "prompt-composer",
  nodes: [WorkspaceFileNode],
  onError: (error: Error) => {
    console.error("Composer error:", error);
  },
};

export const Composer = memo(function Composer({
  ref,
  ...props
}: ComposerProps) {
  return (
    <LexicalComposer initialConfig={INITIAL_CONFIG}>
      <ComposerInner {...props} handleRef={ref} />
      <HistoryPlugin />
    </LexicalComposer>
  );
});
