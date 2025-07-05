import threading
import time
from typing import Optional

try:
    import tiktoken
except ImportError:  # pragma: no cover - tiktoken is optional at runtime
    tiktoken = None

_DEFAULT_ENCODING = None

if tiktoken is not None:
    try:
        _DEFAULT_ENCODING = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _DEFAULT_ENCODING = None


def count_tokens(text: str, model: Optional[str] = None) -> int:
    """Return the number of tokens for ``text`` using ``tiktoken``."""
    if tiktoken is None:
        return len(text.split())
    try:
        enc = tiktoken.encoding_for_model(model) if model else _DEFAULT_ENCODING
        if enc is None:
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return len(text.split())


class TokenBucket:
    """Simple token bucket rate limiter."""

    def __init__(self, tokens_per_minute: int):
        self.capacity = max(1, tokens_per_minute)
        self.tokens = float(self.capacity)
        self.rate = float(tokens_per_minute) / 60.0
        self.timestamp = time.time()
        self.lock = threading.Lock()

    def consume(self, amount: int):
        """Consume ``amount`` tokens, sleeping if necessary."""
        with self.lock:
            now = time.time()
            elapsed = now - self.timestamp
            self.timestamp = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if amount > self.tokens:
                wait_time = (amount - self.tokens) / self.rate
                self.tokens = 0
            else:
                wait_time = 0
                self.tokens -= amount
        if wait_time > 0:
            time.sleep(wait_time)
