"""Explicit field mappings; unknown historical semantics are never guessed."""

from collections.abc import Sequence

from citibike.contracts import FIELDS, SCHEMA_VERSION
from citibike.storage import identity

SCHEMA_REGISTRY_VERSION = "f2-schemas-1"
MAPPINGS = {SCHEMA_VERSION: {field: field for field in FIELDS}}


def detect(columns: Sequence[str]) -> dict[str, str]:
    if len(columns) != len(set(columns)):
        raise ValueError("Duplicate source fields")
    for version, mapping in MAPPINGS.items():
        if set(columns) == set(mapping):
            return {
                "schema_version": version,
                "field_signature": identity(sorted(columns)),
                "ordered_signature": identity(list(columns)),
                "registry_version": SCHEMA_REGISTRY_VERSION,
            }
    raise ValueError(f"Unknown source schema: {list(columns)}; publication blocked")
