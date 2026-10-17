/**
 * Main application component.
 * Composes sidebar, chat interface, and theme provider.
 * Mobile: sidebar as overlay, top header bar.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";

import { fetchJson, type RemoteConfig, type SettingsResponse } from "@/api";
import { InputArea, type InputAreaHandle } from "@/components/composer/InputArea";
import { PermissionPrompt } from "@/components/composer/PermissionPrompt";
import { EmptyState, MessageList } from "@/components/messages/MessageList";
import { MobileHeader } from "@/components/MobileHeader";
import { SettingsPanel } from "@/components/settings/SettingsPanel";
import { SessionSearch } from "@/components/sidebar/SessionSearch";
import { DockedSidebar, Sidebar } from "@/components/sidebar/Sidebar";
import { ThemeProvider } from "@/components/ThemeProvider";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useChat } from "@/hooks/useChat";
import { useIsDesktop } from "@/hooks/useMediaQuery";
import type { AttachedFile, ComposerSubmission, LocalConfig, PendingInput } from "@/types";
import type { SlashCommand } from "@/utils/completion";
import { normalizeConfigWithRemoteDefaults } from "@/utils/config";
import { isMac } from "@/utils/platform";
import { addHistory, loadConfig, loadHistory, saveConfig, saveHistory } from "@/utils/storage";

function modelSupports(
  remoteConfig: RemoteConfig | null,
  providerKey: string,
  model: string,
): { image: boolean; pdf: boolean } {
  const key = providerKey || remoteConfig?.default?.provider || "";
  const info = remoteConfig?.providers?.[key];
  const m = model || remoteConfig?.default?.model || "";
  return {
    image: Boolean(info?.image_input_models.includes(m)),
    pdf: Boolean(info?.pdf_input_models.includes(m)),
  };
}

function AppContent() {
  const [localConfig, setLocalConfig] = useState<LocalConfig>(loadConfig);
  const [cwdHistory, setCwdHistory] = useState<string[]>(loadHistory);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);

  // Persisted state slices are saved on change, not by their mutators.
  useEffect(() => {
    saveConfig(localConfig);
  }, [localConfig]);
  useEffect(() => {
    saveHistory(cwdHistory);
  }, [cwdHistory]);

  const handleOpenSettings = useCallback(() => {
    setSettingsOpen(true);
  }, []);

  const isDesktop = useIsDesktop();

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
  } = useSWR<SettingsResponse, Error>("/api/settings", fetchJson<SettingsResponse>);

  const config = useMemo(
    () =>
      remoteConfig ? normalizeConfigWithRemoteDefaults(localConfig, remoteConfig) : localConfig,
    [localConfig, remoteConfig],
  );

  // Undelivered steers and queued messages come back ahead of the draft.
  const inputAreaRef = useRef<InputAreaHandle>(null);
  const restoreToComposer = useCallback((items: PendingInput[]) => {
    inputAreaRef.current?.prepend(items);
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
    jobs,
    stopJob,
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
      if (event.composedPath().some((el) => el instanceof Element && el.matches("[role=dialog]"))) {
        return;
      }
      cancel();
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
      if (event.composedPath().some((el) => el instanceof Element && el.matches("[role=dialog]"))) {
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
    setCwdHistory((prev) => (prev.includes(cwd) ? prev.filter((item) => item !== cwd) : prev));
  }, []);

  // While a chat runs, Enter steers it and Mod+Enter queues the next turn.
  const handleSubmit = useCallback(
    async (submission: ComposerSubmission, attachments: AttachedFile[], toQueue: boolean) => {
      const deliver = runKind !== "chat" ? send : toQueue ? queue : steer;
      return deliver(submission, attachments);
    },
    [queue, runKind, send, steer],
  );

  const handleEditQueued = useCallback(
    async (id: string) => {
      const item = await takeBackQueued(id);
      if (item) restoreToComposer([item]);
    },
    [restoreToComposer, takeBackQueued],
  );

  const { image: supportsImageInput, pdf: supportsPdfInput } = useMemo(
    () => modelSupports(remoteConfig, config.provider, config.model),
    [config.model, config.provider, remoteConfig],
  );
  const workspaceMissing = remoteConfig?.cwd_exists === false;
  const workspaceDisabledReason = workspaceMissing
    ? "Workspace no longer exists. Choose another workspace."
    : undefined;
  const setupRequired =
    Boolean(remoteConfig?.setup_error) || (!remoteConfig && Boolean(remoteConfigError));

  const handleSelectSession = useCallback(
    (id: string) => {
      void selectSession(id);
      setSidebarOpen(false);
    },
    [selectSession],
  );

  const handleCreateSession = useCallback(() => {
    createSession();
    setSidebarOpen(false);
  }, [createSession]);

  const handleDeleteSession = useCallback(
    async (id: string) => {
      const isActive = activeSession?.id === id;
      await deleteSession(id);
      if (isActive) setSidebarOpen(false);
    },
    [activeSession?.id, deleteSession],
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
    <div className="flex h-dvh w-full flex-col overflow-hidden">
      <div className="relative flex h-full min-h-0 overflow-hidden">
        {/* Mounted exclusively to avoid duplicate SWR / WorkspacePicker state. */}
        {isDesktop ? (
          <DockedSidebar {...sidebarProps} />
        ) : (
          <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
            <SheetContent side="left" showCloseButton={false} className="w-65 gap-0 bg-sidebar p-0">
              <SheetTitle className="sr-only">Navigation</SheetTitle>
              <Sidebar {...sidebarProps} />
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

          {/* Setup required (no provider, or no API): only the empty state, saying what to do. */}
          {setupRequired ? (
            <div className="flex min-h-0 flex-1 flex-col pt-6 pb-4">
              <EmptyState>
                {remoteConfig ? (
                  <div className="flex items-center gap-3">
                    <p className="text-sm text-muted-foreground">Add a provider to get started.</p>
                    <Button variant="outline" size="sm" onClick={handleOpenSettings}>
                      Open settings
                    </Button>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">Couldn&apos;t reach the API.</p>
                )}
              </EmptyState>
            </div>
          ) : (
            <MessageList
              sessionId={messageSessionId ?? activeSession?.id}
              messages={messages}
              loading={runKind === "chat"}
              compacting={runKind === "compact"}
              compactError={compactError}
              sendError={sendError}
              pendingSteers={pending.steers}
              onRewindAndSend={workspaceMissing ? undefined : rewindAndSend}
            />
          )}

          <div className="shrink-0 pb-4 max-md:pb-1">
            {pendingPermission && (
              <PermissionPrompt request={pendingPermission} onDecide={decidePermission} />
            )}
            <InputArea
              key={config.cwd}
              ref={inputAreaRef}
              sessionId={activeSession.id}
              loading={loading}
              compacting={runKind === "compact"}
              onSubmit={handleSubmit}
              onCancel={cancel}
              queued={pending.queue}
              onSteerQueued={steerQueued}
              onEditQueued={handleEditQueued}
              onRemoveQueued={removeQueued}
              jobs={jobs}
              onStopJob={stopJob}
              supportsImages={supportsImageInput}
              supportsDocuments={supportsPdfInput}
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
    </div>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <TooltipProvider delay={500}>
        <AppContent />
      </TooltipProvider>
    </ThemeProvider>
  );
}
