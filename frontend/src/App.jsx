/**
 * Main application component.
 * Composes sidebar, chat interface, and theme provider.
 * Mobile: sidebar as overlay, top header bar.
 */

import { useCallback, useEffect, useState } from 'react'
import { InputArea } from './components/Chat/InputArea'
import { MessageList } from './components/Chat/MessageList'
import { Layout } from './components/Layout'
import { MobileHeader } from './components/MobileHeader'
import { Sidebar } from './components/Sidebar'
import { ThemeProvider, useTheme } from './components/ThemeProvider'
import { useChat } from './hooks/useChat'
import {
  addHistory,
  loadConfig,
  loadHistory,
  saveConfig,
  saveHistory,
} from './utils/storage'

function AppContent() {
  const [config, setConfig] = useState(loadConfig)
  const [input, setInput] = useState('')
  const [cwdHistory, setCwdHistory] = useState(loadHistory)
  const [remoteConfig, setRemoteConfig] = useState(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const { theme, setTheme } = useTheme()

  const {
    messages,
    loading,
    sessions,
    activeSession,
    send,
    cancel,
    createSession,
    selectSession,
    deleteSession,
  } = useChat(config)

  useEffect(() => {
    const controller = new AbortController()

    fetch(`/api/config?cwd=${encodeURIComponent(config.cwd)}`, {
      signal: controller.signal,
    })
      .then((r) => r.json())
      .then((data) => {
        setRemoteConfig(data)
        setConfig((prev) => {
          const providers = data.providers || {}
          const providerNames = Object.keys(providers)
          const currentProviderValid =
            prev.provider && providerNames.includes(prev.provider)
          if (currentProviderValid) return prev

          const nextProvider = data.default?.provider || ''
          const nextModel =
            data.default?.model || providers[nextProvider]?.models?.[0] || ''
          if (prev.provider === nextProvider && prev.model === nextModel)
            return prev

          const updated = {
            ...prev,
            provider: nextProvider,
            model: nextModel,
          }
          saveConfig(updated)
          return updated
        })
      })
      .catch((error) => {
        if (error.name !== 'AbortError') {
          console.error('Failed to load config:', error)
        }
      })

    return () => controller.abort()
  }, [config.cwd])

  const handleConfigUpdate = (newConfig) => {
    if (newConfig.cwd !== config.cwd) {
      const nextHistory = addHistory(cwdHistory, newConfig.cwd)
      setCwdHistory(nextHistory)
      saveHistory(nextHistory)
    }
    setConfig(newConfig)
    saveConfig(newConfig)
  }

  const handleSend = () => {
    send(input)
    setInput('')
  }

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

          <MessageList messages={messages} loading={loading} />

          {/* Gradient fade above input */}
          <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-24 bg-gradient-to-t from-background via-background/80 to-transparent" />

          <div className="shrink-0 relative z-10 pb-4 max-md:pb-2 pt-1">
            <InputArea
              input={input}
              setInput={setInput}
              loading={loading}
              onSend={handleSend}
              onCancel={cancel}
            />
          </div>
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
