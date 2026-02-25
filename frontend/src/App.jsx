/**
 * Main application component.
 * Composes sidebar, chat interface, and theme provider.
 * Adds gradient fade mask above input area.
 */

import { useEffect, useState } from 'react'
import { InputArea } from './components/Chat/InputArea'
import { MessageList } from './components/Chat/MessageList'
import { Layout } from './components/Layout'
import { Sidebar } from './components/Sidebar'
import { ThemeProvider, useTheme } from './components/ThemeProvider'
import { useChat } from './hooks/useChat'
import { addHistory, loadConfig, loadHistory, saveConfig, saveHistory } from './utils/storage'

function AppContent() {
  const [config, setConfig] = useState(loadConfig)
  const [input, setInput] = useState('')
  const [cwdHistory, setCwdHistory] = useState(loadHistory)
  const [remoteConfig, setRemoteConfig] = useState(null)
  const { theme, setTheme } = useTheme()

  const { messages, loading, sessions, activeSession, send, cancel, createSession, selectSession, deleteSession } =
    useChat(config)

  useEffect(() => {
    fetch('/api/config')
      .then((r) => r.json())
      .then((data) => {
        setRemoteConfig(data)
        setConfig((prev) => {
          if (prev.provider || !data.default?.provider) return prev
          const updated = {
            ...prev,
            provider: data.default.provider,
            model: prev.model || data.default.model || '',
          }
          saveConfig(updated)
          return updated
        })
      })
      .catch(() => {})
  }, [])

  const handleConfigUpdate = (newConfig) => {
    if (newConfig.cwd !== config.cwd) {
      const nextHistory = addHistory(cwdHistory, newConfig.cwd)
      setCwdHistory(nextHistory)
      saveHistory(nextHistory)
    }
    setConfig(newConfig)
    saveConfig(newConfig)
  }

  useEffect(() => {
    const nextHistory = addHistory(loadHistory(), config.cwd)
    setCwdHistory(nextHistory)
    saveHistory(nextHistory)
  }, [config.cwd])

  const handleSend = () => {
    send(input)
    setInput('')
  }

  return (
    <Layout>
      <div className="flex h-full">
        <Sidebar
          sessions={sessions}
          activeSession={activeSession}
          onSelectSession={selectSession}
          onCreateSession={createSession}
          onDeleteSession={deleteSession}
          config={config}
          onUpdateConfig={handleConfigUpdate}
          cwdHistory={cwdHistory}
          remoteConfig={remoteConfig}
          theme={theme}
          setTheme={setTheme}
        />

        <main className="flex min-w-0 flex-1 flex-col bg-background relative">
          <MessageList messages={messages} loading={loading} />

          {/* Gradient fade above input */}
          <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-24 bg-gradient-to-t from-background via-background/80 to-transparent" />

          <div className="shrink-0 relative z-10 pb-4 pt-1">
            <InputArea input={input} setInput={setInput} loading={loading} onSend={handleSend} onCancel={cancel} />
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
