import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
// Fonts are bundled instead of loaded from Google Fonts: on a hotspot or LAN without
// internet that request hangs until it times out and blocks the first render.
import '@fontsource/manrope/400.css'
import '@fontsource/manrope/500.css'
import '@fontsource/manrope/600.css'
import '@fontsource/manrope/700.css'
import '@fontsource/manrope/800.css'
import '@fontsource/dm-mono/400.css'
import '@fontsource/dm-mono/500.css'
import './styles.css'
import App from './App'

// The admin panel is a separate chunk, so the main page does not download it.
const HotspotAdmin = lazy(() => import('./HotspotAdmin'))
const page = location.pathname.startsWith('/admin/hotspot') ? <Suspense><HotspotAdmin /></Suspense> : <App />

createRoot(document.getElementById('root')!).render(<StrictMode>{page}</StrictMode>)
