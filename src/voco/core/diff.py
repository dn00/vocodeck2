"""Unified-diff parser (SPEC-WORKBENCH §3, W1).

Ported from diff-annotate's `lib/parse-diff.mjs` (the review oracle) so the
row/line-number mapping is byte-identical — annotation anchors come straight
off row attributes and are never inferred.

Row kinds and line-number semantics:
- context (` `): {old_line, new_line} — advance both
- add     (`+`): {old_line: None, new_line} — advance new
- del     (`-`): {old_line, new_line: None} — advance old
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

HUNK_RE = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@@*(.*)$")

RowKind = Literal["context", "add", "del"]


@dataclass
class Row:
    kind: RowKind
    old_line: int | None
    new_line: int | None
    content: str

    def to_dict(self) -> dict:
        # `side`/`line` are exactly what an annotation anchors to: added and
        # context rows anchor to the new side, deletions to the old side.
        side = "old" if self.kind == "del" else "new"
        line = self.old_line if self.kind == "del" else self.new_line
        return {
            "kind": self.kind,
            "old_line": self.old_line,
            "new_line": self.new_line,
            "content": self.content,
            "side": side,
            "line": line,
        }


@dataclass
class Hunk:
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str
    rows: list[Row] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "header": self.header,
            "old_start": self.old_start,
            "new_start": self.new_start,
            "section": self.section,
            "rows": [r.to_dict() for r in self.rows],
        }


@dataclass
class FileDiff:
    old_path: str | None = None
    new_path: str | None = None
    git_old: str | None = None
    git_new: str | None = None
    path: str = "(unknown)"
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "is_new": self.is_new,
            "is_deleted": self.is_deleted,
            "is_rename": self.is_rename,
            "hunks": [h.to_dict() for h in self.hunks],
        }


def _strip_prefix(p: str) -> str | None:
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        return p[2:]
    return p


def _header_path(line: str) -> str | None:
    # KNOWN LIMITATION (review WARNING 9, shared with the oracle): git quotes
    # paths containing spaces/specials as `"a/b c.py"` with C-style escapes;
    # this does not unquote them, so anchors on such files may mismatch. The
    # common case (ordinary paths) is exact. Full check-quoted-path handling
    # is a deferred follow-up.
    rest = line[4:]
    tab = rest.find("\t")  # non-git diffs may append a tab + timestamp
    if tab != -1:
        rest = rest[:tab]
    return _strip_prefix(rest.strip())


def _choose_path(f: FileDiff) -> str:
    return f.new_path or f.old_path or f.git_new or f.git_old or "(unknown)"


def parse_diff(text: str) -> list[dict]:
    """Parse unified diff text into a list of file dicts (see module doc)."""
    lines = str(text).split("\n")
    files: list[FileDiff] = []
    file: FileDiff | None = None
    hunk: Hunk | None = None
    old_line = new_line = old_remaining = new_remaining = 0

    def new_file(**seed) -> FileDiff:
        nonlocal file, hunk
        file = FileDiff(**seed)
        hunk = None
        files.append(file)
        return file

    for line in lines:
        if line.startswith("diff --git "):
            m = re.match(r"^diff --git (.+) (.+)$", line)
            seed: dict = {}
            if m:
                seed["git_old"] = _strip_prefix(m.group(1))
                seed["git_new"] = _strip_prefix(m.group(2))
            new_file(**seed)
            continue

        if line.startswith("--- "):
            if file is None or file.new_path is not None or file.hunks:
                new_file()
            assert file is not None
            file.old_path = _header_path(line)
            if file.old_path is None:
                file.is_new = True
            hunk = None
            continue

        if line.startswith("+++ "):
            if file is None:
                new_file()
            assert file is not None
            file.new_path = _header_path(line)
            if file.new_path is None:
                file.is_deleted = True
            hunk = None
            continue

        if line.startswith("rename from "):
            if file is None:
                new_file()
            assert file is not None
            file.is_rename = True
            file.git_old = line[len("rename from ") :].strip()
            continue
        if line.startswith("rename to "):
            if file is None:
                new_file()
            assert file is not None
            file.is_rename = True
            file.git_new = line[len("rename to ") :].strip()
            continue
        if line.startswith("new file mode"):
            if file is not None:
                file.is_new = True
            continue
        if line.startswith("deleted file mode"):
            if file is not None:
                file.is_deleted = True
            continue

        hm = HUNK_RE.match(line)
        if hm:
            if file is None:
                new_file()
            assert file is not None
            old_start = int(hm.group(1))
            old_count = 1 if hm.group(2) is None else int(hm.group(2))
            new_start = int(hm.group(3))
            new_count = 1 if hm.group(4) is None else int(hm.group(4))
            hunk = Hunk(
                header=line.rstrip(),
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                section=(hm.group(5) or "").strip(),
            )
            file.hunks.append(hunk)
            old_line, new_line = old_start, new_start
            old_remaining, new_remaining = old_count, new_count
            continue

        # Body line — only inside an unfinished hunk; completion is driven by
        # declared counts so trailing artifacts never become phantom rows.
        if hunk is None or (old_remaining <= 0 and new_remaining <= 0):
            continue
        c = line[0] if line else " "
        if c == "\\":  # "\ No newline at end of file"
            continue
        content = line[1:] if line else ""
        if c == "+":
            hunk.rows.append(Row("add", None, new_line, content))
            new_line += 1
            new_remaining -= 1
        elif c == "-":
            hunk.rows.append(Row("del", old_line, None, content))
            old_line += 1
            old_remaining -= 1
        else:
            hunk.rows.append(Row("context", old_line, new_line, content))
            old_line += 1
            new_line += 1
            old_remaining -= 1
            new_remaining -= 1

    for f in files:
        f.path = _choose_path(f)
    return [f.to_dict() for f in files]
