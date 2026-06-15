"""Versioned JSON checkpoint envelopes; never reconstruct arbitrary Python objects."""

import json
import math
from typing import Any

from langgraph.types import Interrupt


class CheckpointCodec:
    max_bytes = 1024 * 1024
    max_depth = 32
    tag = "__ed_type__"

    @classmethod
    def encode(cls, obj: Any, depth: int = 0) -> Any:
        if depth > cls.max_depth:
            raise ValueError("Checkpoint exceeds maximum depth")
        if obj is None or type(obj) in (str, int, bool):
            return obj
        if type(obj) is float:
            if not math.isfinite(obj):
                raise ValueError("Non-finite checkpoint value")
            return obj
        if type(obj) is Interrupt:
            if obj.response_schema is not None:
                raise ValueError("Unsupported interrupt response schema")
            return {cls.tag: "interrupt", "value": cls.encode(obj.value, depth + 1), "id": obj.id}
        if type(obj) is tuple:
            return {cls.tag: "tuple", "items": [cls.encode(v, depth + 1) for v in obj]}
        if type(obj) is list:
            return [cls.encode(v, depth + 1) for v in obj]
        if type(obj) is dict:
            if cls.tag in obj or not all(type(k) is str for k in obj):
                raise ValueError("Reserved or non-string checkpoint key")
            return {k: cls.encode(v, depth + 1) for k, v in obj.items()}
        raise TypeError("Checkpoint type is not allowlisted")

    @classmethod
    def decode(cls, obj: Any, depth: int = 0) -> Any:
        if depth > cls.max_depth:
            raise ValueError("Checkpoint exceeds maximum depth")
        if type(obj) is list:
            return [cls.decode(v, depth + 1) for v in obj]
        if type(obj) is dict:
            if cls.tag in obj:
                if (
                    obj[cls.tag] == "interrupt"
                    and set(obj) == {cls.tag, "id", "value"}
                    and type(obj["id"]) is str
                ):
                    return Interrupt(id=obj["id"], value=cls.decode(obj["value"], depth + 1))
                if (
                    obj[cls.tag] == "tuple"
                    and set(obj) == {cls.tag, "items"}
                    and type(obj["items"]) is list
                ):
                    return tuple(cls.decode(v, depth + 1) for v in obj["items"])
                raise ValueError("Invalid checkpoint envelope")
            return {k: cls.decode(v, depth + 1) for k, v in obj.items()}
        if obj is None or type(obj) in (str, int, bool):
            return obj
        if type(obj) is float and math.isfinite(obj):
            return obj
        raise ValueError("Invalid checkpoint primitive")

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        payload = json.dumps(self.encode(obj), allow_nan=False, separators=(",", ":")).encode()
        if len(payload) > self.max_bytes:
            raise ValueError("Checkpoint exceeds byte limit")
        return "ed_json_v1", payload

    @staticmethod
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("Duplicate checkpoint key")
            obj[key] = value
        return obj

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        kind, payload = data
        if kind != "ed_json_v1" or len(payload) > self.max_bytes:
            raise ValueError("Unsupported checkpoint format or size")
        try:
            return self.decode(json.loads(payload, object_pairs_hook=self.unique_object))
        except RecursionError as exc:
            raise ValueError("Checkpoint exceeds maximum depth") from exc
