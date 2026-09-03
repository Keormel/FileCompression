# Контейнер FCMP

Фиксированный заголовок имеет размер 62 байта и записывается в big-endian: magic `FCMP` (4), version (1), algorithm id (1), flags (2), original size (8), filename length (2), SHA-256 (32), metadata length (4), payload length (8).

После заголовка расположены UTF-8 filename, algorithm metadata и compressed payload. Huffman хранит frequencies и bit_count, LZW хранит code_width, RLE не хранит параметров. При чтении проверяются magic/version/flags, длины, лимиты, имя, размер восстановленных данных и SHA-256.
