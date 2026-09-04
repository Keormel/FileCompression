from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from filecompression.container import pack, unpack
from filecompression.service import CompressionStats
from filecompression.transfer import TransferClient

try:
    from PySide6.QtCore import QObject, QThread, Signal
    from PySide6.QtWidgets import QApplication, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow, QComboBox, QProgressBar, QPushButton, QSpinBox, QVBoxLayout, QWidget, QMessageBox
except ImportError:
    QObject = QThread = Signal = None


if QObject is not None:
    class BackendWorker(QObject):
        connected = Signal(str)
        compressed = Signal(object)
        received = Signal(str)
        completed = Signal(str)
        failed = Signal(str)

        def __init__(self) -> None:
            super().__init__()
            self.loop: asyncio.AbstractEventLoop | None = None
            self.client: TransferClient | None = None
            self.container: bytes | None = None

        def run(self) -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        def submit(self, command: str, *args: object) -> None:
            if self.loop is None:
                self.failed.emit("Worker is not ready")
                return
            asyncio.run_coroutine_threadsafe(self._execute(command, args), self.loop)

        async def _execute(self, command: str, args: tuple[object, ...]) -> None:
            try:
                if command == "connect":
                    await self._connect(str(args[0]), int(args[1]), str(args[2]))
                elif command == "compress":
                    await self._compress(Path(args[0]), str(args[1]))
                elif command == "send":
                    await self._send(str(args[0]))
                elif command == "receive":
                    await self._receive()
                elif command == "decompress":
                    await self._decompress(Path(args[0]))
            except Exception as error:
                self.failed.emit(str(error))

        async def _connect(self, host: str, port: int, user: str) -> None:
            if self.client is not None:
                await self.client.close()
            self.client = TransferClient(host, port, user)
            await self.client.connect()
            self.connected.emit(user)

        async def _compress(self, source: Path, algorithm: str) -> None:
            from filecompression.algorithms.base import MAX_DECOMPRESSED_SIZE
            if source.stat().st_size > MAX_DECOMPRESSED_SIZE:
                raise ValueError("File exceeds the 256 MiB safety limit")
            data = source.read_bytes()
            started = asyncio.get_running_loop().time()
            self.container = pack(data, source.name, algorithm)
            compression_time = asyncio.get_running_loop().time() - started
            started = asyncio.get_running_loop().time()
            unpack(self.container).decompress()
            decompression_time = asyncio.get_running_loop().time() - started
            self.compressed.emit(CompressionStats(len(data), len(self.container), compression_time, decompression_time))

        async def _send(self, recipient: str) -> None:
            if self.client is None or self.container is None:
                raise RuntimeError("Connect and compress a file first")
            await self.client.send(recipient, self.container)
            self.completed.emit("File sent; server ACK received")

        async def _receive(self) -> None:
            if self.client is None:
                raise RuntimeError("Connect to the server first")
            self.container = await self.client.receive()
            incoming = unpack(self.container)
            self.received.emit(f"{incoming.filename} | {incoming.algorithm} | {len(self.container)} bytes")

        async def _decompress(self, destination: Path) -> None:
            if self.container is None:
                raise RuntimeError("Receive a file first")
            data = unpack(self.container).decompress()
            destination.write_bytes(data)
            self.completed.emit(f"File decompressed; SHA-256 verified: {destination}")

        def close(self) -> None:
            if self.loop is not None:
                future = asyncio.run_coroutine_threadsafe(self._close_client(), self.loop)
                future.result(timeout=5)
                self.loop.call_soon_threadsafe(self.loop.stop)

        async def _close_client(self) -> None:
            if self.client is not None:
                await self.client.close()


def main() -> None:
    if QObject is None:
        raise SystemExit("Install the GUI extra with: python -m pip install -e '.[gui]'")

    class Window(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("FileCompression")
            self.resize(620, 520)
            self.selected: Path | None = None
            self.worker = BackendWorker()
            self.thread = QThread(self)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.run)
            self.worker.connected.connect(lambda user: self.operation_done(f"Connected as {user}"))
            self.worker.compressed.connect(self.show_stats)
            self.worker.received.connect(self.add_incoming)
            self.worker.completed.connect(self.operation_done)
            self.worker.failed.connect(self.operation_failed)
            self.thread.start()

            self.file_label = QLabel("No file selected")
            self.algorithm = QComboBox()
            self.algorithm.addItems(["huffman", "lzw", "rle"])
            self.host = QLineEdit("127.0.0.1")
            self.port = QSpinBox()
            self.port.setRange(1, 65535)
            self.port.setValue(8765)
            self.user = QLineEdit("user_a")
            self.recipient = QLineEdit("user_b")
            self.original = QLabel("-")
            self.compressed = QLabel("-")
            self.ratio = QLabel("-")
            self.saving = QLabel("-")
            self.compression_time = QLabel("-")
            self.decompression_time = QLabel("-")
            self.status = QLabel("Disconnected")
            self.incoming = QListWidget()
            self.progress = QProgressBar()
            self.progress.setRange(0, 1)
            select = QPushButton("Select file")
            select.clicked.connect(self.choose_file)
            connect = QPushButton("Connect")
            connect.clicked.connect(self.connect)
            compress = QPushButton("Compress")
            compress.clicked.connect(self.compress_file)
            send = QPushButton("Send")
            send.clicked.connect(lambda: self.submit("send", self.recipient.text()))
            receive = QPushButton("Receive")
            receive.clicked.connect(lambda: self.submit("receive"))
            decompress = QPushButton("Decompress")
            decompress.clicked.connect(self.decompress_file)
            form = QFormLayout()
            form.addRow("File", self.file_label)
            form.addRow("Algorithm", self.algorithm)
            form.addRow("Host", self.host)
            form.addRow("Port", self.port)
            form.addRow("User", self.user)
            form.addRow("Recipient", self.recipient)
            stats = QFormLayout()
            stats.addRow("Original size", self.original)
            stats.addRow("Compressed size", self.compressed)
            stats.addRow("Ratio", self.ratio)
            stats.addRow("Space saving", self.saving)
            stats.addRow("Compression time", self.compression_time)
            stats.addRow("Decompression time", self.decompression_time)
            actions = QHBoxLayout()
            for button in (select, connect, compress, send, receive, decompress):
                actions.addWidget(button)
            layout = QVBoxLayout()
            layout.addLayout(form)
            layout.addLayout(actions)
            layout.addLayout(stats)
            layout.addWidget(QLabel("Incoming files"))
            layout.addWidget(self.incoming)
            layout.addWidget(self.progress)
            layout.addWidget(self.status)
            central = QWidget()
            central.setLayout(layout)
            self.setCentralWidget(central)

        def choose_file(self) -> None:
            selected, _ = QFileDialog.getOpenFileName(self, "Choose file")
            if selected:
                self.selected = Path(selected)
                self.file_label.setText(str(self.selected))

        def connect(self) -> None:
            self.submit("connect", self.host.text(), self.port.value(), self.user.text())

        def compress_file(self) -> None:
            if self.selected is None:
                self.operation_failed("Choose a file first")
                return
            self.submit("compress", self.selected, self.algorithm.currentText())

        def decompress_file(self) -> None:
            destination, _ = QFileDialog.getSaveFileName(self, "Save restored file")
            if destination:
                self.submit("decompress", Path(destination))

        def submit(self, command: str, *args: object) -> None:
            self.progress.setRange(0, 0)
            self.worker.submit(command, *args)

        def show_stats(self, stats: CompressionStats) -> None:
            self.original.setText(str(stats.original_size))
            self.compressed.setText(str(stats.compressed_size))
            self.ratio.setText(f"{stats.ratio:.3f}")
            self.saving.setText(f"{stats.saving_percent:.2f}%")
            self.compression_time.setText(f"{stats.compression_time:.6f}s")
            self.decompression_time.setText(f"{stats.decompression_time:.6f}s")
            self.operation_done("Compression complete")

        def add_incoming(self, description: str) -> None:
            self.incoming.addItem(description)
            self.operation_done("File received and FCMP validated")

        def operation_done(self, message: str) -> None:
            self.progress.setRange(0, 1)
            self.status.setText("OK: " + message)

        def operation_failed(self, message: str) -> None:
            self.progress.setRange(0, 1)
            self.status.setText("Error: " + message)
            QMessageBox.warning(self, "FileCompression", message)

        def closeEvent(self, event: object) -> None:
            self.worker.close()
            self.thread.quit()
            self.thread.wait(5000)
            super().closeEvent(event)

    application = QApplication(sys.argv)
    window = Window()
    window.show()
    sys.exit(application.exec())


if __name__ == "__main__":
    main()
