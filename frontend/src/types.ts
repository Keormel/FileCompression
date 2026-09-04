export type Algorithm = 'huffman' | 'lzw' | 'rle' | 'stored'

export type Transfer = {
  id: string
  filename: string
  algorithm: Algorithm
  original_size: number
  compressed_size: number
  compression_ratio: number
  space_saving: number
  checksum: string
  status: string
  expires_in: number
}

export type CompressionOperation = {
  operationId: string
  filename: string
  algorithm: Algorithm
  progress: number
  processedBytes: number
  totalBytes: number
  speed: number
  eta: number | null
  status: 'queued' | 'processing' | 'cancelling' | 'completed' | 'cancelled' | 'error'
  error: string | null
  transferId: string | null
}

export type AlgorithmInfo = { id: Algorithm; label: string; description: string }
