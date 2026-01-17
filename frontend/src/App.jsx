import { useState } from 'react'
import { InputArea } from './components/Chat/InputArea'
import { MessageList } from './components/Chat/MessageList'
import { Header } from './components/Header'
import { Layout } from './components/Layout'
import { SettingsDrawer } from './components/Settings/SettingsDrawer'
import { ThemeProvider } from './components/ThemeProvider'
import { useChat } from './hooks/useChat'
import { loadConfig, saveConfig } from './utils/storage'

export default function App() {
  const [config, setConfig] = useState(loadConfig)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [input, setInput] = useState('')

  const { messages, loading, status, send, clear, cancel } = useChat(config)

  const handleConfigSave = (newConfig) => {
    setConfig(newConfig)
    saveConfig(newConfig)
    setDrawerOpen(false)
  }

  const handleSend = () => {
    send(input)
    setInput('')
  }

  return (
    <ThemeProvider>
      <Layout>
        <Header config={config} status={status} onClear={clear} onOpenSettings={() => setDrawerOpen(true)} />

        <main className="flex min-h-0 flex-1 flex-col">
          <MessageList messages={messages} loading={loading} />

          <div className="shrink-0 pb-4 pt-2">
            <InputArea input={input} setInput={setInput} loading={loading} onSend={handleSend} onCancel={cancel} />
          </div>
        </main>

        <SettingsDrawer
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          config={config}
          onSave={handleConfigSave}
        />
      </Layout>
    </ThemeProvider>
  )
}
