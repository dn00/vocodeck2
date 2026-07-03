"""Pane-state classifier (core/pane_state.py) — fixture captures."""

from __future__ import annotations

from voco.core.pane_state import classify

CLAUDE_PERMISSION = """\
 ⏺ Bash(rm -rf build/)

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again this session
   3. No, and tell Claude what to do differently
"""

NUMBERED_MENU = """\
 Select an option:
  1. Continue with the migration
  2. Roll back
"""

YN_CONFIRM = "Overwrite existing file? [y/n] "

CLAUDE_WORKING = """\
 ⏺ Running tests...

 ✻ Churning… (32s · esc to interrupt)
"""

SPINNER_ONLY = "⠹ compiling workspace"

SHELL_IDLE = """\
 ⏺ Done! All 14 tests pass.

dn@mac vocodeck2 %
"""

COMPOSER_IDLE = """\
╭──────────────────────────────────────────────╮
│ >                                            │
╰──────────────────────────────────────────────╯
"""


def test_permission_prompts_classify_as_waiting():
    assert classify(CLAUDE_PERMISSION) == "waiting"
    assert classify(NUMBERED_MENU) == "waiting"
    assert classify(YN_CONFIRM) == "waiting"


def test_working_signals():
    assert classify(CLAUDE_WORKING) == "working"
    assert classify(SPINNER_ONLY) == "working"


def test_waiting_outranks_working():
    # Permission prompt below a spinner line: the prompt is what matters.
    assert classify(CLAUDE_WORKING + CLAUDE_PERMISSION) == "waiting"


def test_bare_numbered_plan_is_not_waiting():
    # Agents print numbered plans constantly (review finding): without
    # ask-context this must NOT read as blocked.
    plan = "Here's my plan:\n 1. Refactor the parser\n 2. Add tests\n"
    assert classify(plan) is None
    # Same list under an actual question IS a menu.
    assert classify("Which approach?\n 1. Refactor\n 2. Rewrite\n") == "waiting"


def test_shell_prompt_and_unsure():
    assert classify(SHELL_IDLE) == "shell"
    assert classify("") is None
    assert classify("some ordinary program output\nmore text") is None
    # An idle composer is not claimed as anything — None is honest.
    assert classify(COMPOSER_IDLE) is None
