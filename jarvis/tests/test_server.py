"""Tests mit einem Platzhalter statt echtem `claude`. Start: python3 -m unittest discover tests"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp())
CALLS = TMP / "calls.jsonl"

FAKE_CLAUDE = TMP / "claude"
FAKE_CLAUDE.write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
with open({str(CALLS)!r}, "a") as f:
    f.write(json.dumps({{"args": args, "prompt": prompt}}) + "\\n")
if "Mail" in prompt:
    result = "[BESTAETIGEN] Soll ich die Mail an Max jetzt senden?"
elif "bestätigt" in prompt:
    result = "Die Mail ist raus."
else:
    result = "## Wetter\\n- **Sonnig** bei 20 %\\n- mehr unter https://example.com 😀"
print(json.dumps({{"result": result, "session_id": "s1", "is_error": False}}))
""")
FAKE_CLAUDE.chmod(0o755)

os.environ.update(JARVIS_CLAUDE_BIN=str(FAKE_CLAUDE), JARVIS_WORKDIR=str(TMP),
                  JARVIS_CONFIG_DIR=str(TMP / "cfg"), JARVIS_TOKEN="geheim")
sys.path.insert(0, str(HERE.parent))
import server  # noqa: E402


def calls():
    return [json.loads(line) for line in CALLS.read_text().splitlines()] if CALLS.exists() else []


class SpeechTest(unittest.TestCase):
    def test_strips_markdown_and_symbols(self):
        out = server.to_speech("## Wetter\n- **Sonnig** bei 20 %\n1. `Code` [Seite](http://x) https://a.b 😀")
        self.assertEqual(out, "Wetter. Sonnig bei 20 Prozent. Code Seite ein Link.")

    def test_limits_length_at_sentence(self):
        out = server.to_speech("Ein Satz. " * 200)
        self.assertLessEqual(len(out), server.MAX_SPOKEN_CHARS)
        self.assertTrue(out.endswith("."))

    def test_yes_detection(self):
        self.assertTrue(server.is_yes("ja bitte senden"))
        self.assertTrue(server.is_yes("mach das"))
        self.assertFalse(server.is_yes("ja nicht senden"))
        self.assertFalse(server.is_yes("was ist mit morgen"))


class ServerTest(unittest.TestCase):
    def setUp(self):
        if CALLS.exists():
            CALLS.unlink()
        self.httpd = HTTPServer(("127.0.0.1", 0), server.make_handler(server.Jarvis(), "geheim"))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/ask"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def ask(self, text, token="geheim"):
        req = urllib.request.Request(self.url, data=json.dumps({"text": text}).encode(),
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode()

    def test_rejects_wrong_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.ask("Hallo", token="falsch")
        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(calls(), [])

    def test_question_is_spoken_and_read_only(self):
        self.assertEqual(self.ask("Wie wird das Wetter?"),
                         "Wetter. Sonnig bei 20 Prozent. mehr unter ein Link.")
        args = calls()[0]["args"]
        self.assertIn("Bash", args[args.index("--disallowedTools") + 1])
        self.assertNotIn("Bash", args[args.index("--allowedTools") + 1])

    def test_confirmation_flow(self):
        self.assertEqual(self.ask("Schick die Mail an Max"),
                         "Soll ich die Mail an Max jetzt senden?")
        self.assertEqual(self.ask("Ja, bitte."), "Die Mail ist raus.")
        execute = calls()[1]["args"]
        self.assertNotIn("--disallowedTools", execute)
        self.assertIn("Bash", execute[execute.index("--allowedTools") + 1])
        self.assertEqual(execute[execute.index("--resume") + 1], "s1")

    def test_confirmation_declined(self):
        self.ask("Schick die Mail an Max")
        self.assertEqual(self.ask("Nein"), "Okay, ich mache nichts.")
        self.assertEqual(len(calls()), 1)

    def test_yes_without_pending_does_not_execute(self):
        self.ask("Ja")
        self.assertIn("--disallowedTools", calls()[0]["args"])

    def test_goodbye(self):
        self.assertEqual(self.ask("Tschüss!"), server.GOODBYE)
        self.assertEqual(calls(), [])


if __name__ == "__main__":
    unittest.main()
