"""Minimal localhost-only Ollama client (the runner is Ollama's only caller)."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import is_loopback_host

TIMEOUT = 900.0  # one vision call on a big model can take minutes on CPU


class OllamaError(RuntimeError):
    """Ollama refused or failed one request (bad model, bad image...)."""


class OllamaModelMissing(OllamaError):
    """The model isn't pulled on this machine: every item would fail, so the job stops."""


class OllamaUnavailable(OllamaError):
    """Ollama isn't answering at all: a job-level, retryable failure."""


class Ollama:
    def __init__(self, url: str) -> None:
        if not is_loopback_host(urlparse(url).hostname):
            raise ValueError("Ollama must be reached on localhost only")
        self.url = url.rstrip("/")

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 (loopback)
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 404:
                model = body.get("model")
                raise OllamaModelMissing(
                    f"ollama has no model {model!r} ({detail}); run `ollama pull {model}` on Ultron"
                ) from exc
            raise OllamaError(f"ollama {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            raise OllamaUnavailable(
                f"ollama unreachable at {self.url}: {getattr(exc, 'reason', exc)}"
            ) from exc

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        images: list[Path] | None = None,
        json_format: bool = False,
        options: dict[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
        if images:
            body["images"] = [base64.b64encode(p.read_bytes()).decode() for p in images]
        if json_format:
            body["format"] = "json"
        if options:
            body["options"] = options
        return str(self._post("/api/generate", body).get("response", ""))

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        out = self._post("/api/embed", {"model": model, "input": texts})
        vectors = out.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OllamaError("ollama /api/embed returned no embeddings")
        return vectors
