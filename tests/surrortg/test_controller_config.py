import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from surrortg.controller_config import (
    ChangeEffect,
    ConfigurationConflict,
    ConfigurationError,
    ControllerConfig,
    ControllerConfigurationStore,
    classify_change,
    load_runtime_config,
)


class ControllerConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = ControllerConfigurationStore(root / "controller.toml", root / "credential")
        self.config = ControllerConfig(1, "controller-a", "https://signal.test/signaling", "12", "games.bot")

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_update_is_atomic_versioned_and_sanitized(self):
        result = self.store.update(self.config)
        self.store.replace_secret("do-not-leak")
        self.assertEqual(self.store.load(), self.config)
        self.assertEqual(result["effect"], ChangeEffect.RESTART.value)
        self.assertNotIn("do-not-leak", repr(self.store.sanitized()))
        self.assertEqual(os.stat(self.store.secret_path).st_mode & 0o777, 0o600)
        self.assertTrue(self.store.last_good_path.exists())

    def test_invalid_and_conflicting_updates_are_rejected(self):
        with self.assertRaises(ConfigurationError):
            ControllerConfig(2, "controller-a", "https://signal.test", "12")
        self.store.update(self.config)
        with self.assertRaises(ConfigurationConflict):
            self.store.update(self.config, expected_revision="stale")

    def test_failed_replace_preserves_usable_configuration(self):
        self.store.update(self.config)
        changed = ControllerConfig(1, "controller-a", "https://other.test", "12", "games.bot")
        real_replace = os.replace

        def fail_new(source, destination):
            if Path(destination) == self.store.config_path:
                raise OSError("interrupted")
            return real_replace(source, destination)

        with patch("surrortg.controller_config.os.replace", side_effect=fail_new):
            with self.assertRaises(OSError):
                self.store.update(changed)
        self.assertEqual(self.store.load(), self.config)

    def test_legacy_split_and_combined_import_and_explicit_path(self):
        root = Path(self.temp.name)
        split = root / "split.toml"
        split.write_text('device_id="c"\n[game_engine]\nurl="http://localhost:3"\nid="7"\ntoken="secret"\n')
        loaded = load_runtime_config(explicit_legacy_path=split)
        self.assertEqual(loaded["game_engine"]["token"], "secret")
        self.assertFalse(self.store.config_path.exists())

        combined = root / "combined.toml"
        combined.write_text('device_id="c"\n[game_engine]\nurl="http://localhost:3"\ntoken="7/secret"\n')
        self.store.import_legacy(combined)
        self.assertEqual(self.store.load().game_id, "7")
        self.assertEqual(self.store.load_secret(), "secret")
        self.assertIn("7/secret", combined.read_text())

    def test_change_classification(self):
        same = self.config
        reconnect = ControllerConfig(1, "controller-a", "https://other.test", "12", "games.bot")
        restart = ControllerConfig(1, "controller-a", "https://signal.test/signaling", "12", "games.other")
        self.assertEqual(classify_change(same, same), ChangeEffect.NONE)
        self.assertEqual(classify_change(same, reconnect), ChangeEffect.RECONNECT)
        self.assertEqual(classify_change(same, restart), ChangeEffect.RESTART)

