/**
 * Main application component.
 * Composes sidebar, chat interface, and theme provider.
 * Mobile: sidebar as overlay, top header bar.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import useSWR from 'swr'
import { InputArea } from './components/Chat/InputArea'
import { MessageList } from './components/Chat/MessageList'
import { Layout } from './components/Layout'
import { MobileHeader } from './components/MobileHeader'
import { Sidebar } from './components/Sidebar'
import { ThemeProvider, useTheme } from './components/ThemeProvider'
import { useChat } from './hooks/useChat'
import { normalizeConfigWithRemoteDefaults } from './utils/config'
import {
  addHistory,
  loadConfig,
  loadHistory,
  saveConfig,
  saveHistory,
} from './utils/storage'

async function fetchJson(url) {
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
  return response.json()
}

function AppContent() {
  const [config, setConfig] = useState(loadConfig)
  const [input, setInput] = useState('')
  const [cwdHistory, setCwdHistory] = useState(loadHistory)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const { theme, setTheme } = useTheme()
  const configUrl = `/api/config?cwd=${encodeURIComponent(config.cwd)}`
  const { data: remoteConfig = null, error: remoteConfigError } = useSWR(
    configUrl,
    fetchJson,
    {
      keepPreviousData: true,
      onError: (error) => {
        console.error('Failed to load config:', error)
      },
    },
  )

  const {
    messages,
    loading,
    sessions,
    activeSession,
    send,
    rewindAndSend,
    cancel,
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

  const handleConfigUpdate = (newConfig) => {
    if (newConfig.cwd !== config.cwd) {
      const nextHistory = addHistory(cwdHistory, newConfig.cwd)
      setCwdHistory(nextHistory)
      saveHistory(nextHistory)
    }
    setConfig(newConfig)
    saveConfig(newConfig)
  }

  const inputRef = useRef(input)
  inputRef.current = input

  const handleSend = useCallback(() => {
    send(inputRef.current)
    setInput('')
  }, [send])

  const handleSelectSession = useCallback(
    (id) => {
      selectSession(id)
      setSidebarOpen(false)
    },
    [selectSession],
  )

  const handleCreateSession = useCallback(() => {
    createSession()
    setSidebarOpen(false)
  }, [createSession])

  return (
    <Layout>
      <div className="flex h-full relative">
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

        {/* Sidebar — fixed on desktop, overlay on mobile */}
        <div
          className={`
            max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-50
            max-md:transition-transform max-md:duration-300 max-md:ease-out
            ${sidebarOpen ? 'max-md:translate-x-0' : 'max-md:-translate-x-full'}
          `}
        >
          <Sidebar
            sessions={sessions}
            activeSession={activeSession}
            onSelectSession={handleSelectSession}
            onCreateSession={handleCreateSession}
            onDeleteSession={deleteSession}
            config={config}
            onUpdateConfig={handleConfigUpdate}
            cwdHistory={cwdHistory}
            remoteConfig={remoteConfig}
            theme={theme}
            setTheme={setTheme}
            className="h-full"
          />
        </div>

        {/* Main content */}
        <main className="flex min-w-0 flex-1 flex-col bg-background relative">
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
                <InputArea
                  input={input}
                  setInput={setInput}
                  loading={loading}
                  onSend={handleSend}
                  onCancel={cancel}
                />
              </div>
            </>
          )}
        </main>
      </div>
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
