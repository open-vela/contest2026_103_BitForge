#!/usr/bin/env python3
"""ESP32-P4 openvela console capture helper (nsh_check).

Companion tool for the ``esp32p4-hw-debug`` skill. It exists to avoid the two
traps documented in docs/issues/ISSUE-004 ("USB 串口抓不到输出"):

* The ESP32-P4 USB-Serial/JTAG only forwards firmware output while the host
  asserts DTR. ``cat /dev/ttyACM0`` never asserts DTR, so it prints nothing.
* Every reset re-enumerates the USB device: the node disappears and comes back
  under a different name/number, so a one-shot open loses the boot log.

What this tool does:

* Locates the port by VID:PID (default 303a:1001) instead of a hardcoded
  ``/dev/ttyACM0`` (the name drifts across re-enumerations).
* Asserts DTR / deasserts RTS on open, and never pulses those lines (a pulse
  can drive the chip into ROM download mode).
* Keeps reconnecting across re-enumerations so capture survives resets.
* Optionally probes for a live NSH prompt.
* Optionally runs a fixed NSH verification command sequence (``--dump``).
* Optionally triggers a hard reset before capturing (``--reset``).

It never writes firmware to flash.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

DEFAULT_VIDPID = "303a:1001"
DEFAULT_BAUD = 115200

# Verification sequence from docs/adr/ADR-0005 (NSH liveness + bring-up).
DUMP_COMMANDS = [
    "uname -a",
    "ps",
    "mount -t procfs /proc",
    "free",
    "ls /dev",
    "dmesg",
]


def parse_vidpid(text: str) -> tuple[int, int]:
    """Parse a '303a:1001' style string into (vid, pid) ints."""
    try:
        vid_s, pid_s = text.split(":", 1)
        return int(vid_s, 16), int(pid_s, 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid VID:PID {text!r}, expected e.g. {DEFAULT_VIDPID}"
        ) from exc


def import_serial():
    try:
        import serial  # noqa: F401
        from serial.tools import list_ports  # noqa: F401
    except ImportError:
        sys.exit(
            "error: pyserial is required.\n"
            "  install it with:  pip3 install --user pyserial"
        )
    return serial, list_ports


def find_port(list_ports, vid: int, pid: int, override: str | None = None) -> str | None:
    if override:
        return override
    for port in list_ports.comports():
        if port.vid == vid and port.pid == pid:
            return port.device
    return None


def open_port(serial, device: str, baud: int):
    ser = serial.Serial()
    ser.port = device
    ser.baudrate = baud  # ignored by USB CDC, kept for clarity
    ser.timeout = 0.2
    ser.write_timeout = 1
    ser.open()
    # USJ output is gated on DTR; keep RTS low. Never pulse these lines.
    ser.dtr = True
    ser.rts = False
    return ser


def emit(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def read_for(ser, seconds: float, quiet: float = 0.6) -> str:
    """Read until the line has been quiet for ``quiet`` seconds or ``seconds`` elapse."""
    end = time.monotonic() + seconds
    last = time.monotonic()
    chunks: list[str] = []
    while time.monotonic() < end:
        data = ser.read(4096)
        if data:
            chunks.append(data.decode("utf-8", errors="replace"))
            last = time.monotonic()
        elif chunks and time.monotonic() - last >= quiet:
            break
    return "".join(chunks)


def capture(ser, deadline: float | None, probe_interval: float) -> bool:
    """Stream output until ``deadline`` (None = forever).

    Returns True if the port dropped and the caller must reconnect.
    """
    next_probe = time.monotonic() + probe_interval if probe_interval else None
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if next_probe is not None and time.monotonic() >= next_probe:
            try:
                ser.write(b"\r\n")
                ser.flush()
            except Exception:
                return True
            next_probe = time.monotonic() + probe_interval
        try:
            data = ser.read(4096)
        except Exception:
            return True
        if data:
            emit(data.decode("utf-8", errors="replace"))


def run_dump(ser, wait: float) -> bool:
    """Run the fixed NSH verification sequence. Returns True on success."""
    for cmd in DUMP_COMMANDS:
        emit(f"\n$ {cmd}\n")
        try:
            ser.write((cmd + "\r\n").encode())
            ser.flush()
            emit(read_for(ser, wait))
        except Exception as exc:
            print(f"\n[nsh_check] port dropped during '{cmd}': {exc}", file=sys.stderr)
            return False
    return True


def do_reset() -> bool:
    cmd = [
        "esptool", "-c", "esp32p4",
        "--before", "usb-reset", "--after", "hard-reset",
        "chip-id",
    ]
    print(f"[nsh_check] resetting via: {' '.join(cmd)}", file=sys.stderr)
    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        print("[nsh_check] error: esptool not found on PATH", file=sys.stderr)
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture the ESP32-P4 openvela (USB-Serial/JTAG) console.",
        epilog=(
            "examples:\n"
            "  %(prog)s                          stream until Ctrl-C\n"
            "  %(prog)s --dump                   run the NSH verification sequence\n"
            "  %(prog)s --reset --seconds 15     reset, then capture boot log\n"
            "  %(prog)s --port /dev/ttyACM1      override port detection\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", help="serial port; default: auto-detect by VID:PID")
    parser.add_argument("--vid-pid", type=parse_vidpid, default=parse_vidpid(DEFAULT_VIDPID),
                        help=f"USB VID:PID to match (default {DEFAULT_VIDPID})")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                        help=f"baud rate (USB CDC ignores it; default {DEFAULT_BAUD})")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="stop after N seconds (0 = run until Ctrl-C)")
    parser.add_argument("--probe-interval", type=float, default=0.0,
                        help="send CRLF every N seconds to probe NSH liveness (0 = off)")
    parser.add_argument("--dump", action="store_true",
                        help="run the verification command sequence and exit")
    parser.add_argument("--dump-wait", type=float, default=3.0,
                        help="seconds to wait for output per dumped command (default 3)")
    parser.add_argument("--reset", action="store_true",
                        help="hard-reset the chip (esptool) before capturing")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.reset and not do_reset():
        return 1

    serial, list_ports = import_serial()
    vid, pid = args.vid_pid
    deadline = time.monotonic() + args.seconds if args.seconds > 0 else None

    ser = None
    waited = False
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                break
            if ser is None:
                device = find_port(list_ports, vid, pid, args.port)
                if not device:
                    if not waited:
                        print(
                            f"[nsh_check] waiting for {vid:#06x}:{pid:04x} ...",
                            file=sys.stderr,
                        )
                        waited = True
                    time.sleep(0.5)
                    continue
                try:
                    ser = open_port(serial, device, args.baud)
                except Exception as exc:
                    print(f"[nsh_check] cannot open {device}: {exc}", file=sys.stderr)
                    time.sleep(0.5)
                    continue
                waited = False
                print(f"[nsh_check] connected to {device} (DTR=1 RTS=0)", file=sys.stderr)
                if args.dump:
                    ok = run_dump(ser, args.dump_wait)
                    return 0 if ok else 1
                continue

            if capture(ser, deadline, args.probe_interval):
                print(
                    "[nsh_check] port dropped, waiting for re-enumeration ...",
                    file=sys.stderr,
                )
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
    except KeyboardInterrupt:
        emit("\n")
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
