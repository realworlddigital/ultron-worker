"""vision_tag output hardening: strict parsing, one repair re-ask, unusable items checkpointed as
failed (never as tag records) and re-run on a later attempt, filler tags dropped."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ultron_worker.jobs.vision import PROMPT, TagParseError, is_tag_record, parse_tags

from .conftest import FakeOllama

# Replies of the shapes behind the ~9/171 malformed qwen2.5vl:7b records of the Nov 19-20
# bake-off (empty caption, "tags" holding one raw-text string). The old fallback split any
# reply that failed json.loads on commas/newlines and kept the pieces as tags, so each of
# these became {"caption": "", "tags": ["<the reply text>"]}.
GARBAGE = {
    "truncated_json": '{"caption": "A man in a black shirt speaking on stage"',
    "runaway_whitespace": "{\n\n\n\n\n\n      \n\n",
    "plain_prose": "A man sitting at a desk and looking at a laptop.",
    "tags_bare_string": '{"description": "Two people talking", "tags": "conversation"}',
    "empty_caption": '{"caption": "", "tags": ["microphone", "stage"]}',
    "json_in_tags": '{"caption": "", "tags": ["{\\"caption\\": \\"a man at a podium\\"}"]}',
    "caption_holds_json": '{"caption": "{\\"caption\\": \\"a man\\", \\"tags\\": []}", '
    '"tags": ["podium"]}',
    "not_an_object": '["podium", "microphone"]',
    "only_filler": '{"caption": "A man indoors.", "tags": ["man", "indoor", "one person"]}',
    "empty": "",
}
GOOD = json.dumps(
    {
        "caption": "A coach holds a GUILD HALL banner beside a whiteboard.",
        "tags": ["banner", "whiteboard", "guild hall", "coaching"],
    }
)


@pytest.mark.parametrize("raw", GARBAGE.values(), ids=GARBAGE.keys())
def test_garbage_replies_never_parse(raw: str) -> None:
    with pytest.raises(TagParseError):
        parse_tags(raw, 25)


def test_parse_accepts_wrapped_json_and_drops_filler() -> None:
    reply = (
        "```json\n"
        '{"caption": "  A coach   at a whiteboard. ", "tags": ["Man", "one person", '
        '"two people", "indoor", "serious", "focus", "#Whiteboard", "whiteboard", '
        '"marker pen", 7, "{bad", "dry erase marker for writing on the whiteboard board"]}'
        "\n``` hope this helps, {not json}"
    )
    caption, tags = parse_tags(reply, 25)
    assert caption == "A coach at a whiteboard."
    assert tags == ["whiteboard", "marker pen"]


def test_parse_caps_tags() -> None:
    reply = json.dumps({"caption": "Kids on a dock.", "tags": [f"t{i}" for i in range(30)]})
    assert len(parse_tags(reply, 12)[1]) == 12


def test_prompt_asks_for_concrete_tags_and_bans_filler() -> None:
    text = PROMPT.format(max_tags=25)
    assert "mood," not in text and "number of people" not in text
    for banned in ("man", "person", "one person", "people", "indoor", "serious", "focus"):
        assert banned in text.split("Never tag", 1)[1]
    assert "readable text" in text


def test_is_tag_record_rejects_old_garbage_shapes() -> None:
    assert is_tag_record({"path": "a", "caption": "A dock.", "tags": ["dock"]})
    assert is_tag_record({"path": "a", "skipped": "ffmpeg missing"})
    assert not is_tag_record({"path": "a", "caption": "", "tags": ["{"]})
    assert not is_tag_record({"path": "a", "caption": "A dock.", "tags": "dock"})
    assert not is_tag_record({"path": "a", "caption": "x", "tags": ['{"caption": "a man']})
    assert not is_tag_record({"path": "a", "failed": "unusable reply"})


class ScriptedOllama(FakeOllama):
    """Replies per image name from a queue; falls back to a good reply."""

    def __init__(self, script: dict[str, list[str]]) -> None:
        super().__init__()
        self.script = script

    def generate(self, model, prompt, *, images=None, json_format=False, options=None) -> str:
        super().generate(model, prompt, images=images, json_format=json_format, options=options)
        queue = self.script.get(Path(images[0]).stem, []) if images else []
        return queue.pop(0) if queue else GOOD


def _lines(out: Path) -> list[dict]:
    return [json.loads(x) for x in out.read_text().splitlines()]


def _calls_for(ollama: FakeOllama, stem: str) -> list[dict]:
    return [c for c in ollama.calls if Path(c["images"][0]).stem == stem]


def test_one_repair_reask_recovers_the_item(make, library, tmp_path) -> None:
    ollama = ScriptedOllama({"b": [GARBAGE["truncated_json"]]})
    runner, bb, _ = make(ollama=ollama)
    out = tmp_path / "tags.jsonl"
    bb.add("t1", inputs=[str(library)], output=str(out))
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    calls = _calls_for(ollama, "b")
    assert len(calls) == 2 and "could not be used" in calls[1]["prompt"]
    assert "A man in a black shirt" in calls[1]["prompt"]  # the bad reply is quoted back
    assert len(ollama.calls) == 6
    assert all(is_tag_record(r) and r["caption"] for r in _lines(out))


def test_still_unparseable_item_is_failed_then_retried_from_checkpoint(
    make, library, tmp_path
) -> None:
    ollama = ScriptedOllama({"c": [GARBAGE["runaway_whitespace"], GARBAGE["plain_prose"]]})
    runner, bb, _ = make(ollama=ollama)
    out = tmp_path / "tags.jsonl"
    bb.add("t1", inputs=[str(library)], output=str(out))
    runner.tick()
    job = runner.store.job("t1")
    assert job["status"] == "retry_wait" and job["done"] == 4
    assert "1 item(s) failed" in job["error"] and "ultron.retry" in bb.actions("t1")
    rows = _lines(out)  # written with the item marked failed, never as a tag record
    [bad] = [r for r in rows if Path(r["path"]).stem == "c"]
    assert set(bad) == {"path", "failed"} and "repair" in bad["failed"]
    assert sum(1 for r in rows if is_tag_record(r)) == 4

    runner.tick()  # next attempt: only the failed item goes back to the model
    assert bb.tasks["t1"]["status"] == "done"
    assert len(_calls_for(ollama, "c")) == 3 and len(ollama.calls) == 7
    rows = _lines(out)
    assert len(rows) == 5 and all(is_tag_record(r) and r["caption"] for r in rows)
    assert "5 tagged, 0 skipped" in bb.notes[-1][1]["body"]


def test_item_that_never_parses_ends_in_needs_david_and_redispatch_retries_it(
    make, library, tmp_path
) -> None:
    ollama = ScriptedOllama({"d": [GARBAGE["empty_caption"]] * 6})
    runner, bb, _ = make(ollama=ollama)
    out = tmp_path / "tags.json"
    bb.add("t1", inputs=[str(library)], output=str(out))
    for _ in range(3):  # max_attempts
        runner.tick()
    assert bb.tasks["t1"]["status"] == "needs_david"
    doc = json.loads(out.read_text())
    assert doc["tagged"] == 4 and doc["failed"] == 1
    assert not any(r.get("caption") == "" or isinstance(r.get("tags"), str) for r in doc["items"])
    assert len(_calls_for(ollama, "d")) == 6 and len(ollama.calls) == 10

    bb.tasks["t1"]["status"] = "dispatched"  # David re-dispatches
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    assert len(_calls_for(ollama, "d")) == 7 and len(ollama.calls) == 11
    assert json.loads(out.read_text())["failed"] == 0


def test_garbage_checkpointed_by_an_older_runner_is_rerun(make, library, tmp_path) -> None:
    ollama = ScriptedOllama({})
    runner, bb, _ = make(ollama=ollama)
    out = tmp_path / "tags.jsonl"
    bb.add("t1", status="in_progress", inputs=[str(library)], output=str(out))
    runner.store.start_job(
        "t1",
        title="t",
        kind="vision_tag",
        model="llava:13b",
        output=str(out),
        spec=json.dumps({"kind": "vision_tag", "model": "llava:13b", "params": {}}, sort_keys=True),
        fresh_dispatch=False,
    )
    old = {"path": str(library / "a.png"), "media": "image", "caption": "", "tags": ["{"]}
    runner.store.checkpoint("t1", str(library / "a.png"), old, done=1, current_item=None)
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    assert len(_calls_for(ollama, "a")) == 1 and len(ollama.calls) == 5
    assert all(is_tag_record(r) and r["caption"] for r in _lines(out))


def test_retry_pass_of_many_failed_items_is_not_treated_as_a_broken_job(
    make, library, tmp_path
) -> None:
    from .conftest import PNG

    for i in range(5):  # sort before the scripted failures, so attempt 1 has successes
        (library / f"0{i}.png").write_bytes(PNG)
    bad = ["a", "b", "c", "d", "e"]
    ollama = ScriptedOllama({s: [GARBAGE["plain_prose"]] * 4 for s in bad})
    runner, bb, _ = make(ollama=ollama)
    out = tmp_path / "tags.jsonl"
    bb.add("t1", inputs=[str(library)], output=str(out))
    runner.tick()
    runner.tick()  # retry pass: all five fail again, back to back, with no success this run
    job = runner.store.job("t1")
    assert job["status"] == "retry_wait" and "5 item(s) failed" in job["error"]
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    assert all(is_tag_record(r) for r in _lines(out)) and len(_lines(out)) == 10
