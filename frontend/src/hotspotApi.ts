import type { HotspotBlocked, HotspotClients, HotspotEvent } from './types'

const API = import.meta.env.VITE_API_URL ?? ''
const AUTH_KEY = 'filecomp-hotspot-auth'

export class HotspotApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

// The password lives only for this tab; sessionStorage may be unavailable (private mode), so failures are ignored.
export const savedAuth = (): string | null => {
  try {
    return sessionStorage.getItem(AUTH_KEY)
  } catch {
    return null
  }
}

export const saveAuth = (auth: string | null) => {
  try {
    if (auth) sessionStorage.setItem(AUTH_KEY, auth)
    else sessionStorage.removeItem(AUTH_KEY)
  } catch {
    // keep working with the in-memory value
  }
}

// btoa only takes Latin-1, so the password is UTF-8 encoded first (Cyrillic passwords work too).
export const basicAuth = (password: string) =>
  'Basic ' + btoa(String.fromCharCode(...new TextEncoder().encode(`admin:${password}`)))

async function call<T>(auth: string, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API}/api/hotspot${path}`, {
    ...init,
    headers: { ...init.headers, Authorization: auth },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new HotspotApiError(body.detail ?? 'Request failed', response.status)
  }
  return response.json() as Promise<T>
}

const mac = (value: string) => encodeURIComponent(value)

export const hotspotApi = (auth: string) => ({
  clients: () => call<HotspotClients>(auth, '/clients'),
  events: (limit: number) => call<{ events: HotspotEvent[]; total: number }>(auth, `/events?limit=${limit}`),
  blocked: () => call<{ blocked: HotspotBlocked[]; error: string | null }>(auth, '/blocked'),
  kick: (value: string) => call<{ method: string }>(auth, `/clients/${mac(value)}/kick`, { method: 'POST' }),
  block: (value: string) => call<{ kicked: boolean }>(auth, `/blocked/${mac(value)}`, { method: 'POST' }),
  unblock: (value: string) => call(auth, `/blocked/${mac(value)}`, { method: 'DELETE' }),
  deleteEvent: (id: number) => call(auth, `/events/${id}`, { method: 'DELETE' }),
  clearEvents: () => call<{ deleted: number }>(auth, '/events', { method: 'DELETE' }),
  setLabel: (value: string, label: string) =>
    call(auth, `/labels/${mac(value)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label }),
    }),
})
