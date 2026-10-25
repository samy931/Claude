#!/usr/bin/env python3
"""Jarvis Sprachschleife, Variante A.

Kleiner HTTP-Server fuer den Mac. Der iPhone-Kurzbefehl schickt diktierten
Text an POST /ask, der Server fragt Claude Code (`claude -p`) und gibt eine
kurze, vorlesbare Antwort als reinen Text zurueck. Das iPhone liest sie mit
"Text sprechen" vor.

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
import time
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


def make_handler(jarvis: Jarvis, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Jarvis/1"

        def _reply(self, status: int, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
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
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._reply(400, "Ich habe nichts gehört.")
                return
            raw = self.rfile.read(length).decode("utf-8", "replace")
            try:
                text = str(json.loads(raw).get("text", ""))
            except (ValueError, AttributeError):
                text = raw  # Kurzbefehl kann auch reinen Text senden
            log.info("Frage: %s", text)
            try:
                answer = jarvis.handle(text)
            except subprocess.TimeoutExpired:
                answer = "Das dauert gerade zu lange. Frag mich bitte gleich noch einmal."
            except Exception:
                log.exception("Fehler bei der Verarbeitung")
                answer = "Entschuldige, da ist etwas schiefgegangen."
            log.info("Antwort: %s", answer)
            self._reply(200, answer)

        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s - %s", self.client_address[0], fmt % args)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
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
