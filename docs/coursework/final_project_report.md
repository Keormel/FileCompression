# Итоговый отчет проекта

## 1. Цель
Разработать систему без потерь для сжатия и передачи файлов между двумя пользователями и сравнить Huffman, LZW и RLE.

## 2. Архитектура
GUI и CLI используют service layer; service вызывает алгоритм и FCMP; transfer передает FCMP через asyncio TCP relay.

## 3. Реализация
Алгоритмы работают с байтами. FCMP хранит magic/version, algorithm id, размер, имя, SHA-256, metadata и payload. Получатель валидирует контейнер, распаковывает данные и проверяет hash.

## 4. Тестирование
Фактический последний запуск: 40 passed, 0 failed, 0 errors; backend coverage 83%. Проверены round-trip, поврежденные потоки, FCMP, checksum и TCP negative cases. Команда: `.venv\Scripts\python.exe -m pytest -v`.

## 5. Эксперимент
Benchmark измеряет full container size, payload size, ratio, saving, время, throughput и peak memory. Реальные результаты находятся в `benchmark-results/benchmark_results.csv` и `.json`; графики находятся в `benchmark-results/plots/`. Выводы делаются по данным запуска, а не по заранее заданным числам.

## 6. Ограничения и развитие
Проект учебный: отсутствуют TLS, аутентификация, persistent inbox и возобновление передачи. GUI поддерживает полный sender/receiver workflow через worker-thread. Automated Qt import проверен, интерактивный desktop-тест в текущей среде недоступен.
