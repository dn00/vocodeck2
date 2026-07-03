"""Config schema/validation/overrides (voco/config.py) + daemon config.set."""

from __future__ import annotations

import pytest

from voco import config as config_mod
from voco.daemon import Daemon, load_config


def test_validate_flags_typos_as_warnings_not_errors():
    errors, warnings = config_mod.validate(
        {"audio": {"vad_treshold": 0.5}, "firstmate": {}}
    )
    assert errors == []
    assert any("vad_treshold" in w for w in warnings)
    assert any("[firstmate]" in w for w in warnings)


def test_validate_type_enum_and_range_errors():
    errors, _ = config_mod.validate(
        {
            "audio": {
                "duplex": "loud",  # bad enum
                "vad_threshold": 1.5,  # out of unit range
                "min_speech_ms": "fast",  # bad type
                "aec": 1,  # int is not bool
            },
            "first_mate": {"timeout_ms": 0},  # must be > 0
        }
    )
    joined = "\n".join(errors)
    assert "audio.duplex" in joined and "loud" in joined
    assert "audio.vad_threshold" in joined
    assert "audio.min_speech_ms" in joined
    assert "audio.aec" in joined
    assert "first_mate.timeout_ms" in joined
    assert len(errors) == 5  # all collected, not first-failure


def test_stt_extras_pass_through_unwarned():
    errors, warnings = config_mod.validate(
        {"stt": {"provider": "faster-whisper", "model_size": "small", "device": "cpu"}}
    )
    assert errors == [] and warnings == []


def test_reopen_shorter_than_hold_warns():
    _, warnings = config_mod.validate(
        {"audio": {"dispatch_hold_ms": 800, "reopen_window_ms": 500}}
    )
    assert any("reopen_window_ms" in w for w in warnings)


def test_merge_overrides_win_per_section():
    merged = config_mod.merge(
        {"audio": {"aec": False, "duplex": "full_duplex"}, "tts": {"voice": "a"}},
        {"audio": {"aec": True}},
    )
    assert merged["audio"] == {"aec": True, "duplex": "full_duplex"}
    assert merged["tts"] == {"voice": "a"}


def test_set_value_persists_and_survives_load(tmp_path):
    base = tmp_path / "config.toml"
    base.write_text("[audio]\nvad_threshold = 0.5\n# hand comment\n")
    cfg = config_mod.merge({"audio": {"vad_threshold": 0.5}}, {})
    new = config_mod.set_value(base, cfg, "audio.vad_threshold", 0.35)
    assert new["audio"]["vad_threshold"] == 0.35
    # Base file untouched (comments survive); overrides file exists.
    assert "# hand comment" in base.read_text()
    loaded = load_config(base)
    assert loaded["audio"]["vad_threshold"] == 0.35


def test_set_value_rejects_bad_key_and_value(tmp_path):
    base = tmp_path / "config.toml"
    with pytest.raises(ValueError, match="unknown config key"):
        config_mod.set_value(base, {}, "audio.nope", 1)
    with pytest.raises(ValueError, match=r"audio\.vad_threshold"):
        config_mod.set_value(base, {}, "audio.vad_threshold", 3.0)
    with pytest.raises(ValueError, match=r"section\.key"):
        config_mod.set_value(base, {}, "vad_threshold", 0.4)


def test_load_config_refuses_invalid(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[audio]\nduplex = "loud"\n')
    with pytest.raises(SystemExit, match=r"audio\.duplex"):
        load_config(p)


async def test_daemon_config_set_hot_applies_timeout(tmp_path):
    base = tmp_path / "config.toml"
    d = Daemon({}, no_audio=True, config_path=base)
    result = await d._control(
        "config.set", {"key": "first_mate.timeout_ms", "value": 1500}
    )
    assert result["applied"] is True and result["restart_required"] is False
    assert d.router._timeout_s == pytest.approx(1.5)
    # Cold key: persisted, honestly reported as restart-required.
    result = await d._control("config.set", {"key": "audio.aec", "value": True})
    assert result["applied"] is False and result["restart_required"] is True
    assert d.cfg["audio"]["aec"] is True
    assert config_mod.overrides_path(base).exists()
