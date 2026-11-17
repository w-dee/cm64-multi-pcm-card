#!/usr/bin/env python3
"""Back up, restore, and test the CM-64 card's 8 MiB parallel NOR over USB."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
import time
import zlib

import serial
from serial.tools import list_ports

FLASH_BYTES = 8 * 1024 * 1024
BANK_BYTES = 512 * 1024
PAGE_BYTES = 256
PRODUCT = "CM-64 PCM Card"
DATA = re.compile(r"DATA ([0-9a-f]{6}) ([0-9a-f]{512}) ([0-9a-f]{8})")


class CardError(RuntimeError):
    pass


def fields(line: str) -> dict[str, str]:
    return dict(re.findall(r"(\w+)=([^ ]+)", line))


def crc32(data: bytes) -> int:
    return zlib.crc32(data)


def bank_number(value: str) -> int:
    try:
        bank = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bank must be 0..15") from exc
    if not 0 <= bank < 16:
        raise argparse.ArgumentTypeError("bank must be 0..15")
    return bank


def card_ports():
    return [p for p in list_ports.comports()
            if p.vid == 0x2E8A and p.pid == 0x000A and p.product == PRODUCT]


def select_port(port: str | None, serial_number: str | None) -> str:
    if port:
        return port
    ports = [p for p in card_ports() if not serial_number or p.serial_number == serial_number]
    if len(ports) != 1:
        raise CardError("expected exactly one card; use list, then --serial or --port")
    return ports[0].device


class Card:
    def __init__(self, port: str):
        options = {"exclusive": True} if os.name == "posix" else {}
        self.port = serial.Serial(port, 115200, timeout=0.25, write_timeout=5, **options)
        self.last_progress = 0.0

    def __enter__(self):
        try:
            time.sleep(0.2)
            self.port.reset_input_buffer()
            self.info = self.identify()
        except BaseException:
            self.port.close()
            raise
        return self

    def __exit__(self, *args):
        self.port.close()  # DTR drops; firmware releases the bus on disconnect.

    def send(self, command: str):
        data = (command + "\n").encode("ascii")
        if self.port.write(data) != len(data):
            raise CardError("short USB write")

    def line(self, timeout: float = 45) -> str:
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        while time.monotonic() < deadline:
            part = self.port.read_until(b"\n", 2048 - len(buffer))
            buffer.extend(part)
            if len(buffer) >= 2048:
                raise CardError("oversized device response")
            if buffer.endswith(b"\n"):
                try:
                    line = buffer.decode("ascii").strip()
                except UnicodeDecodeError as exc:
                    raise CardError("non-ASCII device response") from exc
                if line.startswith("END ") and " FAIL " in line:
                    raise CardError(line)
                return line
        raise CardError("USB response timeout; operation may be incomplete")

    def log(self, line: str):
        if line.startswith("PROGRESS"):
            if time.monotonic() - self.last_progress < 5:
                return
            self.last_progress = time.monotonic()
        print(line, file=sys.stderr, flush=True)

    @staticmethod
    def finish(line: str, command: str, size: int, crc: int):
        values = fields(line)
        expected = {"bytes": str(size), "crc": f"{crc:08x}", "ARM": "0", "PROG_EN_N": "1"}
        if not line.startswith(f"END {command} OK ") or any(values.get(k) != v for k, v in expected.items()):
            raise CardError(f"invalid completion or unsafe bus state: {line}")

    def identify(self) -> str:
        self.send("INFO")
        info = None
        self.details = []
        while True:
            line = self.line()
            if line.startswith("INFO "):
                info = line
                values = fields(line)
                expected = {"protocol": "1", "bytes": str(FLASH_BYTES),
                            "bank_bytes": str(BANK_BYTES), "banks": "16", "page": "256"}
                if any(values.get(k) != v for k, v in expected.items()):
                    raise CardError(f"incompatible firmware: {line}")
            elif line.startswith("END "):
                self.finish(line, "INFO", 0, 0)
                if info is None:
                    raise CardError("missing firmware identification")
                return info
            else:
                self.details.append(line)

    def read(self, base: int, size: int) -> bytes:
        self.send(f"READ {base:06x} {size:06x}")
        data = bytearray()
        while True:
            line = self.line()
            if line.startswith("DATA "):
                match = DATA.fullmatch(line)
                if match is None:
                    raise CardError("malformed DATA response")
                address, encoded, checksum = match.groups()
                block = bytes.fromhex(encoded)
                if int(address, 16) != base + len(data) or len(data) + len(block) > size:
                    raise CardError("out-of-order or excess flash data")
                if crc32(block) != int(checksum, 16):
                    raise CardError(f"read CRC mismatch at {address}")
                data.extend(block)
                if len(data) % 65536 == 0:
                    self.log(f"PROGRESS read={len(data)}/{size}")
            elif line.startswith("END "):
                self.finish(line, "READ", size, crc32(data))
                if len(data) != size:
                    raise CardError("incomplete flash read")
                return bytes(data)
            else:
                self.log(line)

    def write(self, base: int, image: bytes):
        self.send(f"WRITE {base:06x} {len(image):06x} {crc32(image):08x} CONFIRM")
        while True:
            line = self.line()
            if line == "READY WRITE":
                break
            if line.startswith("END "):
                raise CardError("write ended before receiving image")
            self.log(line)
        for offset in range(0, len(image), PAGE_BYTES):
            block = image[offset:offset + PAGE_BYTES]
            address = base + offset
            checksum = crc32(block)
            self.send(f"PAGE {address:06x} {block.hex()} {checksum:08x}")
            while True:
                line = self.line()
                if line.startswith("ACK "):
                    if line != f"ACK {address:06x} {checksum:08x}":
                        raise CardError(f"invalid page acknowledgement: {line}")
                    break
                if line.startswith("END "):
                    raise CardError("write ended before all pages were acknowledged")
                self.log(line)
            if (offset + PAGE_BYTES) % 65536 == 0:
                self.log(f"PROGRESS written={offset + PAGE_BYTES}/{len(image)}")
        self.finish(self.line(), "WRITE", len(image), crc32(image))
        # Independent complete read after programming, in addition to device
        # page readback and CRC checks on every USB transaction.
        if self.read(base, len(image)) != image:
            raise CardError("complete readback does not match the input image")

    def test(self):
        self.send("TEST ERASE_ALL")
        verifications, programs, bus, result = [], [], None, None
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            line = self.line()
            self.log(line)
            if line.startswith("VERIFY end "):
                verifications.append(fields(line))
            elif line.startswith("PROGRAM end "):
                programs.append(fields(line))
            elif line.startswith("BUS_TEST "):
                bus = fields(line)
            elif line.startswith("RESULT "):
                result = line
            elif line.startswith("END "):
                self.finish(line, "TEST", 0, 0)
                break
        else:
            raise CardError("test exceeded one hour")
        expected = [(-1, "ascending", "3de23e27"), (-1, "ascending", "3de23e27"),
                    (0, "ascending", "c763453f"), (-1, "ascending", "3de23e27"),
                    (1, "descending", "19b97c77")]
        if len(verifications) != len(expected):
            raise CardError("missing full-device verification passes")
        for v, (pattern, order, checksum) in zip(verifications, expected):
            if any(v.get(k) != value for k, value in {
                "pattern": str(pattern), "order": order, "checked": str(FLASH_BYTES),
                "mismatches": "0", "crc": checksum, "expected_crc": checksum,
            }.items()):
                raise CardError(f"full-device verification failed: {v}")
        if programs != [{"pattern": str(p), "written": str(FLASH_BYTES), "result": "PASS"} for p in (0, 1)]:
            raise CardError("missing full-device programming passes")
        if bus != {"points": "68", "reads": "136", "mismatches": "0"}:
            raise CardError("address/data bus test failed")
        if not result or not result.startswith("RESULT PASS "):
            raise CardError("missing test PASS result")

    def bootsel(self):
        self.send("BOOTSEL")
        if self.line() != "END BOOTSEL OK":
            raise CardError("BOOTSEL request failed")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    device = p.add_mutually_exclusive_group()
    device.add_argument("--port", help="CDC port (e.g. /dev/ttyACM0 or COM3)")
    device.add_argument("--serial", help="USB serial number from list")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list connected cards")
    commands.add_parser("info", help="show firmware and verify NOR identity")
    commands.add_parser("bootsel", help="reboot to ROM BOOTSEL for UF2 update")
    for name, help_text in [("backup", "read all 8 MiB"), ("restore", "erase and write all 8 MiB"),
                            ("read-bank", "read one 512 KiB bank"), ("write-bank", "erase and write one bank")]:
        sub = commands.add_parser(name, help=help_text)
        if "bank" in name:
            sub.add_argument("bank", type=bank_number)
        sub.add_argument("file", type=Path)
    test = commands.add_parser("test", help="DESTRUCTIVE whole-flash write/read test")
    test.add_argument("--force", action="store_true", help="allow destruction of all parallel flash data")
    return p


def main(argv=None) -> int:
    p = parser()
    args = p.parse_args(argv)
    if args.command == "test" and not args.force:
        p.error("test destroys all 8 MiB; back up first, then specify --force")
    try:
        if args.command == "list":
            for port in card_ports():
                print(f"{port.device}\t{port.serial_number}\t{port.product}")
            return 0
        size = BANK_BYTES if "bank" in args.command else FLASH_BYTES
        base = getattr(args, "bank", 0) * BANK_BYTES
        image = None
        if args.command in ("restore", "write-bank"):
            image = args.file.read_bytes()
            if len(image) != size:
                raise CardError(f"input must be exactly {size} bytes (got {len(image)})")
        if args.command in ("backup", "read-bank"):
            # Exclusive creation protects existing backups. Delete only our own
            # incomplete file on failure; a successful backup is flushed to disk.
            output = args.file.open("xb")
            try:
                with output:
                    with Card(select_port(args.port, args.serial)) as card:
                        image = card.read(base, size)
                    output.write(image)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                args.file.unlink()
                raise
        else:
            with Card(select_port(args.port, args.serial)) as card:
                if args.command == "info":
                    print(card.info)
                    for line in card.details:
                        print(line)
                elif args.command in ("restore", "write-bank"):
                    card.write(base, image)
                elif args.command == "test":
                    card.test()
                elif args.command == "bootsel":
                    card.bootsel()
        if image is not None:
            print(f"OK {args.command}: {len(image)} bytes sha256={hashlib.sha256(image).hexdigest()}")
        else:
            print(f"OK {args.command}")
        return 0
    except (CardError, serial.SerialException, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; writes/tests may be incomplete; restore a backup", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
