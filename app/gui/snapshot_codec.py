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

SCHEMA_VERSION = 1


def encode_snapshot(snapshot: AppSnapshot) -> str:
    """Serialize a snapshot to a JSON document (money as strings, never floats)."""
    return json.dumps(
        {"schema_version": SCHEMA_VERSION, "snapshot": _encode(snapshot)},
        separators=(",", ":"),
    )


def decode_snapshot(payload: str) -> AppSnapshot:
    """Reconstruct the typed snapshot tree from :func:`encode_snapshot` output."""
    document = json.loads(payload)
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
        return _decode(arguments[0], raw)
    if hint is Decimal:
        return Decimal(str(raw))
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint(raw)
    if dataclasses.is_dataclass(hint):
        hints = typing.get_type_hints(hint)
        kwargs = {
            field.name: _decode(hints[field.name], raw[field.name])  # type: ignore[index]
            for field in dataclasses.fields(hint)
            if isinstance(raw, dict) and field.name in raw
        }
        return hint(**kwargs)  # type: ignore[misc]
    if origin is tuple:
        arguments = typing.get_args(hint)
        items = raw if isinstance(raw, list) else []
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(_decode(arguments[0], item) for item in items)
        return tuple(_decode(argument, item) for argument, item in zip(arguments, items))
    if hint is float and raw is not None:
        return float(raw)  # type: ignore[arg-type]
    if hint is int and raw is not None:
        return int(raw)  # type: ignore[arg-type]
    return raw
