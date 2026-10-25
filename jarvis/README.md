# Jarvis: Sprachschleife iPhone ↔ Mac (Variante A)

„Hey Siri, Jarvis“ → du sprichst → dein Mac fragt Claude Code → das iPhone liest die Antwort vor → du sprichst weiter, bis du „Tschüss“ sagst.

```
iPhone-Kurzbefehl ──(Tailscale, Token)──▶ server.py auf dem Mac ──▶ claude -p
   Diktat + „Text sprechen“  ◀── kurze Antwort als reiner Text ──┘
```

Sicherheit: Der Server lauscht nur auf der Tailscale-Adresse deines Macs, also nicht im Heimnetz und nicht im Internet. Jede Anfrage braucht außerdem ein geheimes Token.
Aktionen mit Folgen: Bei einer normalen Frage hat Jarvis nur Lesewerkzeuge. Werkzeuge, die etwas verändern (Shell, Dateien schreiben), bekommt er erst, nachdem du „Ja“ gesagt hast.

## 1. Tailscale (einmalig, ca. 5 Minuten)

1. Auf dem Mac: [Tailscale für macOS](https://tailscale.com/download/mac) installieren und anmelden.
2. Auf dem iPhone: die App „Tailscale“ aus dem App Store installieren und mit **demselben Konto** anmelden. VPN erlauben.
3. In der Tailscale-App auf dem iPhone sollte jetzt dein Mac auftauchen.

## 2. Jarvis auf dem Mac starten

```bash
git clone <dieses Repo> && cd Claude/jarvis
./start.sh
```

`start.sh` prüft Tailscale und `claude`, erzeugt beim ersten Start ein Token (gespeichert in `~/.jarvis/token`) und zeigt dir die **URL** und das **Token**. Beides brauchst du gleich im Kurzbefehl.
Solange Jarvis läuft, bleibt der Mac wach. Stoppen kannst du ihn mit `Ctrl+C`.

Kurzer Test im Terminal eines anderen Geräts im Tailnet (oder auf dem Mac selbst):

```bash
curl -H "Authorization: Bearer DEIN_TOKEN" -d '{"text":"Wie spät ist es?"}' http://100.x.y.z:8765/ask
```

## 3. Kurzbefehl auf dem iPhone bauen

Öffne die App **Kurzbefehle**, tippe auf **+** und nenne den Kurzbefehl **Jarvis**. Füge dann diese Aktionen in dieser Reihenfolge hinzu:

1. **Wiederholen**, Anzahl: `30`
2. Innerhalb der Wiederholung:
   1. **Text diktieren**: Sprache *Deutsch*, „Zuhören beenden“ auf *Nach Pause*
   2. **Inhalt von URL abrufen**
      - URL: die URL aus `start.sh`, z. B. `http://100.x.y.z:8765/ask`
      - Auf den Pfeil tippen und **Methode** auf *POST* stellen
      - **Header** hinzufügen: Schlüssel `Authorization`, Wert `Bearer DEIN_TOKEN`
      - **Anforderungstext**: *JSON*, Feld hinzufügen: Typ *Text*, Schlüssel `text`, Wert die Variable **Diktierter Text**
   3. **Text sprechen**: Eingabe **Inhalt der URL**. Über „Mehr“ die Sprache *Deutsch*, eine Stimme (siehe Tipp) und **Warten bis fertig** einschalten.
   4. **Wenn** **Inhalt der URL** *ist* `Bis später.`
      - darin: **Kurzbefehl stoppen**
      - **Ende Wenn**
3. Fertig. Teste den Kurzbefehl mit „Hey Siri, Jarvis“.

Tipp für die Stimme: Unter *Einstellungen → Bedienungshilfen → Gesprochene Inhalte → Stimmen → Deutsch* kannst du eine Premium-Stimme laden, zum Beispiel „Anna (Premium)“. Sie klingt deutlich natürlicher. Wähle sie danach in „Text sprechen“ aus.

## So sprichst du mit Jarvis

| Du sagst | Jarvis macht |
|---|---|
| eine Frage oder einen Auftrag | antwortet kurz und hört wieder zu |
| einen Auftrag mit Folgen, z. B. „Lösche die Datei …“ | fragt nach: „Soll ich … wirklich …?“ |
| „Ja“, „Ja bitte“, „Mach das“ | führt genau diese Aktion aus |
| „Nein“, „Abbrechen“ | macht nichts |
| „Neues Gespräch“ | vergisst den bisherigen Gesprächsverlauf |
| „Tschüss“, „Ende“, „Danke, das war's“ | „Bis später.“, der Kurzbefehl endet |

Wenn du einfach nichts sagst, endet das Diktat und damit auch der Kurzbefehl.
Nach 10 Minuten Pause beginnt automatisch ein neues Gespräch. Eine offene Rückfrage verfällt nach 3 Minuten.

## Einstellungen (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `JARVIS_WORKDIR` | Home-Ordner | Ordner, in dem Claude Code arbeitet |
| `JARVIS_MODEL` | Standard von Claude Code | z. B. `haiku` für schnellere Antworten |
| `JARVIS_TIMEOUT` | `55` | Sekunden pro Antwort (Kurzbefehle brechen nach etwa 60 Sekunden ab) |
| `JARVIS_READ_TOOLS` | `Read,Grep,Glob,WebSearch,WebFetch` | Werkzeuge ohne Bestätigung |
| `JARVIS_ACTION_TOOLS` | `Bash,Edit,Write,NotebookEdit` | Werkzeuge erst nach „Ja“ |
| `JARVIS_PORT` | `8765` | Port |

Beispiel: `JARVIS_MODEL=haiku ./start.sh`

Hinweis: Wenn du in deinen Claude-Code-Einstellungen bestimmte Werkzeuge dauerhaft erlaubt hast, zum Beispiel MCP-Tools für E-Mail, gelten diese Regeln auch hier. Werkzeuge mit Folgen sollten dort nicht pauschal erlaubt sein, sonst bleibt die Bestätigung die einzige Sicherung über den Systemprompt.

## Tests

```bash
python3 -m unittest discover tests
```
