import os
import threading

from . import prom_client

_COUNTER_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".article_counter")
_lock = threading.Lock()


def next_article() -> str:
    """Returns a unique, human-readable 'vNNNN' article/SKU code.

    Used to be prom_client.count_products() + 1, but that queries Prom.ua's
    live catalog - and Prom.ua's import is async, so it doesn't reflect a
    just-submitted product right away. Two imports run seconds apart could
    both see the same count and generate the same article, and since
    Ідентифікатор_товару/Код_товару is Prom.ua's merge key, the second
    import silently overwrites the first instead of creating a second
    product (confirmed: rapid test imports today all collided into one
    product). A local, synchronous counter can't race with anything.
    """
    with _lock:
        current = _read_counter()
        next_value = current + 1
        _write_counter(next_value)
    return f"v{next_value:04d}"


def _read_counter() -> int:
    if os.path.exists(_COUNTER_FILE):
        with open(_COUNTER_FILE) as f:
            return int(f.read().strip())
    # First run ever: seed from the highest "vNNNN" article already used in
    # the account (not count_products() - the two can differ if Prom.ua's
    # async import hasn't fully reflected a recent product yet).
    return prom_client.find_max_article_number()


def _write_counter(value: int) -> None:
    with open(_COUNTER_FILE, "w") as f:
        f.write(str(value))
