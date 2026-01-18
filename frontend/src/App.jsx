import { useState } from 'react'
import { InputArea } from './components/Chat/InputArea'
import { MessageList } from './components/Chat/MessageList'
import { Layout } from './components/Layout'
import { Sidebar } from './components/Sidebar'
import { ThemeProvider, useTheme } from './components/ThemeProvider'
import { useChat } from './hooks/useChat'
import { loadConfig, saveConfig } from './utils/storage'

function AppContent() {
  const [config, setConfig] = useState(loadConfig)
  const [input, setInput] = useState('')
  const { theme, setTheme } = useTheme()

  const { messages, loading, sessions, activeSession, send, cancel, createSession, selectSession, deleteSession } =
    useChat(config)

  const handleConfigUpdate = (newConfig) => {
    setConfig(newConfig)
    saveConfig(newConfig)
  }

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
          theme={theme}
          setTheme={setTheme}
        />

        <main className="flex min-w-0 flex-1 flex-col bg-background">
          <MessageList messages={messages} loading={loading} />
          <div className="shrink-0 pb-6 pt-2">
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
