/**
 * Main application component.
 * Composes sidebar, chat interface, and theme provider.
 * Mobile: sidebar as overlay, top header bar.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import useSWR from 'swr'
import { InputArea } from './components/Chat/InputArea'
import { MessageList } from './components/Chat/MessageList'
import { PermissionPrompt } from './components/Chat/PermissionPrompt'
import { Layout } from './components/Layout'
import { MobileHeader } from './components/MobileHeader'
import { SettingsPanel } from './components/Settings/SettingsPanel'
import { Sidebar } from './components/Sidebar'
import { ThemeProvider } from './components/ThemeProvider'
import { useChat } from './hooks/useChat'
import type { AttachedFile, LocalConfig, RemoteConfig } from './types'
import { normalizeConfigWithRemoteDefaults } from './utils/config'
import {
  getMaxSidebarWidth,
  SIDEBAR_DEFAULT_WIDTH,
  SIDEBAR_MIN_WIDTH,
} from './utils/sidebar'
import {
  addHistory,
  loadConfig,
  loadHistory,
  loadSidebarWidth,
  saveConfig,
  saveHistory,
  saveSidebarWidth,
} from './utils/storage'

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url)
  if (!response.ok) {
    let message = `Request failed with status ${response.status}`
    try {
      const data = await response.json()
      if (typeof data?.detail === 'string' && data.detail) {
        message = data.detail
      }
    } catch {}
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

function modelSupports(
  remoteConfig: RemoteConfig | null,
  providerKey: string,
  model: string,
): { image: boolean; pdf: boolean } {
  const key = providerKey || remoteConfig?.default?.provider || ''
  const info = remoteConfig?.providers?.[key]
  const m = model || remoteConfig?.default?.model || ''
  return {
    image: Boolean(
      info?.supports_image_input && info.image_input_models?.includes(m),
    ),
    pdf: Boolean(
      info?.supports_pdf_input && info.pdf_input_models?.includes(m),
    ),
  }
}

function pruneAttachments(
  prev: AttachedFile[],
  supportsImage: boolean,
  supportsPdf: boolean,
): AttachedFile[] {
  const next = prev.filter(
    (a) =>
      a.kind === 'text' ||
      (a.kind === 'image' && supportsImage) ||
      (a.kind === 'document' && supportsPdf),
  )
  if (next.length === prev.length) return prev
  for (const a of prev) {
    if (a.kind === 'image' && !next.includes(a)) URL.revokeObjectURL(a.preview)
  }
  return next
}

function AppContent() {
  const [config, setConfig] = useState<LocalConfig>(loadConfig)
  const [input, setInput] = useState('')
  const [attachments, setAttachments] = useState<AttachedFile[]>([])
  const [cwdHistory, setCwdHistory] = useState<string[]>(loadHistory)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  // User's preferred sidebar width — only changes on explicit drag/reset.
  const [sidebarWidth, setSidebarWidth] = useState(loadSidebarWidth)
  // Viewport-dependent cap; recomputed on window resize and used at render time
  // so a narrow viewport clamps the displayed width without touching user intent.
  const [maxSidebarWidth, setMaxSidebarWidth] = useState(getMaxSidebarWidth)
  const [settingsOpen, setSettingsOpen] = useState(false)

  const handleResizeSidebar = useCallback((next: number) => {
    setSidebarWidth((prev) => {
      if (prev === next) return prev
      saveSidebarWidth(next)
      return next
    })
  }, [])

  const handleResetSidebarWidth = useCallback(() => {
    handleResizeSidebar(SIDEBAR_DEFAULT_WIDTH)
  }, [handleResizeSidebar])

  useEffect(() => {
    const onWindowResize = () => setMaxSidebarWidth(getMaxSidebarWidth())
    window.addEventListener('resize', onWindowResize)
    return () => window.removeEventListener('resize', onWindowResize)
  }, [])

  const displayedSidebarWidth = Math.max(
    SIDEBAR_MIN_WIDTH,
    Math.min(maxSidebarWidth, sidebarWidth),
  )
  const configUrl = `/api/config?cwd=${encodeURIComponent(config.cwd)}`
  const {
    data: remoteConfig = null,
    error: remoteConfigError,
    mutate: mutateRemoteConfig,
  } = useSWR<RemoteConfig, Error>(configUrl, fetchJson<RemoteConfig>, {
    keepPreviousData: true,
  })

  const {
    messages,
    loading,
    sessions,
    activeSession,
    pendingPermission,
    send,
    rewindAndSend,
    cancel,
    decidePermission,
    createSession,
    selectSession,
    deleteSession,
  } = useChat(config)

  useEffect(() => {
    if (!remoteConfig) return

    setConfig((prev) => {
      const updated = normalizeConfigWithRemoteDefaults(prev, remoteConfig)
      if (
        prev.provider === updated.provider &&
        prev.model === updated.model &&
        prev.reasoningEffort === updated.reasoningEffort
      ) {
        return prev
      }
      saveConfig(updated)
      return updated
    })
  }, [remoteConfig])

  const handleConfigUpdate = useCallback(
    (newConfig: LocalConfig) => {
      if (newConfig.cwd !== config.cwd) {
        const nextHistory = addHistory(cwdHistory, newConfig.cwd)
        setCwdHistory(nextHistory)
        saveHistory(nextHistory)
      }
      setConfig(newConfig)
      saveConfig(newConfig)
    },
    [config.cwd, cwdHistory],
  )

  const clearAttachments = useCallback(() => {
    setAttachments((prev) => {
      for (const attachment of prev) {
        if (attachment.kind === 'image') URL.revokeObjectURL(attachment.preview)
      }
      return []
    })
  }, [])

  const handleSend = useCallback(() => {
    send(input, attachments.length ? attachments : undefined)
    setInput('')
    clearAttachments()
  }, [attachments, clearAttachments, input, send])

  const handleAttachFiles = useCallback((newFiles: AttachedFile[]) => {
    setAttachments((prev) => [...prev, ...newFiles])
  }, [])

  const handleRemoveAttachment = useCallback((index: number) => {
    setAttachments((prev) => {
      const next = [...prev]
      const removed = next.splice(index, 1)
      if (removed[0]?.kind === 'image') URL.revokeObjectURL(removed[0].preview)
      return next
    })
  }, [])

  const { image: supportsImageInput, pdf: supportsPdfInput } = useMemo(
    () => modelSupports(remoteConfig, config.provider, config.model),
    [config.model, config.provider, remoteConfig],
  )

  // Side effect (not derived state): drop already-attached files the active
  // model can no longer accept and revoke their object URLs. Listening on the
  // capability flags catches both user-initiated model swaps and indirect
  // changes (cwd switch refetching /api/config, normalizeConfigWithRemoteDefaults
  // bumping us to a different default model, etc.).
  useEffect(() => {
    setAttachments((prev) =>
      pruneAttachments(prev, supportsImageInput, supportsPdfInput),
    )
  }, [supportsImageInput, supportsPdfInput])

  const handleSelectSession = useCallback(
    (id: string) => {
      selectSession(id)
      setSidebarOpen(false)
      clearAttachments()
    },
    [selectSession, clearAttachments],
  )

  const handleCreateSession = useCallback(() => {
    createSession()
    setSidebarOpen(false)
    clearAttachments()
  }, [createSession, clearAttachments])

  const handleDeleteSession = useCallback(
    async (id: string) => {
      const isActive = activeSession?.id === id
      await deleteSession(id)
      if (!isActive) return
      setSidebarOpen(false)
      clearAttachments()
    },
    [activeSession?.id, clearAttachments, deleteSession],
  )

  return (
    <Layout>
      <div className="relative flex h-full min-h-0 overflow-hidden">
        {/* Mobile overlay backdrop */}
        {sidebarOpen && (
          <button
            type="button"
            aria-label="Close sidebar"
            tabIndex={-1}
            className="fixed inset-0 z-40 bg-background/60 backdrop-blur-sm md:hidden cursor-default"
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* Sidebar — resizable on desktop, overlay on mobile */}
        <div
          className={`
            md:shrink-0
            max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-50
            max-md:transition-transform max-md:duration-300 max-md:ease-out
            max-md:overscroll-y-contain
            ${sidebarOpen ? 'max-md:translate-x-0' : 'max-md:-translate-x-full'}
          `}
        >
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
            onOpenSettings={() => setSettingsOpen(true)}
            width={displayedSidebarWidth}
            onResize={handleResizeSidebar}
            onResizeReset={handleResetSidebarWidth}
            className="h-full"
          />
        </div>

        {/* Main content */}
        <main className="relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background">
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
                messages={messages}
                loading={loading}
                onRewindAndSend={rewindAndSend}
              />

              {/* Gradient fade above input */}
              <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-24 bg-gradient-to-t from-background via-background/80 to-transparent" />

              <div className="shrink-0 relative z-10 pb-4 max-md:pb-[max(0.5rem,env(safe-area-inset-bottom))] pt-1">
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
                />
              </div>
            </>
          )}
        </main>
      </div>

      <SettingsPanel
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={() => {
          void mutateRemoteConfig()
        }}
        projectConfigPaths={remoteConfig?.config_paths}
      />
    </Layout>
  )
}

export default function App() {
  return (
    <ThemeProvider>
      <AppContent />
    </ThemeProvider>
  )
}
