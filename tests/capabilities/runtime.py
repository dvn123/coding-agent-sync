from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


class CommandTimeout(TimeoutError):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(1)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run(
    *command: str,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 30,
    stdin: int | None = None,
) -> CommandResult:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=stdin,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise CommandTimeout(
                f"{command[0]} did not finish within {timeout:g} seconds"
            ) from error
        return CommandResult(process.returncode, stdout.decode(), stderr.decode())
    finally:
        _terminate_group(process)


@dataclass(frozen=True, slots=True)
class Seatbelt:
    executable: str
    profile: str

    def command(self, *command: str) -> tuple[str, ...]:
        return self.executable, "-p", self.profile, *command


def loopback_seatbelt(executable: str) -> Seatbelt:
    return Seatbelt(
        executable,
        """(version 1)
(allow default)
(deny network*)
(allow network-outbound (remote ip "localhost:*"))
(allow network-inbound (local ip "localhost:*"))""",
    )


def prove_loopback_only(
    seatbelt: Seatbelt, port: int, cwd: Path, env: dict[str, str]
) -> CommandResult:
    script = f"""
import socket
with socket.create_connection(("127.0.0.1", {port}), 1):
    pass
try:
    socket.create_connection(("192.0.2.1", 9), 1)
except PermissionError:
    pass
else:
    raise SystemExit(1)
"""
    return run(
        *seatbelt.command(sys.executable, "-c", script),
        cwd=cwd,
        env=env,
        timeout=5,
    )


def _drain_pty(master: int, output: bytearray) -> None:
    while readable := select.select([master], [], [], 0)[0]:
        try:
            if chunk := os.read(readable[0], 65_536):
                output.extend(chunk)
            else:
                return
        except OSError:
            return


def run_pty(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    complete: Callable[[], bool],
    timeout: float = 20,
) -> CommandResult:
    master, slave = pty.openpty()
    child = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + timeout
    completed = False
    try:
        while time.monotonic() < deadline and child.poll() is None:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                with suppress(OSError):
                    output.extend(os.read(master, 65_536))
            if complete():
                completed = True
                with suppress(OSError):
                    os.write(master, b"\x03\x03")
                break
        timed_out = (
            not completed and child.poll() is None and time.monotonic() >= deadline
        )
        _terminate_group(child)
        _drain_pty(master, output)
        if timed_out:
            raise CommandTimeout(
                f"{command[0]} did not finish within {timeout:g} seconds"
            )
        assert child.returncode is not None
        return CommandResult(child.returncode, output.decode(errors="replace"), "")
    finally:
        _terminate_group(child)
        os.close(master)
