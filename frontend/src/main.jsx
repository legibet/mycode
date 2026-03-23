import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { preloadHighlighter } from './utils/highlighter'
import './index.css'

await preloadHighlighter()

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
