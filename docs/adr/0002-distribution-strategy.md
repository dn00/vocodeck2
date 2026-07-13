# Distribution: staged tiers, no rewrite — Tier 1 is the product path

Decided 2026-07-07 (user-confirmed). voco ships in tiers, none of which
rewrite the Python daemon: **Tier 0** — PyPI (`uvx voco`) for the
terminal/agent persona, gated on first-run work (platform auto-detect:
MLX on Apple silicon, onnx elsewhere; model download with progress +
checksums; no hand-edited per-machine TOML). **Tier 1 — the true
product**: Tauri shell (vocodeck-p) bundling the existing web client
unchanged + the daemon as a sidecar via python-build-standalone with a
pre-built venv (NOT PyInstaller — freezing the onnxruntime/sounddevice/
MLX stack is the known-fragile path; ComfyUI Desktop and JupyterLab
Desktop ship this exact bundled-runtime shape as real products).
Models stay out of the installer and download on first run (Ollama
pattern; installer <100 MB, models 0.5–2 GB). Signing + notarization
(clean mic TCC attribution needs a real .app), CLI shim on PATH
(Docker Desktop pattern) for `voco`/`voco-mcp`, Tauri auto-updater.
**Tier 2** (only if install size / cold start / memory block adoption):
compiled core behind the SAME wire protocol (PROTOCOL.md + tests make
the port tractable); models already run off-Python (kokoro-onnx,
whisper.cpp/sherpa-onnx exist) — Python remains the reference
implementation.

Why recorded: "Python can't ship as a product" is the assumption a
future reader will bring; the counter-evidence and the deliberate
staging (velocity now, packaging when the UI earns daily use, compile
only under proven pressure) is the decision. Trigger for starting
Tier 1: the web UI earning daily use — the same trigger the Tauri port
already had.
