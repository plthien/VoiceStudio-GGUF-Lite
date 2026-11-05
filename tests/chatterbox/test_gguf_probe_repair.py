"""Bounded Windows GGUF interlock: real loopback HTTP and deterministic refusal tests."""
from __future__ import annotations

import asyncio
import errno
import http.server
import json
import socket
import threading
import time
import urllib.error
from contextlib import contextmanager

import pytest

import lite.chatterbox_service as mod
from lite.chatterbox_config import ChatterboxSettings
from lite.chatterbox_service import TurboError, TurboService


def probe(monkeypatch, tmp_path, urlopen, *, timeout=9.0):
    service=TurboService(ChatterboxSettings(root=tmp_path))
    monkeypatch.setattr(mod.urllib.request,"urlopen",urlopen)
    return service,asyncio.run(service._assert_no_gguf(time.monotonic()+timeout))


def refused(*args, **kwargs):
    raise urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED,"Connection refused"))


def test_windows_delayed_tcp_refusal_after_2_seconds_is_safe_no_listener(monkeypatch,tmp_path):
    calls=[]
    def delayed(req, timeout):
        calls.append(timeout)
        time.sleep(2.1)
        raise urllib.error.URLError(ConnectionRefusedError(10061,
                  "No connection could be made because target machine actively refused it"))
    service,result=probe(monkeypatch,tmp_path,delayed)
    assert result is None
    assert len(calls)==1 and calls[0]>2.1
    assert service.state=="UNLOADED" and service.loads==0 and service.proc is None


def test_immediate_refusal_is_safe_no_listener(monkeypatch,tmp_path):
    service,result=probe(monkeypatch,tmp_path,refused)
    assert result is None
    assert service.loads==0 and service.state=="UNLOADED"


@contextmanager
def local_server(payload=b'{"resident":false,"busy":false}', status=200, delay=0):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path=="/lite/worker/status"
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type","application/json")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError,ConnectionResetError):
                pass
        def log_message(self,*args):
            pass
    httpd=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=httpd.serve_forever,daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/lite/worker/status"
    finally:
        httpd.shutdown();httpd.server_close()
        thread.join(timeout=5)


def test_real_loopback_server_stopped_refusal_is_safe(monkeypatch,tmp_path):
    with local_server() as endpoint:
        monkeypatch.setattr(mod,"GGUF_STATUS_URL",endpoint,raising=False)
        svc=TurboService(ChatterboxSettings(root=tmp_path))
        asyncio.run(svc._assert_no_gguf(time.monotonic()+9))
    # Keep the real closed port rather than mocking URLError.
    assert asyncio.run(svc._assert_no_gguf(time.monotonic()+9)) is None


@pytest.mark.parametrize("resident,busy",[(True,False),(False,True),(True,True)])
def test_real_loopback_gguf_resident_or_busy_rejected(monkeypatch,tmp_path,resident,busy):
    body=json.dumps({"resident":resident,"busy":busy}).encode()
    with local_server(body) as endpoint:
        monkeypatch.setattr(mod,"GGUF_STATUS_URL",endpoint,raising=False)
        svc=TurboService(ChatterboxSettings(root=tmp_path))
        with pytest.raises(TurboError,match="GGUF_WORKER_RESIDENT"):
            asyncio.run(svc._assert_no_gguf(time.monotonic()+9))
        assert svc.proc is None and svc.loads==0


def test_real_loopback_idle_gguf_is_allowed(monkeypatch,tmp_path):
    with local_server() as endpoint:
        monkeypatch.setattr(mod,"GGUF_STATUS_URL",endpoint,raising=False)
        svc=TurboService(ChatterboxSettings(root=tmp_path))
        assert asyncio.run(svc._assert_no_gguf(time.monotonic()+9)) is None


@pytest.mark.parametrize("status,payload",[
    (500,b'{"resident":false,"busy":false}'),
    (200,b'{"resident":false}'),
    (200,b'not json'),
    (200,b'{"resident":"false","busy":false}'),
    (200,b'{"resident":0,"busy":0}'),
    (200,b'[]'),
    (200,b'{"resident":false,"busy":false}' + b' ' * 16384),
])
def test_http_500_malformed_or_untrusted_status_fails_closed(monkeypatch,tmp_path,status,payload):
    with local_server(payload,status=status) as endpoint:
        monkeypatch.setattr(mod,"GGUF_STATUS_URL",endpoint,raising=False)
        svc=TurboService(ChatterboxSettings(root=tmp_path))
        with pytest.raises(TurboError,match="GGUF_PROBE_UNAVAILABLE"):
            asyncio.run(svc._assert_no_gguf(time.monotonic()+9))
        assert svc.proc is None and svc.loads==0


def test_real_http_timeout_fails_closed(monkeypatch,tmp_path):
    # Speed up solely the test budget, keep an actual socket/HTTP timeout.
    monkeypatch.setattr(mod,"GGUF_HTTP_TIMEOUT_S",.15,raising=False)
    monkeypatch.setattr(mod,"GGUF_PROBE_OUTER_TIMEOUT_S",.4,raising=False)
    with local_server(delay=.8) as endpoint:
        monkeypatch.setattr(mod,"GGUF_STATUS_URL",endpoint,raising=False)
        svc=TurboService(ChatterboxSettings(root=tmp_path))
        with pytest.raises(TurboError,match="GGUF_PROBE_UNAVAILABLE"):
            asyncio.run(svc._assert_no_gguf(time.monotonic()+3))
        assert svc.proc is None and svc.loads==0


def test_expired_generation_deadline_never_starts_probe(monkeypatch,tmp_path):
    monkeypatch.setattr(mod.urllib.request,"urlopen",lambda *a,**kw:
                        pytest.fail("No URL call permitted after deadline"))
    svc=TurboService(ChatterboxSettings(root=tmp_path))
    with pytest.raises(TurboError,match="DEADLINE_EXCEEDED"):
        asyncio.run(svc._assert_no_gguf(time.monotonic()-1))
    assert svc.proc is None and svc.loads==0


def test_probe_cancellation_propagates_and_does_not_launch_cuda(monkeypatch,tmp_path):
    entered=threading.Event()
    release=threading.Event()
    def blocked(*args,**kwargs):
        entered.set()
        release.wait(3)
        raise urllib.error.URLError(TimeoutError("network read stalled"))
    monkeypatch.setattr(mod.urllib.request,"urlopen",blocked)
    svc=TurboService(ChatterboxSettings(root=tmp_path))
    async def scenario():
        task=asyncio.create_task(svc._assert_no_gguf(time.monotonic()+9))
        try:
            ready=await asyncio.to_thread(entered.wait,2)
            assert ready
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
    asyncio.run(scenario())
    assert svc.loads==0 and svc.proc is None and svc.state=="UNLOADED"


def test_socket_timeout_not_confused_with_refusal(monkeypatch,tmp_path):
    def timed_out(*args,**kwargs):
        raise urllib.error.URLError(TimeoutError("connect timed out"))
    svc=TurboService(ChatterboxSettings(root=tmp_path))
    monkeypatch.setattr(mod.urllib.request,"urlopen",timed_out)
    with pytest.raises(TurboError,match="GGUF_PROBE_UNAVAILABLE"):
        asyncio.run(svc._assert_no_gguf(time.monotonic()+9))
    assert svc.loads==0
