"""Config schema, validation, and the overrides write path (SPEC §11).

ROLE: everything about configuration that is not composition — the known
sections/keys with types and ranges, boot-time validation with actionable
messages, base + local-overrides merge, and the tiny TOML emitter that
`config.set` writes through (scalars only). Hand-rolled guards, no schema
libraries (house rule, same as protocol/).

INVARIANTS: validation never mutates the config; errors are collected and
reported together, not first-failure; `config.set` writes ONLY the
`.local.toml` overrides file — the user's hand-edited base file is never
rewritten (comments survive); unknown sections/keys are warnings, not
errors (additive evolution), but type/range violations are errors.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

_ENUMS = {
    ("audio", "duplex"): {"full_duplex", "half_duplex"},
    ("audio", "attention"): {"always", "wake", "ptt_only", "muted"},
    ("stt", "provider"): {"faster-whisper", "null"},
}

_UNIT_RANGE = {("audio", "vad_threshold"), ("audio", "wake_threshold")}

# section -> key -> allowed types. STT is deliberately open beyond
# `provider` (extra keys pass through to the provider builder).
SCHEMA: dict[str, dict[str, tuple[type, ...]]] = {
    "audio": {
        "duplex": (str,),
        "attention": (str,),
        "aec": (bool,),
        "vad_threshold": (int, float),
        "wake_threshold": (int, float),
        "min_speech_ms": (int,),
        "min_speech_continuation_ms": (int,),
        "min_silence_ms": (int,),
        "dispatch_hold_ms": (int,),
        "reopen_window_ms": (int,),
        "wake_window_s": (int, float),
        "wake_model": (str,),
        "ptt_key": (str,),
        "phrase_bank_dir": (str,),
        "silero_model": (str,),
        "input_device": (str, int),
        "output_device": (str, int),
    },
    "tts": {
        "base_url": (str,),
        "model": (str,),
        "voice": (str,),
        "sample_rate": (int,),
        "api_key": (str,),
    },
    "stt": {"provider": (str,)},
    "first_mate": {
        "base_url": (str,),
        "model": (str,),
        "api_key": (str,),
        "json_mode": (bool,),
        "timeout_ms": (int, float),
        "late_window_ms": (int, float),
        "stream": (bool,),
        "voice": (str,),
    },
    "bridge": {"token": (str,)},
    "state": {"dir": (str,)},
    "watcher": {
        "enabled": (bool,),
        "interval_s": (int, float),
        "speak": (bool,),
    },
}

_POSITIVE_MS = {
    ("audio", "min_speech_ms"),
    ("audio", "min_speech_continuation_ms"),
    ("audio", "min_silence_ms"),
    ("audio", "dispatch_hold_ms"),
    ("audio", "reopen_window_ms"),
    ("first_mate", "timeout_ms"),
    ("first_mate", "late_window_ms"),
}


def validate(cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors should refuse boot; warnings are
    typo-catchers and tuning advice."""
    errors: list[str] = []
    warnings: list[str] = []
    for section, values in cfg.items():
        if section not in SCHEMA:
            warnings.append(f"unknown section [{section}] (typo?)")
            continue
        if not isinstance(values, dict):
            errors.append(f"[{section}] must be a table, got {type(values).__name__}")
            continue
        keys = SCHEMA[section]
        for key, value in values.items():
            if key not in keys:
                if section != "stt":  # stt extras pass through to the provider
                    warnings.append(f"unknown key {section}.{key} (typo?)")
                continue
            if not isinstance(value, keys[key]) or isinstance(value, bool) != (
                bool in keys[key]
            ):
                want = "/".join(t.__name__ for t in keys[key])
                errors.append(
                    f"{section}.{key} must be {want},"
                    f" got {type(value).__name__} ({value!r})"
                )
                continue
            allowed = _ENUMS.get((section, key))
            if allowed and value not in allowed:
                errors.append(
                    f"{section}.{key} must be one of {sorted(allowed)}, got {value!r}"
                )
            # isinstance re-checks are mypy narrowing; the type gate above
            # already guaranteed numerics for these keys.
            if (
                (section, key) in _POSITIVE_MS
                and isinstance(value, (int, float))
                and not value > 0
            ):
                errors.append(f"{section}.{key} must be > 0, got {value!r}")
            if (
                (section, key) in _UNIT_RANGE
                and isinstance(value, (int, float))
                and not 0.0 <= value <= 1.0
            ):
                errors.append(f"{section}.{key} must be in [0, 1], got {value!r}")
    audio = cfg.get("audio", {})
    if isinstance(audio, dict):
        hold = audio.get("dispatch_hold_ms", 800)
        reopen = audio.get("reopen_window_ms", 1200)
        if isinstance(hold, int) and isinstance(reopen, int) and 0 < reopen < hold:
            warnings.append(
                f"audio.reopen_window_ms ({reopen}) < dispatch_hold_ms ({hold}):"
                " reopens will feel shorter than the hold (SPEC §5.2)"
            )
    return errors, warnings


# ---- overrides file (config.set write path) --------------------------------


def overrides_path(base: Path) -> Path:
    return base.with_name(base.stem + ".local.toml")


def merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Section-wise shallow merge; override keys win inside a section."""
    out: dict[str, Any] = {
        k: dict(v) if isinstance(v, dict) else v for k, v in base.items()
    }
    for section, values in overrides.items():
        if isinstance(values, dict) and isinstance(out.get(section), dict):
            out[section].update(values)
        else:
            out[section] = values
    return out


def _emit_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise ValueError(f"only scalar overrides are supported, got {type(value).__name__}")


def write_overrides(path: Path, overrides: dict[str, Any]) -> None:
    """Emit a flat sections-of-scalars TOML file atomically."""
    lines = [
        "# Written by voco (config.set) — overrides the base config.",
        "# Hand-edits here survive; the base file is never rewritten.",
    ]
    for section in sorted(overrides):
        values = overrides[section]
        if not isinstance(values, dict) or not values:
            continue
        lines.append(f"\n[{section}]")
        for key in sorted(values):
            lines.append(f"{key} = {_emit_value(values[key])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text())


def set_value(
    base_path: Path, cfg: dict[str, Any], dotted_key: str, value: Any
) -> dict[str, Any]:
    """Apply one override: validate against the schema on the MERGED result,
    persist to the overrides file, and return the new merged config.
    Raises ValueError with the validation message on a bad key/value."""
    parts = dotted_key.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"key must be section.key, got {dotted_key!r}")
    section, key = parts
    if section not in SCHEMA or (key not in SCHEMA[section] and section != "stt"):
        raise ValueError(f"unknown config key {dotted_key!r}")
    candidate = merge(cfg, {section: {key: value}})
    errors, _ = validate(candidate)
    mine = [e for e in errors if e.startswith(f"{section}.{key} ")]
    if mine:
        raise ValueError("; ".join(mine))
    path = overrides_path(base_path)
    current = read_overrides(path)
    current.setdefault(section, {})[key] = value
    write_overrides(path, current)
    return candidate
