"""Unified-diff parser (core/diff.py) — line-mapping fidelity vs the oracle."""

from __future__ import annotations

from voco.core.diff import parse_diff

SAMPLE = """\
diff --git a/src/foo.py b/src/foo.py
index 111..222 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,4 +1,5 @@
 import os
-import sys
+import sys  # noqa
+import json
 def main():
     pass
"""


def test_rows_carry_exact_side_and_line():
    (f,) = parse_diff(SAMPLE)
    assert f["path"] == "src/foo.py"
    (hunk,) = f["hunks"]
    rows = hunk["rows"]
    # context "import os" -> new side, line 1
    assert rows[0]["kind"] == "context"
    assert rows[0]["side"] == "new" and rows[0]["line"] == 1
    # deletion "import sys" -> old side, old line 2
    dels = [r for r in rows if r["kind"] == "del"]
    assert dels[0]["side"] == "old" and dels[0]["line"] == 2
    # additions land on new lines 2 and 3
    adds = [r for r in rows if r["kind"] == "add"]
    assert [a["line"] for a in adds] == [2, 3]
    assert all(a["side"] == "new" for a in adds)


def test_new_and_deleted_file_flags():
    added = parse_diff(
        "diff --git a/new.txt b/new.txt\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hello\n"
    )
    assert added[0]["is_new"] and added[0]["path"] == "new.txt"
    deleted = parse_diff(
        "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n"
        "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
    )
    assert deleted[0]["is_deleted"] and deleted[0]["path"] == "gone.txt"


def test_multiple_files_and_rename():
    text = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/old.txt b/new.txt\nrename from old.txt\nrename to new.txt\n"
    )
    files = parse_diff(text)
    assert len(files) == 2
    assert files[1]["is_rename"] and files[1]["path"] == "new.txt"


def test_hunk_counts_stop_absorbing_trailing_lines():
    # A single-line hunk must not eat the blank line after it.
    text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n\ntrailing junk\n"
    (f,) = parse_diff(text)
    rows = f["hunks"][0]["rows"]
    assert [r["kind"] for r in rows] == ["del", "add"]
