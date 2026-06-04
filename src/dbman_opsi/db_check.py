"""Parse the spooled output of 04-validate-monitoring-user.sql.

Lets `preflight` turn the otherwise-manual monitoring-user check into a real
pass/fail by ingesting what the DBA actually ran on the database side. The parser
is deliberately tolerant: it scans the spool text for the privilege tokens the
validation script selects, rather than depending on exact SQL*Plus formatting.
"""

from __future__ import annotations

from dataclasses import dataclass

# Database Management Basic monitoring needs a usable session plus dictionary read.
_REQUIRED = ("CREATE SESSION",)
# Any one of these satisfies the dictionary-read requirement.
_ONE_OF = ("SELECT ANY DICTIONARY", "SELECT_CATALOG_ROLE")
_LOCKED_TOKENS = ("LOCKED", "EXPIRED")


@dataclass(frozen=True)
class DbUserCheck:
    account_open: bool
    found: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.account_open and not self.missing


def parse_validation_output(text: str) -> DbUserCheck:
    upper = text.upper()
    account_open = "OPEN" in upper and not any(token in upper for token in _LOCKED_TOKENS)

    found: list[str] = [token for token in _REQUIRED if token in upper]
    missing: list[str] = [token for token in _REQUIRED if token not in upper]

    if any(token in upper for token in _ONE_OF):
        found.append(next(token for token in _ONE_OF if token in upper))
    else:
        missing.append(" or ".join(_ONE_OF))

    return DbUserCheck(account_open=account_open, found=tuple(found), missing=tuple(missing))
