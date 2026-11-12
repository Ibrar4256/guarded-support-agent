"""Reading typed settings from the environment. Every reader names the variable in its error,
so a refused startup says exactly what to fix."""

from collections.abc import Mapping
from datetime import timedelta


def required(environ: Mapping[str, str], name: str, error: type[Exception]) -> str:
    value = environ.get(name, "")
    if not value:
        raise error(f"{name} is not set")
    return value


def seconds(environ: Mapping[str, str], name: str, error: type[Exception]) -> timedelta:
    raw = required(environ, name, error)
    try:
        return timedelta(seconds=float(raw))
    except ValueError as exc:
        raise error(f"{name} must be a number of seconds, got {raw!r}") from exc


def count(environ: Mapping[str, str], name: str, error: type[Exception]) -> int:
    raw = required(environ, name, error)
    try:
        return int(raw)
    except ValueError as exc:
        raise error(f"{name} must be a whole number, got {raw!r}") from exc
