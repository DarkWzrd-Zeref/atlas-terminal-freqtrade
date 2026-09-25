import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("atlas_start", Path(__file__).with_name("start.py"))
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class ConfigurationIsolation(unittest.TestCase):
    def test_restarts_retain_credentials_and_lab_cannot_replace_paper_strategy(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            paper = entry.prepare_config(data, source, {"PORT": "8080"})
            restart = entry.prepare_config(data, source, {"PORT": "8080"})
            self.assertEqual(paper["api_server"], restart["api_server"])
            lab = entry.prepare_lab_config(paper, data)
            self.assertEqual(paper["api_server"]["listen_port"], 8080)
            self.assertEqual(lab["api_server"]["listen_port"], 8081)
            self.assertNotIn("timeframe", lab, "use the selected strategy's native timeframe")
            standalone = entry.prepare_lab_config(paper, data, 8080)
            self.assertEqual(standalone["api_server"]["listen_port"], 8080)
            self.assertTrue(lab["dry_run"])
            self.assertEqual(lab["exchange"]["key"], "")
            research = Path(lab["user_data_dir"]) / "strategies/sample_strategy.py"
            research.write_text("# candidate under evaluation")
            self.assertNotEqual(research.read_text(), (data / "strategies/sample_strategy.py").read_text())
            entry.prepare_lab_config(paper, data)
            self.assertEqual(research.read_text(), "# candidate under evaluation")

    def test_only_explicit_roles_are_accepted(self):
        self.assertEqual(entry.service_role({"ATLAS_FREQTRADE_ROLE": "lab"}), "lab")
        self.assertEqual(entry.service_role({"ATLAS_FREQTRADE_ROLE": "paper"}), "paper")
        with self.assertRaises(ValueError):
            entry.service_role({"ATLAS_FREQTRADE_ROLE": "live"})


if __name__ == "__main__":
    unittest.main()
