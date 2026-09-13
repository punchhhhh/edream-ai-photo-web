import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import OpsApp from './ops/OpsApp.tsx'

const RootApp = window.location.pathname.startsWith('/ops') ? OpsApp : App

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RootApp />
  </StrictMode>,
)
