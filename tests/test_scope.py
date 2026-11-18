"""The runner key reads only its own cards and writes only their results; care, household and
finance are hard-excluded (client-side, on top of the backbone's own scopes)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ultron_worker.backbone import ScopeViolation, is_runner_card
from ultron_worker.config import FORBIDDEN_DOMAINS, ConfigError, validate

from .conftest import FakeBackbone

HARD_EXCLUDED = ("care", "household", "finance")


def test_hard_excluded_domains_are_in_the_forbidden_set() -> None:
    assert set(HARD_EXCLUDED) <= FORBIDDEN_DOMAINS


@pytest.mark.parametrize("domain", HARD_EXCLUDED)
def test_config_refuses_excluded_domain(cfg, domain) -> None:
    with pytest.raises(ConfigError, match="hard-excluded"):
        validate(replace(cfg, domain=domain))


@pytest.mark.parametrize("domain", HARD_EXCLUDED)
def test_client_refuses_excluded_domain(cfg, domain) -> None:
    with pytest.raises((ConfigError, ScopeViolation)):
        FakeBackbone(replace(cfg, domain=domain))
    bad = object.__new__(type(cfg))  # even a Config that skipped validation
    for f in cfg.__dataclass_fields__:
        object.__setattr__(bad, f, getattr(cfg, f))
    object.__setattr__(bad, "domain", domain)
    with pytest.raises(ScopeViolation):
        FakeBackbone(bad)


@pytest.mark.parametrize("domain", HARD_EXCLUDED)
def test_client_never_queries_or_writes_excluded_domains(cfg, domain) -> None:
    bb = FakeBackbone(cfg)
    with pytest.raises(ScopeViolation):
        bb.request("GET", "/tasks", params={"domain": domain, "status": "dispatched"})
    with pytest.raises(ScopeViolation):
        bb.request(
            "POST", "/events", body={"action": "ultron.progress", "domain": domain, "summary": "x"}
        )
    assert bb.sent == []


@pytest.mark.parametrize("domain", HARD_EXCLUDED)
def test_excluded_domain_cards_are_never_seen_or_run(make, library, tmp_path, domain) -> None:
    runner, bb, ollama = make()
    bb.add("x1", domain=domain, inputs=[str(library)], output=str(tmp_path / "o.jsonl"))
    runner.tick()
    assert bb.tasks["x1"]["status"] == "dispatched" and ollama.calls == []
    assert all(p != "/tasks/x1/transition" for _, p in bb.sent)


def test_only_local_ultron_job_cards_are_runner_cards() -> None:
    base = {"type": "task.local_job", "domain": "compute", "data": {"executor": "local-ultron"}}
    assert is_runner_card(base, "compute")
    assert not is_runner_card({**base, "type": "task.dev_build"}, "compute")
    assert not is_runner_card({**base, "type": "task.generic"}, "compute")
    assert not is_runner_card({**base, "data": {"executor": "devin"}}, "compute")
    assert not is_runner_card({**base, "data": {}}, "compute")
    assert not is_runner_card({**base, "domain": "dev"}, "compute")


def test_foreign_cards_in_the_listing_are_ignored(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    kw = {"inputs": [str(library)], "output": str(tmp_path / "o.jsonl")}
    bb.add("dev1", type_="task.dev_build", executor="devin", **kw)
    bb.add("gen1", type_="task.generic", **kw)
    bb.add("cdx1", executor="codex-cloud", **kw)
    runner.tick()
    assert ollama.calls == [] and bb.transitions == [] and bb.events == []


def test_writes_only_to_own_cards(make) -> None:
    runner, bb, _ = make()
    bb.add("mine")
    bb.add("dev1", type_="task.dev_build", executor="devin")
    bb.cards("dispatched")
    with pytest.raises(ScopeViolation):
        bb.transition("dev1", "in_progress")
    with pytest.raises(ScopeViolation):
        bb.note("dev1", "hello")
    with pytest.raises(ScopeViolation):
        bb.event("progress", "dev1", "x")
    with pytest.raises(ScopeViolation):
        bb.request(
            "POST",
            "/events",
            body={"action": "gilfoyle.dispatch", "domain": "compute", "summary": "x"},
        )
    with pytest.raises(ScopeViolation):
        bb.request("PUT", "/system/status/gilfoyle", body={"ok": True})
    bb.transition("mine", "in_progress")
    assert bb.tasks["mine"]["status"] == "in_progress"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/records"),
        ("POST", "/tasks"),
        ("GET", "/registry/actors"),
        ("PUT", "/system/kill-switch"),
        ("POST", "/records"),
        ("PATCH", "/tasks/t1"),
        ("GET", "/hall/inbox"),
        ("GET", "/events"),
    ],
)
def test_fixed_api_surface(cfg, method, path) -> None:
    bb = FakeBackbone(cfg)
    with pytest.raises(ScopeViolation):
        bb.request(method, path)
    assert bb.sent == []


def test_backbone_must_be_https(cfg) -> None:
    with pytest.raises(ConfigError, match="https"):
        validate(replace(cfg, backbone_url="http://backbone.example"))
    validate(replace(cfg, backbone_url="http://127.0.0.1:8080"))  # local dev backbone is fine


def test_ollama_and_gui_stay_on_localhost(cfg) -> None:
    with pytest.raises(ConfigError):
        validate(replace(cfg, ollama_url="http://192.168.1.20:11434"))
    with pytest.raises(ConfigError):
        validate(replace(cfg, gui_host="0.0.0.0"))
