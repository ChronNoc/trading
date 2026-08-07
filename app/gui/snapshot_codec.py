"""Lossless JSON codec for :class:`AppSnapshot` across the process boundary.

Process isolation means the GUI no longer shares memory with the backend: the
backend serializes each snapshot to JSON and the GUI reconstructs the SAME
typed object tree (``Decimal`` stays ``Decimal``, enums stay enums), so every
screen renders identically whether the snapshot came from memory or from disk.

Decoding is driven by the dataclasses' type hints, not by guessing from values,
so a string field that looks numeric can never silently become a number and a
``Decimal`` field can never silently become a float.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from decimal import Decimal
from enum import Enum

from app.gui.view_models import AppSnapshot

SCHEMA_VERSION = 4


def encode_snapshot(snapshot: AppSnapshot) -> str:
    """Serialize a snapshot to a JSON document (money as strings, never floats)."""
    return json.dumps(
        {"schema_version": SCHEMA_VERSION, "snapshot": _encode(snapshot)},
        separators=(",", ":"),
    )


def decode_snapshot(payload: str) -> AppSnapshot:
    """Reconstruct the typed snapshot tree from :func:`encode_snapshot` output."""
    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError("snapshot document must be an object")
    if set(document) != {"schema_version", "snapshot"}:
        raise ValueError("snapshot document fields do not match the versioned schema")
    version = int(document.get("schema_version", 0))
    if version != SCHEMA_VERSION:
        raise ValueError(f"snapshot schema {version} != supported {SCHEMA_VERSION}")
    return _decode(AppSnapshot, document["snapshot"])


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _encode(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _decode(hint: object, raw: object) -> object:
    origin = typing.get_origin(hint)
    # Optional[X] / X | None
    if origin in (typing.Union, types.UnionType):
        arguments = [a for a in typing.get_args(hint) if a is not type(None)]
        if raw is None:
            return None
        if not arguments:
            raise ValueError(f"unsupported union type {hint!r}")
        return _decode(arguments[0], raw)
    if hint is Decimal:
        if not isinstance(raw, str):
            raise ValueError("Decimal fields must be JSON strings")
        return Decimal(str(raw))
    if isinstance(hint, type) and issubclass(hint, Enum):
        if not isinstance(raw, str):
            raise ValueError(f"enum {hint.__name__} must be a string")
        return hint(raw)
    if dataclasses.is_dataclass(hint):
        if not isinstance(raw, dict):
            raise ValueError(f"{hint.__name__} must be an object")
        hints = typing.get_type_hints(hint)
        expected = {field.name for field in dataclasses.fields(hint)}
        actual = set(raw)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(
                f"{hint.__name__} fields mismatch; missing={missing}, extra={extra}",
            )
        kwargs = {
            field.name: _decode(hints[field.name], raw[field.name])
            for field in dataclasses.fields(hint)
        }
        return hint(**kwargs)  # type: ignore[misc]
    if origin is tuple:
        arguments = typing.get_args(hint)
        if not isinstance(raw, list):
            raise ValueError("tuple fields must be JSON arrays")
        items = raw
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(_decode(arguments[0], item) for item in items)
        if len(items) != len(arguments):
            raise ValueError(
                f"fixed tuple length {len(items)} != expected {len(arguments)}",
            )
        return tuple(_decode(argument, item) for argument, item in zip(arguments, items))
    if hint is bool:
        if type(raw) is not bool:
            raise ValueError("boolean field must be true or false")
        return raw
    if hint is str:
        if not isinstance(raw, str):
            raise ValueError("string field must be a string")
        return raw
    if hint is float:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("float field must be a JSON number")
        return float(raw)
    if hint is int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError("integer field must be a JSON integer")
        return raw
    return raw
