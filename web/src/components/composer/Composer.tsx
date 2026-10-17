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
  $getRoot,
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
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { SkillInfo } from "@/api";
import {
  type CompletionItem,
  CompletionMenu,
  completionItemDomId,
} from "@/components/composer/CompletionMenu";
import {
  $buildSubmission,
  $completeAtToken,
  $completeSkillToken,
  $prependSubmissions,
  $readEditorContext,
  $setPlainText,
  type EditorContext,
  sameEditorContext,
} from "@/components/composer/composerEditor";
import { WorkspaceFileNode } from "@/components/composer/WorkspaceFileNode";
import { useWorkspaceFiles } from "@/hooks/useWorkspaceFiles";
import type { ComposerSubmission } from "@/types";
import { cn } from "@/utils/cn";
import { matchSlashCommands, type SlashCommand } from "@/utils/completion";

const MENU_ID = "composer-completion-menu";
const HISTORY_RECALL_TAG = "prompt-history-recall";

export interface ComposerHandle {
  submit: (toQueue: boolean) => void;
  /** Put submissions back ahead of the draft, blank-line separated. */
  prepend: (submissions: ComposerSubmission[]) => void;
}

interface ComposerProps {
  ref?: Ref<ComposerHandle>;
  disabled: boolean;
  placeholder: string;
  /** A run is active; built-in slash commands are idle-only. */
  loading: boolean;
  cwd: string;
  supportsImages: boolean;
  supportsDocuments: boolean;
  skills: SkillInfo[];
  hasUploads: boolean;
  /** Previously sent prompts for this workspace, oldest first. */
  history: string[];
  /** Return false to reject the submission (composer keeps its content).
   * `toQueue` is set by ⌘/Ctrl+Enter. */
  onSubmit: (submission: ComposerSubmission, toQueue: boolean) => Promise<boolean>;
  onSlashCommand?: ((name: SlashCommand["name"]) => void) | undefined;
  onPasteFiles: (files: File[]) => void;
  onHasContentChange: (hasContent: boolean) => void;
}

function ComposerInner({
  ref,
  disabled,
  placeholder,
  loading,
  cwd,
  supportsImages,
  supportsDocuments,
  skills,
  hasUploads,
  history,
  onSubmit,
  onSlashCommand,
  onPasteFiles,
  onHasContentChange,
}: ComposerProps) {
  const [editor] = useLexicalComposerContext();
  const [context, setContext] = useState<EditorContext>({
    rootText: "",
    atQuery: null,
    skillQuery: null,
  });
  const [activeIndex, setActiveIndex] = useState(0);
  const submittingRef = useRef(false);
  // Position in `history` while recalling prompts; null when not navigating.
  const historyIndexRef = useRef<number | null>(null);
  // Remembers the root text it was set for, so any edit invalidates it.
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);

  useEffect(() => {
    editor.setEditable(!disabled);
  }, [editor, disabled]);

  useEffect(() => {
    return editor.registerUpdateListener(({ editorState, prevEditorState, tags }) => {
      const next = editorState.read($readEditorContext);
      // Any text change not caused by recall leaves history navigation.
      if (
        !tags.has(HISTORY_RECALL_TAG) &&
        next.rootText !== prevEditorState.read(() => $getRoot().getTextContent())
      ) {
        historyIndexRef.current = null;
      }
      setContext((prev) => (sameEditorContext(prev, next) ? prev : next));
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
    if (disabled || !query) return [];
    return skills.filter((skill) => skill.name.startsWith(query.prefix));
  }, [context.skillQuery, disabled, skills]);

  const atQuery =
    disabled || slashCandidates.length > 0 || skillCandidates.length > 0 ? null : context.atQuery;
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
          entry.kind === "image" ? "image" : entry.kind === "document" ? "pdf" : "file";
        return {
          id: entry.path,
          label: entry.name,
          hint: unsupported ? `${kindLabel} · not supported by model` : kindLabel,
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
    slashCandidates,
    skillCandidates,
    atQuery,
    workspaceFiles,
    supportsImages,
    supportsDocuments,
  ]);

  const menuOpen =
    (menuItems.length > 0 || Boolean(menuFooter)) && dismissedFor !== context.rootText;
  const menuIndex = Math.min(activeIndex, Math.max(menuItems.length - 1, 0));

  const submit = async (toQueue: boolean) => {
    if (submittingRef.current) return;
    const submission = editor.getEditorState().read($buildSubmission);
    submittingRef.current = true;
    try {
      if (!(await onSubmit(submission, toQueue))) return;
      editor.update(() => {
        $getRoot().clear();
      });
    } finally {
      submittingRef.current = false;
    }
  };

  const selectMenuItem = (index: number) => {
    const command = slashCandidates[index];
    if (command) {
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
  // this ref. Lexical's commands run on native listeners, so the ref is
  // refreshed in a layout effect to close the pre-paint staleness window.
  const keyState = {
    menuOpen,
    menuItems,
    menuIndex,
    rootText: context.rootText,
    history,
    selectMenuItem,
    submit,
  };
  const keyStateRef = useRef(keyState);
  useLayoutEffect(() => {
    keyStateRef.current = keyState;
  });

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

    // Recall only starts from an empty editor (no text, no pills), so arrow
    // keys keep moving the caret in a draft; once recalling, they keep walking.
    const recallHistory = (event: KeyboardEvent | null, step: -1 | 1): boolean => {
      if (event?.isComposing) return false;
      const { history } = keyStateRef.current;
      const index = historyIndexRef.current;
      if (index === null) {
        const empty = editor.getEditorState().read(() => $getRoot().getTextContent() === "");
        if (step === 1 || !empty || history.length === 0) return false;
      }
      const nextIndex = (index ?? history.length) + step;
      event?.preventDefault();
      // Already at the oldest entry: keep it and swallow the key.
      if (nextIndex < 0) return true;
      const text = history[nextIndex];
      historyIndexRef.current = text === undefined ? null : nextIndex;
      editor.update(() => $setPlainText(text ?? ""), {
        tag: HISTORY_RECALL_TAG,
      });
      return true;
    };

    return mergeRegister(
      editor.registerCommand(
        KEY_ARROW_DOWN_COMMAND,
        (event) =>
          withMenu(event, (state) =>
            setActiveIndex(Math.min(state.menuIndex + 1, state.menuItems.length - 1)),
          ) || recallHistory(event, 1),
        COMMAND_PRIORITY_HIGH,
      ),
      editor.registerCommand(
        KEY_ARROW_UP_COMMAND,
        (event) =>
          withMenu(event, (state) => setActiveIndex(Math.max(state.menuIndex - 1, 0))) ||
          recallHistory(event, -1),
        COMMAND_PRIORITY_HIGH,
      ),
      editor.registerCommand(
        KEY_TAB_COMMAND,
        (event) => withMenu(event, (state) => state.selectMenuItem(state.menuIndex)),
        COMMAND_PRIORITY_HIGH,
      ),
      editor.registerCommand(
        KEY_ESCAPE_COMMAND,
        (event) => {
          const state = keyStateRef.current;
          if (!state.menuOpen) return false;
          event?.preventDefault();
          setDismissedFor(state.rootText);
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
          void state.submit(Boolean(event?.metaKey || event?.ctrlKey));
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

  useImperativeHandle(ref, () => ({
    submit: (toQueue) => void submit(toQueue),
    prepend: (submissions) => editor.update(() => $prependSubmissions(submissions)),
  }));

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
                "block max-h-50 w-full overflow-y-auto bg-transparent px-3.5 pt-4 pb-1.5 text-base leading-relaxed wrap-anywhere whitespace-pre-wrap focus-visible:outline-none max-md:pt-3.5 md:text-sm",
                disabled ? "text-muted-foreground/50" : "text-foreground",
              )}
            />
          }
          placeholder={
            <div className="pointer-events-none absolute top-4 left-3.5 text-base leading-relaxed text-muted-foreground/40 max-md:top-3.5 md:text-sm">
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

export const Composer = memo(function Composer(props: ComposerProps) {
  return (
    <LexicalComposer initialConfig={INITIAL_CONFIG}>
      <ComposerInner {...props} />
      <HistoryPlugin />
    </LexicalComposer>
  );
});
