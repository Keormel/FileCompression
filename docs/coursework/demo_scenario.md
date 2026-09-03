# Сценарий демонстрации (3-5 минут)

1. Запустить `.venv\Scripts\python.exe -m filecompression.server_cli --port 8765`.
2. Открыть два GUI-клиента; в первом указать `user_a`, во втором `user_b`.
3. В обоих окнах нажать `Connect`.
4. В окне User A выбрать `tests/fixtures/text_large.txt` и алгоритм `lzw`.
5. Нажать `Compress` и показать original size, container size, ratio и saving.
6. Нажать `Send`, указав `user_b`; показать подтверждение ACK.
7. В окне User B нажать `Receive`; показать имя, алгоритм и размер FCMP.
8. Нажать `Decompress`, выбрать выходной файл и показать сообщение `SHA-256 verified`.
9. При необходимости повторить пункты 4-8 для Huffman и RLE.
