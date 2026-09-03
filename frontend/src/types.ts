export type Algorithm = 'huffman' | 'lzw' | 'rle'

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

export type AlgorithmInfo = { id: Algorithm; label: string; description: string }
