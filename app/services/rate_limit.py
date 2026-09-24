"""Ограничение частоты запросов — один механизм на всё приложение (ТЗ, п. 2.4).

Скользящее окно в памяти процесса. Шлюз по ТЗ разворачивается одним процессом
(N-07), поэтому внешнее хранилище не нужно; при переходе на несколько
экземпляров заменяется реализация счётчика, а не места вызова.
"""

import threading
import time
from collections import deque


class RateLimiter:
    """Скользящее окно в 60 секунд на ключ. `limit <= 0` отключает проверку."""

    WINDOW_SECONDS = 60.0
    #: Ключей больше этого числа не храним — защита от роста памяти на переборе.
    MAX_KEYS = 10_000

    def __init__(self, limit_per_minute: int) -> None:
        self._limit = limit_per_minute
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def allow(self, key: str) -> bool:
        if not self.enabled:
            return True

        now = time.monotonic()
        cutoff = now - self.WINDOW_SECONDS
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self.MAX_KEYS:
                    self._evict(cutoff)
                hits = self._hits[key] = deque()

            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def _evict(self, cutoff: float) -> None:
        """Выбрасывает ключи, по которым в окне не осталось обращений."""
        stale = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
        for key in stale:
            del self._hits[key]
        if not stale:  # все ключи активны — освобождаем самый старый
            oldest = min(self._hits, key=lambda k: self._hits[k][-1])
            del self._hits[oldest]
