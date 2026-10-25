"""Tests mit einem Platzhalter statt echtem `claude`. Start: python3 -m unittest discover tests"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
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

ELEVEN_REQUESTS = []
ELEVEN_FAIL = threading.Event()


class FakeEleven(BaseHTTPRequestHandler):
    """Ahmt die beiden ElevenLabs-Endpunkte nach."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        ELEVEN_REQUESTS.append((self.path, dict(self.headers), body))
        if ELEVEN_FAIL.is_set() or self.headers.get("xi-api-key") != "xi-test":
            self.send_response(401)
            self.end_headers()
            return
        if self.path.startswith("/v1/text-to-speech/"):
            out, ctype = b"MP3:" + json.loads(body)["text"].encode(), "audio/mpeg"
        else:
            out, ctype = json.dumps({"text": "Wie wird das Wetter?"}).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


ELEVEN = HTTPServer(("127.0.0.1", 0), FakeEleven)
threading.Thread(target=ELEVEN.serve_forever, daemon=True).start()

os.environ.update(JARVIS_CLAUDE_BIN=str(FAKE_CLAUDE), JARVIS_WORKDIR=str(TMP),
                  JARVIS_CONFIG_DIR=str(TMP / "cfg"), JARVIS_TOKEN="geheim",
                  ELEVENLABS_API_KEY="xi-test",
                  JARVIS_ELEVEN_API=f"http://127.0.0.1:{ELEVEN.server_port}")
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
        ELEVEN_REQUESTS.clear()
        ELEVEN_FAIL.clear()
        self.httpd = HTTPServer(("127.0.0.1", 0), server.make_handler(server.Jarvis(), "geheim"))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.url = f"{self.base}/ask"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def ask(self, text, token="geheim"):
        req = urllib.request.Request(self.url, data=json.dumps({"text": text}).encode(),
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode()

    def audio(self, token="geheim"):
        req = urllib.request.Request(f"{self.base}/audio",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as resp:
            return resp.headers["Content-Type"], resp.read()

    def test_answer_audio_from_elevenlabs(self):
        answer = self.ask("Wie wird das Wetter?")
        self.assertEqual(self.audio(), ("audio/mpeg", b"MP3:" + answer.encode()))
        path, headers, _ = ELEVEN_REQUESTS[0]
        self.assertTrue(path.startswith(f"/v1/text-to-speech/{server.ELEVEN_VOICE}"))

    def test_audio_requires_token(self):
        self.ask("Wie wird das Wetter?")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.audio(token="falsch")
        self.assertEqual(ctx.exception.code, 401)

    def test_falls_back_to_mac_voice(self):
        ELEVEN_FAIL.set()
        original = server.mac_say
        server.mac_say = lambda text: b"M4A:" + text.encode()
        try:
            self.ask("Tschüss")
            self.assertEqual(self.audio(), ("audio/mp4", b"M4A:Bis sp\xc3\xa4ter."))
        finally:
            server.mac_say = original

    def test_recorded_audio_is_transcribed(self):
        req = urllib.request.Request(self.url, data=b"FAKE-M4A-BYTES", headers={
            "Authorization": "Bearer geheim", "Content-Type": "audio/x-m4a"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.read().decode(),
                             "Wetter. Sonnig bei 20 Prozent. mehr unter ein Link.")
        path, headers, body = ELEVEN_REQUESTS[0]
        self.assertEqual(path, "/v1/speech-to-text")
        self.assertIn(b"FAKE-M4A-BYTES", body)
        self.assertIn(b'name="model_id"', body)
        self.assertEqual(calls()[0]["prompt"], "Wie wird das Wetter?")

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
