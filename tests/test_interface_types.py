"""Offline alias normalization, with no disk measurements or model calls."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from interface_types import normalise_interface


class InterfaceTypesTests(unittest.TestCase):
    def test_confirmed_cli_interface_reaches_upload_without_overriding_the_system_protocol(self):
        import benchmark
        import hardware
        from test_community import local_result, ID
        from community_data import project_result, transport
        result=local_result();original=result["target"]["protocol"]
        # The real argument parser and hardware fact collector, with discovery
        # mocked; this does not start benchmark or probe a disk.
        args=benchmark.parser().parse_args(["--target","/fixture-only","--interface-name","用户接口 X1"])
        with patch.object(hardware.platform,"system",return_value="Fixture"):
            facts=hardware.collect_hardware(result["target"],access_interface=args.interface_name)
        result["hardware"]["access_interface"]=facts["access_interface"]
        payload=project_result(result,"https://x.com/fixture_user",submission_id=ID)
        self.assertEqual(payload["storage"]["connection"],"用户接口 X1")
        self.assertEqual(result["target"]["protocol"],original)
        self.assertIn("用户接口 X1",json.loads(transport(payload))["payload_json"])

    def test_explicit_equivalent_names_share_one_type(self):
        for name in ("USB 3.1 Gen 2","USB 3.2 Gen 2"," USB  3.2 GEN 2x1 ","ＵＳＢ ３.２ Ｇｅｎ ２"):
            with self.subTest(name=name):
                result=normalise_interface(name)
                self.assertEqual(result["type_id"],"usb3-10gbps")
                self.assertEqual(result["original_name"],name)
        self.assertEqual(normalise_interface("IDE")["type_id"],"pata")

    def test_ambiguous_or_new_name_stays_as_supplied_without_nearest_match(self):
        for name in ("USB-C","USB 3.2","USB 20 Gbps","用户接口 X1"):
            with self.subTest(name=name):
                value=normalise_interface(name,"zh-CN")
                self.assertEqual(value["status"],"user")
                self.assertIsNone(value["type_id"])
                self.assertEqual(value["name"],name)
        self.assertNotEqual(normalise_interface("USB 3.2 Gen 2x2")["type_id"],normalise_interface("USB4 20 Gbps")["type_id"])

    def test_current_dictionary_is_in_memory_only_and_bad_input_is_rejected(self):
        catalog={"schema_version":1,"revision":7,"items":[{"id":"new-test","label":{"en":"New test","zh":"新接口"},"aliases":["new alias"]}]}
        self.assertEqual(normalise_interface("new alias",catalog=catalog)["revision"],7)
        for value in (None,False,"x"*81):
            with self.subTest(value=value),self.assertRaises(ValueError):
                normalise_interface(value)
