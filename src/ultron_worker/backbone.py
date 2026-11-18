"""Outbound-only backbone client, scoped in code to the runner's own cards.

The backbone enforces the runner key's scopes; this client enforces them again so a
misconfigured key can't widen what the runner does: it calls a fixed set of paths, only in
its own domain, only on ``task.local_job`` cards routed to ``local-ultron``, and appends only
``ultron.*`` events. Anything else raises :class:`ScopeViolation` before a byte leaves.
"""

from __future__ import annotations

import contextlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import EVENT_PREFIX, EXECUTOR, FORBIDDEN_DOMAINS, TASK_TYPE, Config

USER_AGENT = "ultron-worker/0.1"
TIMEOUT = 30.0
TASK_ID = r"[A-Za-z0-9_\-]+"
# (method, path pattern): the runner's whole API surface.
ALLOWED = (
    ("GET", re.compile(r"/me")),
    ("GET", re.compile(r"/tasks")),
    ("GET", re.compile(rf"/tasks/{TASK_ID}")),
    ("POST", re.compile(rf"/tasks/{TASK_ID}/transition")),
    ("POST", re.compile(rf"/tasks/{TASK_ID}/notes")),
    ("POST", re.compile(r"/events")),
    ("PUT", re.compile(r"/system/status/[A-Za-z0-9_\-]+")),
)


class ScopeViolation(RuntimeError):
    """The runner tried to step outside its own cards. Always a bug, never retried."""


class BackboneUnavailable(RuntimeError):
    """Network down, DNS gone, 5xx, 429: the runner is offline for now, not failing."""


class BackboneError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"backbone {status}: {detail}")
        self.status = status
        self.detail = detail


def is_runner_card(task: dict[str, Any], domain: str) -> bool:
    data = task.get("data") or {}
    return (
        task.get("type") == TASK_TYPE
        and task.get("domain") == domain
        and data.get("executor") == EXECUTOR
    )


class Backbone:
    def __init__(self, cfg: Config) -> None:
        self.base = cfg.backbone_url.rstrip("/") + "/api/v1"
        self.key = cfg.key
        self.actor = cfg.actor
        self.domain = cfg.domain
        if self.domain in FORBIDDEN_DOMAINS:
            raise ScopeViolation(f"domain {self.domain!r} is hard-excluded")
        self._own: set[str] = set()  # card ids seen as runner cards in our domain

    # ---- transport ----------------------------------------------------------
    def _check(
        self, method: str, path: str, params: dict[str, Any] | None, body: dict[str, Any] | None
    ) -> None:
        if not any(m == method and p.fullmatch(path) for m, p in ALLOWED):
            raise ScopeViolation(f"{method} {path} is outside the runner's API surface")
        for value in [*(params or {}).values(), (body or {}).get("domain")]:
            if isinstance(value, str) and value in FORBIDDEN_DOMAINS:
                raise ScopeViolation(f"{method} {path} names hard-excluded domain {value!r}")
        if method == "GET" and path == "/tasks" and (params or {}).get("domain") != self.domain:
            raise ScopeViolation("task listings are always filtered to the runner's domain")
        m = re.fullmatch(rf"/tasks/({TASK_ID})/(transition|notes)", path)
        if m and m.group(1) not in self._own:
            raise ScopeViolation(f"{path}: not a card routed to {EXECUTOR}")
        if path.startswith("/system/status/") and path != f"/system/status/{self.actor}":
            raise ScopeViolation("the runner writes only its own status")
        if (method, path) == ("POST", "/events"):
            body = body or {}
            target = body.get("target") or {}
            if not str(body.get("action", "")).startswith(EVENT_PREFIX):
                raise ScopeViolation("the runner appends only ultron.* events")
            if body.get("domain") != self.domain:
                raise ScopeViolation("runner events stay in the runner's domain")
            foreign = target.get("collection") != "tasks" or target.get("id") not in self._own
            if target and foreign:
                raise ScopeViolation("runner events target only its own cards")

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        self._check(method, path, params, body)
        return self._send(method, path, params, body)

    def _send(
        self, method: str, path: str, params: dict[str, Any] | None, body: dict[str, Any] | None
    ) -> Any:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.key}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (https only)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            with contextlib.suppress(ValueError, AttributeError):
                detail = str(json.loads(detail).get("detail", detail))
            if exc.code >= 500 or exc.code == 429:
                raise BackboneUnavailable(f"backbone {exc.code}: {detail}") from exc
            raise BackboneError(exc.code, detail) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise BackboneUnavailable(str(getattr(exc, "reason", exc))) from exc
        return json.loads(raw) if raw else {}

    # ---- API ----------------------------------------------------------------
    def me(self) -> dict[str, Any]:
        return self.request("GET", "/me")

    def _remember(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        mine = [t for t in tasks if is_runner_card(t, self.domain)]
        self._own.update(t["id"] for t in mine)
        return mine

    def cards(self, status: str) -> list[dict[str, Any]]:
        """Runner cards in ``status``, oldest filed first (the queue order)."""
        out = self.request(
            "GET", "/tasks", params={"domain": self.domain, "status": status, "limit": 200}
        )
        mine = self._remember(list(out.get("items") or []))
        return sorted(mine, key=lambda t: (str(t.get("created_at") or ""), t["id"]))

    def card(self, task_id: str) -> dict[str, Any] | None:
        task = self.request("GET", f"/tasks/{task_id}")
        return task if self._remember([task]) else None

    def transition(self, task_id: str, to: str, note: str | None = None) -> dict[str, Any]:
        return self.request("POST", f"/tasks/{task_id}/transition", body={"to": to, "note": note})

    def note(self, task_id: str, body: str, *, kind: str = "update") -> dict[str, Any]:
        return self.request(
            "POST", f"/tasks/{task_id}/notes", body={"kind": kind, "body": body[:4000]}
        )

    def event(
        self, action: str, task_id: str, summary: str, /, *, severity: str = "routine", **data: Any
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/events",
            body={
                "action": f"{EVENT_PREFIX}{action}",
                "summary": summary[:500],
                "domain": self.domain,
                "severity": severity,
                "target": {"collection": "tasks", "id": task_id},
                "data": data,
            },
        )

    def status(self, *, ok: bool, state: str, detail: dict[str, Any], host: str) -> None:
        self.request(
            "PUT",
            f"/system/status/{self.actor}",
            body={"ok": ok, "state": state, "detail": detail, "host": host},
        )
