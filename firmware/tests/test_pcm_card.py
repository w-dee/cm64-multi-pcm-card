import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pcm_card as tool


def end(command, data=b"", **overrides):
    values = {"bytes": str(len(data)), "crc": f"{tool.crc32(data):08x}",
              "ARM": "0", "PROG_EN_N": "1"}
    values.update(overrides)
    return f"END {command} OK " + " ".join(f"{k}={v}" for k, v in values.items())


def read_lines(base, data):
    lines = []
    for offset in range(0, len(data), 256):
        block = data[offset:offset+256]
        lines.append(f"DATA {base+offset:06x} {block.hex()} {tool.crc32(block):08x}")
    return lines + [end("READ", data)]


class FakePort:
    def __init__(self, lines):
        self.responses = bytearray(("\n".join(lines) + "\n").encode())
        self.commands = []

    def write(self, data):
        self.commands.append(data.decode().strip())
        return len(data)

    def read_until(self, expected, size):
        if not self.responses:
            raise AssertionError("unexpected read past end of fake response")
        # Deliberately fragment lines to exercise USB/serial partial reads.
        count = min(17, size, self.responses.find(b"\n")+1)
        data = bytes(self.responses[:count])
        del self.responses[:count]
        return data


def card(lines):
    result = tool.Card.__new__(tool.Card)
    result.port = FakePort(lines)
    result.last_progress = 0.0
    return result


class ToolTests(unittest.TestCase):
    def test_reads_multiple_pages_and_checks_absolute_bank_address(self):
        data = bytes(range(256))*2
        device = card(read_lines(15*tool.BANK_BYTES, data))
        self.assertEqual(device.read(15*tool.BANK_BYTES, len(data)), data)

    def test_rejects_wrong_page_crc_address_and_length(self):
        data = bytes(range(256))
        lines = read_lines(0, data)
        for first in [lines[0][:-8] + "00000000", lines[0].replace("DATA 000000", "DATA 000100"),
                      "DATA 000000 00 00000000"]:
            with self.subTest(first=first[:30]), self.assertRaises(tool.CardError):
                card([first, lines[-1]]).read(0, 256)

    def test_rejects_missing_page_and_bad_final_crc(self):
        data = bytes(range(256))
        for lines in [[end("READ", data)], read_lines(0, data)[:-1] + [end("READ", data, crc="00000000")]]:
            with self.assertRaises(tool.CardError):
                card(lines).read(0, 256)

    def test_rejects_unsafe_final_state(self):
        for override in [{"ARM": "1"}, {"PROG_EN_N": "0"}]:
            with self.assertRaises(tool.CardError):
                tool.Card.finish(end("READ", **override), "READ", 0, 0)

    def test_writes_pages_then_independently_reads_back_all_data(self):
        image = bytes(range(256))*2
        checksum = tool.crc32(image[:256])
        device = card(["READY WRITE", "PROGRESS stage=program", f"ACK 000000 {checksum:08x}", f"ACK 000100 {checksum:08x}",
                       end("WRITE", image)] + read_lines(0, image))
        with contextlib.redirect_stderr(io.StringIO()):
            device.write(0, image)
        self.assertTrue(device.port.commands[0].endswith(" CONFIRM"))
        self.assertEqual(device.port.commands[-1], "READ 000000 000200")

    def test_write_rejects_bad_ack_and_readback_mismatch(self):
        image = bytes(range(256))
        with self.assertRaises(tool.CardError):
            card(["READY WRITE", "ACK 000100 00000000"]).write(0, image)
        checksum = tool.crc32(image)
        device = card(["READY WRITE", f"ACK 000000 {checksum:08x}", end("WRITE", image)] +
                      read_lines(0, bytes(256)))
        with self.assertRaisesRegex(tool.CardError, "complete readback"):
            device.write(0, image)

    def test_device_fail_is_never_accepted(self):
        with self.assertRaises(tool.CardError):
            card(["END WRITE FAIL reason=bad-page"]).write(0, bytes(256))

    def test_identification_rejects_incompatible_geometry(self):
        with self.assertRaisesRegex(tool.CardError, "incompatible"):
            card(["INFO protocol=1 bytes=1024"]).identify()

    def test_force_required_before_opening_any_port(self):
        with patch.object(tool, "Card") as constructor, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                tool.main(["test"])
            self.assertEqual(exc.exception.code, 2)
            constructor.assert_not_called()

    def test_bank_bounds(self):
        for bank in ("-1", "16", "1.5", "not-a-number"):
            with self.subTest(bank=bank), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                tool.parser().parse_args(["read-bank", bank, "unused.bin"])
        self.assertEqual(tool.bank_number("0"), 0)
        self.assertEqual(tool.bank_number("15"), 15)

    def test_wrong_image_size_and_existing_backup_do_not_open_port(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"image.bin"
            path.write_bytes(b"preserve me")
            with patch.object(tool, "Card") as constructor, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(tool.main(["restore", str(path)]), 1)
                self.assertEqual(tool.main(["write-bank", "15", str(path)]), 1)
                self.assertEqual(tool.main(["backup", str(path)]), 1)
                constructor.assert_not_called()
            self.assertEqual(path.read_bytes(), b"preserve me")

    def test_failed_backup_removes_only_its_incomplete_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"image.bin"
            with patch.object(tool, "select_port", side_effect=tool.CardError("no card")), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(tool.main(["backup", str(path)]), 1)
            self.assertFalse(path.exists())

    def test_test_requires_all_full_device_passes(self):
        device = card([end("TEST")])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(tool.CardError, "missing full-device"):
            device.test()


if __name__ == "__main__":
    unittest.main()
