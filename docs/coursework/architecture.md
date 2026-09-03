# Архитектура

Система разделена на независимые слои: algorithms реализует кодеки, container хранит FCMP, service выполняет файловые операции, transfer отвечает за TCP relay, а GUI/CLI являются адаптерами интерфейса.

```mermaid
flowchart LR
    UserA[User A] --> ClientA[Client]
    ClientA --> Service[Compression service]
    Service --> Algorithms[Huffman / LZW / RLE]
    Service --> FCMP[FCMP container]
    FCMP --> Server[TCP relay server]
    Server --> ClientB[Client]
    ClientB --> Integrity[SHA-256 verification]
    Integrity --> UserB[User B]
```

Core-слой не зависит от GUI и сети, поэтому алгоритмы тестируются отдельно.
