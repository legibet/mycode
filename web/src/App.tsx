/**
 * Main application component.
 * Composes sidebar, chat interface, and theme provider.
 * Mobile: sidebar as overlay, top header bar.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { InputArea, type InputAreaHandle } from "./components/Chat/InputArea";
import { MessageList } from "./components/Chat/MessageList";
import { PermissionPrompt } from "./components/Chat/PermissionPrompt";
import { Layout } from "./components/Layout";
import { MobileHeader } from "./components/MobileHeader";
import { SessionSearch } from "./components/SessionSearch";
import { SettingsPanel } from "./components/Settings/SettingsPanel";
import { Sidebar } from "./components/Sidebar";
import { ThemeProvider } from "./components/ThemeProvider";
import { useChat } from "./hooks/useChat";
import type {
  AttachedFile,
  ComposerSubmission,
  LocalConfig,
  PendingInput,
  RemoteConfig,
  SettingsResponse,
} from "./types";
import type { SlashCommand } from "./utils/completion";
import { normalizeConfigWithRemoteDefaults } from "./utils/config";
import { isMac } from "./utils/platform";
import {
  getMaxSidebarWidth,
  SIDEBAR_DEFAULT_WIDTH,
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_MIN_WIDTH,
} from "./utils/sidebar";
import {
  addHistory,
  loadConfig,
  loadHistory,
  loadSidebarWidth,
  saveConfig,
  saveHistory,
  saveSidebarWidth,
} from "./utils/storage";

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    try {
      const data = await response.json();
      if (typeof data?.detail === "string" && data.detail) {
        message = data.detail;
      }
    } catch {}
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

function modelSupports(
  remoteConfig: RemoteConfig | null,
  providerKey: string,
  model: string,
): { image: boolean; pdf: boolean } {
  const key = providerKey || remoteConfig?.default?.provider || "";
  const info = remoteConfig?.providers?.[key];
  const m = model || remoteConfig?.default?.model || "";
  return {
    image: Boolean(
      info?.supports_image_input && info.image_input_models?.includes(m),
    ),
    pdf: Boolean(
      info?.supports_pdf_input && info.pdf_input_models?.includes(m),
    ),
  };
}

function settingsPanelKey(open: boolean, settings: SettingsResponse | null) {
  return JSON.stringify({
    open,
    path: settings?.path ?? "",
    config: settings?.config ?? null,
    options: settings?.options ?? null,
  });
}

function subscribeToWindowResize(onChange: () => void) {
  window.addEventListener("resize", onChange);
  return () => window.removeEventListener("resize", onChange);
}

function AppContent() {
  const [localConfig, setLocalConfig] = useState<LocalConfig>(loadConfig);
  const [attachments, setAttachments] = useState<AttachedFile[]>([]);
  const [cwdHistory, setCwdHistory] = useState<string[]>(loadHistory);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // User's preferred sidebar width — only changes on explicit drag/reset.
  const [sidebarWidth, setSidebarWidth] = useState(loadSidebarWidth);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const maxSidebarWidth = useSyncExternalStore(
    subscribeToWindowResize,
    getMaxSidebarWidth,
    () => SIDEBAR_MAX_WIDTH,
  );

  // Revoke each image's blob URL once its attachment leaves state, whatever
  // path removed it.
  const prevAttachmentsRef = useRef<AttachedFile[]>([]);
  useEffect(() => {
    const prev = prevAttachmentsRef.current;
    prevAttachmentsRef.current = attachments;
    const alive = new Set(attachments.map((a) => a.id));
    for (const a of prev) {
      if (a.kind === "image" && !alive.has(a.id))
        URL.revokeObjectURL(a.preview);
    }
  }, [attachments]);

  // Persisted state slices are saved on change, not by their mutators.
  useEffect(() => {
    saveConfig(localConfig);
  }, [localConfig]);
  useEffect(() => {
    saveHistory(cwdHistory);
  }, [cwdHistory]);
  useEffect(() => {
    saveSidebarWidth(sidebarWidth);
  }, [sidebarWidth]);

  const handleOpenSettings = useCallback(() => {
    setSettingsOpen(true);
  }, []);

  const handleResetSidebarWidth = useCallback(() => {
    setSidebarWidth(SIDEBAR_DEFAULT_WIDTH);
  }, []);

  const isDesktop = useMediaQuery("(min-width: 768px)");

  const displayedSidebarWidth = Math.max(
    SIDEBAR_MIN_WIDTH,
    Math.min(maxSidebarWidth, sidebarWidth),
  );
  const configUrl = `/api/config?cwd=${encodeURIComponent(localConfig.cwd)}`;
  const {
    data: remoteConfig = null,
    error: remoteConfigError,
    mutate: mutateRemoteConfig,
  } = useSWR<RemoteConfig, Error>(configUrl, fetchJson<RemoteConfig>, {
    keepPreviousData: true,
  });
  const {
    data: settingsResponse = null,
    error: settingsError,
    mutate: mutateSettings,
  } = useSWR<SettingsResponse, Error>(
    "/api/settings",
    fetchJson<SettingsResponse>,
  );

  const config = useMemo(
    () =>
      remoteConfig
        ? normalizeConfigWithRemoteDefaults(localConfig, remoteConfig)
        : localConfig,
    [localConfig, remoteConfig],
  );

  // Undelivered steers and queued messages come back ahead of the draft.
  const inputAreaRef = useRef<InputAreaHandle>(null);
  const restoreToComposer = useCallback((items: PendingInput[]) => {
    inputAreaRef.current?.prepend(items.map((item) => item.submission));
    // Image previews were revoked when the uploads left the composer.
    const restored = items
      .flatMap((item) => item.attachments)
      .map((file) =>
        file.kind === "image"
          ? { ...file, preview: `data:${file.mime_type};base64,${file.data}` }
          : file,
      );
    if (restored.length) setAttachments((prev) => [...restored, ...prev]);
  }, []);

  const {
    messages,
    messageSessionId,
    sessionUsage,
    currentContext,
    loading,
    runKind,
    compactError,
    sendError,
    sessions,
    activeSession,
    pendingPermission,
    pending,
    send,
    steer,
    queue,
    removeQueued,
    steerQueued,
    takeBackQueued,
    rewindAndSend,
    compactSession,
    cancel,
    decidePermission,
    createSession,
    selectSession,
    deleteSession,
  } = useChat(config, remoteConfig, restoreToComposer);

  // Esc is the keyboard twin of the composer's stop button. Controls that
  // own Esc (permission prompt, completion menu, message edit) preventDefault
  // first, and a Base UI dialog in the path (settings sheet) closes itself.
  useEffect(() => {
    if (!loading) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (event.isComposing) return;
      if (
        event
          .composedPath()
          .some((el) => el instanceof Element && el.matches("[role=dialog]"))
      ) {
        return;
      }
      void cancel();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [loading, cancel]);

  // Cmd+K / Ctrl+K opens session search. It lives here rather than in
  // Sidebar because the mobile sidebar is unmounted while its drawer is closed.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing) return;
      if (event.key.toLowerCase() !== "k") return;
      if (!(isMac ? event.metaKey : event.ctrlKey)) return;
      if (event.altKey || event.shiftKey) return;
      if (
        event
          .composedPath()
          .some((el) => el instanceof Element && el.matches("[role=dialog]"))
      ) {
        return;
      }
      event.preventDefault();
      setSearchOpen(true);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const handleOpenSearch = useCallback(() => {
    setSidebarOpen(false);
    setSearchOpen(true);
  }, []);

  const handleConfigUpdate = useCallback(
    (newConfig: LocalConfig) => {
      if (newConfig.cwd !== config.cwd) {
        setCwdHistory((prev) => addHistory(prev, newConfig.cwd));
      }
      setLocalConfig(newConfig);
    },
    [config.cwd],
  );

  const handleRemoveHistory = useCallback((cwd: string) => {
    setCwdHistory((prev) =>
      prev.includes(cwd) ? prev.filter((item) => item !== cwd) : prev,
    );
  }, []);

  const clearAttachments = useCallback(() => {
    setAttachments([]);
  }, []);

  // While a chat runs, Enter steers it and Mod+Enter queues the next turn.
  const handleSubmit = useCallback(
    async (submission: ComposerSubmission, toQueue: boolean) => {
      const deliver = runKind !== "chat" ? send : toQueue ? queue : steer;
      const accepted = await deliver(submission, attachments);
      if (!accepted) return false;
      clearAttachments();
      return true;
    },
    [attachments, clearAttachments, queue, runKind, send, steer],
  );

  const handleEditQueued = useCallback(
    async (id: string) => {
      const item = await takeBackQueued(id);
      if (item) restoreToComposer([item]);
    },
    [restoreToComposer, takeBackQueued],
  );

  const handleAttachFiles = useCallback((newFiles: AttachedFile[]) => {
    setAttachments((prev) => [...prev, ...newFiles]);
  }, []);

  const handleRemoveAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((attachment) => attachment.id !== id));
  }, []);

  const { image: supportsImageInput, pdf: supportsPdfInput } = useMemo(
    () => modelSupports(remoteConfig, config.provider, config.model),
    [config.model, config.provider, remoteConfig],
  );
  const workspaceMissing = remoteConfig?.cwd_exists === false;
  const workspaceDisabledReason = workspaceMissing
    ? "Workspace no longer exists. Choose another workspace."
    : undefined;
  const setupRequired =
    Boolean(remoteConfig?.setup_error) ||
    (!remoteConfig && Boolean(remoteConfigError));
  const emptyStateFooter = setupRequired ? (
    remoteConfig ? (
      <div className="flex items-center gap-3">
        <p className="text-sm text-muted-foreground">
          Add a provider to get started.
        </p>
        <Button variant="outline" size="sm" onClick={handleOpenSettings}>
          Open settings
        </Button>
      </div>
    ) : (
      <p className="text-sm text-muted-foreground">
        Couldn&apos;t reach the API.
      </p>
    )
  ) : undefined;

  const handleSelectSession = useCallback(
    (id: string) => {
      void selectSession(id);
      setSidebarOpen(false);
      clearAttachments();
    },
    [selectSession, clearAttachments],
  );

  const handleCreateSession = useCallback(() => {
    createSession();
    setSidebarOpen(false);
    clearAttachments();
  }, [createSession, clearAttachments]);

  const handleDeleteSession = useCallback(
    async (id: string) => {
      const isActive = activeSession?.id === id;
      await deleteSession(id);
      if (!isActive) return;
      setSidebarOpen(false);
      clearAttachments();
    },
    [activeSession?.id, clearAttachments, deleteSession],
  );

  const handleSlashCommand = useCallback(
    (name: SlashCommand["name"]) => {
      if (name === "/compact") {
        void compactSession();
        return;
      }
      handleCreateSession();
    },
    [handleCreateSession, compactSession],
  );

  const sidebarProps = {
    sessions,
    activeSession,
    onSelectSession: handleSelectSession,
    onCreateSession: handleCreateSession,
    onOpenSearch: handleOpenSearch,
    onDeleteSession: handleDeleteSession,
    config,
    remoteConfig,
    cwdHistory,
    onUpdateConfig: handleConfigUpdate,
    onRemoveHistory: handleRemoveHistory,
    onOpenSettings: handleOpenSettings,
    workspaceMissing,
  };

  return (
    <Layout>
      <div className="relative flex h-full min-h-0 overflow-hidden">
        {/* Mounted exclusively to avoid duplicate SWR / WorkspacePicker state. */}
        {isDesktop ? (
          <div className="shrink-0">
            <Sidebar
              {...sidebarProps}
              width={displayedSidebarWidth}
              onResize={setSidebarWidth}
              onResizeReset={handleResetSidebarWidth}
              className="h-full"
            />
          </div>
        ) : (
          <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
            <SheetContent
              side="left"
              showCloseButton={false}
              className="p-0 gap-0 w-65 bg-sidebar-bg"
            >
              <SheetTitle className="sr-only">Navigation</SheetTitle>
              <Sidebar {...sidebarProps} width={260} className="h-full" />
            </SheetContent>
          </Sheet>
        )}

        {/* Main content */}
        <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background">
          {/* Mobile header */}
          <MobileHeader
            title={activeSession?.title}
            onMenuToggle={() => setSidebarOpen((v) => !v)}
            onCreateSession={handleCreateSession}
          />

          <MessageList
            sessionId={messageSessionId ?? activeSession?.id}
            messages={setupRequired ? [] : messages}
            loading={setupRequired ? false : runKind === "chat"}
            compacting={!setupRequired && runKind === "compact"}
            compactError={setupRequired ? null : compactError}
            sendError={setupRequired ? null : sendError}
            pendingSteers={pending.steers}
            onRewindAndSend={
              workspaceMissing || setupRequired ? undefined : rewindAndSend
            }
            emptyStateFooter={emptyStateFooter}
          />

          <div className="shrink-0 pb-4 max-md:pb-1">
            {pendingPermission && (
              <PermissionPrompt
                request={pendingPermission}
                onDecide={decidePermission}
              />
            )}
            <InputArea
              key={config.cwd}
              ref={inputAreaRef}
              loading={loading}
              compacting={runKind === "compact"}
              onSubmit={handleSubmit}
              onCancel={cancel}
              queued={pending.queue}
              onSteerQueued={steerQueued}
              onEditQueued={handleEditQueued}
              onRemoveQueued={removeQueued}
              supportsImages={supportsImageInput}
              supportsDocuments={supportsPdfInput}
              files={attachments}
              onAttachFiles={handleAttachFiles}
              onRemoveFile={handleRemoveAttachment}
              config={config}
              remoteConfig={remoteConfig}
              onUpdateConfig={handleConfigUpdate}
              onSlashCommand={handleSlashCommand}
              disabled={setupRequired}
              disabledReason={workspaceDisabledReason}
              sessionUsage={sessionUsage}
              currentContext={currentContext}
            />
          </div>
        </main>
      </div>

      <SessionSearch
        open={searchOpen}
        onClose={() => setSearchOpen(false)}
        cwd={config.cwd}
        activeSessionId={activeSession?.id}
        onSelect={handleSelectSession}
      />

      <SettingsPanel
        key={settingsPanelKey(settingsOpen, settingsResponse)}
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        settings={settingsResponse}
        loadError={settingsError?.message}
        onSettingsSaved={(settings) => {
          void mutateSettings(settings, { revalidate: false });
          void mutateRemoteConfig();
        }}
        projectConfigPaths={remoteConfig?.config_paths}
      />
    </Layout>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <AppContent />
    </ThemeProvider>
  );
}
