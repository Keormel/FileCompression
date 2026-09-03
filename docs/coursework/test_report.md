# Отчет о тестировании

Команда проверки:

```text
.venv\Scripts\python.exe -m pytest -v
```

Последний фактический прогон: **37 passed, 0 failed, 0 errors** на Python 3.14.3 в `.venv`. Тесты покрывают round-trip всех кодеков, пустые и бинарные данные, поврежденные Huffman/LZW/RLE потоки, FCMP header/truncation/trailing bytes/checksum/path traversal и TCP передачу между двумя пользователями.

Покрытие последнего запуска: **69%** (`589` statements, `181` missed). Непокрытые строки в основном относятся к интерактивным CLI/GUI entrypoints и негативным ветвям сетевого сервера.

Фактические значения Passed/Failed/Errors должны обновляться после запуска pytest и не должны вписываться вручную. Для покрытия используется дополнительная зависимость `pytest-cov`:

```text
.venv\Scripts\python.exe -m pytest --cov=filecompression --cov-report=term-missing
```

Benchmark-результаты находятся в `benchmark-results/benchmark_results.csv`, `benchmark_results.json` и `benchmark_results.md`. Графики находятся в `benchmark-results/plots/`.
