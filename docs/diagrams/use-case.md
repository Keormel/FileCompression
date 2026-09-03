```mermaid
flowchart LR
  A[User A] --> Select[Select file]
  A --> Compress[Compress]
  A --> Send[Send to User B]
  B[User B] --> Receive[Receive]
  B --> Decompress[Decompress]
  B --> Verify[Verify SHA-256]
```