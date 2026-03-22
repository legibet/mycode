import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { preloadHighlightedCode } from './components/Chat/CodeBlock'
import './index.css'

void preloadHighlightedCode()

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
