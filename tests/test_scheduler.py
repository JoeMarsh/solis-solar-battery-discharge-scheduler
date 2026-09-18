import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import script
from script import Setting, SolisClient, SolisError, check_result, load_env


class Inverter:
    """Model shared switch registers, including stale-original rejection."""
    def __init__(self, soc=60, marker="43605", mode=96):
        self.soc = soc
        self.mode = mode
        self.flags = 0
        self.values = {6798: marker}
        self.values.update({cid: "50" for cid in script.SLOT_FIELDS})
        self.values.update({5946: script.EMPTY_TIME, 5964: script.EMPTY_TIME})
        self.writes = []
        self.fail_cid = None
        self.drop_cid = None
        self.snapshot_dir = None

    def battery_soc(self):
        return self.soc

    def read_setting(self, cid):
        if cid == 636:
            return Setting(str(self.mode), str(self.mode))
        if 5916 <= cid <= 5927:
            return Setting(str((self.flags >> (cid - 5916)) & 1), str(self.flags))
        return Setting(self.values[cid], self.values[cid])

    def write_setting(self, cid, value, original):
        if self.snapshot_dir is not None:
            assert list(Path(self.snapshot_dir).glob("*.json")), "Missing pre-write snapshot"
        if original != self.read_setting(cid):
            raise SolisError("Stale raw register value")
        self.writes.append((cid, str(value), original.raw))
        if cid == self.fail_cid:
            raise SolisError("Injected device rejection")
        if cid == self.drop_cid:
            return {"code": "0"}  # Simulate a success-looking ACK with no change.
        if cid == 636:
            self.mode = int(value)
            if self.values[6798] == "43605":
                self.mode &= ~2  # HMI 5103 ignores the old global TOU bit.
        elif 5916 <= cid <= 5927:
            mask = 1 << (cid - 5916)
            self.flags = (self.flags | mask) if value == "1" else (self.flags & ~mask)
        else:
            self.values[cid] = str(value)
        return {"code": "0", "data": [{"code": 0}]}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch("script.time.sleep")
        self.sleep.start()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.sleep.stop)
        self.addCleanup(self.directory.cleanup)

    def run_schedule(self, inverter, **kwargs):
        inverter.snapshot_dir = self.directory.name
        return script.manage_discharge(1, client=inverter, snapshot_dir=self.directory.name, **kwargs)

    def test_migrates_charge_and_discharge_and_preserves_feed_in(self):
        inv = Inverter(mode=96 | 16 | 128)
        self.run_schedule(inv)
        self.assertEqual(inv.mode, 96 | 16 | 128)
        self.assertEqual(inv.flags, 1 | 64)
        self.assertEqual(inv.values[5946], "02:05-05:55")
        self.assertEqual(inv.values[5948], "100")
        self.assertEqual(inv.values[5928], "100")
        self.assertEqual(inv.values[5964], "01:00-02:00")
        self.assertEqual(inv.values[5965], "20")
        self.assertNotIn(103, [cid for cid, _, _ in inv.writes])
        self.assertEqual(inv.writes[-1][0:2], (5922, "1"))

    def test_low_soc_restores_charge_and_disables_all_stale_discharge(self):
        for soc in (0, 15, 20):
            with self.subTest(soc=soc):
                inv = Inverter(soc=soc)
                inv.flags = 0xFFF
                self.run_schedule(inv)
                self.assertEqual(inv.flags, 1)
                self.assertEqual(inv.values[5964], "00:00-00:00")
                self.assertEqual(inv.values[5967], "0")
                self.assertEqual(inv.values[5946], "02:05-05:55")

    def test_clear_never_needs_soc(self):
        inv = Inverter()
        inv.battery_soc = Mock(side_effect=AssertionError("Must not query SOC"))
        self.run_schedule(inv, clear=True)
        self.assertEqual(inv.flags, 1)

    def test_dry_run_has_no_writes_or_snapshot(self):
        inv = Inverter()
        self.run_schedule(inv, dry_run=True)
        self.assertEqual(inv.writes, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_disabled_switch_updates_preserve_sibling_bits(self):
        inv = Inverter()
        inv.flags = 0xFFF
        script.write_verified(inv, 5922, "0")
        self.assertEqual(inv.flags, 0xFFF & ~64)
        self.assertEqual(inv.writes[0][2], "4095")
        script.write_verified(inv, 5923, "0")
        self.assertEqual(inv.writes[1][2], str(0xFFF & ~64))

    def test_failed_charge_write_never_enables_discharge_or_changes_mode(self):
        inv = Inverter()
        inv.flags = 64
        inv.fail_cid = 5946
        with self.assertRaises(SolisError):
            self.run_schedule(inv)
        self.assertEqual(inv.flags & 64, 0)
        self.assertEqual(inv.mode, 96)
        self.assertNotIn((5922, "1"), [(cid, value) for cid, value, _ in inv.writes])

    def test_success_ack_with_wrong_readback_fails(self):
        inv = Inverter()
        inv.drop_cid = 5946
        with self.assertRaisesRegex(SolisError, "readback"):
            self.run_schedule(inv)
        self.assertEqual(sum(cid == 5946 for cid, _, _ in inv.writes), 1)

    def test_final_verification_failure_disables_discharge(self):
        inv = Inverter()
        with patch("script.verify_schedule", side_effect=SolisError("drift")):
            with self.assertRaises(SolisError):
                self.run_schedule(inv)
        self.assertEqual(inv.flags, 1)

    def test_same_clear_schedule_does_not_write_again(self):
        inv = Inverter()
        self.run_schedule(inv, clear=True)
        inv.writes.clear()
        self.run_schedule(inv, clear=True)
        self.assertEqual(inv.writes, [])

    def test_preserves_self_use_if_selected(self):
        inv = Inverter(mode=1)
        self.run_schedule(inv, clear=True)
        self.assertEqual(inv.mode, 33)

    def test_new_firmware_uses_slot_enables_without_legacy_tou_bit(self):
        inv = Inverter(mode=64)
        self.run_schedule(inv, clear=True)
        self.assertEqual(inv.mode, 96)
        self.assertEqual(inv.flags, 1)
        self.assertEqual([value for cid, value, _ in inv.writes if cid == 636], ["96"])

    def test_concurrent_mode_change_is_not_overwritten(self):
        inv = Inverter(mode=33)
        with self.assertRaisesRegex(SolisError, "mode changed"):
            script.write_verified(inv, 636, "98")
        self.assertEqual(inv.writes, [])

    def test_mode_write_cannot_change_old_tou_bit(self):
        inv = Inverter(mode=96)
        with self.assertRaisesRegex(SolisError, "mode changed"):
            script.write_verified(inv, 636, "98")
        self.assertEqual(inv.writes, [])

    def test_unknown_capability_or_offgrid_fails_without_writes(self):
        for inv in [Inverter(marker="123"), Inverter(mode=4)]:
            with self.assertRaises(SolisError):
                self.run_schedule(inv)
            self.assertEqual(inv.writes, [])

    def test_legacy_firmware_rejected_without_writes_or_snapshot(self):
        inv = Inverter(marker="0")
        with self.assertRaisesRegex(SolisError, "Six-slot firmware required"):
            self.run_schedule(inv)
        self.assertEqual(inv.writes, [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_invalid_duration_or_soc_and_midnight_boundary(self):
        for hours in (0, -1, 21, 24, 1.5, True):
            with self.assertRaises(ValueError):
                script.calculate_discharge_current(60, hours)
        for soc in (None, -1, 101, float("nan"), True):
            with self.assertRaises(ValueError):
                script.calculate_discharge_current(soc, 1)
        self.assertEqual(script.schedule_values(60, 2), (80, "00:00-02:00"))
        self.assertEqual(script.schedule_values(60, 3)[1], "23:00-02:00")

    def test_dry_run_cli_never_notifies(self):
        with patch("script.configure_logging"), patch("script.load_env"), patch("script.client_from_config", return_value=Inverter()), patch("script.send_discord_message") as notify:
            self.assertEqual(script.main(["--dry-run", "--clear"]), 0)
            notify.assert_not_called()


class ConfigurationTests(unittest.TestCase):
    def test_hardcoded_settings_take_precedence(self):
        values = {"API_KEY": "inline-key", "API_SECRET": "inline-secret", "INVERTER_SN": "inline-serial"}
        with patch.multiple(script, **values), patch.dict(os.environ, {name: "environment" for name in values}):
            client = script.client_from_config()
            self.assertEqual((client.key, client.secret, client.serial), tuple(values.values()))

    def test_placeholder_settings_allow_optional_environment(self):
        values = {"API_KEY": "env-key", "API_SECRET": "env-secret", "INVERTER_SN": "env-serial"}
        with patch.multiple(script, **{name: "your_placeholder" for name in values}), patch.dict(os.environ, values, clear=True):
            client = script.client_from_config()
            self.assertEqual((client.key, client.secret, client.serial), tuple(values.values()))

    def test_hardcoded_webhook_is_used(self):
        with patch.object(script, "DISCORD_WEBHOOK_URL", "https://example.invalid/inline"), patch.dict(os.environ, {}, clear=True), patch("script.requests.post") as post:
            script.send_discord_message("test")
            self.assertEqual(post.call_args.args[0], "https://example.invalid/inline")

    def test_copied_script_runs_alone_with_hardcoded_config(self):
        with tempfile.TemporaryDirectory() as directory:
            standalone = Path(directory) / "script.py"
            source = Path(script.__file__).read_text(encoding="utf-8")
            for name in ("api_key", "api_secret", "inverter_sn"):
                source = source.replace(f'"your_{name}_here"', f'"inline-{name}"')
            standalone.write_text(source, encoding="utf-8")
            code = (
                "import os, runpy, sys; "
                "[os.environ.pop(k, None) for k in ('API_KEY', 'API_SECRET', 'INVERTER_SN')]; "
                "ns = runpy.run_path(sys.argv[1]); "
                "ns['load_env'](sys.argv[1] + '.missing-env'); "
                "client = ns['client_from_config'](); "
                "assert (client.key, client.secret, client.serial) == "
                "('inline-api_key', 'inline-api_secret', 'inline-inverter_sn')"
            )
            result = subprocess.run([sys.executable, "-I", "-c", code, str(standalone)], cwd=directory, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch("script.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_direct_completed_read_does_not_poll_orderid_value(self):
        client = SolisClient("key", "secret", "serial")
        client.post = Mock(return_value={"code": "0", "data": {"msg": "0", "yuanzhi": "96", "needLoop": "false"}, "orderId": "0"})
        self.assertEqual(client.read_setting(543), Setting("0", "96"))
        client.post.assert_called_once()

    def test_async_read_waits_and_requeries_parameter(self):
        client = SolisClient("key", "secret", "serial")
        client.post = Mock(side_effect=[
            {"code": "0", "data": {"needLoop": "true"}, "orderId": "pending"},
            {"code": "0", "data": None},
            {"code": "0", "data": {"msg": "1", "yuanzhi": "1", "needLoop": False}},
        ])
        self.assertEqual(client.read_setting(5916).value, "1")
        self.assertEqual(client.post.call_args_list[1].args[0], "/v2/api/result")

    def test_missing_read_payload_is_not_zero(self):
        client = SolisClient("key", "secret", "serial")
        client.post = Mock(return_value={"code": "0", "data": None})
        with self.assertRaises(SolisError):
            client.read_setting(6798)

    def test_masked_switch_read_requires_raw_value(self):
        client = SolisClient("key", "secret", "serial")
        client.post = Mock(return_value={"code": "0", "data": [{"code": "0", "cid": "5916", "value": "0"}]})
        with self.assertRaisesRegex(SolisError, "raw switch"):
            client.read_setting(5916)

    def test_soc_rejects_stale_or_missing_telemetry_timestamp(self):
        client = SolisClient("key", "secret", "serial")
        with patch("script.time.time", return_value=10000):
            for data in [{"batteryCapacitySoc": 60}, {"batteryCapacitySoc": 60, "dataTimestamp": "1000000"}]:
                client.post = Mock(return_value={"code": "0", "data": data})
                with self.assertRaises(SolisError):
                    client.battery_soc()
            client.post = Mock(return_value={"code": "0", "data": {"batteryCapacitySoc": 60, "dataTimestamp": "9900000"}})
            self.assertEqual(client.battery_soc(), 60)

    def test_server_and_nested_device_errors_are_rejected(self):
        for response in [{"code": "403"}, {"success": False, "code": "0"}, {"code": "0", "data": [{"code": 1}]}, {"code": "0", "data": [{"errorMsg": "failed"}]}, {}]:
            with self.assertRaises(SolisError):
                check_result(response)

    def test_write_timeout_is_not_retried(self):
        session = Mock()
        session.post.side_effect = requests.ReadTimeout()
        client = SolisClient("key", "secret", "serial", session=session)
        with self.assertRaisesRegex(SolisError, "outcome unknown"):
            client.write_setting(5916, "1", Setting("0", "64"))
        session.post.assert_called_once()
        body = json.loads(session.post.call_args.kwargs["data"])
        self.assertEqual(body["yuanzhi"], "64")

    def test_time_write_uses_separate_start_end_fields(self):
        client = SolisClient("key", "secret", "serial")
        client.post = Mock(return_value={"code": "0"})
        client.write_setting(5946, "02:05-05:55", Setting("00:00-00:00", "00:00-00:00"))
        self.assertEqual(client.post.call_args.args[1]["value"], "02:05,05:55")
        client.write_setting(5964, "01:00-02:00", Setting("00:00-00:00", "00:00-00:00"))
        self.assertEqual(client.post.call_args.args[1]["value"], "01:00,02:00")

    def test_env_loading_keeps_exported_values_and_never_evaluates_shell(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"API_KEY": "exported"}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('API_KEY=file\nAPI_SECRET="literal$(command)" # comment\nINVERTER_SN=serial\n')
            load_env(path)
            self.assertEqual(os.environ["API_KEY"], "exported")
            self.assertEqual(os.environ["API_SECRET"], "literal$(command)")


if __name__ == "__main__":
    unittest.main()
