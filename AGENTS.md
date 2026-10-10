# Repository Guidelines

## Project Structure & Module Organization

AcePoint is a Python 3.10+ badminton gesture-scoring application using a `src` layout. Core code lives in `src/acepoint/`: scoring and recognition logic is in `engine.py`, `fsm.py`, `rules.py`, and `state.py`; input backends are under `sources/`; display backends and the web template are under `displays/`. Tests mirror these areas in `tests/test_*.py`. Use `scripts/` for model setup, camera probes, profiling, and gesture calibration. `config.toml` targets recorded videos and the web display; `config.pi.toml` targets Raspberry Pi hardware. Treat `legacy/badminton_pi4.py` as read-only reference. Large generated media and model files belong in `videos/` and `models/` and are ignored by Git.

## Build, Test, and Development Commands

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"                # editable install plus pytest/tools
python scripts/fetch_model.py           # fetch MediaPipe model once
acepoint                                 # run development config on port 8080
acepoint --check                         # validate model, sources, and dependencies
acepoint --display console --duration 20 # hardware-free smoke test
pytest                                   # run the complete suite
pytest tests/test_fsm.py::test_cooldown_blocks_everything
```

On Raspberry Pi, install with `pip install -e ".[pi]"` and run `acepoint -c config.pi.toml`.

## Coding Style & Naming Conventions

Follow existing Python style: four-space indentation, type hints, concise docstrings, and `snake_case` for modules, functions, variables, and test names. Use `PascalCase` for classes and `UPPER_CASE` for constants. Keep hardware imports lazy so all modules remain importable on Windows. Store tunable capture, recognition, and scoring values in TOML rather than module-level constants. No formatter or linter is currently configured; keep edits consistent with neighboring code.

## Testing Guidelines

Tests use pytest. Name files `test_<area>.py` and cases `test_<behavior>`. Add focused regression tests for changes to timing, scoring rules, configuration validation, web behavior, and video wall-clock synchronization. Run the full suite before submitting. After changing `poll_hz` or any `hold_*` value, also run `python scripts/check_sampling.py`.

## Commit & Pull Request Guidelines

Git history is not present in this checkout, so no established commit convention can be verified. Use short, imperative subjects such as `Fix video frame synchronization`, and keep each commit focused. Pull requests should explain the user-visible effect, list tests and calibration commands run, link relevant issues or requirement sections, and include screenshots for dashboard changes. Call out configuration or Raspberry Pi hardware impacts explicitly.
