"""Structured rotating daemon logging (BUILD-PROD P4): voco.logsetup.

setup() mutates the global `voco` logger; every test restores handlers,
level, and propagate in teardown so the rest of the suite never sees a
leftover file handler.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

import pytest

from voco import logsetup

LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (INFO|DEBUG|WARNING|ERROR)\s+voco\.\S+: "
)


@pytest.fixture(autouse=True)
def restore_voco_logger():
    root = logging.getLogger("voco")
    saved_level, saved_propagate = root.level, root.propagate
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(saved_level)
    root.propagate = saved_propagate


def test_rotating_file_with_timestamps_and_levels(tmp_path):
    path = logsetup.setup(log_dir=tmp_path, console=False)
    assert path == tmp_path / "daemon.log"
    logging.getLogger("voco.daemon").info("hello %s", "world")
    logging.getLogger("voco.floor").warning("floor gripe")
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert all(LINE.match(ln) for ln in lines)
    assert "hello world" in lines[0] and "INFO" in lines[0]
    assert "floor gripe" in lines[1] and "WARNING" in lines[1]


def test_rotation_actually_rotates(tmp_path):
    logsetup.setup(log_dir=tmp_path, console=False, max_bytes=512, backups=2)
    log = logging.getLogger("voco.daemon")
    for i in range(60):  # ~60 × >64 bytes ≫ 512
        log.info("filler line %04d %s", i, "x" * 40)
    assert (tmp_path / "daemon.log").exists()
    assert (tmp_path / "daemon.log.1").exists()  # rollover happened
    # backups are capped, not unbounded
    assert not (tmp_path / "daemon.log.3").exists()


def test_verbose_flips_debug(tmp_path):
    path = logsetup.setup(log_dir=tmp_path, console=False)
    logging.getLogger("voco.x").debug("quiet")
    assert path is not None and "quiet" not in path.read_text()
    logsetup.setup(verbose=True, log_dir=tmp_path, console=False)
    logging.getLogger("voco.x").debug("loud")
    assert "loud" in path.read_text()


def test_setup_is_idempotent_never_stacks_handlers(tmp_path):
    root = logging.getLogger("voco")
    for _ in range(3):
        logsetup.setup(log_dir=tmp_path, console=False)
    assert len(root.handlers) == 1  # file only
    for _ in range(3):
        logsetup.setup(log_dir=tmp_path, console=True)
    assert len(root.handlers) == 2  # stderr + file, once each
    logging.getLogger("voco.daemon").info("once")
    text = (tmp_path / "daemon.log").read_text()
    assert text.count("once") == 1  # no duplicate sinks


def test_console_env_kill_switch(tmp_path, monkeypatch):
    # managed spawns set VOCO_LOG_CONSOLE=0 so daemon.out doesn't
    # duplicate every structured line
    monkeypatch.setenv("VOCO_LOG_CONSOLE", "0")
    logsetup.setup(log_dir=tmp_path)
    root = logging.getLogger("voco")
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0], RotatingFileHandler)
    # explicit console= beats the env
    logsetup.setup(log_dir=tmp_path, console=True)
    assert len(root.handlers) == 2


def test_uncreatable_dir_degrades_to_stderr_not_death(tmp_path, capsys):
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the dir should go")
    path = logsetup.setup(log_dir=blocked / "sub", console=True)
    assert path is None  # honest signal: no file logging
    root = logging.getLogger("voco")
    assert len(root.handlers) == 1  # the stderr mirror survived
    assert "file logging unavailable" in capsys.readouterr().err
    # logging still works
    logging.getLogger("voco.daemon").error("still alive")
    assert "still alive" in capsys.readouterr().err


def test_uncreatable_dir_with_console_off_still_gets_a_sink(tmp_path, capsys):
    blocked = tmp_path / "blocked"
    blocked.write_text("in the way")
    path = logsetup.setup(log_dir=blocked / "sub", console=False)
    assert path is None
    root = logging.getLogger("voco")
    assert len(root.handlers) == 1  # fallback stderr — never zero sinks
    assert "file logging unavailable" in capsys.readouterr().err


def test_untrusted_text_cannot_forge_records_or_drive_the_terminal(tmp_path):
    # floor output and daemon.error payloads are untrusted: newlines
    # must not forge a second record, ESC must not reach `voco logs`
    path = logsetup.setup(log_dir=tmp_path, console=False)
    logging.getLogger("voco.floor").info(
        "evil\n2099-01-01 00:00:00 INFO    voco.daemon: forged\x1b[31m red"
    )
    assert path is not None
    lines = path.read_text().splitlines()
    assert len(lines) == 1  # one record, one line — no forged second record
    assert "\\n2099-01-01" in lines[0]  # the newline is visible, inert text
    assert "\x1b" not in lines[0]  # ANSI lost its teeth


def test_state_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCO_STATE_DIR", str(tmp_path / "sd"))
    assert logsetup.state_dir() == tmp_path / "sd"
