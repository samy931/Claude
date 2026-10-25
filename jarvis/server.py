#!/usr/bin/env python3
"""Jarvis Sprachschleife, Variante B (ElevenLabs).

Kleiner HTTP-Server fuer den Mac. Der iPhone-Kurzbefehl schickt diktierten
Text (oder eine Audioaufnahme) an POST /ask, der Server fragt Claude Code
(`claude -p`) und gibt eine kurze, vorlesbare Antwort als reinen Text zurueck.
Parallel erzeugt ElevenLabs daraus Sprache, die der Kurzbefehl mit GET /audio
abholt und abspielt.

Nur Python-Standardbibliothek, laeuft mit dem vorinstallierten python3 auf macOS.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("JARVIS_CONFIG_DIR", Path.home() / ".jarvis"))
HOST = os.environ.get("JARVIS_HOST", "")
PORT = int(os.environ.get("JARVIS_PORT", "8765"))
CLAUDE_BIN = os.environ.get("JARVIS_CLAUDE_BIN", "claude")
WORKDIR = os.environ.get("JARVIS_WORKDIR", str(Path.home()))
MODEL = os.environ.get("JARVIS_MODEL", "")
CLAUDE_TIMEOUT = int(os.environ.get("JARVIS_TIMEOUT", "55"))
SESSION_IDLE = int(os.environ.get("JARVIS_SESSION_IDLE", "600"))
CONFIRM_TTL = int(os.environ.get("JARVIS_CONFIRM_TTL", "180"))
MAX_SPOKEN_CHARS = 700
MAX_BODY = 16 * 1024
MAX_AUDIO_BODY = 15 * 1024 * 1024

ELEVEN_API = os.environ.get("JARVIS_ELEVEN_API", "https://api.elevenlabs.io")
# Standardstimme "George" (ruhig, britisch, spricht mit multilingual_v2 Deutsch).
ELEVEN_VOICE = os.environ.get("JARVIS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
ELEVEN_TTS_MODEL = os.environ.get("JARVIS_TTS_MODEL", "eleven_multilingual_v2")
ELEVEN_STT_MODEL = os.environ.get("JARVIS_STT_MODEL", "scribe_v1")
# Ersatzstimme von macOS, falls ElevenLabs nicht antwortet.
MAC_VOICE = os.environ.get("JARVIS_MAC_VOICE", "Anna")

# Werkzeuge ohne Folgen: duerfen immer laufen.
READ_TOOLS = os.environ.get("JARVIS_READ_TOOLS", "Read,Grep,Glob,WebSearch,WebFetch")
# Werkzeuge mit Folgen: nur nach muendlicher Bestaetigung.
ACTION_TOOLS = os.environ.get("JARVIS_ACTION_TOOLS", "Bash,Edit,Write,NotebookEdit")

CONFIRM_MARKER = "[BESTAETIGEN]"
GOODBYE = "Bis später."

SYSTEM_PROMPT = f"""Du bist Jarvis, ein persönlicher Sprachassistent. Der Nutzer spricht über sein iPhone mit dir, deine Antwort wird laut vorgelesen.

So antwortest du:
Immer auf Deutsch, kurz, meist ein bis drei Sätze, wie in einem Telefongespräch.
Keine Listen, keine Aufzählungszeichen, kein Markdown, kein Code, keine Links, keine Emojis, keine Sonderzeichen.
Zahlen, Uhrzeiten und Daten so, wie man sie ausspricht.
Wenn etwas unklar ist, stelle eine kurze Rückfrage.

Du darfst jederzeit Informationen lesen und nachschlagen.
Aktionen mit Folgen führst du nie direkt aus. Dazu zählen E-Mails oder Nachrichten senden, Dateien anlegen, ändern oder löschen, Termine anlegen oder ändern, Käufe, Reservierungen und jeder Befehl, der etwas verändert.
Stattdessen beginnst du deine Antwort mit {CONFIRM_MARKER} und fragst in einem Satz konkret, was du tun würdest, zum Beispiel: {CONFIRM_MARKER} Soll ich die Mail an Max mit dem Betreff Angebot jetzt senden?
Erst nach der Bestätigung des Nutzers bekommst du die nötigen Werkzeuge."""

EXECUTE_PROMPT = (
    "Der Nutzer hat mündlich bestätigt ({answer}). Führe jetzt genau die zuletzt "
    "angekündigte Aktion aus, nichts anderes, und melde das Ergebnis in einem kurzen Satz."
)

YES_WORDS = {"ja", "jawohl", "jep", "jo", "okay", "ok", "genau", "mach", "machs",
             "mach das", "mach es", "los", "bestätigt", "bestätige", "ja bitte",
             "ja mach", "ja mach das", "ja genau", "ja gerne", "gerne", "klar",
             "sicher", "richtig", "tu es", "ausführen", "senden", "abschicken"}
NO_WORDS = {"nein", "nö", "ne", "nee", "abbrechen", "abbruch", "stopp", "stop",
            "lieber nicht", "nein danke", "auf keinen fall", "vergiss es", "doch nicht"}
END_WORDS = {"ende", "danke ende", "tschüss", "tschüs", "auf wiedersehen", "bis später",
             "beenden", "gespräch beenden", "das wars", "das war's", "danke das wars",
             "danke das war's", "danke jarvis", "danke tschüss"}
RESET_WORDS = {"neues gespräch", "neues thema", "vergiss das gespräch"}

log = logging.getLogger("jarvis")


# --------------------------------------------------------------------------- #
# Text fuer die Sprachausgabe aufbereiten
# --------------------------------------------------------------------------- #

def normalize(text: str) -> str:
    """Kleinbuchstaben, ohne Satzzeichen, fuer den Vergleich mit Stichwoertern."""
    text = re.sub(r"[^\wäöüß' ]+", " ", text.lower())
    return " ".join(text.split())


def to_speech(text: str) -> str:
    """Entfernt alles, was beim Vorlesen stoert (Markdown, Code, Links, Symbole)."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "ein Link", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", text, flags=re.M)
    text = text.replace("&", " und ").replace("%", " Prozent").replace("€", " Euro")
    text = re.sub(r"[*_~#|<>{}\[\]\\^=]", " ", text)
    # Emojis und sonstige Bildzeichen entfernen.
    text = re.sub("[\U0001F000-\U0001FAFF☀-➿️]", "", text)
    # Zeilenumbrueche zu Satzpausen machen.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    text = " ".join(ln if ln[-1] in ".!?:;," else ln + "." for ln in lines)
    text = " ".join(text.split())
    if len(text) > MAX_SPOKEN_CHARS:
        cut = text[:MAX_SPOKEN_CHARS]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: end + 1] if end > 0 else cut.rsplit(" ", 1)[0] + "."
    return text


def is_yes(words: str) -> bool:
    """Bestaetigung erkennen, auch bei "ja bitte senden" o. ae., aber nie bei Verneinung."""
    tokens = words.split()
    if not tokens or {"nein", "nicht", "nö", "ne", "nee", "abbrechen"} & set(tokens):
        return False
    return words in YES_WORDS or tokens[0] in {"ja", "jawohl", "okay", "ok", "genau", "klar"}


# --------------------------------------------------------------------------- #
# Gespraechszustand und Aufruf von Claude Code
# --------------------------------------------------------------------------- #

class Jarvis:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.last_activity = 0.0
        self.pending_since: float | None = None  # wartet auf Ja/Nein

    def _call_claude(self, prompt: str, tools: str, deny: str) -> dict:
        cmd = [CLAUDE_BIN, "-p", "--output-format", "json",
               "--append-system-prompt", SYSTEM_PROMPT,
               "--allowedTools", tools]
        if deny:
            cmd += ["--disallowedTools", deny]
        if MODEL:
            cmd += ["--model", MODEL]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=CLAUDE_TIMEOUT, cwd=WORKDIR)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise RuntimeError(f"claude exit {proc.returncode}: {proc.stderr.strip()[:500]}")
        data = json.loads(proc.stdout)
        if data.get("is_error"):
            raise RuntimeError(f"claude error: {str(data.get('result'))[:500]}")
        self.session_id = data.get("session_id") or self.session_id
        return data

    def handle(self, text: str) -> str:
        now = time.time()
        if self.session_id and now - self.last_activity > SESSION_IDLE:
            self.session_id = None
            self.pending_since = None
        self.last_activity = now

        words = normalize(text)
        if not words:
            return "Ich habe dich nicht verstanden."
        if words in END_WORDS:
            self.pending_since = None
            return GOODBYE
        if words in RESET_WORDS:
            self.session_id = None
            self.pending_since = None
            return "Alles klar, wir fangen neu an."

        if self.pending_since is not None:
            expired = now - self.pending_since > CONFIRM_TTL
            self.pending_since = None
            if not expired and is_yes(words):
                log.info("Aktion bestaetigt")
                data = self._call_claude(EXECUTE_PROMPT.format(answer=text),
                                         f"{READ_TOOLS},{ACTION_TOOLS}", "")
                return to_speech(str(data.get("result", ""))) or "Erledigt."
            if not expired and words in NO_WORDS:
                return "Okay, ich mache nichts."
            # Etwas anderes gesagt: Aktion verwerfen und normal weiter.

        data = self._call_claude(text, READ_TOOLS, ACTION_TOOLS)
        answer = str(data.get("result", "")).strip()
        if CONFIRM_MARKER in answer:
            self.pending_since = now
            answer = answer.replace(CONFIRM_MARKER, "")
            spoken = to_speech(answer)
            if "?" not in spoken:
                spoken += " Soll ich das machen?"
            return spoken
        return to_speech(answer) or "Dazu habe ich gerade keine Antwort."


# --------------------------------------------------------------------------- #
# ElevenLabs: Sprache zu Text und Text zu Sprache
# --------------------------------------------------------------------------- #

def eleven_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    path = CONFIG_DIR / "elevenlabs_key"
    if not key and path.exists():
        key = path.read_text().strip()
    return key


def _eleven(path: str, body: bytes, content_type: str, accept: str) -> bytes:
    req = urllib.request.Request(f"{ELEVEN_API}{path}", data=body, method="POST", headers={
        "xi-api-key": eleven_key(), "Content-Type": content_type, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"ElevenLabs {err.code}: {detail}") from None


def eleven_tts(text: str) -> bytes:
    body = json.dumps({"text": text, "model_id": ELEVEN_TTS_MODEL}).encode()
    return _eleven(f"/v1/text-to-speech/{ELEVEN_VOICE}?output_format=mp3_44100_128",
                   body, "application/json", "audio/mpeg")


def eleven_stt(audio: bytes, content_type: str) -> str:
    boundary = secrets.token_hex(16)
    parts = []
    for name, value in (("model_id", ELEVEN_STT_MODEL), ("language_code", "de")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                     f'\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="aufnahme.m4a"\r\nContent-Type: {content_type}\r\n\r\n'.encode())
    body = b"".join(parts) + audio + f"\r\n--{boundary}--\r\n".encode()
    raw = _eleven("/v1/speech-to-text", body,
                  f"multipart/form-data; boundary={boundary}", "application/json")
    return str(json.loads(raw).get("text", "")).strip()


def mac_say(text: str) -> bytes:
    """Ersatz ohne Internet: macOS-Stimme als m4a."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "antwort.m4a"
        subprocess.run(["say", "-v", MAC_VOICE, "--file-format=m4af", "--data-format=aac",
                        "-o", str(out), text], check=True, timeout=30)
        return out.read_bytes()


class Speaker:
    """Erzeugt die Sprache zur letzten Antwort im Hintergrund, GET /audio holt sie ab."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._ready.set()
        self._audio: tuple[bytes, str] | None = None

    def prepare(self, text: str) -> None:
        self._ready.clear()
        self._audio = None
        threading.Thread(target=self._render, args=(text,), daemon=True).start()

    def _render(self, text: str) -> None:
        try:
            self._audio = (eleven_tts(text), "audio/mpeg")
        except Exception as err:
            log.warning("ElevenLabs nicht verfuegbar (%s), nehme macOS-Stimme", err)
            try:
                self._audio = (mac_say(text), "audio/mp4")
            except Exception:
                log.exception("Auch die macOS-Stimme ist fehlgeschlagen")
        finally:
            self._ready.set()

    def get(self, timeout: float = 45) -> tuple[bytes, str] | None:
        self._ready.wait(timeout)
        return self._audio


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def load_token() -> str:
    env = os.environ.get("JARVIS_TOKEN")
    if env:
        return env
    path = CONFIG_DIR / "token"
    if path.exists():
        return path.read_text().strip()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n")
    path.chmod(0o600)
    log.info("Neues Token erzeugt: %s", path)
    return token


def make_handler(jarvis: Jarvis, token: str, speaker: Speaker | None = None):
    speaker = speaker or Speaker()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Jarvis/2"

        def _reply(self, status: int, body: str | bytes,
                   content_type: str = "text/plain; charset=utf-8") -> None:
            raw = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self) -> bool:
            given = self.headers.get("Authorization", "")
            if given.lower().startswith("bearer "):
                given = given[7:]
            return hmac.compare_digest(given.strip().encode(), token.encode())

        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, "ok")
            elif self.path.split("?")[0] == "/audio":
                if not self._authorized():
                    self._reply(401, "Nicht berechtigt.")
                    return
                audio = speaker.get()
                if audio is None:
                    self._reply(503, "Keine Sprachausgabe verfügbar.")
                else:
                    self._reply(200, *audio)
            else:
                self._reply(404, "Nicht gefunden.")

        def do_POST(self) -> None:
            if self.path != "/ask":
                self._reply(404, "Nicht gefunden.")
                return
            if not self._authorized():
                log.warning("Abgelehnt: falsches Token von %s", self.client_address[0])
                self._reply(401, "Nicht berechtigt.")
                return
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            is_text = ctype in ("", "application/json", "text/plain",
                                "application/x-www-form-urlencoded")
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > (MAX_BODY if is_text else MAX_AUDIO_BODY):
                self._reply(400, "Ich habe nichts gehört.")
                return
            body = self.rfile.read(length)
            try:
                if is_text:
                    raw = body.decode("utf-8", "replace")
                    try:
                        text = str(json.loads(raw).get("text", ""))
                    except (ValueError, AttributeError):
                        text = raw  # Kurzbefehl kann auch reinen Text senden
                else:
                    text = eleven_stt(body, ctype or "audio/mp4")
                log.info("Frage: %s", text)
                answer = jarvis.handle(text)
            except subprocess.TimeoutExpired:
                answer = "Das dauert gerade zu lange. Frag mich bitte gleich noch einmal."
            except Exception:
                log.exception("Fehler bei der Verarbeitung")
                answer = "Entschuldige, da ist etwas schiefgegangen."
            log.info("Antwort: %s", answer)
            speaker.prepare(answer)
            self._reply(200, answer)

        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s - %s", self.client_address[0], fmt % args)

    return Handler


def list_voices() -> None:
    req = urllib.request.Request(f"{ELEVEN_API}/v1/voices", headers={"xi-api-key": eleven_key()})
    with urllib.request.urlopen(req, timeout=20) as resp:
        voices = json.load(resp).get("voices", [])
    for v in voices:
        labels = ", ".join(str(x) for x in (v.get("labels") or {}).values())
        print(f"{v.get('voice_id')}  {v.get('name')}  ({labels})")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    if "--voices" in sys.argv:
        list_voices()
        return
    if not eleven_key():
        log.warning("Kein ElevenLabs-Schluessel gefunden, es spricht die macOS-Stimme.")
    if not HOST:
        sys.exit("JARVIS_HOST fehlt. Bitte ueber start.sh starten "
                 "(nimmt automatisch die Tailscale-Adresse).")
    token = load_token()
    server = HTTPServer((HOST, PORT), make_handler(Jarvis(), token))
    log.info("Jarvis hoert auf http://%s:%d/ask", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
