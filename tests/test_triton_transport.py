"""Юнит-тесты triton-транспорта, не требующие tritonclient/сервера."""

import pytest

from loadgen.client import make_transport

np = pytest.importorskip("numpy")


def test_f32_to_s16le_roundtrip():
    from loadgen.client import f32_to_s16le

    wave = np.array([0.0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5], dtype=np.float32)
    raw = f32_to_s16le(wave)
    back = np.frombuffer(raw, dtype=np.int16)
    assert len(raw) == wave.size * 2  # s16le: длительность чанков считается по байтам
    assert back[0] == 0
    assert abs(back[1] - 16383) <= 1 and abs(back[2] + 16383) <= 1
    assert back[3] == 32767 and back[4] == -32767
    # клиппинг за пределами [-1, 1]
    assert back[5] == 32767 and back[6] == -32767


def test_make_transport_triton():
    from loadgen.client import TritonTransport

    t = make_transport("triton", "10.0.0.1:8001")
    assert isinstance(t, TritonTransport)
    assert t.model_name == "cosyvoice2"
