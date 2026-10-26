"""Per-operation cancellation check after shared outbound pacing."""
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class DeliveryCancelled(Exception):
    """No HTTP request has started for this attempt."""


_guard: ContextVar[Callable[[], Awaitable[bool]] | None] = ContextVar("delivery_guard", default=None)


@contextmanager
def send_guard(check: Callable[[], Awaitable[bool]] | None) -> Iterator[None]:
    token = _guard.set(check)
    try:
        yield
    finally:
        _guard.reset(token)


async def check_before_send() -> None:
    check = _guard.get()
    if check is not None and not await check():
        raise DeliveryCancelled
