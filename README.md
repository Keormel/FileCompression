# FileCompression

Учебная система сжатия и передачи файлов между двумя пользователями. Проект реализует три алгоритма сжатия без потерь самостоятельно: Huffman, LZW и RLE.

## Реализовано

- бинарное сжатие и восстановление файлов;
- собственный контейнер `FCMP` с версией, алгоритмом, размером, именем и SHA-256;
- TCP relay-сервер и два клиента с очередью входящих файлов;
- CLI для сжатия и распаковки;
- unit/integration-тесты и CSV benchmark.

## Требования и установка

Нужен Python 3.12 или новее. Установка пакета из корня проекта:

```text
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## CLI

```text
.venv\Scripts\python.exe -m filecompression.cli compress input.bin output.fcmp --algorithm huffman
.venv\Scripts\python.exe -m filecompression.cli decompress output.fcmp restored.bin
```

CLI показывает размеры, коэффициент сжатия, экономию и время обработки. Размером передачи считается полный контейнер, включая заголовок и метаданные. Для случайных или уже сжатых данных размер может увеличиться.

## Передача

Сервер и клиентский API находятся в `src/filecompression/transfer`. Сервер поддерживает именованных пользователей, принимает контейнер от отправителя и помещает его во входящую очередь получателя. Используется TCP с 4-байтным big-endian размером каждого фрейма.

## Web interface

Соберите React UI и запустите FastAPI Web server:

```text
cd frontend
npm install
npm run build
cd ..
.venv\Scripts\python.exe -m pip install -e ".[web]"
.venv\Scripts\filecompression-web.exe --host 0.0.0.0 --port 8000
```

Откройте `http://localhost:8000` на первом компьютере. Для второго устройства используйте URL из `GET /api/network-info`, например `http://192.168.1.15:8000`. Web UI создаёт share links, выполняет асинхронное сжатие с реальным progress и скачивает восстановленный исходный файл после FCMP/SHA-256 проверки. Transfer-хранилище in-memory и истекает через 30 минут.

Запуск сервера после установки пакета:

```text
.venv\Scripts\filecompression-server.exe --host 127.0.0.1 --port 8765
```

Минимальный графический клиент запускается так:

```text
.venv\Scripts\python.exe -m pip install -e ".[gui]"
.venv\Scripts\python.exe -m filecompression.gui.app
```

Сетевой транспорт предназначен для учебной локальной демонстрации: аутентификация и шифрование пока не входят в область проекта.

## Benchmark и тесты

```text
.venv\Scripts\python.exe benchmarks/compression_benchmark.py data/sample.txt data/sample.bin
.venv\Scripts\python.exe benchmarks/compression_benchmark.py --generate-fixtures benchmark-results/fixtures --output benchmark-results/benchmark_results.csv
.venv\Scripts\python.exe benchmarks/generate_plots.py benchmark-results/benchmark_results.csv
.venv\Scripts\python.exe -m pytest
```

Benchmark формирует CSV с фактическими размерами, коэффициентами и временами для каждого алгоритма. Численные результаты не должны добавляться в документацию вручную.

## Архитектура

```text
src/filecompression/
├── algorithms/   Huffman, LZW, RLE и общий интерфейс
├── container/    бинарный контейнер FCMP и checksum
├── transfer/     TCP-протокол, сервер и клиент
├── service.py    файловые операции и статистика
└── cli.py        консольный интерфейс
tests/            round-trip и integration-тесты
benchmarks/       воспроизводимые измерения
```

## Формат контейнера

Заголовок использует фиксированный big-endian формат: `FCMP`, версия, идентификатор алгоритма, flags, исходный размер, длина имени, SHA-256, длина метаданных и длина payload. После заголовка последовательно находятся UTF-8 имя файла, метаданные алгоритма и сжатые байты. Huffman хранит частотную таблицу; LZW хранит ширину кода; RLE не требует дополнительных параметров.

При распаковке проверяются версия, длины, имя файла, размер восстановленных данных и SHA-256. Ошибки повреждения контейнера не принимаются за успешное восстановление.

## Ограничения текущего этапа

GUI поддерживает выбор файла и алгоритма, сжатие, статистику, подключение, отправку, получение, распаковку и проверку SHA-256. Операции выполняются в отдельном worker-thread; progress bar использует indeterminate-состояние для операций, где точный прогресс не передается backend. Для PNG, JPEG и PDF benchmark использует воспроизводимые fixtures из `tests/fixtures`; репозиторий не содержит copyrighted material.

Материалы защиты находятся в `docs/coursework/`: сценарий демонстрации, вопросы преподавателя и итоговый отчет. Исходники Mermaid-диаграмм находятся в `docs/diagrams/`.

## Production deployment

Production-параметры задаются через переменные окружения; пример находится в `.env.example`. Не коммитьте `.env` и секреты. По умолчанию публичные TCP-сессии отключены (`FILECOMP_ENABLE_SESSIONS=false`), CORS ограничен локальными origins, uploads ограничены 256 MiB, а in-memory transfers ограничены количеством и общим размером.

Для production задайте `FILECOMP_STORAGE_DIR` на persistent volume и `FILECOMP_API_KEY` на случайный секрет. При заданном API key все `/api/*`, кроме health, требуют заголовок `X-API-Key`. Встроенный rate limiter ограничивает запросы по IP; для нескольких реплик используйте rate limiting на reverse proxy или Redis.

Сборка Docker:

```text
copy .env.example .env
docker compose up --build -d
```

Проверка:

```text
curl http://localhost:8000/api/health
```

В production рекомендуется поставить TLS reverse proxy перед приложением, задать явный `FILECOMP_ALLOWED_ORIGINS`, ограничить сетевой доступ к порту и вынести transfer storage в persistent object storage. Текущий storage in-memory подходит для одной демонстрационной реплики: данные исчезают после рестарта.

Upload теперь читается чанками во временный persistent storage, а готовые FCMP-контейнеры сохраняются на диске и восстанавливаются после рестарта. Однако текущие Huffman/LZW/RLE-кодеки всё ещё принимают `bytes` во время самой компрессии; для файлов больше safety limit нужен отдельный streaming codec/container refactor.

CI выполняет Python tests/compile, frontend production build и Docker build через `.github/workflows/ci.yml`.