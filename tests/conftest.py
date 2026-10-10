"""Unit tests must never reach the user's account, network or emulator."""
import socket
import subprocess

import pytest


@pytest.fixture(autouse=True)
def block_external_effects(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("External I/O is forbidden in offline tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
