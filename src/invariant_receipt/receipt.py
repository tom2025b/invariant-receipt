"""Receipt schema loading, validation and writing (spec §5)."""

from __future__ import annotations

import json
import os
from functools import cache
from importlib import resources
from pathlib import Path

import jsonschema

RECEIPT_SCHEMA_ID = "invariant-receipt/receipt/1"
SUMMARY_SCHEMA_ID = "invariant-receipt/summary/1"


@cache
def schema(name: str) -> dict:
    text = resources.files("invariant_receipt").joinpath("schema", name).read_text("utf-8")
    return json.loads(text)


def receipt_schema() -> dict:
    return schema("receipt-1.json")


def summary_schema() -> dict:
    return schema("summary-1.json")


def validate_receipt(data: dict) -> None:
    """Raise jsonschema.ValidationError if ``data`` is not a valid receipt."""
    jsonschema.Draft202012Validator(receipt_schema()).validate(data)


def validate_summary(data: dict) -> None:
    jsonschema.Draft202012Validator(summary_schema()).validate(data)


def write_json(path: Path, data: dict) -> None:
    """Write atomically: a reader never sees half a receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
