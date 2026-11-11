"""The page must not ask the server anything while nothing is happening: on the hosted site every request kept a paid
GPU switched on (Nov 2026: an open tab asking every 20 seconds cost about £23). Runs web/static/app.js's polling
functions in Node with a fake page and counts what they would schedule."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "web" / "static" / "app.js"

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function grab(name) {
  const at = src.search(new RegExp(`(async )?function ${name}\\(`));
  if (at < 0) throw new Error(`no function ${name}`);
  let depth = 0;
  for (let k = src.indexOf('{', at); k < src.length; k++) {
    if (src[k] === '{') depth++;
    if (src[k] === '}' && --depth === 0) return src.slice(at, k + 1);
  }
}
const line = re => src.split('\n').filter(l => re.test(l)).join('\n');
const consts = line(/^const (ACTIVE|STAGE_KEYS|running|busyRun|IDLE_MS)\b/);
async function scenario(s) {
  const timers = [];
  let fetches = 0;
  const document = { hidden: s.hidden };
  const notices = {};
  const $ = id => (notices[id] = notices[id] || { textContent: '' });
  const setTimeout = (fn, ms) => { timers.push(ms); return 1; };
  const clearTimeout = () => {};
  const fetch = async () => { fetches++; return { ok: true, status: 200, json: async () => s.runs }; };
  const signedOut = () => false, renderRuns = () => {}, loadUsage = () => {};
  let runs = [], upload = s.upload || null, runsTimer = null, wasBusy = false, napping = false;
  let lastActivity = Date.now() - (s.idleMinutes || 0) * 60000;
  eval(consts + '\n' + grab('awake') + '\n' + grab('later') + '\n' + grab('loadRuns'));
  await loadRuns();
  return { fetches, scheduled: timers };
}
(async () => {
  const cases = JSON.parse(process.argv[2]);
  const out = {};
  for (const [name, s] of Object.entries(cases)) out[name] = await scenario(s);
  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exit(1); });
"""

CASES = {
    "idle": {"runs": [{"status": "complete"}]},
    "idle_empty": {"runs": []},
    "transcribing": {"runs": [{"status": "transcribing"}]},
    "translating": {"runs": [{"status": "complete", "translation": {"status": "running"}}]},
    "uploading": {"runs": [], "upload": {"name": "talk.mp3"}},
    "busy_but_hidden": {"runs": [{"status": "transcribing"}], "hidden": True},
    "busy_but_away": {"runs": [{"status": "transcribing"}], "idleMinutes": 31},
}


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class PagePollingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["node", "-e", HARNESS, str(APP_JS), json.dumps(CASES)], capture_output=True, text=True, timeout=30
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.results = json.loads(result.stdout)

    def test_nothing_is_scheduled_while_idle(self):
        for name in ("idle", "idle_empty"):
            with self.subTest(name):
                self.assertEqual(self.results[name]["scheduled"], [])

    def test_live_updates_while_something_runs(self):
        for name in ("transcribing", "translating", "uploading"):
            with self.subTest(name):
                self.assertEqual(len(self.results[name]["scheduled"]), 1)
                self.assertGreaterEqual(self.results[name]["scheduled"][0], 1000)

    def test_no_updates_when_hidden_or_away(self):
        for name in ("busy_but_hidden", "busy_but_away"):
            with self.subTest(name):
                self.assertEqual(self.results[name]["scheduled"], [])

    def test_no_repeating_timers_anywhere(self):
        """setInterval would poll forever whatever the page is doing."""
        self.assertNotIn("setInterval(", APP_JS.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
