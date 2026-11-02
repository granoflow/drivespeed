"""Firmware identity admission using synthetic metadata; no device queries."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import device_identity as identity
from system_io import BenchError


class DeviceIdentityTests(unittest.TestCase):
    def target(self, disk="disk7", path="/fixture/first"):
        return dict(scope="physical", physical_devices=[disk], path=path, device=disk + "s1")

    def smart(self, device="/dev/disk7", model="Fixture SSD", serial="Private-Drive-8Q2"):
        return dict(device=dict(name=device, protocol="NVMe"), model_name=model, serial_number=serial)

    def collect(self, target=None, payload=None, internal=False, system="Darwin", error=None):
        target = target or self.target()
        payload = payload if payload is not None else self.smart()
        commands = []
        def query(command, **options):
            commands.append(command)
            self.assertEqual(options["timeout"], 5)
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertNotIn("shell", options)
            if command[0] == "/fixture/smartctl":
                self.assertEqual(command[1:3], ["-i", "-j"])
                if error:
                    raise error
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return subprocess.CompletedProcess(command, 0, stdout=raw)
            self.assertEqual(command, ["diskutil", "info", "-plist", "/dev/disk7"])
            raw = plistlib.dumps({"DeviceIdentifier": "disk7", **({} if internal is None else {"Internal": internal})})
            return subprocess.CompletedProcess(command, 0, stdout=raw)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(identity.shutil, "which", return_value="/fixture/smartctl"), \
             patch.object(identity.platform, "system", return_value=system), \
             patch.object(identity, "check_target", return_value=target), \
             patch.object(identity.subprocess, "run", side_effect=query), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            value = identity.collect_device_identity(target)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        return value, commands

    def test_same_drive_survives_mount_and_attachment_changes_without_serial_in_output(self):
        first, commands = self.collect(internal=True)
        second, _ = self.collect(target=self.target(path="/fixture/second"), internal=False)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual((first["attachment"], second["attachment"]), ("internal", "external"))
        expected = hashlib.sha256(json.dumps([identity.DOMAIN, "Fixture SSD", "Private-Drive-8Q2"],
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(first["fingerprint"], expected)
        self.assertEqual(set(first), {"version", "fingerprint", "source", "attachment"})
        self.assertNotIn("Private-Drive-8Q2", json.dumps(first))
        self.assertNotIn("Fixture SSD", json.dumps(first))
        self.assertEqual(commands[0][-1], "/dev/disk7")

    def test_model_and_serial_are_normalized_but_serial_case_is_not_folded(self):
        first, _ = self.collect(payload=self.smart(model="  Fixtu\u0301re SSD  ", serial="  Prive\u0301-8Q2 "))
        same, _ = self.collect(payload=self.smart(model="Fixtúre SSD", serial="Privé-8Q2"))
        other, _ = self.collect(payload=self.smart(model="Fixtúre SSD", serial="privé-8Q2"))
        self.assertEqual(first["fingerprint"], same["fingerprint"])
        self.assertNotEqual(first["fingerprint"], other["fingerprint"])

    def test_same_model_different_serial_and_different_model_do_not_merge(self):
        first, _ = self.collect()
        other, _ = self.collect(payload=self.smart(serial="Private-Drive-8Q3"))
        different_model, _ = self.collect(payload=self.smart(model="Different SSD"))
        self.assertEqual(len({first["fingerprint"], other["fingerprint"], different_model["fingerprint"]}), 3)

    def test_placeholder_repeated_missing_and_control_serials_are_not_identity(self):
        for serial in (None, "", "  ", "Unknown", "N/A", "00000000", "FFFFFFFF", "ABABAB",
                       "1234567890", "To be filled by O.E.M.", "private\nserial", True, 123):
            with self.subTest(serial=serial):
                value, _ = self.collect(payload=self.smart(serial=serial))
                self.assertIsNone(value)

    def test_wrong_disk_bridge_fields_and_non_drive_protocol_are_rejected(self):
        samples = [self.smart(device="/dev/disk8"),
                   dict(device=dict(name="/dev/disk7", protocol="SCSI"), model_name="USB Bridge", serial_number="Bridge-8Q2"),
                   dict(device=dict(name="/dev/disk7", protocol="NVMe"), usb={"serial_number": "Bridge-8Q2"}),
                   dict(model_name="Fixture SSD", serial_number="Private-Drive-8Q2")]
        for payload in samples:
            with self.subTest(payload=payload):
                value, _ = self.collect(payload=payload)
                self.assertIsNone(value)

    def test_nonunique_virtual_and_unresolved_targets_never_query_a_device(self):
        for target in (dict(self.target(), physical_devices=["disk7", "disk8"]),
                       dict(self.target(), physical_devices=["disk7", "disk7"]),
                       dict(self.target(), scope="virtual"), dict(self.target(), scope="unresolved"),
                       self.target(disk="disk7; command")):
            with self.subTest(target=target):
                value, commands = self.collect(target=target)
                self.assertIsNone(value)
                self.assertEqual(commands, [])

    def test_no_tool_permission_timeout_bad_json_and_nonzero_exit_return_missing(self):
        with patch.object(identity.shutil, "which", return_value=None), \
             patch.object(identity, "check_target") as inspect:
            self.assertIsNone(identity.collect_device_identity(self.target()))
            inspect.assert_not_called()
        for error in (PermissionError("private metadata"), subprocess.TimeoutExpired("smartctl", 5)):
            with self.subTest(error=type(error).__name__):
                value, _ = self.collect(error=error)
                self.assertIsNone(value)
        for raw in (b"not json", b"[]", b'{"serial_number":"one","serial_number":"two"}', b"x" * (identity.MAX_OUTPUT_BYTES + 1)):
            value, _ = self.collect(payload=raw)
            self.assertIsNone(value)
        with patch.object(identity.shutil, "which", return_value="/fixture/smartctl"), \
             patch.object(identity, "check_target", return_value=self.target()), \
             patch.object(identity.platform, "system", return_value="Darwin"), \
             patch.object(identity.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, stdout=b"private")):
            self.assertIsNone(identity.collect_device_identity(self.target()))

    def test_target_change_while_collecting_discards_fingerprint(self):
        target = self.target()
        with patch.object(identity.shutil, "which", return_value="/fixture/smartctl"), \
             patch.object(identity.platform, "system", return_value="Darwin"), \
             patch.object(identity, "check_target", side_effect=[target, BenchError("unsafe_target", "changed")]), \
             patch.object(identity, "_query", side_effect=[json.dumps(self.smart()).encode(), plistlib.dumps({"Internal": False})]):
            self.assertIsNone(identity.collect_device_identity(target))

    def test_attachment_requires_explicit_boolean_and_query_failure_does_not_lose_identity(self):
        for internal, expected in ((True, "internal"), (False, "external"), (None, "unknown"), (1, "unknown"), ("true", "unknown")):
            with self.subTest(internal=internal):
                value, _ = self.collect(internal=internal)
                self.assertEqual(value["attachment"], expected)
        with patch.object(identity.shutil, "which", return_value="/fixture/smartctl"), \
             patch.object(identity.platform, "system", return_value="Darwin"), \
             patch.object(identity, "check_target", return_value=self.target()), \
             patch.object(identity, "_query", side_effect=[json.dumps(self.smart()).encode(), OSError("metadata unavailable")]):
            self.assertEqual(identity.collect_device_identity(self.target())["attachment"], "unknown")

    def test_linux_exact_whole_disk_metadata_is_supported_without_attachment_guess(self):
        for disk in ("sda", "nvme0n1"):
            with self.subTest(disk=disk):
                value, commands = self.collect(target=self.target(disk=disk),
                    payload=self.smart(device="/dev/" + disk), system="Linux")
                self.assertEqual(value["attachment"], "unknown")
                self.assertEqual(commands, [["/fixture/smartctl", "-i", "-j", "/dev/" + disk]])

    def test_windows_unique_id_only_binds_the_exact_disk_number_not_fingerprint(self):
        target = dict(self.target(disk="windows-disk:fixture-hash", path="Q:\\data"), device="fixture-volume")
        with patch.object(identity.windows_storage, "volume_identity", return_value=("Q:\\", "fixture-volume")), \
             patch.object(identity.windows_storage, "disk_metadata", return_value={"number": 3, "identity": "fixture-hash"}):
            value, commands = self.collect(target=target, payload=self.smart(device="/dev/pd3"), system="Windows")
        self.assertIsNotNone(value)
        self.assertEqual(commands[0][-1], "/dev/pd3")
        with patch.object(identity.windows_storage, "volume_identity", return_value=("Q:\\", "fixture-volume")), \
             patch.object(identity.windows_storage, "disk_metadata", return_value={"number": 3, "identity": "different"}):
            missing, commands = self.collect(target=target, system="Windows")
        self.assertIsNone(missing)
        self.assertEqual(commands, [])

    def test_validation_rejects_raw_serial_extra_fields_and_invalid_contracts(self):
        valid, _ = self.collect()
        self.assertEqual(identity.validate_device_identity(valid), valid)
        for changes in ({"serial_number": "private"}, {"version": True}, {"fingerprint": "f" * 63},
                        {"source": "unknown"}, {"attachment": "PCIExpress"}, {"attachment": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                identity.validate_device_identity(dict(valid, **changes))


if __name__ == "__main__":
    unittest.main()
