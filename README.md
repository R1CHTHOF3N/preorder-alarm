# Preorder-Alarm

Prüft alle 10 Minuten deutsche Shops auf **englische Displays** von Dragon Ball Fusion World und
One Piece und schickt eine Discord-Nachricht, sobald etwas neu gelistet oder bestellbar wird.
Läuft kostenlos auf GitHub, auch wenn dein PC aus ist.

**Shops:** Comic Planet, Card Collector, FantasiaCards, GoodGameGuys

**Serien:**
- Dragon Ball Fusion World: FB (Booster), ST (Story Booster), SB (Manga Booster)
- One Piece: OP (Booster), EB (Extra Booster), PRB (Premium Booster), PEB (Premium Extra Booster)

Einzelne Booster, Cases, Starter Decks, Event-Tickets, Zubehör und japanische Ware werden ignoriert.

**Neue Sets kommen automatisch dazu:** Das Skript merkt sich je Serie die höchste Nummer, die
irgendein Shop listet (auch japanisch, weil die früher erscheinen), und sucht immer 2 Nummern weiter.
Taucht z. B. FB14 auf, wird ab dann auch nach FB15 und FB16 gesucht. Eine ganz neue Serie
(neues Kürzel) müsste man in `check.py` unter `SERIES` eintragen.

Jede Meldung pingt mit `@everyone` (auf deinem eigenen Server also dich).

| Nachricht | Bedeutung |
|---|---|
| 🟢 JETZT BESTELLBAR / WIEDER BESTELLBAR | Kaufen-Button ist da → sofort zuschlagen |
| 🆕 Neu gelistet, noch nicht bestellbar | Seite existiert, aber nur „Benachrichtigen“ |
| ⚠️ Shop lässt sich nicht prüfen | 3× hintereinander (~30 Min.) Fehler oder Shop-Seite umgebaut; Erinnerung alle ~12 Std. |
| ✅ Shop wird wieder geprüft | Entwarnung |
| ❌ Preorder-Alarm ist abgestürzt | Der ganze Lauf ist fehlgeschlagen (nur beim ersten Mal gemeldet) |
| 💓 Wochen-Check | Lebenszeichen einmal pro Woche. **Bleibt es aus, läuft der Alarm nicht mehr** → unter Actions nachsehen |

**Absicherungen:** Bei Aussetzern wird automatisch neu versucht. Kann das Skript auf einer Shopseite
den Kaufen-/Benachrichtigen-Bereich nicht eindeutig erkennen (Shop umgebaut), meldet es ⚠️ statt
falsch „bestellbar“ oder „nicht bestellbar“ zu melden. Fehlt das Webhook-Secret, schlägt der Lauf
fehl (GitHub schickt dir dann eine E-Mail), statt stumm zu laufen.

## Einrichtung (einmalig, ca. 10 Minuten)

### 1. Discord-Webhook anlegen
1. In Discord einen eigenen Server erstellen (z. B. „Preorder Alarm“).
2. Kanal-Einstellungen (Zahnrad) → **Integrationen** → **Webhooks** → **Neuer Webhook** → **Webhook-URL kopieren**.
3. Die URL **nirgends posten** – nur in Schritt 3 bei GitHub einfügen.

### 2. GitHub-Repo anlegen und Code hochladen
1. Auf <https://github.com/new> ein Repo anlegen, z. B. `preorder-alarm`.
   - **Public** empfohlen: dann sind die Actions-Minuten unbegrenzt gratis und der 10-Minuten-Takt ist möglich.
     Im Code stehen keine persönlichen Daten.
   - **Private** geht auch, dann in `.github/workflows/check.yml` den Takt auf `*/30 * * * *` stellen
     (sonst reichen die 2000 Gratis-Minuten/Monat nicht).
   - **Keine** README/.gitignore anhaken (Repo leer lassen).
2. In diesem Ordner im Terminal (die Repo-Adresse zeigt GitHub nach dem Anlegen an):
   ```bash
   git remote add origin https://github.com/DEIN-NAME/preorder-alarm.git
   git push -u origin main
   ```
   Beim ersten Push öffnet sich ein Browserfenster zum GitHub-Login.

### 3. Webhook als Secret speichern
Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
- Name: `DISCORD_WEBHOOK_URL`
- Secret: die kopierte Webhook-URL

### 4. Starten und testen
Repo → **Actions** → (falls gefragt: Workflows aktivieren) → **Preorder-Check** → **Run workflow**.
Nach ~1 Minute kommt in Discord die Nachricht „🤖 Preorder-Alarm ist aktiv“ mit dem aktuellen Stand.
Danach läuft es automatisch.

Handy-Benachrichtigungen: In der Discord-App den Server auf **Alle Nachrichten** stellen.

## Sicherheit

- Die Webhook-URL liegt verschlüsselt in GitHub Secrets, steht nicht im Code und wird in Logs als `***` ausgeblendet.
  Kopien des Repos (Forks) bekommen die Secrets nicht.
- Das Skript liest nur öffentliche Shopseiten. Es loggt sich nirgends ein, kauft nichts und kennt keine Zahlungsdaten.
- Die Webhook-URL kann nur Nachrichten in **diesen einen Kanal** posten. Falls sie doch mal öffentlich wird:
  Webhook in Discord löschen, neuen anlegen, Secret in GitHub ersetzen.
- Pro Lauf gehen nur wenige Anfragen an jeden Shop, mit Pause dazwischen.

## Gut zu wissen

- GitHub startet geplante Läufe manchmal ein paar Minuten zu spät, zu Stoßzeiten auch mal 15–30 Minuten.
- Bei Public-Repos pausiert GitHub geplante Läufe nach 60 Tagen ohne Aktivität. Das Skript speichert deshalb
  einmal am Tag den Stand (`state.json`), damit das nicht passiert.
- Cardmarket ist nicht dabei, weil die Seite automatische Abfragen per Cloudflare blockiert.
- Ändert ein Shop sein Layout, kann die Erkennung ausfallen. Dann kommt nach ~1 Stunde die ⚠️-Warnung.

## Lokal testen

```bash
python check.py
```
Ohne `DISCORD_WEBHOOK_URL` wird nur in der Konsole ausgegeben. `state.json` danach löschen,
damit der erste Lauf auf GitHub die Übersicht nach Discord schickt.
