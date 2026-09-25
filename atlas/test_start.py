import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("atlas_start", Path(__file__).with_name("start.py"))
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class ConfigurationIsolation(unittest.TestCase):
    def test_root_bootstrap_reexecutes_after_uid_drop_before_package_imports(self):
        # No actual privilege or filesystem changes occur in this regression test.
        with patch.object(entry.os, "getuid", return_value=0, create=True), \
             patch.object(entry.os, "chown", create=True) as chown, \
             patch.object(entry.os, "setgroups", create=True) as groups, \
             patch.object(entry.os, "setgid", create=True) as gid, \
             patch.object(entry.os, "setuid", create=True) as uid, \
             patch.object(entry.os, "execvpe") as execute, \
             patch.dict(entry.os.environ, {"HOME": "/root"}):
            entry.drop_privileges(Path("/freqtrade/user_data"))
            chown.assert_called_once_with(Path("/freqtrade/user_data"), 1000, 1000)
            groups.assert_called_once_with([])
            gid.assert_called_once_with(1000)
            uid.assert_called_once_with(1000)
            self.assertEqual(execute.call_args.args[:2],
                             (entry.sys.executable, [entry.sys.executable, str(Path(entry.__file__).resolve())]))
            self.assertEqual(execute.call_args.args[2]["HOME"], "/home/ftuser")
        with patch.object(entry.os, "getuid", return_value=1000, create=True), \
             patch.object(entry.os, "execvpe") as execute:
            entry.drop_privileges(Path("/freqtrade/user_data"))
            execute.assert_not_called()

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
