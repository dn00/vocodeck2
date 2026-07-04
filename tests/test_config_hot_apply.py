"""config.set / mic.set hot-apply against a REAL VoiceLoop (fake edges).

Regression coverage for the live-test report: `voco config set
audio.duplex half_duplex` returned applied:true while mic.state kept the
boot values. These tests pin the contract: applied:true MUST mean the
running loop changed, and headless (voice=None) MUST answer
restart_required, never a false applied.
"""

from __future__ import annotations

from fakes import FakeMic, FakePlayer, FakeStt, FakeTts, ScriptedVad
from voco.core.arbitration import DuplexMode
from voco.core.attention import AttentionMode
from voco.daemon import Daemon
from voco.voice_loop import VoiceLoop, VoiceLoopDeps


def base_cfg(tmp_path) -> dict:
    return {
        "audio": {
            "duplex": "full_duplex",
            "attention": "always",
            "phrase_bank_dir": str(tmp_path / "bank"),
        },
        "stt": {"provider": "fake"},
        "tts": {"base_url": "http://none", "model": "x", "voice": "test"},
        "state": {"dir": str(tmp_path / "state")},
    }


def make_daemon(tmp_path, with_voice: bool = True):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[audio]\nduplex = "full_duplex"\n', encoding="utf-8")
    cfg = base_cfg(tmp_path)
    d = Daemon(cfg, no_audio=True, config_path=cfg_path)
    if with_voice:
        deps = VoiceLoopDeps(
            load_vad_model=lambda path: ScriptedVad(),
            stt_builder=lambda provider, **kw: FakeStt(""),
            tts_factory=FakeTts,
            mic_factory=FakeMic,
            player_factory=FakePlayer,
            hotkey_factory=None,
        )
        d.voice = VoiceLoop(cfg, d.bus, host=d, deps=deps)
    events: list = []
    d.bus.subscribe(lambda env: events.append((env.type, env.payload)))
    return d, events


async def test_config_set_duplex_hot_applies(tmp_path):
    d, events = make_daemon(tmp_path)
    result = await d._control(
        "config.set", {"key": "audio.duplex", "value": "half_duplex"}
    )
    assert result["applied"] is True
    assert result["restart_required"] is False
    assert d.voice is not None and d.voice.duplex is DuplexMode.HALF
    mic = [p for t, p in events if t == "mic.state"]
    assert mic and mic[-1] == {"duplex": "half_duplex", "attention": "always"}


async def test_config_set_attention_hot_applies(tmp_path):
    d, events = make_daemon(tmp_path)
    result = await d._control(
        "config.set", {"key": "audio.attention", "value": "ptt_only"}
    )
    assert result["applied"] is True
    assert d.voice is not None
    assert d.voice.attention.mode is AttentionMode.PTT_ONLY
    mic = [p for t, p in events if t == "mic.state"]
    assert mic and mic[-1]["attention"] == "ptt_only"


async def test_config_set_duplex_headless_reports_restart_required(tmp_path):
    """voice=None: nothing can change at runtime — applied:true is a lie."""
    d, _ = make_daemon(tmp_path, with_voice=False)
    result = await d._control(
        "config.set", {"key": "audio.duplex", "value": "half_duplex"}
    )
    assert result["applied"] is False
    assert result["restart_required"] is True


async def test_mic_set_duplex_headless_raises(tmp_path):
    d, _ = make_daemon(tmp_path, with_voice=False)
    try:
        await d._control("mic.set", {"duplex": "half_duplex"})
    except ValueError as e:
        assert "voice loop" in str(e)
    else:
        raise AssertionError("mic.set with no voice loop must raise")


async def test_config_set_patience_hot_applies(tmp_path):
    d, _ = make_daemon(tmp_path)
    r1 = await d._control("config.set", {"key": "audio.dispatch_hold_ms", "value": 600})
    r2 = await d._control(
        "config.set", {"key": "audio.incomplete_hold_ms", "value": 1200}
    )
    assert r1["applied"] is True and r2["applied"] is True
    assert d.voice is not None
    cfg = d.voice.machine._cfg
    assert cfg.dispatch_hold_ms == 600
    assert cfg.incomplete_hold_ms == 1200


async def test_config_set_patience_headless_reports_restart_required(tmp_path):
    d, _ = make_daemon(tmp_path, with_voice=False)
    r = await d._control("config.set", {"key": "audio.incomplete_hold_ms", "value": 0})
    assert r["applied"] is False
    assert r["restart_required"] is True


async def test_mic_set_duplex_hot_applies(tmp_path):
    d, events = make_daemon(tmp_path)
    await d._control("mic.set", {"duplex": "half_duplex"})
    assert d.voice is not None and d.voice.duplex is DuplexMode.HALF
    mic = [p for t, p in events if t == "mic.state"]
    assert mic and mic[-1]["duplex"] == "half_duplex"
