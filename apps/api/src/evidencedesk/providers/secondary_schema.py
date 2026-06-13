"""Explicit provider schema subsets; unsupported constraints stay mandatory in local validation."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from evidencedesk.providers.auxiliary import OutputKind
from evidencedesk.providers.schema import output_format


@dataclass(frozen=True)
class SchemaCompilation:
    schema: dict[str, Any]
    local_constraints: tuple[str, ...]


def compile_schema(kind: OutputKind, provider: Literal["anthropic", "google"]) -> SchemaCompilation:
    return compile_subset(output_format(kind)["schema"], provider)


def compile_subset(
    schema: dict[str, Any], provider: Literal["anthropic", "google"]
) -> SchemaCompilation:
    definitions = schema.get("$defs", {})
    local: list[str] = []
    allowed = {
        "$defs",
        "$ref",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "items",
        "description",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
    }

    def visit(node: dict[str, Any], path: str, seen: frozenset[str]) -> dict[str, Any]:
        if unknown := node.keys() - allowed:
            raise ValueError("Unsupported provider schema keyword: " + ",".join(sorted(unknown)))
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/") or ref in seen:
                raise ValueError("Only acyclic internal schema references are allowed")
            name = ref.removeprefix("#/$defs/")
            if name not in definitions or len(node) != 1:
                raise ValueError("Invalid schema reference")
            return visit(definitions[name], path, seen | {ref})
        result = deepcopy({key: value for key, value in node.items() if key != "$defs"})
        unsupported = {"minLength", "maxLength"}
        if provider == "anthropic":
            unsupported.add("maxItems")
            if result.get("minItems", 0) not in (0, 1):
                unsupported.add("minItems")
        else:
            unsupported.add("pattern")
        notes = []
        for keyword in sorted(unsupported & result.keys()):
            constraint = f"{keyword}={result.pop(keyword)}"
            local.append(path + ":" + constraint)
            notes.append(constraint)
        if notes:
            result["description"] = (
                result.get("description", "")
                + " Local validation requires "
                + ", ".join(notes)
                + "."
            ).strip()
        if result.get("type") == "object" and result.get("additionalProperties") is not False:
            raise ValueError("Project schemas require closed objects")
        if "properties" in result:
            result["properties"] = {
                key: visit(value, path + "." + key, seen)
                for key, value in result["properties"].items()
            }
        if "items" in result:
            result["items"] = visit(result["items"], path + "[]", seen)
        return result

    return SchemaCompilation(visit(schema, "$", frozenset()), tuple(local))
