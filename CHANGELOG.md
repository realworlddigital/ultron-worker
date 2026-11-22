# Changelog

Newest first, one line per version. The version lives in `pyproject.toml` only; every merged PR
bumps it (PATCH for hotfixes and bug fixes, MINOR for new capability, MAJOR for contract breaks)
and says which in its description. 0.1.1-0.1.4 are backfilled: those builds still called
themselves 0.1.0.

- **0.2.0** (#7) - One version from `pyproject.toml`, shown in the runner log, status GUI, `--check`, `--version`, the app footer beside the sha, and the backbone heartbeat (`detail.version` + `detail.commit`); folds in #5 (vision_tag records carry `root` + root-relative paths).
- **0.1.4** (#6) - Hotfix: runner survives its log file (share-friendly, retried, stderr fallback), pid lock against rival runners, wider git discovery.
- **0.1.3** (#4) - Fix: desktop app runner spawn env + captured output, robust git resolution, single check-unavailable light.
- **0.1.2** (#3) - Desktop app: pywebview shell, tray, autostart, health lights, manual update.
- **0.1.1** (#2) - vision_tag: strict reply parsing, repair re-ask, failed-item retry, filler-tag filter.
- **0.1.0** (#1) - Ultron local job runner + localhost status GUI (`task.local_job` / `local-ultron`).
