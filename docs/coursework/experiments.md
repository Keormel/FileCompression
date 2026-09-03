# Экспериментальное исследование

Запуск выполняется так:

```text
.venv\Scripts\python.exe benchmarks/compression_benchmark.py --generate-fixtures benchmark-results/fixtures --output benchmark-results/benchmark_results.csv
.venv\Scripts\python.exe benchmarks/generate_plots.py benchmark-results/benchmark_results.csv
```

Все численные значения берутся из `benchmark_results.csv` и `benchmark_results.json`; вручную значения не редактируются. Измеряются размер полного FCMP-контейнера, payload, коэффициент `original / compressed`, экономия, время и throughput. Peak memory измеряется через `tracemalloc`.

Теоретическая экономия передачи вычисляется как `original_size - compressed_size`. При известной скорости канала `v` разница времени равна `(original_size - compressed_size) / v`; это расчетная величина, не измерение сети.

Выводы следует делать отдельно для каждого типа файла. Отрицательная экономия является корректным результатом для данных, где служебные данные алгоритма и контейнера превышают выигрыш.
