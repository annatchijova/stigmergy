"""
STIGMERGY — Canonicalization discipline.

Everything that participates in a hash goes through this module first.
Two nodes hashing "the same" payload MUST produce the same bytes, or
forensic integrity is fiction. That requirement dictates every rule here:

  - Keys sorted ascending, no insignificant whitespace, UTF-8.
  - floats are REJECTED, not serialized carefully. A float in an audit
    payload is non-determinism waiting for its moment (platform repr
    differences, accumulated drift upstream, JSON parsers disagreeing
    about 1e-17). Exact values travel as Decimal quantized to the
    canonical scale and serialized as strings.
  - Decimal must arrive already quantized to CANONICAL_SCALE. This
    module verifies; it does not silently re-quantize. Implicit rounding
    at the serialization boundary is exactly the failure mode the schema
    header forbids ("non-determinism smuggled in through the driver").

THIS MODULE IS PART OF THE PROTOCOL, not a convenience. Any writer in
any language must reproduce these exact bytes (sorted keys, no
whitespace, UTF-8 unescaped, fixed-point Decimals as strings) or its
hashes will not verify. A Go or Rust node does not get to use its
standard library's JSON defaults and hope.

Note on rounding modes vs CockroachDB: there is no divergence path,
because the database NEVER rounds. The discipline (schema header) is
that the application quantizes to scale 10 BEFORE insert; storing an
already-at-scale value in DECIMAL(11,10) involves no rounding at all.
CANONICAL_ROUNDING only ever executes here, in one interpreter.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN
from fractions import Fraction
from typing import Any

# Matches DECIMAL(11,10) in schema.sql — single source of truth for scale.
CANONICAL_SCALE = 10
_QUANTUM = Decimal(1).scaleb(-CANONICAL_SCALE)  # Decimal('1E-10')

# One rounding mode, named once, used everywhere. ROUND_HALF_EVEN is
# IEEE 754's default and eliminates the systematic bias of half-up.
CANONICAL_ROUNDING = ROUND_HALF_EVEN


def quantize(value: Fraction | Decimal | int, field: str = "value") -> Decimal:
    """
    Convert an exact value to the canonical Decimal scale, deterministically.

    This is the ONLY place in the codebase where Fraction becomes Decimal.
    The application computes in Fraction (exact), quantizes here (explicit,
    documented rounding), and only then does the value touch SQL or a hash.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise TypeError(f"{field}: bool is not a quantizable numeric value.")
    if isinstance(value, Fraction):
        d = Decimal(value.numerator) / Decimal(value.denominator)
    elif isinstance(value, (Decimal, int)):
        d = Decimal(value)
    else:
        raise TypeError(
            f"{field}: cannot quantize {type(value).__name__}. "
            "Floats are forbidden by design — compute in Fraction."
        )
    return d.quantize(_QUANTUM, rounding=CANONICAL_ROUNDING)


def _canonicalize(obj: Any, path: str) -> Any:
    """Recursively validate and normalize a payload for canonical JSON."""
    if obj is None or isinstance(obj, (str, int)) and not isinstance(obj, bool):
        return obj
    if isinstance(obj, bool):
        # Deliberate asymmetry with quantize(), which REJECTS bool:
        # quantize answers "is this a quantizable NUMBER?" (bool is an
        # int subclass by historical accident, not a number), while this
        # function answers "does this have exactly one canonical JSON
        # form?" — and true/false does. A flag like {"migrated": true}
        # is legitimate payload content.
        return obj
    if isinstance(obj, float):
        raise TypeError(
            f"{path}: float is forbidden in audit payloads. Quantize to "
            f"Decimal (scale {CANONICAL_SCALE}) and it will serialize as a "
            "string, exactly."
        )
    if isinstance(obj, Decimal):
        # Scale is checked via the EXPONENT, not numeric equality —
        # Decimal("0.5") == Decimal("0.5000000000") is True numerically,
        # but they are two representations, and two representations of
        # one value is exactly what canonical form exists to prevent.
        if obj.as_tuple().exponent != -CANONICAL_SCALE:
            raise ValueError(
                f"{path}: Decimal {obj} is not at canonical scale "
                f"{CANONICAL_SCALE}. Quantize explicitly with quantize() — "
                "this module verifies, it never silently re-rounds."
            )
        if not obj.is_finite():
            raise ValueError(f"{path}: non-finite Decimal in audit payload.")
        # format(d, 'f') — NEVER str(d): str emits scientific notation for
        # small magnitudes ('2E-10'), which is a second representation of
        # the same value. Fixed-point, always, exactly scale digits.
        return format(obj, "f")
    if isinstance(obj, dict):
        out = {}
        for key in obj:
            if not isinstance(key, str):
                raise TypeError(f"{path}: JSON object keys must be str, got {type(key).__name__}.")
            out[key] = _canonicalize(obj[key], f"{path}.{key}")
        return out
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(v, f"{path}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, (datetime, date)):
        raise TypeError(
            f"{path}: {type(obj).__name__} is not serializable in an audit "
            "payload. Format it explicitly — for event timestamps use the "
            "chain module's _format_ts (UTC, microseconds, ISO 8601) so "
            "there is exactly one representation."
        )
    raise TypeError(f"{path}: {type(obj).__name__} is not serializable in an audit payload.")


def canonical_json(payload: dict[str, Any]) -> str:
    """
    Serialize a payload to canonical JSON: keys sorted ascending,
    separators without whitespace, non-ASCII preserved as UTF-8
    (ensure_ascii=False — escaping is a representation choice, and two
    representations of one string is exactly what canonical form exists
    to prevent).

    The returned string is what gets hashed AND what gets stored in
    payload_json, so the stored bytes and the hashed bytes cannot drift.
    """
    if not isinstance(payload, dict):
        raise TypeError("Audit payload must be a dict at the top level.")
    normalized = _canonicalize(payload, "payload")
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
