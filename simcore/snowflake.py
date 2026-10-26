from __future__ import annotations

import threading
import time

# 63-bit Snowflake: 41 bits ms timestamp since custom epoch | 10 bits worker | 12 bits sequence.
EPOCH_MS = 1704067200000  # 2024-01-01T00:00:00Z


class Snowflake:
    def __init__(self, worker_id: int = 1):
        self.worker_id = worker_id & 0x3FF
        self.seq = 0
        self.last_ms = -1
        self.lock = threading.Lock()

    def next_id(self) -> int:
        with self.lock:
            now = int(time.time() * 1000)
            if now == self.last_ms:
                self.seq = (self.seq + 1) & 0xFFF
                if self.seq == 0:
                    while now <= self.last_ms:
                        now = int(time.time() * 1000)
            else:
                self.seq = 0
            self.last_ms = now
            return ((now - EPOCH_MS) << 22) | (self.worker_id << 12) | self.seq

    def next_str(self) -> str:
        return str(self.next_id())
