import { useEffect, useMemo, useState } from 'react'
import QRCode from 'qrcode'
import { api } from './api'
import type { Algorithm, AlgorithmInfo, Transfer } from './types'

const formatBytes = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1024 ** 2 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 ** 2).toFixed(2)} MB`

function App() {
  const [file, setFile] = useState<File>()
  const [algorithm, setAlgorithm] = useState<Algorithm>('lzw')
  const [algorithms, setAlgorithms] = useState<AlgorithmInfo[]>([])
  const [transfer, setTransfer] = useState<Transfer>()
  const [share, setShare] = useState('')
  const [network, setNetwork] = useState<{ url: string }>()
  const [status, setStatus] = useState('Ready for a local transfer')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [verified, setVerified] = useState(false)
  const [history, setHistory] = useState<Transfer[]>([])
  const [qr, setQr] = useState('')

  useEffect(() => { Promise.all([api.algorithms(), api.networkInfo()]).then(([items, info]) => { setAlgorithms(items); setNetwork(info) }).catch((e: Error) => setError(e.message)) }, [])
  const selectedInfo = useMemo(() => algorithms.find(item => item.id === algorithm), [algorithms, algorithm])

  const createTransfer = async () => {
    if (!file) return setError('Choose a file first.')
    setBusy(true); setError(''); setStatus('Compressing with the backend...')
    try {
      const result = await api.createTransfer(file, algorithm)
      setTransfer(result); setShare(`${window.location.origin}/share/${result.id}`); setHistory(current => [result, ...current.filter(item => item.id !== result.id)].slice(0, 5)); setStatus('Transfer ready to share')
    } catch (e) { setError((e as Error).message); setStatus('Compression failed') } finally { setBusy(false) }
  }
  const copy = async (value: string) => { await navigator.clipboard?.writeText(value); setStatus('Copied to clipboard') }
  const makeQr = async (value: string) => setQr(await QRCode.toDataURL(value, { width: 220, margin: 1, color: { dark: '#13202a', light: '#fffdf8' } }))
  const download = async (id: string) => {
    setBusy(true); setVerified(false); setStatus('Downloading FCMP container...')
    try { const response = await api.download(id); if (!response.ok) throw new Error('Download failed'); const blob = await response.blob(); const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = `${transfer?.filename ?? 'file'}.fcmp`; link.click(); URL.revokeObjectURL(url); setStatus('Download complete; verifying...'); const result = await api.verify(id); setVerified(result.verified); setStatus('FCMP validated and SHA-256 verified') } catch (e) { setError((e as Error).message); setStatus('Transfer failed') } finally { setBusy(false) }
  }

  if (location.pathname.startsWith('/share/')) {
    const id = location.pathname.split('/').pop()!
    if (!transfer || transfer.id !== id) api.transfer(id).then(setTransfer).catch((e: Error) => setError(e.message))
    return <main className="share-page"><div className="share-card"><span className="eyebrow">INCOMING FILE</span><h1>{transfer?.filename ?? 'Loading transfer...'}</h1>{transfer && <><p className="lead">{formatBytes(transfer.original_size)} <b>→</b> {formatBytes(transfer.compressed_size)}</p><div className="tag-row"><span>{transfer.algorithm.toUpperCase()}</span><span>{transfer.space_saving.toFixed(1)}% saved</span></div><button className="primary" disabled={busy} onClick={() => download(id)}>↓ Download file</button><p className={`status ${verified ? 'success' : ''}`}>{verified ? '✓ File received · FCMP validated · SHA-256 verified' : status}</p>{error && <p className="error">{error}</p>}</>}</div></main>
  }

  return <main><header><div className="brand"><span className="mark">FC</span><span>FileComp</span></div><div className="online"><i /> {network ? 'Online · Local network' : 'Connecting'}</div></header><section className="hero"><div><span className="eyebrow">LOCAL FILE TRANSFER</span><h1>Compress.<br /><em>Send.</em> Verify.</h1><p>Move files across your network with transparent compression and integrity checks.</p><div className="steps"><span className="active">01 Compress</span><span>02 Send</span><span>03 Verify</span></div></div><div className="address"><small>YOUR LOCAL ADDRESS</small><strong>{network?.url ?? 'Loading...'}</strong><div><button onClick={() => network && copy(network.url)}>Copy address</button><button onClick={() => network && makeQr(network.url)}>Show QR</button></div>{qr && <img className="qr" src={qr} alt="QR code for local address" />}</div></section><section className="workspace"><div className="panel send-panel"><div className="panel-head"><div><span className="eyebrow">SEND A FILE</span><h2>Prepare a transfer</h2></div><span className="step-number">01</span></div><label className="dropzone" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); setFile(e.dataTransfer.files[0]) }}><input type="file" onChange={e => setFile(e.target.files?.[0])} /><span className="upload-icon">↑</span><b>{file ? file.name : 'Drop a file here'}</b><small>{file ? formatBytes(file.size) : 'or choose from your device'}</small></label><h3>Compression algorithm</h3><div className="algorithms">{algorithms.map(item => <button key={item.id} className={algorithm === item.id ? 'selected' : ''} onClick={() => setAlgorithm(item.id)}><b>{item.label}</b><small>{item.description}</small></button>)}</div>{selectedInfo && <p className="hint">{selectedInfo.description}</p>}<button className="primary full" disabled={!file || busy} onClick={createTransfer}>{busy ? 'Working...' : 'Compress & create transfer →'}</button>{error && <p className="error">{error}</p>}</div><aside><div className="panel stats-panel"><span className="eyebrow">COMPRESSION PREVIEW</span><h2>{transfer ? 'Ready to share' : 'Your result'}</h2>{transfer ? <div className="metrics"><Metric label="Original" value={formatBytes(transfer.original_size)} /><Metric label="Compressed" value={formatBytes(transfer.compressed_size)} /><Metric label="Ratio" value={`${transfer.compression_ratio.toFixed(2)}x`} /><Metric label="Saved" value={`${transfer.space_saving.toFixed(1)}%`} /></div> : <div className="empty-metrics">Select a file and algorithm to see backend measurements.</div>}{transfer && <><div className="share-link"><small>SHARE THIS FILE</small><strong>{share}</strong><div><button onClick={() => copy(share)}>Copy link</button><button onClick={() => makeQr(share)}>QR code</button></div></div>{qr && <img className="qr" src={qr} alt="QR code for transfer" />}</>}</div><div className="panel history"><div className="panel-head"><div><span className="eyebrow">RECENT TRANSFERS</span><h2>History</h2></div></div>{history.length ? history.map(item => <div className="history-row" key={item.id}><span className="file-dot">{item.filename.slice(-3).toUpperCase()}</span><div><b>{item.filename}</b><small>{item.algorithm.toUpperCase()} · {formatBytes(item.compressed_size)}</small></div><span className="check">✓</span></div>) : <div className="empty-metrics">No transfers yet.</div>}</div></aside></section><footer><span>FileComp · Local-first file exchange</span><span>{status}</span></footer></main>
}
function Metric({ label, value }: { label: string; value: string }) { return <div><small>{label}</small><strong>{value}</strong></div> }
export default App
