#!/usr/bin/env python3
"""Boot a hardware target over a network console and run Linux smoke tests.

Requires Python >= 3.11, telnetlib3 >= 2.0 for Telnet transports.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shlex
import string
import sys
import tempfile
import termios
import tty
import time
import tomllib
import uuid
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_ERROR_PATTERNS = [
    r"(?i)Kernel panic",
    r"(?i)\bOops(?:\s*:\s*|\b)",
    r"(?i)\bBUG:\s",
    r"(?i)\bCall Trace:",
    r"(?i)\bWARNING:\s",
    r"(?i)Unable to mount root fs",
    r"(?i)No working init found",
    r"(?i)\b(?:error|failed|failure|timeout|timed out)\b",
]

DEFAULT_LOGIN_REGEX = r"(?:^|(?<=\r))[ \t]*(?:[A-Za-z0-9_.-]+[ \t]+)?login:[ \t]*"
DEFAULT_SHELL_REGEX = (
    r"(?:^|(?<=\r))(?:[ \t]*#|[A-Za-z0-9_.-]+@[^\r\n#]+#|/[^\r\n#]*#)"
    r"[ \t]*(?=\r?$|\[\s*\d+\.\d+\])"
)


class BootFailure(RuntimeError):
    pass


class Console:
    """Asynchronous reader; concurrent PDU actions cannot lose console bytes."""

    def __init__(self, cfg: dict, log_path: Path, *, live_log: bool = False):
        self.cfg = cfg
        self.live_log = live_log
        self.log_path = log_path
        self.reader: Any = None
        self.writer: Any = None
        self._task: asyncio.Task | None = None
        self._log = None
        self.text = ""
        self.cursor = 0
        self.closed = False
        self.disconnect_reason: str | None = None
        self._panic_offset: int | None = None
        self.kernel_panic: str | None = None
        self.condition = asyncio.Condition()
        self.reconnect_events: list[dict[str, Any]] = []
        self._handoff_start: re.Pattern | None = None
        self._handoff_end: re.Pattern | None = None
        self._handoff_offset = 0
        self._handoff_timeout = 0.0
        self._reconnect_deadline = 0.0
        self._connected = False
        self._send_lock = asyncio.Lock()
        self.write_delay_s = float(cfg.get("write_delay_s", 0))
        if not math.isfinite(self.write_delay_s) or self.write_delay_s < 0:
            raise BootFailure("Console write_delay_s must be finite and nonnegative")
        self.write_chunk_size = int(cfg.get("write_chunk_size", 1))
        if self.write_chunk_size < 1:
            raise BootFailure("Console write_chunk_size must be positive")

    async def connect(self) -> None:
        try:
            await self._open_connection()
        except asyncio.TimeoutError as exc:
            raise BootFailure(
                f"Timeout connecting to console {self.cfg['host']}:{self.cfg['port']}"
            ) from exc
        self._log = self.log_path.open("wb")
        self._task = asyncio.create_task(self._read_loop())

    async def _open_connection(self, timeout: float = 12) -> None:
        transport = self.cfg.get("transport", "telnet")
        host = self.cfg["host"]
        port = int(self.cfg["port"])
        if transport == "telnet":
            try:
                import telnetlib3
            except ImportError as exc:
                raise BootFailure("Install Telnet support: pip install -r requirements.txt") from exc
            self.reader, self.writer = await asyncio.wait_for(
                telnetlib3.open_connection(host, port, encoding=False), timeout=timeout
            )
        elif transport == "tcp":
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        else:
            raise BootFailure(f"Unknown console transport: {transport!r}")
        self._connected = True

    def arm_handoff(self, cfg: dict) -> None:
        """Allow reconnect only after the configured handoff banner is seen."""
        self.disarm_handoff()
        if cfg.get("reconnect_start_regex"):
            self._handoff_start = re.compile(cfg["reconnect_start_regex"])
            self._handoff_end = re.compile(cfg["reconnect_end_regex"])
            self._handoff_timeout = float(cfg.get("reconnect_timeout_s", 30))
            self._handoff_offset = len(self.text)

    def disarm_handoff(self) -> None:
        self._handoff_start = None
        self._handoff_end = None
        self._reconnect_deadline = 0.0

    def _track_handoff(self) -> None:
        if self._handoff_start:
            match = self._handoff_start.search(self.text, self._handoff_offset)
            if match:
                self._handoff_offset = match.end()
                self._handoff_start = None
                self._reconnect_deadline = time.monotonic() + self._handoff_timeout
        if self._reconnect_deadline and self._handoff_end:
            if self._handoff_end.search(self.text, self._handoff_offset):
                self.disarm_handoff()

    async def _close_connection(self) -> None:
        self._connected = False
        if self.writer is not None:
            self.writer.close()
            if hasattr(self.writer, "wait_closed"):
                try:
                    await asyncio.wait_for(self.writer.wait_closed(), timeout=2)
                except (OSError, asyncio.TimeoutError):
                    pass

    async def _reconnect(self) -> bool:
        if time.monotonic() >= self._reconnect_deadline:
            return False
        self.reconnect_events.append({"event": "disconnected", "offset": len(self.text)})
        await self._close_connection()
        while time.monotonic() < self._reconnect_deadline:
            try:
                await self._open_connection(min(12, self._reconnect_deadline - time.monotonic()))
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(min(0.25, max(0, self._reconnect_deadline - time.monotonic())))
            else:
                self.reconnect_events.append({"event": "reconnected", "offset": len(self.text)})
                return True
        return False

    async def _read_loop(self) -> None:
        session_text = ""
        try:
            while True:
                try:
                    chunk = await self.reader.read(4096)
                except OSError as exc:
                    self.disconnect_reason = str(exc)
                    chunk = b""
                if not chunk:
                    self._connected = False
                    if re.search(r"(?im)^Port already in use\s*$", session_text):
                        self.disconnect_reason = "Console server refused connection: Port already in use"
                        break
                    if await self._reconnect():
                        session_text = ""
                        self.disconnect_reason = None
                        continue
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                self._log.write(chunk)
                self._log.flush()
                fragment = chunk.decode("utf-8", errors="replace")
                if self.live_log:
                    sys.stdout.write(fragment)
                    sys.stdout.flush()
                session_text = (session_text + fragment)[-4096:]
                async with self.condition:
                    self.text += fragment
                    self._track_handoff()
                    if self._panic_offset is not None and self.kernel_panic is None:
                        panic = re.search(r"(?i)Kernel panic[^\r\n]*", self.text[self._panic_offset:])
                        if panic:
                            self.kernel_panic = panic.group(0)
                    self.condition.notify_all()
        finally:
            async with self.condition:
                self.closed = True
                self.condition.notify_all()

    async def close(self) -> None:
        self.disarm_handoff()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._close_connection()
        if self._log:
            self._log.close()

    async def interactive(self) -> None:
        """Forward terminal input to the target until Ctrl-] or disconnect."""
        if not self.connected:
            return
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        previous_live_log = self.live_log
        loop = asyncio.get_running_loop()
        inputs: asyncio.Queue[bytes] = asyncio.Queue()

        def read_input() -> None:
            inputs.put_nowait(os.read(fd, 4096))

        async def forward_input() -> None:
            while True:
                data = await inputs.get()
                payload, escape, _ = data.partition(b"\x1d")
                if payload:
                    self.writer.write(payload)
                if escape or not data:
                    return

        print("Interactive target shell; press Ctrl-] to detach.", flush=True)
        sender = None
        try:
            tty.setraw(fd)
            self.live_log = True
            loop.add_reader(fd, read_input)
            sender = asyncio.create_task(forward_input())
            # The existing reader continues displaying and logging target output.
            self.writer.write(b"\r")
            await asyncio.wait({sender, self._task}, return_when=asyncio.FIRST_COMPLETED)
            if sender.done():
                sender.result()
        finally:
            loop.remove_reader(fd)
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            self.live_log = previous_live_log
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            print()

    def mark(self) -> int:
        """Discard buffered matches from previous boot, not buffered log data."""
        self.cursor = len(self.text)
        return self.cursor

    def monitor_panics(self) -> None:
        self._panic_offset = len(self.text)
        self.kernel_panic = None

    def check_panic(self) -> None:
        if self.kernel_panic:
            raise BootFailure(f"Kernel panic detected: {self.kernel_panic}")

    @property
    def connected(self) -> bool:
        return self._connected and not self.closed

    async def send(self, content: str, *, newline: bool = False) -> None:
        payload = (content + ("\r" if newline else "")).encode("ascii")
        async with self._send_lock:
            parts = [payload] if not self.write_delay_s else [
                payload[offset:offset + self.write_chunk_size]
                for offset in range(0, len(payload), self.write_chunk_size)
            ]
            for index, part in enumerate(parts):
                if index:
                    await asyncio.sleep(self.write_delay_s)
                self.check_panic()
                if not self.connected:
                    raise BootFailure("Console disconnected while sending input")
                try:
                    self.writer.write(part)
                except OSError as exc:
                    raise BootFailure(f"Console write failed: {exc}") from exc

    async def expect(self, pattern: str, timeout: float, label: str = "console") -> re.Match:
        compiled = re.compile(pattern)
        deadline = time.monotonic() + timeout
        async with self.condition:
            while True:
                self.check_panic()
                match = compiled.search(self.text, self.cursor)
                if match is not None:
                    self.cursor = match.end()
                    return match
                if self.closed:
                    reason = f": {self.disconnect_reason}" if self.disconnect_reason else ""
                    raise BootFailure(f"Console disconnected while waiting for {label}{reason}")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise BootFailure(f"Timeout ({timeout:g}s) waiting for {label}: {pattern}")
                try:
                    await asyncio.wait_for(self.condition.wait(), timeout=left)
                except asyncio.TimeoutError as exc:
                    raise BootFailure(f"Timeout ({timeout:g}s) waiting for {label}: {pattern}") from exc

    async def command(self, cmd: str, timeout: float) -> tuple[int, str]:
        """Use a uniquely generated, non-echoed marker to read real exit status."""
        nonce = uuid.uuid4().hex[:12]
        marker = "__BOOTSMOKE_RC_" + nonce + "__"
        start = len(self.text)
        # A check may use exit; keep it from terminating the login shell.
        # Marker is split in the echoed command, so output alone can match it.
        await self.send("sh -c " + shlex.quote(cmd) + "; __bs_rc=$?; printf '%s%s:%s\\n' '__BOOTSMOKE_' 'RC_" + nonce + "__' \"$__bs_rc\"", newline=True)
        # printk can interrupt a userspace write between any two UART bytes.
        # Accept timestamped kernel lines within the marker without changing
        # the raw transcript or weakening the non-echoed nonce handshake.
        kernel_line = r"\[\s*\d+\.\d+\][^\r\n]*(?:\r\n|\n|\r)"
        noise = "(?:" + kernel_line + ")*"
        marker_pattern = noise.join(re.escape(char) for char in marker)
        status_pattern = r"(\d(?:" + noise + r"\d)*)"
        try:
            matched = await self.expect(
                marker_pattern + noise + ":" + noise + status_pattern + noise + r"(?:\r\n|\r|\n)",
                timeout, cmd)
        except BootFailure as exc:
            if str(exc).startswith("Timeout ("):
                raise BootFailure(f"Timeout ({timeout:g}s) waiting for shell exit status: {cmd}") from exc
            raise
        status = re.sub(kernel_line, "", matched.group(1))
        return int(status), self.text[start:matched.start()]


async def exec_argv(argv: list[str], values: dict[str, Any], timeout: float) -> None:
    resolved = [str(part).format_map(values) for part in argv]
    if not resolved:
        raise BootFailure("PDU command is empty")
    if any("CHANGE_ME" in x or "<" in x or ">" in x for x in resolved):
        raise BootFailure("Configure the PDU command and outlet before enabling power control")
    proc = await asyncio.create_subprocess_exec(
        *resolved, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise BootFailure(f"PDU command timed out: {resolved[0]}")
    if proc.returncode:
        raise BootFailure(f"PDU command failed (exit {proc.returncode}): {stdout.decode(errors='replace').strip()}")


def error_lines(text: str, patterns: list[str], ignored: list[str]) -> list[str]:
    """Return unique unexpected error lines (the ignore rules are board-specific)."""
    fail_re = [re.compile(p) for p in patterns]
    ignore_re = [re.compile(p) for p in ignored]
    seen: set[str] = set()
    findings: list[str] = []
    for line in text.splitlines():
        clean = re.sub(r"\x1b\[[\d;]*[A-Za-z]", "", line).strip()
        if not clean:
            continue
        if any(p.search(clean) for p in fail_re) and not any(p.search(clean) for p in ignore_re):
            if clean not in seen:
                seen.add(clean)
                findings.append(clean)
    return findings


def render(template: str, context: dict[str, Any]) -> str:
    try:
        return template.format_map(context)
    except KeyError as exc:
        raise BootFailure(f"Missing boot-command template parameter: {exc}") from exc


def configure_rpi_mode(filename: str, arm_64bit: int) -> dict[str, Any]:
    """Toggle the existing arm_64bit=1 line, preserving its symlink and other settings."""
    if type(arm_64bit) is not int or arm_64bit not in (0, 1):
        raise BootFailure("Raspberry Pi arm_64bit must be 0 or 1")
    path = Path(filename).expanduser().resolve(strict=True)
    original = path.read_bytes().decode("utf-8")
    pattern = re.compile(r"(?m)^([ \t]*)(?:#[ \t]*)?(arm_64bit[ \t]*=[ \t]*1[ \t]*(?:#[^\r\n]*)?)(\r?)$")
    matches = list(pattern.finditer(original))
    settings = re.findall(r"(?m)^[ \t]*(?:#[ \t]*)?arm_64bit[ \t]*=", original)
    if len(matches) != 1 or len(settings) != 1:
        raise BootFailure("Pi config must contain exactly one commented or active arm_64bit=1 line")
    if re.search(r"(?m)^[ \t]*\[", original):
        raise BootFailure("Pi config sections require a mode-specific configuration; refusing ambiguous toggle")
    updated = pattern.sub(lambda m: m[1] + ("" if arm_64bit else "#") + m[2] + m[3], original)
    if updated != original:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                os.fchmod(output.fileno(), path.stat().st_mode & 0o7777)
                output.write(updated.encode("utf-8"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {"path": str(path), "arm_64bit": arm_64bit, "changed": updated != original}


async def login(console: Console, cfg: dict) -> None:
    """Handle login prompt or an already-running root shell, then probe it."""
    wait = float(cfg.get("shell_timeout_s", 90))
    user = cfg.get("user", "root")
    login_re = cfg.get("login_regex", DEFAULT_LOGIN_REGEX)
    root_re = cfg.get("shell_regex", DEFAULT_SHELL_REGEX)
    chosen = await console.expect(f"(?im)(?:{login_re})|(?:{root_re})", wait, "Linux login/root shell")
    if re.search(login_re, chosen.group(0), re.I | re.M):
        await console.send(user, newline=True)
        pw_regex = cfg.get("password_regex", r"Password:[ \t]*")
        # No password is normal on tiny Buildroot images; a password prompt is optional.
        chosen = await console.expect(f"(?im)(?:{pw_regex})|(?:{root_re})", wait, "password prompt or root shell")
        if re.search(pw_regex, chosen.group(0), re.I | re.M):
            env_key = cfg.get("password_env", "BOOTSMOKE_LOGIN_PASSWORD")
            if env_key not in os.environ:
                raise BootFailure(f"Password prompt seen; set environment variable {env_key}")
            await console.send(os.environ[env_key], newline=True)
            await console.expect("(?m)" + root_re, wait, "root shell")
    # Do not trust a prompt alone. The generated marker is only present in output.
    status, _ = await console.command("true", 10)
    if status:
        raise BootFailure("Shell command handshake returned failure")


async def wait_for_firmware_and_boot(console: Console, cfg: dict, profile: dict,
                                     power: dict, variables: dict, skip_power: bool,
                                     progress: Callable[[str, str], None] | None = None) -> int:
    def report(stage: str, message: str) -> None:
        if progress:
            progress(stage, message)
        else:
            print(message, flush=True)

    bootloader = cfg.get("bootloader", "prompt")
    if bootloader not in ("none", "pxe", "prompt", "uboot", "cfe", "yamon"):
        raise BootFailure(f"Unknown bootloader setting: {bootloader}")
    poke = cfg.get("interrupt", "")
    interval = float(cfg.get("interrupt_interval_s", 0.25))
    timeout = float(cfg.get("firmware_timeout_s", 30))
    settle = float(cfg.get("firmware_settle_s", 0))
    if not math.isfinite(settle) or settle < 0:
        raise BootFailure("firmware_settle_s must be finite and nonnegative")
    power_timeout = float(power.get("timeout_s", 15))
    off_delay = float(power.get("off_delay_s", 2))

    if not skip_power:
        await exec_argv(power["off_argv"], variables, power_timeout)
        await asyncio.sleep(off_delay)
    console.mark()  # ignore anything previously in the terminal server's buffer
    console.monitor_panics()
    console.arm_handoff(cfg)

    if bootloader in ("none", "pxe"):
        if not skip_power:
            await exec_argv(power["on_argv"], variables, power_timeout)
            report("power_on_requested", "Power-on command completed")
        if cfg.get("power_on_regex"):
            await console.expect(cfg["power_on_regex"], timeout, "power-on banner")
            report("power_on", "Power on detected (console boot banner)")
        message = ("Waiting for PXE kernel boot" if bootloader == "pxe"
                   else "Waiting for direct network kernel boot")
        report("direct_boot", message)
    else:
        firmware_re = cfg["firmware_prompt"]
        async def nudges() -> None:
            if cfg.get("power_on_regex"):
                await console.expect(cfg["power_on_regex"], timeout, "power-on banner")
                report("power_on", "Power on detected (console boot banner)")
            if not poke:
                return
            if cfg.get("interrupt_when_regex"):
                await console.expect(cfg["interrupt_when_regex"], timeout, "boot-menu interrupt banner")
                if not cfg.get("power_on_regex"):
                    report("power_on", "Power on detected (bootloader interrupt banner)")
                await console.send(poke)
                return
            while True:
                if console.connected:
                    await console.send(poke)
                await asyncio.sleep(interval)

        nudge_task = asyncio.create_task(nudges())

        async def find_prompt() -> None:
            try:
                if cfg.get("interrupt_when_regex"):
                    await nudge_task
                await console.expect(firmware_re, timeout, "firmware prompt")
            finally:
                # Stop repeated input immediately, even if power-on is still
                # waiting for the PDU helper to return.
                nudge_task.cancel()
                await asyncio.gather(nudge_task, return_exceptions=True)

        prompt_task = asyncio.create_task(find_prompt())
        try:
            if not skip_power:
                await exec_argv(power["on_argv"], variables, power_timeout)
            await prompt_task
        finally:
            prompt_task.cancel()
            nudge_task.cancel()
            await asyncio.gather(prompt_task, nudge_task, return_exceptions=True)

        if settle:
            # Discard queued interrupt echoes, then require a fresh prompt.
            await asyncio.sleep(settle)
            console.mark()
            await console.send("", newline=True)
            await console.expect(firmware_re, timeout, "settled firmware prompt")
        report("bootloader_prompt", "Bootloader prompt detected")

        steps = profile.get("boot_steps", cfg.get("boot_steps"))
        if steps is None:
            steps = [{"command": profile["boot_command"]}]
        if not steps:
            raise BootFailure("No boot steps configured")
        for step in steps:
            boot_cmd = render(step["command"], variables)
            console.mark()
            await console.send(boot_cmd, newline=True)
            if "expect" in step:
                await console.expect(step["expect"], float(step.get("timeout_s", 30)), boot_cmd)
    kernel_match = await console.expect(
        cfg.get("kernel_start_regex", r"Linux version\s"),
        float(cfg.get("kernel_timeout_s", 120)), "Linux kernel startup")
    console.disarm_handoff()
    report("kernel_boot", "Kernel boot detected (Linux startup banner)")
    return kernel_match.start()


async def run_one(lab: dict, target_name: str, mode: str, out_base: Path,
                  skip_power: bool, live_log: bool = False, shell: bool = False) -> dict:
    cfg = lab["targets"][target_name]
    profile = cfg["profiles"][mode]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = out_base / target_name / f"{stamp}-{mode}"
    folder.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "target": target_name, "mode": mode, "utc": stamp, "status": "FAIL",
        "checks": [], "kernel_findings": [], "failure": None, "steps": [],
        "artifacts": {"console": str(folder / "console.log"),
                      "dmesg": str(folder / "dmesg.log")},
    }
    t0 = time.monotonic()
    console = Console(cfg["console"], folder / "console.log", live_log=live_log)
    def progress(stage: str, message: str) -> None:
        result["steps"].append({"stage": stage, "elapsed_s": round(time.monotonic() - t0, 2)})
        print(f"[{target_name}/{mode}] {message}", flush=True)
    try:
        variables = {
            "pdu_host": lab["power"].get("hostname", "pdu"),
            "outlet": cfg["outlet"], "image": profile.get("image", ""),
            "nfsroot": profile.get("nfsroot", ""),
            "ping_host": cfg.get("ping_host", ""),
            **profile.get("variables", {}),
            "python": sys.executable,
            "wti_script": str(Path(lab["power"].get(
                "wti_script", "~/dev/github/misc-scripts/wti-nps.py"
            )).expanduser()),
        }
        if not skip_power:
            if not all(lab["power"].get(key) for key in ("off_argv", "on_argv")):
                raise BootFailure("PDU off_argv/on_argv must be configured")
            if not cfg.get("outlet") or cfg["outlet"] == "CHANGE_ME":
                raise BootFailure("Target's power outlet must be configured")
            power_commands = [
                [render(str(part), variables) for part in lab["power"][key]]
                for key in ("off_argv", "on_argv")
            ]
            if any("CHANGE_ME" in part for argv in power_commands for part in argv):
                raise BootFailure("Configure both PDU commands before enabling power control")
            if any("{wti_script}" in str(part)
                   for key in ("off_argv", "on_argv") for part in lab["power"][key]):
                if str(cfg["outlet"]) not in [str(r) for r in range(1, 9)]:
                    raise BootFailure("WTI target outlet must be a single relay from 1 to 8")
                if not Path(variables["wti_script"]).is_file():
                    raise BootFailure(f"WTI helper not found: {variables['wti_script']}")
        if not cfg.get("ping_host") or "CHANGE_ME" in cfg["ping_host"]:
            raise BootFailure("Target ping_host must be configured")
        await console.connect()
        if cfg.get("rpi_config"):
            result["boot_configuration"] = configure_rpi_mode(cfg["rpi_config"], profile["arm_64bit"])
            progress("boot_config", f"Pi boot configured: arm_64bit={profile['arm_64bit']}")
        kernel_start = await wait_for_firmware_and_boot(
            console, cfg["boot"], profile, lab["power"], variables, skip_power, progress
        )
        result["kernel_started"] = True
        await login(console, cfg.get("login", {}))
        result["shell_ready"] = True
        defaults = [
            ("uname", "uname -a", 15),
            ("kernel_arch", "uname -m", 15),
            ("cmdline", "cat /proc/cmdline", 15),
            ("uptime", "uptime", 15),
            ("memory", "free", 15),
            ("filesystem", "df -h", 15),
            ("ping", "ping -c 2 -W 2 {ping_host}", 20),
            ("dmesg", "dmesg", 30),
        ]
        checks = cfg.get("checks", []) or [
            {"name": name, "command": cmd, "timeout_s": wait} for name, cmd, wait in defaults
        ]
        dmesg = None
        for check in checks:
            name = check["name"]
            progress("check_start", f"Running check: {name}")
            try:
                cmd = render(check["command"], variables)
                code, output = await console.command(cmd, float(check.get("timeout_s", 20)))
                passed = code == 0
                if passed and "output_regex" in check:
                    passed = bool(re.search(check["output_regex"], output))
                if passed and name == "kernel_arch" and profile.get("uname_regex"):
                    passed = bool(re.search(profile["uname_regex"], output))
                result["checks"].append({"name": name, "pass": passed,
                                         "exit_status": code, "output": output[-8000:]})
                progress("check_result", f"{name}: {'PASS' if passed else 'FAIL'} (exit {code})")
                if name == "dmesg":
                    dmesg = output
                    (folder / "dmesg.log").write_text(output)
            except BootFailure as exc:
                result["checks"].append({"name": name, "pass": False, "error": str(exc)})
                # A lost shell/console usually makes later commands unhelpful.
                break
        if dmesg is None:
            result["checks"].append({"name": "dmesg_captured", "pass": False,
                                     "error": "No successful dmesg capture"})
        patterns = cfg.get("error_patterns", DEFAULT_ERROR_PATTERNS)
        ignored = cfg.get("ignore_patterns", [])
        result["kernel_findings"] = error_lines(console.text[kernel_start:], patterns, ignored)
        if dmesg is not None:
            result["kernel_findings"] = list(dict.fromkeys(
                result["kernel_findings"] + error_lines(dmesg, patterns, ignored)
            ))
        if result["kernel_findings"]:
            result["failure"] = f"{len(result['kernel_findings'])} unexpected kernel error line(s)"
        elif not all(check["pass"] for check in result["checks"]):
            result["failure"] = "One or more smoke tests failed"
        else:
            result["status"] = "PASS"
    except (BootFailure, OSError, KeyError, ValueError) as exc:
        result["failure"] = str(exc)
    finally:
        if console.kernel_panic:
            result["status"] = "FAIL"
            result["failure"] = f"Kernel panic detected: {console.kernel_panic}"
            result["kernel_findings"] = list(dict.fromkeys(
                result["kernel_findings"] + [console.kernel_panic]))
        result["console_reconnects"] = console.reconnect_events
        result["elapsed_s"] = round(time.monotonic() - t0, 2)
        try:
            (folder / "results.json").write_text(json.dumps(result, indent=2) + "\n")
            if shell and result.get("shell_ready") and console.connected:
                progress("interactive", f"Smoke tests: {result['status']}; results saved to {folder}")
                await console.interactive()
        finally:
            await console.close()
    return result


def cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="lab.toml", type=Path)
    parser.add_argument("--target", default="qube2")
    parser.add_argument("--mode", default="all", help="profile name or 'all'")
    parser.add_argument("--output", default="results", type=Path)
    parser.add_argument("--live-log", action="store_true",
                        help="Stream console output to the terminal while saving console.log")
    parser.add_argument("--shell", action="store_true",
                        help="Enter the target shell after each profile's tests; Ctrl-] detaches")
    parser.add_argument("--skip-power", action="store_true",
                        help="For manually powered targets; still interrupts firmware")
    args = parser.parse_args()
    if args.shell and not sys.stdin.isatty():
        parser.error("--shell requires an interactive terminal on stdin")
    return args


async def async_main(args: argparse.Namespace) -> int:
    with args.config.open("rb") as source:
        lab = tomllib.load(source)
    profiles = lab["targets"][args.target]["profiles"]
    modes = list(profiles) if args.mode == "all" else [args.mode]
    for mode in modes:
        if mode not in profiles:
            raise BootFailure(f"Invalid profile {mode!r}: choose {list(profiles)}")
    failures = 0
    for mode in modes:
        print(f"[{args.target}/{mode}] power → firmware → Linux → smoke tests", flush=True)
        outcome = await run_one(lab, args.target, mode, args.output, args.skip_power, args.live_log, args.shell)
        print(f"[{args.target}/{mode}] {outcome['status']} in {outcome['elapsed_s']}s", flush=True)
        if outcome.get("failure"):
            print("  " + outcome["failure"], flush=True)
        if outcome.get("kernel_findings"):
            print("  Flagged console/dmesg lines:", flush=True)
            for line in outcome["kernel_findings"]:
                print("    " + line, flush=True)
        for check in outcome["checks"]:
            print(f"  {'OK' if check['pass'] else 'FAIL'}: {check['name']}", flush=True)
        if outcome["status"] != "PASS":
            failures += 1
    return 0 if not failures else 1


def main() -> None:
    args = cli()
    try:
        raise SystemExit(asyncio.run(async_main(args)))
    except (BootFailure, FileNotFoundError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
