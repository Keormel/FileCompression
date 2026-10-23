import type { Algorithm, AlgorithmChoice, AlgorithmInfo, CompressionOperation, NetworkInfo, Recommendation, Transfer } from './types'

const API = import.meta.env.VITE_API_URL ?? ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(body.detail ?? 'Request failed')
  }
  return response.json() as Promise<T>
}

export type UploadProgress = (loaded: number, total: number) => void

// Sends the file as the raw request body: the server writes it straight to disk, and
// XMLHttpRequest (unlike fetch) reports upload progress, which matters for large files.
function upload<T>(path: string, file: Blob, onProgress?: UploadProgress): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API}${path}`)
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    xhr.responseType = 'json'
    xhr.upload.onprogress = (event) => onProgress?.(event.loaded, event.lengthComputable ? event.total : file.size)
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response as T)
      else reject(new Error(xhr.response?.detail ?? `Upload failed (${xhr.status})`))
    }
    xhr.onerror = () => reject(new Error('Network error during upload'))
    xhr.onabort = () => reject(new Error('Upload cancelled'))
    xhr.send(file)
  })
}

const query = (params: Record<string, string>) => new URLSearchParams(params).toString()

export const api = {
  algorithms: () => request<AlgorithmInfo[]>('/api/algorithms'),
  networkInfo: () => request<NetworkInfo>('/api/network-info'),
  createTransfer: (file: File, algorithm: Algorithm) => {
    const form = new FormData()
    form.append('file', file)
    form.append('algorithm', algorithm)
    return request<Transfer>('/api/transfers', { method: 'POST', body: form })
  },
  recommend: (sample: Blob) => {
    const form = new FormData()
    form.append('file', sample, 'sample.bin')
    return request<Recommendation>('/api/recommend', { method: 'POST', body: form })
  },
  startCompression: (file: File, algorithm: AlgorithmChoice, onProgress?: UploadProgress) =>
    upload<CompressionOperation>(`/api/compress/stream?${query({ filename: file.name, algorithm })}`, file, onProgress),
  compressionStatus: (id: string) => request<CompressionOperation>(`/api/compress/${id}/status`),
  cancelCompression: (id: string) => request<CompressionOperation>(`/api/compress/${id}/cancel`, { method: 'POST' }),
  transfer: (id: string) => request<Transfer>(`/api/transfers/${id}`),
  verify: (id: string) => request<{ verified: boolean; checksum: string; size: number }>(`/api/transfers/${id}/verify`, { method: 'POST' }),
  // A plain URL lets the browser stream the download to disk instead of buffering it in memory.
  downloadUrl: (id: string, compressed = false) => `${API}/api/transfers/${id}/download${compressed ? '?compressed=true' : ''}`,
  importArchive: (file: File, onProgress?: UploadProgress) =>
    upload<Transfer>(`/api/archives?${query({ filename: file.name })}`, file, onProgress),
}
