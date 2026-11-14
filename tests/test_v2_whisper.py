"""v2.0: Whisper picks the GPU when it works, otherwise falls back to the CPU."""
import pytest

from friday import voice


class FakeModel:
    def __init__(self, model, device, compute_type, broken=()):
        if device in broken:
            raise RuntimeError("Library cublas64_12.dll is not found")
        self.device = device

    def transcribe(self, audio, **kw):
        return iter([]), None


def test_whisper_prefers_gpu():
    m, dev = voice.load_whisper("small.en", "auto", factory=FakeModel)
    assert dev == "cuda/float16"


def test_whisper_falls_back_to_cpu():
    f = lambda *a, **k: FakeModel(*a, broken=("cuda",), **k)
    m, dev = voice.load_whisper("small.en", "auto", factory=f)
    assert dev == "cpu/int8"
    assert voice.whisper_devices("cpu") == [("cpu", "int8")]
    with pytest.raises(RuntimeError):
        voice.load_whisper("small.en", "cuda", factory=f)


def test_gpu_probe_is_remembered():
    from types import SimpleNamespace
    calls = []
    run = lambda: calls.append(1) or SimpleNamespace(returncode=3221225477, stdout="", stderr="cudnn_ops64_9.dll missing")
    assert voice.gpu_probe_ok("small.en", "float16", run=run) is False
    assert voice.gpu_probe_ok("small.en", "float16", run=run) is False
    assert len(calls) == 1  # second start doesn't re-test
    ok = lambda: SimpleNamespace(returncode=0, stdout="GPU-OK\n", stderr="")
    assert voice.gpu_probe_ok("medium.en", "float16", run=ok) is True
