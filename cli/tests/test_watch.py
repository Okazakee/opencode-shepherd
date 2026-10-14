import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocshepherd.watch import check, fail_alert, format_alerts, load_state

NOW = 1_800_000_000_000


def snap(sessions):
    return {"ts": NOW, "sessions": sessions}


def sess(sid, last=None, **kw):
    base = {
        "id": sid,
        "parent": "",
        "title": sid,
        "active": False,
        "updated": NOW,
        "idle_ts": 0,
        "outcome": "succeeded",
        "last": last or {"type": "idle", "ts": NOW - 300_000, "outcome": "succeeded"},
    }
    base.update(kw)
    return base


class CheckTests(unittest.TestCase):
    def fresh_state(self):
        return load_state("/nonexistent-path-state.json")

    def test_seed_is_silent_then_new_idle_alerts_once(self):
        st = self.fresh_state()
        s = sess("ses_a", last={"type": "idle", "ts": NOW - 300_000,
                                "outcome": "succeeded", "text": "old"})
        self.assertEqual(check(snap([s]), st, now_ms=NOW), [])
        s2 = sess("ses_a", last={"type": "idle", "ts": NOW - 200_000,
                                 "outcome": "succeeded", "text": "done here"})
        alerts = check(snap([s2]), st, now_ms=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertIn("waiting for input", alerts[0])
        self.assertIn("done here", alerts[0])
        # same event again -> no duplicate
        self.assertEqual(check(snap([s2]), st, now_ms=NOW + 1000), [])

    def test_form_alert_reported_even_on_first_run(self):
        st = self.fresh_state()
        s = sess(
            "ses_b",
            form=[{
                "id": "f1",
                "title": "Pick",
                "fields": [{
                    "key": "q0", "type": "string", "title": "Choose",
                    "options": [{"value": "a", "label": "Alpha"},
                                {"value": "b", "label": "Beta"}],
                }],
            }],
            last={"type": "idle", "ts": NOW - 300_000, "outcome": "succeeded"},
        )
        alerts = check(snap([s]), st, now_ms=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertIn("asks", alerts[0])
        self.assertIn("Alpha / Beta", alerts[0])
        self.assertEqual(check(snap([s]), st, now_ms=NOW + 1000), [])

    def test_permission_alert(self):
        st = self.fresh_state()
        st["seeded"] = True
        s = sess("ses_c", perm=[{
            "id": "p1", "action": "bash",
            "resources": ["rm -rf /tmp/x"], "message": "allow?",
        }])
        alerts = check(snap([s]), st, now_ms=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertIn("requests permission", alerts[0])
        self.assertIn("rm -rf /tmp/x", alerts[0])

    def test_background_shell_never_alerts(self):
        st = self.fresh_state()
        st["seeded"] = True
        s = sess("ses_d", last={
            "type": "idle", "ts": NOW - 300_000, "outcome": "succeeded",
            "shells": {"ids": ["sh_1"], "running": ["sh_1"]},
        })
        self.assertEqual(check(snap([s]), st, now_ms=NOW), [])

    def test_fresh_transition_grace(self):
        st = self.fresh_state()
        st["seeded"] = True
        s = sess("ses_e", last={"type": "idle", "ts": NOW - 30_000, "outcome": "succeeded"})
        self.assertEqual(check(snap([s]), st, now_ms=NOW), [])
        alerts = check(snap([s]), st, now_ms=NOW + 60_000)
        self.assertEqual(len(alerts), 1)

    def test_child_sessions_do_not_alert_on_stop(self):
        st = self.fresh_state()
        st["seeded"] = True
        s = sess("ses_f", parent="ses_parent")
        self.assertEqual(check(snap([s]), st, now_ms=NOW), [])

    def test_failed_turn_alerts(self):
        st = self.fresh_state()
        st["seeded"] = True
        s = sess("ses_g", last={"type": "idle", "ts": NOW - 300_000,
                                "outcome": "failed", "text": "boom"})
        alerts = check(snap([s]), st, now_ms=NOW)
        self.assertEqual(len(alerts), 1)
        self.assertIn("turn ended (failed)", alerts[0])

    def test_fail_counter_and_cooldown(self):
        st = self.fresh_state()
        self.assertIsNone(fail_alert(st, "boom", max_fails=3, cooldown_hours=6, now_ms=NOW))
        self.assertIsNone(fail_alert(st, "boom", max_fails=3, cooldown_hours=6, now_ms=NOW + 1))
        alert = fail_alert(st, "boom", max_fails=3, cooldown_hours=6, now_ms=NOW + 2)
        self.assertIsNotNone(alert)
        self.assertIsNone(fail_alert(st, "boom", max_fails=3, cooldown_hours=6, now_ms=NOW + 3))

    def test_format_alerts(self):
        self.assertEqual(format_alerts([]), "")
        text = format_alerts(["one"])
        self.assertIn("opencode-shepherd", text)
        self.assertIn("one", text)

    def test_reminder_after_threshold_repeats(self):
        st = self.fresh_state()
        st["seeded"] = True
        s = sess("ses_r", form=[{"id": "f9", "title": "Q", "fields": [
            {"key": "k", "type": "string", "title": "Pick"}]}])
        t0 = NOW
        alerts = check(snap([s]), st, now_ms=t0, remind_after_minutes=30)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(check(snap([s]), st, now_ms=t0 + 10 * 60000,
                               remind_after_minutes=30), [])
        alerts = check(snap([s]), st, now_ms=t0 + 31 * 60000,
                       remind_after_minutes=30)
        self.assertEqual(len(alerts), 1)
        self.assertIn("Still waiting", alerts[0])
        alerts = check(snap([s]), st, now_ms=t0 + 62 * 60000,
                       remind_after_minutes=30)
        self.assertEqual(len(alerts), 1)
        self.assertIn("Still waiting", alerts[0])
        self.assertEqual(check(snap([s]), st, now_ms=t0 + 63 * 60000,
                               remind_after_minutes=30), [])

    def test_disabled_suppresses_then_recap_on_resume(self):
        st = self.fresh_state()
        st["seeded"] = True
        s = sess("ses_o", form=[{"id": "f8", "title": "Q", "fields": [
            {"key": "k", "type": "string", "title": "Pick", "options": [
                {"value": "a", "label": "A"}]}]}])
        self.assertEqual(check(snap([s]), st, now_ms=NOW, enabled=False), [])
        self.assertIn("form:f8", st["seen"])
        self.assertEqual(check(snap([s]), st, now_ms=NOW + 1000, enabled=False), [])
        alerts = check(snap([s]), st, now_ms=NOW + 2000, enabled=True)
        self.assertEqual(len(alerts), 1)
        self.assertIn("resumed", alerts[0])
        self.assertIn("ses_o", alerts[0])

    def test_deferred_idle_sets_meta(self):
        st = self.fresh_state()
        st["seeded"] = True
        s = sess("ses_dm", last={"type": "idle", "ts": NOW - 10_000,
                                 "outcome": "succeeded"})
        meta = {}
        alerts = check(snap([s]), st, now_ms=NOW, meta=meta)
        self.assertEqual(alerts, [])
        self.assertTrue(meta.get("deferred_idle"))


if __name__ == "__main__":
    unittest.main()
