from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path


def generate(csv_path: Path, output_dir: Path) -> None:
    import matplotlib.pyplot as plt

    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics = [
        ("sizes", "original_size", "compressed_size", "Original vs compressed size", "bytes"),
        ("ratio", "compression_ratio", None, "Compression ratio", "ratio"),
        ("saving", "space_saving_percent", None, "Space saving", "%"),
        ("compression_time", "compression_time", None, "Compression time", "seconds"),
        ("decompression_time", "decompression_time", None, "Decompression time", "seconds"),
    ]
    for filename, primary, secondary, title, ylabel in metrics:
        groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for row in rows:
            groups[row["file"]][row["algorithm"]].append(float(row[primary]))
        labels = list(groups)
        algorithms = sorted({row["algorithm"] for row in rows})
        width = 0.8 / max(len(algorithms), 1)
        figure, axis = plt.subplots(figsize=(11, 6))
        for index, algorithm in enumerate(algorithms):
            values = [groups[label][algorithm][0] for label in labels]
            axis.bar([position + index * width for position in range(len(labels))], values, width, label=algorithm)
            if secondary is not None:
                secondary_groups: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
                for row in rows:
                    secondary_groups[row["file"]][row["algorithm"]].append(float(row[secondary]))
                axis.bar([position + index * width + width / 2 for position in range(len(labels))], [secondary_groups[label][algorithm][0] for label in labels], width, alpha=0.45, label=f"{algorithm} original")
        axis.set_title(title)
        axis.set_ylabel(ylabel)
        axis.set_xticks([position + width for position in range(len(labels))], labels, rotation=45, ha="right")
        axis.legend()
        figure.tight_layout()
        figure.savefig(output_dir / f"{filename}.png", dpi=180)
        plt.close(figure)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate plots from benchmark CSV")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results/plots"))
    arguments = parser.parse_args()
    generate(arguments.csv, arguments.output)
