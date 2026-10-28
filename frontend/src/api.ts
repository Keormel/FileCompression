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
// Called before an automatic retry with the delay in seconds and the attempt number.
export type UploadRetry = (delaySeconds: number, attempt: number) => void

class UploadError extends Error {
  // status 0 means the connection itself failed (Wi-Fi dropped, server refused it).
  constructor(message: string, readonly status: number, readonly retryAfter?: number) {
    super(message)
  }
}

const UPLOAD_ATTEMPTS = 8
// Busy server, per-device limit, or a dropped connection: worth trying again. Anything else
// (too large, bad file, storage full) would fail the same way again.
const retryable = (error: unknown) => error instanceof UploadError && [0, 429, 503].includes(error.status)

// Sends the file as the raw request body: the server writes it straight to disk, and
// XMLHttpRequest (unlike fetch) reports upload progress, which matters for large files.
function sendOnce<T>(path: string, file: Blob, onProgress?: UploadProgress): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API}${path}`)
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    xhr.responseType = 'json'
    xhr.upload.onprogress = (event) => onProgress?.(event.loaded, event.lengthComputable ? event.total : file.size)
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response as T)
      else {
        const retryAfter = Number(xhr.getResponseHeader('Retry-After')) || undefined
        reject(new UploadError(xhr.response?.detail ?? `Upload failed (${xhr.status})`, xhr.status, retryAfter))
      }
    }
    xhr.onerror = () => reject(new UploadError('Network error during upload: the connection dropped or the server is overloaded. Try again.', 0))
    xhr.onabort = () => reject(new UploadError('Upload cancelled', -1))
    xhr.send(file)
  })
}

// When many people upload at once the server answers "busy" instead of queueing everyone;
// waiting the time it asks for, plus random jitter so clients do not all return together,
// turns that into a queue that costs the server nothing.
async function upload<T>(path: string, file: Blob, onProgress?: UploadProgress, onRetry?: UploadRetry): Promise<T> {
  for (let attempt = 1; ; attempt++) {
    try {
      return await sendOnce<T>(path, file, onProgress)
    } catch (error) {
      if (!retryable(error) || attempt >= UPLOAD_ATTEMPTS) throw error
      const base = (error as UploadError).retryAfter ?? Math.min(2 ** attempt, 30)
      const delay = Math.round(base + Math.random() * base * 0.5)
      onRetry?.(delay, attempt)
      onProgress?.(0, file.size)
      await new Promise((resolve) => setTimeout(resolve, delay * 1000))
    }
  }
}

const query = (params: Record<string, string>) => new URLSearchParams(params).toString()

// Above this size the gzip copy is not built: the browser has to hold it before sending,
// which is too much for a phone.
const GZIP_UPLOAD_LIMIT = 256 * 1024 * 1024

// Over Wi-Fi or a hotspot the network is the bottleneck, so a text-like file is gzipped in the
// browser first (native code, much faster than the link) and the server inflates it while writing.
// Returns undefined when gzip is unavailable or does not shrink the file enough to be worth it.
export async function gzipForUpload(file: File): Promise<Blob | undefined> {
  if (typeof CompressionStream === 'undefined' || file.size > GZIP_UPLOAD_LIMIT || file.size < 64 * 1024) return undefined
  try {
    const packed = await new Response(file.stream().pipeThrough(new CompressionStream('gzip'))).blob()
    return packed.size < file.size * 0.9 ? packed : undefined
  } catch {
    return undefined
  }
}

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
  // `gzipped` is the gzip copy from gzipForUpload; `size` lets the server check limits and disk space up front.
  startCompression: (file: File, algorithm: AlgorithmChoice, onProgress?: UploadProgress, gzipped?: Blob, onRetry?: UploadRetry) =>
    upload<CompressionOperation>(
      `/api/compress/stream?${query({ filename: file.name, algorithm, size: String(file.size), ...(gzipped ? { encoding: 'gzip' } : {}) })}`,
      gzipped ?? file,
      onProgress,
      onRetry,
    ),
  compressionStatus: (id: string) => request<CompressionOperation>(`/api/compress/${id}/status`),
  cancelCompression: (id: string) => request<CompressionOperation>(`/api/compress/${id}/cancel`, { method: 'POST' }),
  transfer: (id: string) => request<Transfer>(`/api/transfers/${id}`),
  verify: (id: string) => request<{ verified: boolean; checksum: string; size: number }>(`/api/transfers/${id}/verify`, { method: 'POST' }),
  // A plain URL lets the browser stream the download to disk instead of buffering it in memory.
  downloadUrl: (id: string, compressed = false) => `${API}/api/transfers/${id}/download${compressed ? '?compressed=true' : ''}`,
  importArchive: (file: File, onProgress?: UploadProgress, onRetry?: UploadRetry) =>
    upload<Transfer>(`/api/archives?${query({ filename: file.name })}`, file, onProgress, onRetry),
}
