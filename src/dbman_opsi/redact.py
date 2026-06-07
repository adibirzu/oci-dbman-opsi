"""Redaction helpers for sensitive OCI topology and secret-shaped values."""

from __future__ import annotations

import hashlib

import re
from collections.abc import Mapping, Sequence
from typing import Any

class _PrivateNamespacePattern:
    """Match private identifier fingerprints without retaining their inputs."""
    def __init__(self, entries, boundary=True):
        self.entries = tuple(entries)
        self.boundary = boundary

    def finditer(self, text):
        cursor = 0
        while cursor < len(text):
            matched = 0
            if not self.boundary or cursor == 0 or not (text[cursor - 1].isalnum() or text[cursor - 1] == "_"):
                for digest, width in self.entries:
                    stop = cursor + width
                    if stop > len(text):
                        continue
                    if self.boundary and stop < len(text) and (text[stop].isalnum() or text[stop] == "_"):
                        continue
                    if hashlib.sha256(text[cursor:stop].lower().encode()).hexdigest() == digest:
                        matched = width
                        break
            if matched:
                yield re.compile(re.escape(text[cursor:cursor + matched])).match(text, cursor)
                cursor += matched
            else:
                cursor += 1

    def search(self, text):
        return next(self.finditer(text), None)

    def sub(self, replacement, text):
        output = []
        cursor = 0
        for match in self.finditer(text):
            output.extend((text[cursor:match.start()], replacement))
            cursor = match.end()
        output.append(text[cursor:])
        return "".join(output)


class _PrivateCombinedPattern:
    def __init__(self, ordinary, private):
        self.ordinary = ordinary
        self.private = private

    def search(self, text):
        matches = [match for match in (self.ordinary.search(text), self.private.search(text)) if match]
        return min(matches, key=lambda match: match.start()) if matches else None


_OCIR_NAMESPACE_PATTERN = (('5764d2ac137cbb20013ad9af899e574470ce26e8dced52a4f168a571f768f62d', 12), ('d2d2deaecc2445e6ffb86631616643d43c19399aeacea2209a30cffb2f2c6ec6', 12), ('fc50c4fceff52b6afec019d07e0a70c7a3a71c62a27f40336bf0ab7dd403d240', 12))
_APM_DOMAIN_PATTERN = (('f402141310e08d15aa58eec2859097725051705f5bc578255ac0d912038374b3', 26),)
_LA_NAMESPACE_PATTERN = (('0f38d94001c2cfab0f32713bd86099b6b2039056f407d168fb2f7a5d26e9fb02', 12),)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"ocid1\.(tenancy|compartment|instance|cluster|networksecuritygroup|"
            r"loadbalancer|subnet|vcn|vnic|bootvolume|loganalytics[a-z]+|user|"
            r"database|autonomousdatabase|pluggabledatabase|dbsystem|vault|key|secret|"
            r"databaseinsight|datasafe[a-z]+|dataguardassociation)\.oc1\.[a-z0-9.-]*",
            re.IGNORECASE,
        ),
        "<OCI_OCID>",
    ),
    (re.compile(r"\b(130\.61|161\.153|144\.24|129\.153|141\.147|82\.77|109\.166)\.[0-9]+\.[0-9]+\b"), "<PUBLIC_IP>"),
    (re.compile(r"\b(10\.42|10\.0\.10)\.[0-9]+\.[0-9]+\b"), "<PRIVATE_IP>"),
    (_PrivateNamespacePattern(_OCIR_NAMESPACE_PATTERN), "${OCIR_TENANCY}"),
    (_PrivateNamespacePattern(_APM_DOMAIN_PATTERN), "<APM_DOMAIN_ID>"),
    (_PrivateNamespacePattern(_LA_NAMESPACE_PATTERN), "<LA_NAMESPACE>"),
    (re.compile(r"\b[a-f0-9]{2}(?::[a-f0-9]{2}){15}\b", re.IGNORECASE), "<KEY_FINGERPRINT>"),
    (re.compile(r"\bisk_[a-f0-9]{40}\b", re.IGNORECASE), "<INTERNAL_SERVICE_KEY>"),
    (re.compile(r"(?<![/A-Za-z0-9+])[A-Za-z0-9+/]{32,}={0,2}(?![/A-Za-z0-9+=])"), "<SECRET_VALUE>"),
    (re.compile(r"/Users/[^/\s]+"), "/Users/<USER>"),
)


def redact_text(value: str) -> str:
    """Return a redacted copy of a log/config string."""

    redacted = value
    for pattern, replacement in _REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_data(value: Any) -> Any:
    """Recursively redact strings in common Python containers."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {key: redact_data(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact_data(item) for item in value]
    return value
