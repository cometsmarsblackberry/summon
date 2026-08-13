"""Reservation-owner server command policy and plugin response parsing.

The SourceMod KeyValues file is intentionally the only allowlist.  This module
parses its deliberately small schema strictly so a typo or partial deploy
disables the web command interface instead of widening it.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.utils.maps import is_valid_map_name


OWNER_COMMAND_CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "tf2-reservation-plugin"
    / "configs"
    / "summon_owner_commands.cfg"
)
OWNER_COMMAND_RESULT_MARKER = "SUMMON_OWNER_COMMAND_RESULT"
MAX_COMMAND_BYTES = 511
MAX_COMMAND_NAME_BYTES = 63
MAX_OUTPUT_BYTES = 4096
_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")


class OwnerCommandAllowlistError(RuntimeError):
    """The shared command allowlist is absent or invalid."""


class OwnerCommandValidationError(ValueError):
    """A submitted command line violates the backend policy."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PluginCommandResult:
    ok: bool
    output: str
    error_code: str | None = None


@dataclass(frozen=True)
class _CacheEntry:
    path: Path
    signature: tuple[int, int]
    commands: tuple[str, ...] | None
    error: str | None


_cache_lock = threading.Lock()
_cache: _CacheEntry | None = None


def _tokenize_keyvalues(source: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if character in "{}":
            tokens.append(character)
            index += 1
            continue
        if character != '"':
            raise OwnerCommandAllowlistError(
                f"unexpected character at offset {index}"
            )
        index += 1
        value: list[str] = []
        while index < length:
            character = source[index]
            if character == '"':
                index += 1
                tokens.append("".join(value))
                break
            if character == "\\":
                # Escapes are unnecessary for command identifiers and can be
                # interpreted differently by KeyValues implementations.
                raise OwnerCommandAllowlistError("escaped strings are not allowed")
            if character in "\r\n" or unicodedata.category(character) == "Cc":
                raise OwnerCommandAllowlistError("control character in quoted key")
            value.append(character)
            index += 1
        else:
            raise OwnerCommandAllowlistError("unterminated quoted string")
    return tokens


def parse_owner_command_allowlist(source: str) -> tuple[str, ...]:
    """Parse the exact ``root { "command" {} ... }`` schema."""
    tokens = _tokenize_keyvalues(source)
    if len(tokens) < 3 or tokens[0] != "SummonOwnerCommands" or tokens[1] != "{":
        raise OwnerCommandAllowlistError("invalid SummonOwnerCommands root")

    commands: list[str] = []
    seen: set[str] = set()
    index = 2
    while index < len(tokens) and tokens[index] != "}":
        command = tokens[index]
        index += 1
        if command in {"{", "}"}:
            raise OwnerCommandAllowlistError("missing command section name")
        encoded = command.encode("utf-8")
        if (
            not encoded
            or len(encoded) > MAX_COMMAND_NAME_BYTES
            or _COMMAND_NAME_RE.fullmatch(command) is None
        ):
            raise OwnerCommandAllowlistError(f"invalid command name: {command!r}")
        normalized = command.lower()
        if normalized in seen:
            raise OwnerCommandAllowlistError(
                f"duplicate command name: {command!r}"
            )
        seen.add(normalized)
        if index + 1 >= len(tokens) or tokens[index : index + 2] != ["{", "}"]:
            raise OwnerCommandAllowlistError(
                f"command section must be empty: {command!r}"
            )
        index += 2
        commands.append(normalized)

    if index >= len(tokens) or tokens[index] != "}" or index != len(tokens) - 1:
        raise OwnerCommandAllowlistError("malformed or trailing KeyValues data")
    if not commands:
        raise OwnerCommandAllowlistError("owner command allowlist is empty")
    return tuple(commands)


def clear_owner_command_allowlist_cache() -> None:
    """Clear cached policy state (primarily useful for tests and reload tools)."""
    global _cache
    with _cache_lock:
        _cache = None


def get_owner_commands(path: Path | None = None) -> tuple[str, ...]:
    """Return the allowlist, reloading whenever the file mtime or size changes."""
    global _cache
    selected = Path(path or OWNER_COMMAND_CONFIG_PATH)
    try:
        stat = selected.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        raise OwnerCommandAllowlistError(
            f"owner command allowlist is unavailable: {exc}"
        ) from exc

    with _cache_lock:
        if (
            _cache is not None
            and _cache.path == selected
            and _cache.signature == signature
        ):
            if _cache.commands is None:
                raise OwnerCommandAllowlistError(
                    _cache.error or "owner command allowlist is invalid"
                )
            return _cache.commands

        try:
            # A descriptor-based read ensures the content belongs to the file
            # we stat again below; a concurrent replacement simply retries on
            # the next request under its new signature.
            with selected.open("r", encoding="utf-8") as handle:
                source = handle.read()
                read_stat = os.fstat(handle.fileno())
            read_signature = (read_stat.st_mtime_ns, read_stat.st_size)
            commands = parse_owner_command_allowlist(source)
            _cache = _CacheEntry(selected, read_signature, commands, None)
            return commands
        except (OSError, UnicodeError, OwnerCommandAllowlistError) as exc:
            _cache = _CacheEntry(selected, signature, None, str(exc))
            raise OwnerCommandAllowlistError(str(exc)) from exc


def validate_owner_command_line(
    value: str, commands: tuple[str, ...] | None = None
) -> str:
    """Normalize and validate a single submitted owner command line."""
    if not isinstance(value, str):
        raise OwnerCommandValidationError("invalid_command", "Command must be text.")
    command = value.strip()
    if not command:
        raise OwnerCommandValidationError("empty_command", "Enter a command.")
    if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise OwnerCommandValidationError(
            "command_too_long", "Command must be at most 511 bytes."
        )
    if ";" in command:
        raise OwnerCommandValidationError(
            "unsafe_command", "Command separators are not allowed."
        )
    if any(unicodedata.category(character) == "Cc" for character in command):
        raise OwnerCommandValidationError(
            "unsafe_command", "Control characters are not allowed."
        )

    operation = command.split(" ", 1)[0]
    if (
        not operation
        or len(operation.encode("utf-8")) > MAX_COMMAND_NAME_BYTES
        or _COMMAND_NAME_RE.fullmatch(operation) is None
    ):
        raise OwnerCommandValidationError(
            "invalid_command_name", "The command name is invalid."
        )
    allowed = commands if commands is not None else get_owner_commands()
    if operation.lower() not in set(allowed):
        raise OwnerCommandValidationError(
            "command_not_allowed", "That server command is not allowed."
        )
    if operation.lower() == "changelevel":
        arguments = [argument for argument in command.split(" ")[1:] if argument]
        if len(arguments) != 1 or not is_valid_map_name(arguments[0]):
            raise OwnerCommandValidationError(
                "invalid_arguments",
                "Changelevel requires exactly one valid map name.",
            )
    return command


def truncate_command_output(value: object, limit: int = MAX_OUTPUT_BYTES) -> str:
    """Return valid UTF-8 bounded to the protocol's output budget."""
    text = "" if value is None else str(value)
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def parse_plugin_command_result(output: object) -> PluginCommandResult | None:
    """Parse the updated plugin's result marker and subsequent output.

    ``None`` means the RCON request reached something that is not the expected
    restricted command interface (normally an old or missing plugin).

    SourceMod log messages emitted while the wrapped command runs can precede
    the marker in the same RCON response, so ignore that preamble.  Output is
    accepted only after the plugin's marker has still been observed.
    """
    text = "" if output is None else str(output)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    prefix = OWNER_COMMAND_RESULT_MARKER + " "
    marker_index = next(
        (index for index, line in enumerate(lines) if line.startswith(prefix)),
        None,
    )
    if marker_index is None:
        return None
    header = lines[marker_index]
    status = header[len(prefix) :]
    body = truncate_command_output("\n".join(lines[marker_index + 1 :]).strip())
    if status == "OK":
        return PluginCommandResult(ok=True, output=body)
    error_prefix = "ERROR "
    if status.startswith(error_prefix):
        code = status[len(error_prefix) :]
        if _ERROR_CODE_RE.fullmatch(code):
            return PluginCommandResult(ok=False, output=body, error_code=code)
    return None
