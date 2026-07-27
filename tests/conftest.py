"""Shared test helpers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def login_banner() -> bytes:
    """A real capture of the BatMUD login banner, including IAC GA."""
    return (FIXTURES / "login_banner.bin").read_bytes()


def split_every_way(
    data: bytes, *, sizes: tuple[int, ...] = (1, 2, 3, 5, 7, 13, 64)
) -> Iterator[list[bytes]]:
    """Yield the same bytes chopped into chunks of several fixed sizes.

    Combined with :func:`split_at`, this covers the failure mode the previous
    client had: a pattern that only matches when it lands inside one read.
    """
    for size in sizes:
        yield [data[index : index + size] for index in range(0, len(data), size)]
    yield [data]


def split_at(data: bytes, index: int) -> list[bytes]:
    return [data[:index], data[index:]]
