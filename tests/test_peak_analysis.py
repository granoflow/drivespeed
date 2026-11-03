import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from peak_analysis import assess_peak_pair, assess_peak_scan


def case(qd, rate):
    return {
        "status": "completed", "rw": "read", "bs": 1048576, "qd": qd,
        "metrics": {"runtime_ms": 5000,
                    "read": {"bytes": 1000000, "runtime_ms": 5000, "mb_s": rate},
                    "write": {"bytes": 0},
                    "achieved_depth": {str(qd): 80}},
        "series": [{"seconds": second, "direction": "read", "mb_s": rate}
                   for second in (1, 2, 3, 4, 5)],
    }


class PeakAnalysisTests(unittest.TestCase):
    def test_plateau_increase_and_unstable(self):
        cases = [case(16, 100), case(16, 102), case(32, 103), case(32, 104)]
        self.assertEqual(assess_peak_pair(cases)["status"], "plateau")
        cases[2]["metrics"]["read"]["mb_s"] = 120
        cases[3]["metrics"]["read"]["mb_s"] = 122
        self.assertEqual(assess_peak_pair(cases)["status"], "increase")
        cases[2]["metrics"]["read"]["mb_s"] = 70
        cases[3]["metrics"]["read"]["mb_s"] = 70
        self.assertEqual(assess_peak_pair(cases)["status"], "decrease")
        cases[2]["metrics"]["read"]["mb_s"] = 120
        cases[3]["metrics"]["read"]["mb_s"] = 150
        self.assertEqual(assess_peak_pair(cases)["status"], "unstable")

    def test_scan_retains_highest_qualified_single_result(self):
        cases = []
        for repeat, rate in ((1, 100), (2, 130)):
            item = case(16, rate)
            item["name"] = f"peak_seq_read_16_{repeat}"
            cases.append(item)
        result = assess_peak_scan(cases)
        self.assertEqual(result["status"], "incomplete")
        best = result["workloads"]["seq_read"]["highest_single_by_qd"]
        self.assertEqual(best[0]["mb_s"], 130)
        self.assertEqual(best[0]["name"], "peak_seq_read_16_2")
        cases[1]["metrics"]["achieved_depth"]["16"] = 1
        result = assess_peak_scan(cases)
        self.assertEqual(result["workloads"]["seq_read"]["highest_single_by_qd"][0]["mb_s"], 100)

    def test_open_ended_64_bucket_and_higher_depth(self):
        cases = [case(32, 100), case(32, 100), case(64, 101), case(64, 101)]
        for item in cases[2:]:
            item["metrics"]["achieved_depth"] = {">=64": 80}
        self.assertEqual(assess_peak_pair(cases)["status"], "plateau")
        cases[2]["qd"] = cases[3]["qd"] = 128
        self.assertEqual(assess_peak_pair(cases)["status"], "incomplete")

    def test_scan_requires_four_independent_workloads(self):
        samples = []
        for workload, rw, block in (("seq_read", "read", 1048576),
                                    ("seq_write", "write", 1048576),
                                    ("rand_read", "randread", 4096),
                                    ("rand_write", "randwrite", 4096)):
            for depth in (16, 32):
                for repeat in (1, 2):
                    item = case(depth, 100)
                    item.update(name=f"peak_{workload}_{depth}_{repeat}", rw=rw, bs=block)
                    if rw in ("write", "randwrite"):
                        item["metrics"]["write"] = item["metrics"]["read"]
                        item["metrics"]["read"] = {"bytes": 0}
                        for point in item["series"]:
                            point["direction"] = "write"
                    samples.append(item)
        self.assertEqual(assess_peak_scan(samples)["status"], "complete")
        self.assertEqual(assess_peak_scan(samples[:-1])["status"], "incomplete")
        self.assertEqual(assess_peak_scan(samples + [samples[0]])["status"], "incomplete")

    def test_short_depth_and_different_workload_do_not_qualify(self):
        valid = [case(16, 100), case(16, 100), case(32, 101), case(32, 101)]
        for change in ("duration", "depth", "workload", "missing"):
            cases = copy.deepcopy(valid)
            if change == "duration":
                cases[2]["metrics"]["read"]["runtime_ms"] = 100
            elif change == "depth":
                cases[2]["metrics"]["achieved_depth"]["32"] = 1
            elif change == "workload":
                cases[2]["bs"] = 4096
            else:
                cases.pop()
            self.assertEqual(assess_peak_pair(cases)["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
