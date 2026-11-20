"""Ultron desktop app: a native shell (pywebview over WebView2, plus a tray icon) that owns the
runner process, shows the status GUI with Start/Stop/Restart, a log tail and health lights, and
offers one-click updates David confirms. The runner engine itself is unchanged: the shell spawns
``python -m ultron_worker --config ... --stop-on-stdin-close`` from the install's venv.
"""
