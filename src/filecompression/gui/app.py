from __future__ import annotations

import sys
from pathlib import Path

from filecompression.service import compress_file, decompress_file


def main() -> None:
    try:
        from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QMainWindow, QMessageBox, QPushButton, QComboBox, QVBoxLayout, QWidget
    except ImportError as error:
        raise SystemExit("Install the GUI extra with: python -m pip install -e '.[gui]'") from error

    class Window(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("FileCompression")
            self.resize(440, 280)
            self.selected: Path | None = None
            self.file_label = QLabel("No file selected")
            self.algorithm = QComboBox()
            self.algorithm.addItems(["huffman", "lzw", "rle"])
            select = QPushButton("Choose file")
            select.clicked.connect(self.choose_file)
            compress = QPushButton("Compress")
            compress.clicked.connect(self.compress)
            layout = QVBoxLayout()
            for widget in (self.file_label, select, self.algorithm, compress):
                layout.addWidget(widget)
            container = QWidget()
            container.setLayout(layout)
            self.setCentralWidget(container)

        def choose_file(self) -> None:
            selected, _ = QFileDialog.getOpenFileName(self, "Choose file")
            if selected:
                self.selected = Path(selected)
                self.file_label.setText(str(self.selected))

        def compress(self) -> None:
            if self.selected is None:
                QMessageBox.warning(self, "FileCompression", "Choose a file first")
                return
            destination, _ = QFileDialog.getSaveFileName(self, "Save container", self.selected.name + ".fcmp")
            if destination:
                stats = compress_file(self.selected, Path(destination), self.algorithm.currentText())
                QMessageBox.information(self, "Compression complete", f"Ratio: {stats.ratio:.3f}\nSaving: {stats.saving_percent:.2f}%")

    application = QApplication(sys.argv)
    window = Window()
    window.show()
    sys.exit(application.exec())


if __name__ == "__main__":
    main()
