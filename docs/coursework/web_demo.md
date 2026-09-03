# Web UI демонстрация

1. В каталоге `frontend` выполнить `npm install` и `npm run build`.
2. Запустить `.venv-1\Scripts\filecompression-web.exe --host 0.0.0.0 --port 8000`.
3. Открыть `http://localhost:8000` на компьютере A.
4. Показать адрес из блока `YOUR LOCAL ADDRESS` и открыть его на компьютере B в той же LAN.
5. На User A выбрать `tests/fixtures/text_large.txt`, выбрать LZW и нажать `Compress & create transfer`.
6. Показать реальные размеры, ratio и saving, полученные от FastAPI.
7. Передать User B ссылку `/share/<id>` или QR-код.
8. На User B открыть share page, проверить имя, algorithm и срок действия.
9. Нажать `Download file`; браузер скачает FCMP-контейнер, затем API выполнит распаковку и SHA-256 verification.

Полный сценарий рассчитан на 3-5 минут. Хранилище transfer временное in-memory, срок действия ссылки 30 минут. Для публичной сети приложение не предназначено: TLS и authentication не реализованы.
