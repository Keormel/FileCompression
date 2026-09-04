import { useEffect, useMemo, useState } from "react";
import QRCode from "qrcode";
import { api } from "./api";
import type {
  Algorithm,
  AlgorithmInfo,
  CompressionOperation,
  Transfer,
} from "./types";

const bytes = (value: number) =>
  value < 1024
    ? `${value} B`
    : value < 1024 ** 2
      ? `${(value / 1024).toFixed(1)} KB`
      : value < 1024 ** 3
        ? `${(value / 1024 ** 2).toFixed(2)} MB`
        : `${(value / 1024 ** 3).toFixed(2)} GB`;
const Metric = ({ label, value }: { label: string; value: string }) => (
  <div>
    <small>{label}</small>
    <strong>{value}</strong>
  </div>
);

export default function App() {
  const [file, setFile] = useState<File>();
  const [algorithm, setAlgorithm] = useState<Algorithm>("lzw");
  const [algorithms, setAlgorithms] = useState<AlgorithmInfo[]>([]);
  const [transfer, setTransfer] = useState<Transfer>();
  const [operation, setOperation] = useState<CompressionOperation>();
  const [network, setNetwork] = useState<{ url: string }>();
  const [share, setShare] = useState("");
  const [status, setStatus] = useState("Ready");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<Transfer[]>([]);
  const [qr, setQr] = useState("");
  const [shareQr, setShareQr] = useState("");
  const [verified, setVerified] = useState(false);

  useEffect(() => {
    Promise.all([api.algorithms(), api.networkInfo()])
      .then(([items, info]) => {
        setAlgorithms(items);
        setNetwork(info);
      })
      .catch(() => setError("Unable to connect to the FileComp server."));
  }, []);
  useEffect(() => {
    if (location.pathname.startsWith("/share/"))
      api
        .transfer(location.pathname.split("/").pop()!)
        .then(setTransfer)
        .catch(() => setError("Transfer not found."));
  }, []);
  const selectedInfo = useMemo(
    () => algorithms.find((item) => item.id === algorithm),
    [algorithms, algorithm],
  );
  const choose = (candidate?: File) => {
    setFile(candidate);
    setTransfer(undefined);
    setOperation(undefined);
    setError("");
  };

  const compress = async () => {
    if (!file) {
      setError("Choose a file first.");
      return;
    }
    setBusy(true);
    setError("");
    setStatus("Uploading...");
    try {
      let current = await api.startCompression(file, algorithm);
      setOperation(current);
      setStatus("Compressing...");
      while (!["completed", "cancelled", "error"].includes(current.status)) {
        await new Promise((resolve) => setTimeout(resolve, 400));
        current = await api.compressionStatus(current.operationId);
        setOperation(current);
      }
      if (current.status === "cancelled") {
        setStatus("Compression cancelled");
        return;
      }
      if (current.status === "error" || !current.transferId)
        throw new Error(current.error ?? "Compression failed");
      const result = await api.transfer(current.transferId);
      setTransfer(result);
      setShare(`${window.location.origin}/share/${result.id}`);
      setHistory((items) =>
        [result, ...items.filter((item) => item.id !== result.id)].slice(0, 5),
      );
      setStatus(
        result.algorithm === "stored"
          ? "Complete; no size benefit"
          : "Transfer ready",
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Compression failed");
      setStatus("Error");
    } finally {
      setBusy(false);
    }
  };
  const cancel = async () => {
    if (
      operation &&
      !["completed", "cancelled", "error"].includes(operation.status)
    ) {
      setOperation(await api.cancelCompression(operation.operationId));
      setStatus("Cancelling...");
    }
  };
  const download = async (id: string) => {
    setBusy(true);
    setVerified(false);
    try {
      const response = await api.download(id);
      if (!response.ok) throw new Error();
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = transfer?.filename ?? "file";
      link.click();
      URL.revokeObjectURL(url);
      const result = await api.verify(id);
      setVerified(result.verified);
      setStatus("Downloaded and SHA-256 verified");
    } catch {
      setError("Download or verification failed.");
      setStatus("Error");
    } finally {
      setBusy(false);
    }
  };
  const copy = async (value: string) => {
    await navigator.clipboard?.writeText(value);
    setStatus("Copied");
  };
  const qrCode = async (value: string) =>
    setQr(await QRCode.toDataURL(value, { width: 220, margin: 1 }));
  const shareQrCode = async () => {
    if (share)
      setShareQr(await QRCode.toDataURL(share, { width: 220, margin: 1 }));
  };
  if (location.pathname.startsWith("/share/")) {
    const id = location.pathname.split("/").pop()!;
    return (
      <main className="share-page">
        <div className="share-card">
          <span className="eyebrow">INCOMING FILE</span>
          <h1>{transfer?.filename ?? "Loading transfer..."}</h1>
          {transfer && (
            <>
              <p className="lead">
                {bytes(transfer.original_size)} to{" "}
                {bytes(transfer.compressed_size)}
              </p>
              <button
                className="primary"
                disabled={busy}
                onClick={() => download(id)}
              >
                Download and verify
              </button>
              <p className={`status ${verified ? "success" : ""}`}>
                {verified ? "File received and verified" : status}
              </p>
              {error && <p className="error">{error}</p>}
            </>
          )}
        </div>
      </main>
    );
  }

  return (
    <main>
      <header>
        <div className="brand">
          <span className="mark">FC</span>
          <span>FileComp</span>
        </div>
        <div className="online">
          <i /> {network ? "Online" : "Connecting"}
        </div>
      </header>
      <section className="hero">
        <div>
          <span className="eyebrow">LOCAL FILE TRANSFER</span>
          <h1>
            Compress.
            <br />
            <em>Send.</em> Verify.
          </h1>
          <p>
            Move files across your network with transparent compression and
            integrity checks.
          </p>
        </div>
        <div className="address">
          <small>YOUR LOCAL ADDRESS</small>
          <strong>{network?.url ?? "Loading..."}</strong>
          <div>
            <button onClick={() => network && copy(network.url)}>
              Copy address
            </button>
            <button onClick={() => network && qrCode(network.url)}>
              Show QR
            </button>
          </div>
          {qr && <img className="qr" src={qr} alt="QR code" />}
        </div>
      </section>
      <section className="workspace">
        <div className="panel send-panel">
          <span className="eyebrow">SEND A FILE</span>
          <h2>Prepare a transfer</h2>
          <label
            className="dropzone"
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              event.preventDefault();
              choose(event.dataTransfer.files[0]);
            }}
          >
            <input
              type="file"
              onChange={(event) => choose(event.target.files?.[0])}
            />
            <span className="upload-icon">Upload</span>
            <b>{file ? file.name : "Drop a file here"}</b>
            <small>
              {file ? bytes(file.size) : "or choose from your device"}
            </small>
          </label>
          <h3>Compression algorithm</h3>
          <div className="algorithms">
            {algorithms
              .filter((item) => item.id !== "stored")
              .map((item) => (
                <button
                  key={item.id}
                  className={algorithm === item.id ? "selected" : ""}
                  onClick={() => setAlgorithm(item.id)}
                >
                  <b>{item.label}</b>
                  <small>{item.description}</small>
                </button>
              ))}
          </div>
          {selectedInfo && <p className="hint">{selectedInfo.description}</p>}
          {operation && busy && (
            <div className="operation" aria-live="polite">
              <strong>{operation.filename}</strong>
              <progress
                value={operation.processedBytes}
                max={Math.max(operation.totalBytes, 1)}
              />
              <span>{operation.progress.toFixed(1)}%</span>
              <small>
                {bytes(operation.processedBytes)} /{" "}
                {bytes(operation.totalBytes)}; remaining{" "}
                {bytes(
                  Math.max(operation.totalBytes - operation.processedBytes, 0),
                )}
              </small>
              <small>
                {operation.speed
                  ? `${bytes(operation.speed)}/s`
                  : "Calculating speed..."}
                ; ETA{" "}
                {operation.eta === null
                  ? "Calculating time..."
                  : `${operation.eta.toFixed(1)} sec`}
                ; {operation.status}
              </small>
              <button type="button" onClick={cancel}>
                Cancel
              </button>
            </div>
          )}
          <button
            className="primary full"
            disabled={!file || busy}
            onClick={compress}
          >
            {busy ? (
              <span className="compress-button-content">
                <span>Compressing {operation?.progress.toFixed(0) ?? 0}%</span>
                <span className="compress-button-track">
                  <span
                    className="compress-button-fill"
                    style={{ width: `${operation?.progress ?? 0}%` }}
                  />
                </span>
              </span>
            ) : (
              "Compress and create transfer"
            )}
          </button>
          {error && <p className="error">{error}</p>}
        </div>
        <aside>
          <div className="panel stats-panel">
            <span className="eyebrow">COMPRESSION PREVIEW</span>
            <h2>{transfer ? "Ready to share" : "Your result"}</h2>
            {transfer ? (
              <div className="metrics">
                <Metric
                  label="Original"
                  value={bytes(transfer.original_size)}
                />
                <Metric
                  label="Compressed"
                  value={bytes(transfer.compressed_size)}
                />
                <Metric
                  label="Ratio"
                  value={`${transfer.compression_ratio.toFixed(2)}x`}
                />
                <Metric
                  label="Saved"
                  value={`${transfer.space_saving.toFixed(1)}%`}
                />
              </div>
            ) : (
              <div className="empty-metrics">
                Select a file to see backend measurements.
              </div>
            )}
            {transfer && (
              <div className="share-link">
                <small>SHARE THIS FILE</small>
                <strong>{share}</strong>
                <button onClick={() => copy(share)}>Copy link</button>
                <button onClick={shareQrCode}>Create QR code</button>
                <button onClick={() => download(transfer.id)} disabled={busy}>
                  Download and verify
                </button>
                {shareQr && <img className="qr share-qr" src={shareQr} alt="QR code for file sharing" />}
                {verified && <p className="status success">SHA-256 verified</p>}
              </div>
            )}
          </div>
          <div className="panel history">
            <span className="eyebrow">RECENT TRANSFERS</span>
            <h2>History</h2>
            {history.length ? (
              history.map((item) => (
                <div className="history-row" key={item.id}>
                  <b>{item.filename}</b>
                  <small>
                    {item.algorithm.toUpperCase()};{" "}
                    {bytes(item.compressed_size)}
                  </small>
                </div>
              ))
            ) : (
              <div className="empty-metrics">No transfers yet.</div>
            )}
          </div>
        </aside>
      </section>
      <footer>
        <span>FileComp; Local-first file exchange</span>
        <span>{status}</span>
        <a
          href="https://www.keormel.xyz/"
          target="_blank"
          rel="noreferrer"
          style={{ marginLeft: "auto", color: "inherit", textDecoration: "none" }}
        >
          by keormel
        </a>
      </footer>
    </main>
  );
}
