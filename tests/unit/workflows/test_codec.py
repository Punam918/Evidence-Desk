import pytest
from evidencedesk.workflows.checkpoint_codec import CheckpointCodec
from langgraph.types import Interrupt


def test_framework_envelopes_roundtrip():
    codec = CheckpointCodec()
    obj = {
        "pending": (Interrupt(id="approval", value={"execution_id": "reference"}),),
        "versions": {"node": 1},
    }
    assert codec.loads_typed(codec.dumps_typed(obj)) == obj


@pytest.mark.parametrize(
    "obj", [object(), float("nan"), float("inf"), {1: "invalid"}, {"__ed_type__": "pickle"}]
)
def test_reject_unapproved_objects(obj):
    with pytest.raises((ValueError, TypeError)):
        CheckpointCodec().dumps_typed(obj)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"__ed_type__":"module","path":"os.system"}',
        b'{"x":1,"x":2}',
        b"NaN",
        b'{"__ed_type__":null}',
        b'{"__ed_type__":"interrupt","id":42,"value":null}',
    ],
)
def test_reject_tampered_payloads(payload):
    with pytest.raises(ValueError):
        CheckpointCodec().loads_typed(("ed_json_v1", payload))


def test_size_depth_and_pickle_limits():
    codec = CheckpointCodec()
    with pytest.raises(ValueError):
        codec.loads_typed(("pickle", b"unsafe"))
    with pytest.raises(ValueError):
        codec.dumps_typed("x" * codec.max_bytes)
    nested = None
    for _ in range(34):
        nested = [nested]
    with pytest.raises(ValueError):
        codec.dumps_typed(nested)
