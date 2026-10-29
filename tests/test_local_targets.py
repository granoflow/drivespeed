"""Local volume admission with machine-output fixtures; never invokes real fio."""

import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark
import hardware
import system_io


class LocalTargetTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="drivespeed-target-fixture-")).resolve()
        self.target = self.root / "target"
        self.target.mkdir()
        self.disk = dict(DeviceIdentifier="disk3s5", MountPoint="/System/Volumes/Data",
                         Internal=True, Writable=True, WritableVolume=True,
                         FilesystemType="apfs", BusProtocol="Apple Fabric",
                         APFSContainerReference="disk3",
                         APFSPhysicalStores=[{"APFSPhysicalStore": "disk0s2"}])
        self.disks = {"disk3s5": self.disk,
                      "disk0s2": dict(DeviceIdentifier="disk0s2", ParentWholeDisk="disk0",
                                       VirtualOrPhysical="Physical", Whole=False),
                      "disk0": dict(DeviceIdentifier="disk0", Whole=True, VirtualOrPhysical="Physical")}
        self.stat_devices = {}

    def mac_capture(self, args):
        if args[:3] == ["stat", "-f", "%Sd"]:
            return (self.stat_devices.get(args[3], "disk3s5") + "\n").encode()
        if args[:3] == ["diskutil", "info", "-plist"]:
            return plistlib.dumps(self.disks[args[3]])
        if args[:4] == ["diskutil", "apfs", "list", "-plist"]:
            return plistlib.dumps({"Containers": [{"ContainerReference": "disk3",
                "PhysicalStores": [{"DeviceIdentifier": "disk0s2"}]}]})
        if args == ["hdiutil", "info", "-plist"]:
            return plistlib.dumps(self.images)
        self.fail("Unexpected external command: " + repr(args))

    def mac_info(self, path=None):
        with patch.object(system_io.platform, "system", return_value="Darwin"), \
             patch.object(system_io, "capture", side_effect=self.mac_capture):
            return system_io.target_info(path or self.target)

    def linux_info(self, nodes, *, options="rw,relatime", source="/dev/nvme0n1p2", mount="/"):
        def capture(args):
            if args[0] == "findmnt":
                return json.dumps({"filesystems": [{"source": source, "fstype": "ext4",
                                                    "target": mount, "options": options}]}).encode()
            if args[0] == "lsblk":
                return json.dumps({"blockdevices": nodes}).encode()
            self.fail("Unexpected external command: " + repr(args))
        original_read = Path.read_text
        def read_text(path, *args, **kwargs):
            if str(path).startswith("/sys/class/nvme/") and path.name == "transport":
                return "pcie"
            return original_read(path, *args, **kwargs)
        with patch.object(system_io.platform, "system", return_value="Linux"), \
             patch.object(system_io, "capture", side_effect=capture), \
             patch.object(Path, "read_text", read_text):
            return system_io.target_info(self.target)

    def test_mac_internal_data_directory_uses_device_and_real_mount(self):
        with patch.object(system_io, "capture", side_effect=self.mac_capture) as query, \
             patch.object(system_io.platform, "system", return_value="Darwin"):
            result = system_io.target_info(self.target)
        self.assertEqual(result["physical_devices"], ["disk0"])
        self.assertEqual(result["mount"], "/System/Volumes/Data")
        self.assertEqual(result["scope"], "physical")
        self.assertTrue(result["internal"])
        self.assertIn(["stat", "-f", "%Sd", str(self.target)], [call.args[0] for call in query.call_args_list])
        self.assertFalse(any(call.args[0] == ["diskutil", "info", "-plist", str(self.target)]
                             for call in query.call_args_list))

    def test_mac_apple_fabric_unknown_whole_device_matches_current_plist(self):
        self.disks["disk0"] = dict(WholeDisk=True, ParentWholeDisk="disk0", Internal=True,
            BusProtocol="Apple Fabric", SystemImage=False, VirtualOrPhysical="Unknown")
        self.assertEqual(self.mac_info()["physical_devices"], ["disk0"])
        self.disks["disk0"]["BusProtocol"] = "Unknown"
        with self.assertRaises(system_io.BenchError):
            self.mac_info()

    def test_mac_external_and_internal_share_admission_rules(self):
        for internal in (True, False):
            with self.subTest(internal=internal):
                self.disk["Internal"] = internal
                self.assertEqual(self.mac_info()["physical_devices"], ["disk0"])

    def test_mac_read_only_volume_cannot_be_overridden_by_writable_media(self):
        self.disk["WritableVolume"] = False
        with self.assertRaises(system_io.BenchError):
            self.mac_info()

    def test_mac_apfs_sibling_volume_uses_same_physical_lock(self):
        first = self.mac_info()
        self.disk["DeviceIdentifier"] = "disk3s6"
        self.disks["disk3s6"] = self.disk
        self.stat_devices[str(self.target)] = "disk3s6"
        second = self.mac_info()
        self.assertNotEqual(first["device"], second["device"])
        self.assertEqual(first["physical_devices"], second["physical_devices"])

    def test_mac_container_lookup_is_used_when_volume_omits_stores(self):
        self.disk.pop("APFSPhysicalStores")
        self.assertEqual(self.mac_info()["physical_devices"], ["disk0"])

    def test_mac_multiple_physical_stores_are_all_retained(self):
        self.disk["APFSPhysicalStores"].append({"APFSPhysicalStore": "disk1s2"})
        self.disks["disk1s2"] = dict(ParentWholeDisk="disk1", Whole=False)
        self.disks["disk1"] = dict(Whole=True, VirtualOrPhysical="Physical")
        result = self.mac_info()
        self.assertEqual(result["physical_devices"], ["disk0", "disk1"])
        self.assertEqual(result["scope"], "multi_device")

    def test_mac_missing_store_identity_is_rejected(self):
        self.disk["APFSPhysicalStores"] = [{}]
        with self.assertRaises(system_io.BenchError):
            self.mac_info()

    def test_mac_image_maps_to_its_local_host_disk(self):
        image = self.root / "fixture.dmg"
        image.write_bytes(b"fixture, not a real disk image")
        self.stat_devices[str(self.target)] = "disk9s1"
        self.disks["disk9s1"] = dict(MountPoint=str(self.target), Writable=True,
                                      ParentWholeDisk="disk9", FilesystemType="hfs")
        self.disks["disk9"] = dict(Whole=True, VirtualOrPhysical="Virtual")
        self.images = {"images": [{"image-path": str(image),
                       "system-entities": [{"dev-entry": "/dev/disk9"}]}]}
        result = self.mac_info()
        self.assertEqual(result["physical_devices"], ["disk0"])
        self.assertEqual(result["scope"], "virtual")

    def test_mac_unknown_virtual_backing_is_not_accepted(self):
        self.disk["APFSPhysicalStores"] = [{"APFSPhysicalStore": "disk9"}]
        self.disks["disk9"] = dict(Whole=True, VirtualOrPhysical="Virtual")
        self.images = {"images": []}
        with self.assertRaises(system_io.BenchError):
            self.mac_info()

    def test_linux_nvme_sata_usb_are_local(self):
        for transport, name in (("nvme", "nvme0n1"), ("sata", "sda"), ("usb", "sdb")):
            with self.subTest(transport=transport):
                result = self.linux_info([dict(name=name, type="disk", tran=transport, ro=0)])
                self.assertEqual(result["physical_devices"], [name])
                self.assertEqual(result["scope"], "physical")

    def test_linux_lvm_locks_every_underlying_disk(self):
        node = dict(name="dm-0", type="lvm", ro=0, children=[
            dict(name="nvme0n1p2", type="part", ro=0, children=[dict(name="nvme0n1", type="disk", ro=0, tran="nvme")]),
            dict(name="sda2", type="part", ro=0, children=[dict(name="sda", type="disk", ro=0, tran="sata")])])
        result = self.linux_info([node], source="/dev/mapper/data")
        self.assertEqual(result["physical_devices"], ["nvme0n1", "sda"])
        self.assertEqual(result["scope"], "multi_device")

    def test_linux_network_and_read_only_mounts_are_rejected(self):
        for source, options in (("server:/share", "rw"), ("//server/share", "rw"), ("/dev/sda1", "ro,relatime")):
            with self.subTest(source=source, options=options), self.assertRaises(system_io.BenchError):
                self.linux_info([], source=source, options=options)

    def test_linux_incomplete_or_read_only_dependencies_are_rejected(self):
        for node in (dict(name="dm-0", type="lvm", ro=0), dict(name="sda", type="disk", ro=1),
                     dict(name="sda", type="disk"), dict(name="unknown", type="unknown", ro=0)):
            with self.subTest(node=node), self.assertRaises(system_io.BenchError):
                self.linux_info([node])

    def test_linux_loop_maps_backing_file_to_host_disk(self):
        image = self.root / "fixture.img"
        image.write_bytes(b"ordinary fixture")
        def capture(args):
            if args[0] == "findmnt":
                source = "/dev/loop0" if args[3] == str(self.target) else "/dev/sda1"
                return json.dumps({"filesystems": [{"source": source, "fstype": "ext4",
                    "target": str(self.target) if source == "/dev/loop0" else "/", "options": "rw"}]}).encode()
            if args[0] == "lsblk":
                node = dict(name="loop0", type="loop", ro=0) if args[-1] == "/dev/loop0" else dict(name="sda", type="disk", ro=0, tran="sata")
                return json.dumps({"blockdevices": [node]}).encode()
            self.fail("Unexpected command")
        original = Path.read_text
        def read_text(path, *args, **kwargs):
            if str(path) == "/sys/class/block/loop0/loop/backing_file":
                return str(image)
            return original(path, *args, **kwargs)
        with patch.object(system_io.platform, "system", return_value="Linux"), \
             patch.object(system_io, "capture", side_effect=capture), \
             patch.object(Path, "read_text", read_text):
            result = system_io.target_info(self.target)
        self.assertEqual(result["physical_devices"], ["sda"])
        self.assertEqual(result["scope"], "virtual")

    def test_linux_remote_and_unresolved_transport_are_rejected(self):
        for name, transport in (("sda", "iscsi"), ("nbd0", "sata"), ("rbd0", None),
                                 ("vda", None), ("sda", "fc"), ("sda", "unknown")):
            with self.subTest(name=name, transport=transport), self.assertRaises(system_io.BenchError):
                self.linux_info([dict(name=name, type="disk", ro=0, tran=transport)])

    def test_linux_missing_transport_requires_positive_local_sysfs_evidence(self):
        for location, accepted in (("/sys/devices/pci0000:00/ata1/host0/target0:0:0", True),
                                   ("/sys/devices/platform/host0/session2/target0:0:0", False),
                                   ("/sys/devices/pci0000:00/virtio0/host0", False)):
            with self.subTest(location=location), patch.object(Path, "resolve", return_value=Path(location)):
                if accepted:
                    system_io._linux_local_disk(dict(name="sdc", tran=None))
                else:
                    with self.assertRaises(system_io.BenchError):
                        system_io._linux_local_disk(dict(name="sdc", tran=None))

    def test_linux_nvme_over_fabrics_is_rejected(self):
        original = Path.read_text
        def read_text(path, *args, **kwargs):
            if str(path).startswith("/sys/class/nvme/"):
                return "tcp"
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read_text):
            with self.assertRaises(system_io.BenchError):
                system_io._linux_local_disk(dict(name="nvme0n1", tran="nvme"))

    def test_mac_partition_parent_virtual_metadata_is_resolved(self):
        self.disk["APFSPhysicalStores"] = [{"APFSPhysicalStore": "disk9s2"}]
        self.disks["disk9s2"] = dict(ParentWholeDisk="disk9", Whole=False)
        self.disks["disk9"] = dict(Whole=True, VirtualOrPhysical="Virtual")
        self.images = {"images": []}
        with self.assertRaises(system_io.BenchError):
            self.mac_info()

    def test_mac_device_cycle_and_malformed_records_are_rejected(self):
        for store in ("bad", 42):
            with self.subTest(store=store), self.assertRaises(system_io.BenchError):
                self.disk["APFSPhysicalStores"] = [store]
                self.mac_info()
        self.disk["APFSPhysicalStores"] = [{"APFSPhysicalStore": "disk3s5"}]
        with self.assertRaises(system_io.BenchError):
            self.mac_info()

    def test_backing_file_on_different_mount_is_rejected(self):
        image = self.root / "bind.img"
        image.write_bytes(b"ordinary fixture")
        original = Path.stat
        class DifferentDevice:
            st_dev = -123
        def stat(path, *args, **kwargs):
            actual = original(path, *args, **kwargs)
            return DifferentDevice() if path == image else actual
        # Preserve normal file/type checks, isolate the cross-device stat result.
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "is_symlink", return_value=False), \
             patch.object(Path, "stat", stat), self.assertRaises(system_io.BenchError):
            system_io._backing_target(str(image), set())

    def test_unwritable_directory_is_rejected_before_device_query(self):
        with patch.object(system_io.os, "access", return_value=False), \
             patch.object(system_io, "capture") as query, self.assertRaises(system_io.BenchError):
            system_io.target_info(self.target)
        query.assert_not_called()

    def test_mapping_cycle_is_rejected(self):
        with self.assertRaises(system_io.BenchError):
            system_io.target_info(self.target, _seen={str(self.target)})

    def test_target_help_describes_local_storage_without_relaxation_flag(self):
        help_text = benchmark.parser().format_help()
        self.assertIn("internal or external", help_text)
        self.assertIn("never a raw device", help_text)
        self.assertNotIn("--allow-system", help_text)

    def test_hardware_matching_model_is_separate_from_display(self):
        result = hardware.mac_hardware({"SPHardwareDataType": [{"machine_name": "Mac",
                  "machine_model": "Mac99,1", "chip_type": "Chip", "serial_number": "PRIVATE"}]}, ["disk0"])
        self.assertEqual(result["computer"]["matching_model"], "Mac99,1")
        self.assertIn("Chip", result["computer"]["model"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_multi_disk_display_is_not_a_single_drive_matching_model(self):
        profile = {"SPNVMeDataType": [{"bsd_name": "disk0", "device_model": "SSD"},
                                      {"bsd_name": "disk1", "device_model": "SSD"}]}
        result = hardware.mac_hardware(profile, ["disk0", "disk1"])
        self.assertIsNone(result["drive"]["matching_model"])


if __name__ == "__main__":
    unittest.main()
