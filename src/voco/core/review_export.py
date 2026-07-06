"""Review export (SPEC-WORKBENCH §3.3, W1).

Writes the legacy-compatible findings JSON + the full anchors sidecar so
downstream consumers (onebrain3 Lane C and friends) read byte-identical
shapes to diff-annotate:

- `<out>`: `[{file, side, startLine, endLine, concern}]` — DIFF-page
  findings only, excluding withdrawn (the legacy contract).
- `<out-stem>.anchors.json`: every finding across all pages with ids,
  kinds, statuses, answers, rev + stale flags.

Atomic write; default lands in the workspace data dir, never the checkout.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voco.core.workspace import WorkspaceStore


def _legacy_records(ws) -> list[dict]:
    out = []
    for f in ws.findings.values():
        if f.status == "withdrawn":
            continue
        page = ws.pages.get(f.page_id)
        if page is None or page.type != "diff":
            continue
        a = f.anchor
        if "file" not in a or "startLine" not in a:
            continue
        out.append(
            {
                "file": a["file"],
                "side": a.get("side", "new"),
                "startLine": a["startLine"],
                "endLine": a.get("endLine", a["startLine"]),
                "concern": f.text,
            }
        )
    return out


def _anchors(ws) -> list[dict]:
    rows = []
    for f in ws.findings.values():
        page = ws.pages.get(f.page_id)
        d = f.to_dict()
        d["page_type"] = page.type if page else None
        d["page_ref"] = page.ref if page else None
        d["stale"] = bool(page and f.rev < page.rev)
        rows.append(d)
    return rows


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def export_workspace(
    store: WorkspaceStore,
    workspace_key: str,
    *,
    out: str | None = None,
    data_dir: Path,
    stamp: str | None = None,
) -> dict:
    ws = store.get(workspace_key)
    if ws is None:
        raise ValueError(f"unknown workspace: {workspace_key}")
    if out:
        out_path = Path(out)
    else:
        stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        # <data-dir>/workspaces/<safe-key>/review-<stamp>.json
        safe = workspace_key.replace("/", "%2F").replace(":", "%3A")
        out_path = data_dir / "workspaces" / safe / f"review-{stamp}.json"

    records = _legacy_records(ws)
    _atomic_write(out_path, json.dumps(records, indent=2))
    anchors_path = out_path.with_name(out_path.stem + ".anchors.json")
    _atomic_write(anchors_path, json.dumps(_anchors(ws), indent=2))
    # 0600 — proprietary review data (§8 sensitivity).
    for p in (out_path, anchors_path):
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return {
        "out": str(out_path),
        "anchors": str(anchors_path),
        "count": len(records),
    }
