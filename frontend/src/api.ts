import type { Algorithm, AlgorithmInfo, CompressionOperation, Transfer } from './types'

const API = import.meta.env.VITE_API_URL ?? ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(body.detail ?? 'Request failed')
  }
  return response.json() as Promise<T>
}

export const api = {
  algorithms: () => request<AlgorithmInfo[]>('/api/algorithms'),
  networkInfo: () => request<{ url: string }>('/api/network-info'),
  createTransfer: (file: File, algorithm: Algorithm) => {
    const form = new FormData()
    form.append('file', file)
    form.append('algorithm', algorithm)
    return request<Transfer>('/api/transfers', { method: 'POST', body: form })
  },
  startCompression: (file: File, algorithm: Algorithm) => {
    const form = new FormData()
    form.append('file', file)
    form.append('algorithm', algorithm)
    return request<CompressionOperation>('/api/compress', { method: 'POST', body: form })
  },
  compressionStatus: (id: string) => request<CompressionOperation>(`/api/compress/${id}/status`),
  cancelCompression: (id: string) => request<CompressionOperation>(`/api/compress/${id}/cancel`, { method: 'POST' }),
  transfer: (id: string) => request<Transfer>(`/api/transfers/${id}`),
  verify: (id: string) => request<{ verified: boolean; checksum: string; size: number }>(`/api/transfers/${id}/verify`, { method: 'POST' }),
  download: (id: string) => fetch(`${API}/api/transfers/${id}/download`),
}
