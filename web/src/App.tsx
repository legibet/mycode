/**
 * Main application component.
 * Composes sidebar, chat interface, and theme provider.
 * Mobile: sidebar as overlay, top header bar.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import useSWR from "swr";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { InputArea } from "./components/Chat/InputArea";
import { MessageList } from "./components/Chat/MessageList";
import { PermissionPrompt } from "./components/Chat/PermissionPrompt";
import { Layout } from "./components/Layout";
import { MobileHeader } from "./components/MobileHeader";
import { SettingsPanel } from "./components/Settings/SettingsPanel";
import { Sidebar } from "./components/Sidebar";
import { ThemeProvider } from "./components/ThemeProvider";
import { useChat } from "./hooks/useChat";
import type {
  AttachedFile,
  LocalConfig,
  RemoteConfig,
  SettingsResponse,
} from "./types";
import { normalizeConfigWithRemoteDefaults } from "./utils/config";
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

function pruneAttachments(
  prev: AttachedFile[],
  supportsImage: boolean,
  supportsPdf: boolean,
): AttachedFile[] {
  const next: AttachedFile[] = [];
  let changed = false;

  for (const attachment of prev) {
    const keep =
      attachment.kind === "text" ||
      (attachment.kind === "image" && supportsImage) ||
      (attachment.kind === "document" && supportsPdf);

    if (keep) {
      next.push(attachment);
      continue;
    }

    changed = true;
    if (attachment.kind === "image") {
      URL.revokeObjectURL(attachment.preview);
    }
  }

  return changed ? next : prev;
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
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<AttachedFile[]>([]);
  const [cwdHistory, setCwdHistory] = useState<string[]>(loadHistory);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // User's preferred sidebar width — only changes on explicit drag/reset.
  const [sidebarWidth, setSidebarWidth] = useState(loadSidebarWidth);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const maxSidebarWidth = useSyncExternalStore(
    subscribeToWindowResize,
    getMaxSidebarWidth,
    () => SIDEBAR_MAX_WIDTH,
  );

  const handleOpenSettings = useCallback(() => {
    setSettingsOpen(true);
  }, []);

  const handleResizeSidebar = useCallback((next: number) => {
    setSidebarWidth((prev) => {
      if (prev === next) return prev;
      saveSidebarWidth(next);
      return next;
    });
  }, []);

  const handleResetSidebarWidth = useCallback(() => {
    handleResizeSidebar(SIDEBAR_DEFAULT_WIDTH);
  }, [handleResizeSidebar]);

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

  const {
    messages,
    messageSessionId,
    loading,
    sessions,
    activeSession,
    sessionLoading,
    pendingPermission,
    send,
    rewindAndSend,
    cancel,
    decidePermission,
    createSession,
    selectSession,
    deleteSession,
  } = useChat(config);

  const handleConfigUpdate = useCallback(
    (newConfig: LocalConfig) => {
      if (newConfig.cwd !== config.cwd) {
        const nextHistory = addHistory(cwdHistory, newConfig.cwd);
        setCwdHistory(nextHistory);
        saveHistory(nextHistory);
      }
      setLocalConfig(newConfig);
      saveConfig(newConfig);
    },
    [config.cwd, cwdHistory],
  );

  const handleRemoveHistory = useCallback((cwd: string) => {
    setCwdHistory((prev) => {
      if (!prev.includes(cwd)) return prev;
      const next = prev.filter((item) => item !== cwd);
      saveHistory(next);
      return next;
    });
  }, []);

  const clearAttachments = useCallback(() => {
    setAttachments((prev) => {
      for (const attachment of prev) {
        if (attachment.kind === "image")
          URL.revokeObjectURL(attachment.preview);
      }
      return [];
    });
  }, []);

  const handleSend = useCallback(() => {
    send(input, attachments.length ? attachments : undefined);
    setInput("");
    clearAttachments();
  }, [attachments, clearAttachments, input, send]);

  const handleAttachFiles = useCallback((newFiles: AttachedFile[]) => {
    setAttachments((prev) => [...prev, ...newFiles]);
  }, []);

  const handleRemoveAttachment = useCallback((id: string) => {
    setAttachments((prev) => {
      const removed = prev.find((attachment) => attachment.id === id);
      if (!removed) return prev;
      if (removed.kind === "image") URL.revokeObjectURL(removed.preview);
      return prev.filter((attachment) => attachment.id !== id);
    });
  }, []);

  const { image: supportsImageInput, pdf: supportsPdfInput } = useMemo(
    () => modelSupports(remoteConfig, config.provider, config.model),
    [config.model, config.provider, remoteConfig],
  );
  const workspaceMissing = remoteConfig?.cwd_exists === false;
  const workspaceDisabledReason = workspaceMissing
    ? "Workspace no longer exists. Choose another workspace."
    : undefined;

  // Side effect (not derived state): drop already-attached files the active
  // model can no longer accept and revoke their object URLs. Listening on the
  // capability flags catches both user-initiated model swaps and indirect
  // changes (cwd switch refetching /api/config, normalizeConfigWithRemoteDefaults
  // bumping us to a different default model, etc.).
  useEffect(() => {
    setAttachments((prev) =>
      pruneAttachments(prev, supportsImageInput, supportsPdfInput),
    );
  }, [supportsImageInput, supportsPdfInput]);

  const handleSelectSession = useCallback(
    (id: string) => {
      selectSession(id);
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

  return (
    <Layout>
      <div className="relative flex h-full min-h-0 overflow-hidden">
        {/* Mounted exclusively to avoid duplicate SWR / WorkspacePicker state. */}
        {isDesktop ? (
          <div className="shrink-0">
            <Sidebar
              sessions={sessions}
              activeSession={activeSession}
              onSelectSession={handleSelectSession}
              onCreateSession={handleCreateSession}
              onDeleteSession={handleDeleteSession}
              config={config}
              remoteConfig={remoteConfig}
              cwdHistory={cwdHistory}
              onUpdateConfig={handleConfigUpdate}
              onRemoveHistory={handleRemoveHistory}
              onOpenSettings={handleOpenSettings}
              workspaceMissing={workspaceMissing}
              width={displayedSidebarWidth}
              onResize={handleResizeSidebar}
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
              <Sidebar
                sessions={sessions}
                activeSession={activeSession}
                onSelectSession={handleSelectSession}
                onCreateSession={handleCreateSession}
                onDeleteSession={handleDeleteSession}
                config={config}
                remoteConfig={remoteConfig}
                cwdHistory={cwdHistory}
                onUpdateConfig={handleConfigUpdate}
                onRemoveHistory={handleRemoveHistory}
                onOpenSettings={handleOpenSettings}
                workspaceMissing={workspaceMissing}
                width={260}
                className="h-full"
              />
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

          {remoteConfigError && !remoteConfig ? (
            <div className="flex flex-1 items-center justify-center px-6">
              <div className="max-w-xl font-mono text-xs leading-6 text-muted-foreground">
                {remoteConfigError.message}
              </div>
            </div>
          ) : (
            <>
              <MessageList
                sessionId={messageSessionId ?? activeSession?.id}
                messages={messages}
                loading={loading}
                sessionLoading={sessionLoading}
                onRewindAndSend={workspaceMissing ? undefined : rewindAndSend}
              />

              <div className="shrink-0 pb-4 max-md:pb-1">
                {pendingPermission && (
                  <PermissionPrompt
                    request={pendingPermission}
                    onDecide={decidePermission}
                  />
                )}
                <InputArea
                  input={input}
                  setInput={setInput}
                  loading={loading}
                  onSend={handleSend}
                  onCancel={cancel}
                  supportsImages={supportsImageInput}
                  supportsDocuments={supportsPdfInput}
                  files={attachments}
                  onAttachFiles={handleAttachFiles}
                  onRemoveFile={handleRemoveAttachment}
                  config={config}
                  remoteConfig={remoteConfig}
                  onUpdateConfig={handleConfigUpdate}
                  disabledReason={workspaceDisabledReason}
                />
              </div>
            </>
          )}
        </main>
      </div>

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
