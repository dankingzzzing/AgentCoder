"""Bounded IO workers; callbacks and checkpoint writes stay on the caller thread."""

import queue
import socket
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from urllib.request import HTTPHandler, HTTPSHandler

from .errors import Cancelled, ProviderError


LOCAL = threading.local()
WORKERS = threading.BoundedSemaphore(8)


class IOJob:
    def __init__(self):
        self.messages = queue.Queue(maxsize=64)
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.sockets = []

    def track(self, connection):
        with self.lock:
            self.sockets.append(connection)
            stopped = self.stopped.is_set()
        if stopped:
            self.close_socket(connection)

    @staticmethod
    def close_socket(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def stop(self):
        self.stopped.set()
        with self.lock:
            connections = self.sockets[:]
        for connection in connections:
            self.close_socket(connection)

    def send(self, kind, value):
        while not self.stopped.is_set():
            try:
                self.messages.put((kind, value), timeout=.1)
                return
            except queue.Full:
                continue


class TrackedConnection:
    def connect(self):
        job = getattr(LOCAL, "job", None)
        if job:
            original = self._create_connection
            def create(*args, **options):
                connection = original(*args, **options)
                job.track(connection)
                return connection
            self._create_connection = create
        super().connect()
        if job:
            job.track(self.sock)


class AbortableHTTPConnection(TrackedConnection, HTTPConnection):
    pass


class AbortableHTTPSConnection(TrackedConnection, HTTPSConnection):
    pass


class AbortableHTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(AbortableHTTPConnection, request)


class AbortableHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(AbortableHTTPSConnection, request, context=self._context)


def check_stopped():
    job = getattr(LOCAL, "job", None)
    if job and job.stopped.is_set():
        raise Cancelled("已停止模型请求，未完成的输出不会提交。")


def poll_io(producer, *, cancelled=None, on_activity=None, timeout=600):
    job = IOJob()
    started = time.monotonic()
    while not WORKERS.acquire(timeout=.1):
        if cancelled and cancelled.is_set():
            raise Cancelled("已停止等待模型连接。")
        if time.monotonic() - started > timeout:
            raise ProviderError("模型连接队列等待超时。", code="queue_timeout", retryable=True)

    def worker():
        LOCAL.job = job
        try:
            for value in producer():
                if job.stopped.is_set():
                    break
                job.send("value", value)
        except Exception as exc:
            job.send("error", exc)
        finally:
            job.send("done", None)
            job.stop()
            LOCAL.job = None
            WORKERS.release()

    threading.Thread(target=worker, daemon=True, name="agentcoder-http").start()
    try:
        while True:
            if cancelled and cancelled.is_set():
                raise Cancelled("已停止模型请求，检查点和已完成交付保留。")
            if time.monotonic() - started > timeout:
                raise ProviderError("模型请求超过总等待上限。", code="request_timeout", retryable=True)
            if on_activity:
                on_activity()
            try:
                kind, value = job.messages.get(timeout=.1)
            except queue.Empty:
                continue
            if kind == "error":
                raise value
            if kind == "done":
                break
            yield value
    finally:
        job.stop()
