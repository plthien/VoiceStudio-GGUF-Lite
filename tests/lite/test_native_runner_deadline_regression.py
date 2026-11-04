"""Behavioral check: an expired native deadline cannot leak a coroutine."""
import asyncio
import warnings
from unittest.mock import patch
import pytest

from lite import native_runner


def test_expired_legacy_wait_creates_no_communicate_coroutine():
    calls = []
    async def never_started(*_):
        calls.append("called")
    def expired():
        raise native_runner.NativeTimeout("expired")
    async def run():
        with patch.object(native_runner, "_bounded_communicate", never_started):
            with pytest.raises(native_runner.NativeTimeout):
                await native_runner._communicate_before_deadline(object(), b"hello", expired)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        asyncio.run(run())
    assert calls == []
