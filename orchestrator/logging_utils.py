"""Shared logging helpers for command-line orchestration tools."""

from __future__ import annotations

import logging
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import IO, Optional, TextIO, Type


@dataclass(frozen=True)
class CommandLogPaths:
    """Resolved paths for one command invocation."""

    command_name: str
    timestamp: str
    date: str
    directory: Path
    stdout_path: Path
    stderr_path: Path
    app_log_path: Path


class TeeTextIO:
    """Write text to an original stream and a log file."""

    def __init__(self, original: TextIO, log_file: IO[str]) -> None:
        self._original = original
        self._log_file = log_file

    def write(self, text: str) -> int:
        written = self._original.write(text)
        self._log_file.write(text)
        return written

    def flush(self) -> None:
        self._original.flush()
        self._log_file.flush()

    @property
    def encoding(self) -> Optional[str]:
        return getattr(self._original, "encoding", None)

    def isatty(self) -> bool:
        return self._original.isatty()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_command_log_paths(
    *,
    base_log_dir: Path | str | None = None,
    command_name: str,
    now: Optional[datetime] = None,
) -> CommandLogPaths:
    """Build the per-stage/per-command log paths for one command run."""
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    timestamp = current.strftime("%Y%m%dT%H%M%SZ")
    date = current.strftime("%Y-%m-%d")
    root_log_dir = Path(base_log_dir) if base_log_dir is not None else Path.cwd() / "logs"
    directory = root_log_dir / "input_staging" / command_name / date
    stem = f"{command_name}.{timestamp}"

    return CommandLogPaths(
        command_name=command_name,
        timestamp=timestamp,
        date=date,
        directory=directory,
        stdout_path=directory / f"{stem}.out",
        stderr_path=directory / f"{stem}.err",
        app_log_path=directory / f"{stem}.log",
    )


class CommandLoggingSession(AbstractContextManager[CommandLogPaths]):
    """Context manager that tees stdout/stderr and writes Python logs to a file."""

    def __init__(
        self,
        *,
        paths: CommandLogPaths,
        log_level: int | str = logging.INFO,
    ) -> None:
        self.paths = paths
        self.log_level = log_level
        self._stdout_file: Optional[IO[str]] = None
        self._stderr_file: Optional[IO[str]] = None
        self._app_log_file: Optional[IO[str]] = None
        self._previous_stdout: Optional[TextIO] = None
        self._previous_stderr: Optional[TextIO] = None
        self._previous_handlers: list[logging.Handler] = []
        self._previous_level: int = logging.NOTSET
        self._file_handler: Optional[logging.Handler] = None

    def __enter__(self) -> CommandLogPaths:
        self.paths.directory.mkdir(parents=True, exist_ok=True)
        self._stdout_file = self.paths.stdout_path.open("a", encoding="utf-8")
        self._stderr_file = self.paths.stderr_path.open("a", encoding="utf-8")
        self._app_log_file = self.paths.app_log_path.open("a", encoding="utf-8")

        self._previous_stdout = sys.stdout
        self._previous_stderr = sys.stderr
        sys.stdout = TeeTextIO(self._previous_stdout, self._stdout_file)  # type: ignore[assignment]
        sys.stderr = TeeTextIO(self._previous_stderr, self._stderr_file)  # type: ignore[assignment]

        root_logger = logging.getLogger()
        self._previous_handlers = list(root_logger.handlers)
        self._previous_level = root_logger.level
        for handler in self._previous_handlers:
            root_logger.removeHandler(handler)

        level = self.log_level
        if isinstance(level, str):
            level = getattr(logging, level.upper())

        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s - %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        self._file_handler = logging.StreamHandler(self._app_log_file)
        self._file_handler.setFormatter(formatter)
        self._file_handler.setLevel(level)
        root_logger.addHandler(self._file_handler)
        root_logger.setLevel(level)
        return self.paths

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        root_logger = logging.getLogger()
        if self._file_handler is not None:
            self._file_handler.flush()
            root_logger.removeHandler(self._file_handler)
        for handler in self._previous_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(self._previous_level)

        if self._previous_stdout is not None:
            sys.stdout = self._previous_stdout
        if self._previous_stderr is not None:
            sys.stderr = self._previous_stderr

        for file_obj in (self._stdout_file, self._stderr_file, self._app_log_file):
            if file_obj is not None:
                file_obj.flush()
                file_obj.close()


def command_logging(
    *,
    base_log_dir: Path | str | None = None,
    command_name: str,
    now: Optional[datetime] = None,
    log_level: int | str = logging.INFO,
) -> CommandLoggingSession:
    """Create a logging session for one command invocation."""
    paths = build_command_log_paths(
        base_log_dir=base_log_dir,
        command_name=command_name,
        now=now,
    )
    return CommandLoggingSession(paths=paths, log_level=log_level)
