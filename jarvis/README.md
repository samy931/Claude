# Jarvis: Sprachschleife iPhone ↔ Mac (Variante B, ElevenLabs)

„Hey Siri, Jarvis“ → du sprichst → dein Mac fragt Claude Code → du hörst die Antwort in einer natürlichen ElevenLabs-Stimme → du sprichst weiter, bis du „Tschüss“ sagst.

```
iPhone-Kurzbefehl ──(Tailscale + Token)──▶ server.py auf dem Mac ──▶ claude -p
  1. POST /ask  (diktierter Text)   ◀── kurze Antwort als Text
  2. GET /audio                     ◀── dieselbe Antwort als MP3 (ElevenLabs)
```

- **Sicherheit:** Der Server lauscht nur auf der Tailscale-Adresse deines Macs, also nicht im Heimnetz und nicht im Internet. Jede Anfrage braucht außerdem ein geheimes Token.
- **Aktionen mit Folgen:** Bei einer normalen Frage hat Jarvis nur Lesewerkzeuge. Werkzeuge, die etwas verändern (Shell, Dateien schreiben), bekommt er erst, nachdem du „Ja“ gesagt hast.
- **Ausfallsicherung:** Wenn ElevenLabs nicht antwortet (Kontingent leer, Internet weg), spricht automatisch die macOS-Stimme „Anna“.

## 1. Tailscale (einmalig, ca. 5 Minuten)

1. Auf dem Mac: [Tailscale für macOS](https://tailscale.com/download/mac) installieren und anmelden.
2. Auf dem iPhone: die App „Tailscale“ aus dem App Store installieren und mit **demselben Konto** anmelden. VPN erlauben.
3. In der Tailscale-App auf dem iPhone sollte jetzt dein Mac auftauchen.

## 2. ElevenLabs-Schlüssel und Stimme

1. Auf [elevenlabs.io](https://elevenlabs.io) unter *Profil → API Keys* einen Schlüssel anlegen. Er braucht die Rechte „Text to Speech“ und „Speech to Text“, für die Stimmenliste zusätzlich „Voices: Read“.
2. `start.sh` fragt beim ersten Start danach und speichert ihn in `~/.jarvis/elevenlabs_key`. Die Datei kann nur dein Benutzer lesen.
3. Stimme wählen (optional). Standard ist „George“, eine ruhige Butler-Stimme, die auch Deutsch spricht. Für eine deutsche Muttersprachler-Stimme fügst du in der ElevenLabs Voice Library eine Stimme zu „My Voices“ hinzu und lässt dir dann alle IDs anzeigen:

   ```bash
   python3 server.py --voices
   ```

   Danach mit der gewünschten ID starten: `JARVIS_VOICE_ID=<ID> ./start.sh`

## 3. Jarvis auf dem Mac starten

```bash
git clone <dieses Repo> && cd Claude/jarvis
./start.sh
```

`start.sh` prüft Tailscale und `claude`, erzeugt beim ersten Start ein Token (`~/.jarvis/token`) und zeigt dir **URL** und **Token**. Beides brauchst du gleich im Kurzbefehl.
Solange Jarvis läuft, bleibt der Mac wach. Stoppen kannst du ihn mit `Ctrl+C`.

## 4. Kurzbefehl auf dem iPhone bauen

Öffne die App **Kurzbefehle**, tippe auf **+** und nenne den Kurzbefehl **Jarvis**. Füge diese Aktionen in dieser Reihenfolge hinzu. `100.x.y.z` und `DEIN_TOKEN` stehen in der Ausgabe von `start.sh`.

1. **Wiederholen**, Anzahl: `30`
2. Innerhalb der Wiederholung:
   1. **Text diktieren**: Sprache *Deutsch*, „Zuhören beenden“ auf *Nach Pause*
   2. **Inhalt von URL abrufen**
      - URL: `http://100.x.y.z:8765/ask`
      - Auf den Pfeil tippen und **Methode** auf *POST* stellen
      - **Header** hinzufügen: Schlüssel `Authorization`, Wert `Bearer DEIN_TOKEN`
      - **Anforderungstext**: *JSON*, Feld hinzufügen: Typ *Text*, Schlüssel `text`, Wert die Variable **Diktierter Text**
   3. **Wenn** **Inhalt der URL** *ist* `Bis später.`
      - darin: **Text sprechen** „Bis später“, danach **Kurzbefehl stoppen**
      - **Ende Wenn**
   4. **Inhalt von URL abrufen** (eine zweite Aktion)
      - URL: `http://100.x.y.z:8765/audio`
      - Methode bleibt *GET*
      - **Header**: `Authorization` = `Bearer DEIN_TOKEN`
   5. **Ton wiedergeben**: Eingabe ist das Ergebnis der zweiten Aktion „Inhalt von URL abrufen“. Falls dein iOS diese Aktion nicht hat, nimm **Medien wiedergeben**.
3. Fertig. Teste den Kurzbefehl mit „Hey Siri, Jarvis“.

Warum zwei Abrufe? Über den ersten erkennt der Kurzbefehl das Gesprächsende. Den Ton erzeugt der Mac schon, während der Text zurückkommt, deshalb kostet der zweite Abruf kaum Zeit.

### Optional: Spracherkennung durch ElevenLabs statt Apple

Wenn das Apple-Diktat Namen oder Fachbegriffe schlecht versteht, kannst du auch Audio schicken. ElevenLabs Scribe erkennt Deutsch meist besser.
Ersetze dafür *Text diktieren* durch **Audio aufnehmen** (Start *Sofort*, Ende *Bei Tippen*). Stell im ersten „Inhalt von URL abrufen“ den **Anforderungstext** auf *Datei* und wähle die **Aufgenommene Audiodatei**.
Der Nachteil: Du musst zum Beenden der Aufnahme tippen, freihändig geht es nur mit dem Diktat.

## So sprichst du mit Jarvis

| Du sagst | Jarvis macht |
|---|---|
| eine Frage oder einen Auftrag | antwortet kurz und hört wieder zu |
| einen Auftrag mit Folgen, z. B. „Lösche die Datei …“ | fragt nach: „Soll ich … wirklich …?“ |
| „Ja“, „Ja bitte“, „Mach das“ | führt genau diese Aktion aus |
| „Nein“, „Abbrechen“ | macht nichts |
| „Neues Gespräch“ | vergisst den bisherigen Gesprächsverlauf |
| „Tschüss“, „Ende“, „Danke, das war's“ | „Bis später“, der Kurzbefehl endet |

Wenn du einfach nichts sagst, endet das Diktat und damit auch der Kurzbefehl.
Nach 10 Minuten Pause beginnt automatisch ein neues Gespräch. Eine offene Rückfrage verfällt nach 3 Minuten.

## Einstellungen (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `JARVIS_VOICE_ID` | `JBFqnCBsd6RMkjVDRZzb` (George) | ElevenLabs-Stimme |
| `JARVIS_TTS_MODEL` | `eleven_multilingual_v2` | Beste Qualität. `eleven_flash_v2_5` ist schneller und günstiger. |
| `JARVIS_STT_MODEL` | `scribe_v1` | Modell für die optionale Audio-Erkennung |
| `JARVIS_MAC_VOICE` | `Anna` | Ersatzstimme ohne ElevenLabs |
| `JARVIS_WORKDIR` | Home-Ordner | Ordner, in dem Claude Code arbeitet |
| `JARVIS_MODEL` | Standard von Claude Code | z. B. `haiku` für schnellere Antworten |
| `JARVIS_TIMEOUT` | `55` | Sekunden pro Antwort (Kurzbefehle brechen nach etwa 60 Sekunden ab) |
| `JARVIS_READ_TOOLS` | `Read,Grep,Glob,WebSearch,WebFetch` | Werkzeuge ohne Bestätigung |
| `JARVIS_ACTION_TOOLS` | `Bash,Edit,Write,NotebookEdit` | Werkzeuge erst nach „Ja“ |
| `JARVIS_PORT` | `8765` | Port |

Beispiel: `JARVIS_TTS_MODEL=eleven_flash_v2_5 JARVIS_MODEL=haiku ./start.sh`

**Kosten:** ElevenLabs rechnet nach Zeichen ab. Eine typische Jarvis-Antwort hat 100 bis 300 Zeichen. Mit `eleven_flash_v2_5` kostet ein Zeichen etwa die Hälfte.

**Hinweis:** Wenn du in deinen Claude-Code-Einstellungen bestimmte Werkzeuge dauerhaft erlaubt hast, zum Beispiel MCP-Tools für E-Mail, gelten diese Regeln auch hier. Werkzeuge mit Folgen sollten dort nicht pauschal erlaubt sein, sonst bleibt die Bestätigung über den Systemprompt die einzige Sicherung.

## Tests

```bash
python3 -m unittest discover tests
```
