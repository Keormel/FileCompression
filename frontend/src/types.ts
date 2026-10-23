export type Algorithm = 'huffman' | 'lzw' | 'rle' | 'stored'
export type AlgorithmChoice = Algorithm | 'auto'

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
  compression_time: number | null
  recommended_algorithm: Algorithm | null
}

export type CompressionOperation = {
  operationId: string
  filename: string
  algorithm: AlgorithmChoice
  progress: number
  processedBytes: number
  totalBytes: number
  speed: number
  eta: number | null
  status: 'queued' | 'processing' | 'cancelling' | 'completed' | 'cancelled' | 'error'
  error: string | null
  transferId: string | null
  recommendedAlgorithm: Algorithm | null
  compressionTime: number | null
}

export type AlgorithmInfo = { id: Algorithm; label: string; description: string }

export type Recommendation = {
  recommended: Algorithm
  beneficial: boolean
  sampleSize: number
  estimates: { algorithm: Algorithm; size: number; ratio: number }[]
}

export type NetworkInfo = {
  host: string
  port: number
  url: string
  urls: string[]
  mdns: string | null
  maxUploadBytes: number
}
