# Leonida — Schnellstart (`de`)

> [!NOTE]
> Die englische Dokumentation ([`README.md`](../../../README.md), [`CONTRIBUTING.md`](../../../CONTRIBUTING.md) und
> [`docs/README.md`](../../README.md)) ist **maßgeblich**. Diese Anleitung enthält nur die stabilen Schritte zum
> Einrichten und Bauen (abgeglichen mit Commit `4eb6155d`). Den aktuellen Projektstand findest du in der
> [`README.md`](../../../README.md).

## Was ist Leonida?

Leonida ist ein Static-Recompilation-Projekt, das Grand Theft Auto VI (Xbox Series) nativ unter Windows ausführen
soll. Der Port wird **auf deinem PC aus deiner eigenen Kopie des Spiels** gebaut. Die CPU der Xbox Series ist wie ein
PC x86-64, daher wird der Code des Spiels geladen und direkt ausgeführt, und die Systemsoftware der Konsole wird als
sogenannte Shims nachgebaut. Die vorkompilierten RDNA2-Shader der Konsole werden nach SPIR-V (Vulkan) übersetzt,
Modelle, Texturen und Audio werden direkt aus deinem Paket gelesen.

> [!IMPORTANT]
> Dieses Repository **enthält weder das Spiel noch Schlüssel oder eine ausführbare Datei** und erklärt auch nicht,
> wie man sie bekommt. Nichts hier entschlüsselt Daten oder umgeht einen Schutz. Alles, was der Build erzeugt
> (übersetzte Shader, extrahierte Assets, der Arbeitsordner und deine Spielstände), stammt aus deiner Kopie und
> **muss auf deinem PC bleiben**. Hänge diese Dateien niemals an Issues oder Pull Requests an und teile sie nicht
> (siehe [`CONTRIBUTING.md`](../../../CONTRIBUTING.md)).

Das Projekt steht noch am Anfang: **Das Spiel startet noch nicht.**

## Voraussetzungen

- **Deine eigene Kopie** des Xbox-Series-Pakets in Version `1.0.2.1` (der Ordner mit `MicrosoftGame.Config` und den
  `.rpf`-Archiven, als normale, lesbare Dateien auf der Festplatte).
- **Windows 10/11 x64** mit etwa **350 GB freiem Speicher** für die Arbeitsdateien (mit `--work` auch auf einem
  anderen Laufwerk).
- [Python 3.12+](https://www.python.org/downloads/) (bei der Installation *„Add python.exe to PATH“* anhaken).
- [.NET 8 SDK](https://dotnet.microsoft.com/download/dotnet/8.0).
- [Vulkan SDK](https://vulkan.lunarg.com/) (`spirv-as`, `spirv-val` und `vulkaninfo`).
- [LLVM 21+](https://github.com/llvm/llvm-project/releases) mit dem **AMDGPU-Target** (`llvm-mc`):
  - Der offizielle LLVM-Build für Windows enthält das AMDGPU-Target **nicht**.
  - Ein natives `llvm-mc.exe` mit AMDGPU baut `.\scripts\build_llvm_mc.ps1`, ausgeführt in PowerShell im
    Hauptordner des Repositorys. Das Skript braucht `git` und die C++-Tools von Visual Studio, setzt die Variable
    `LLVM_MC` für deinen Benutzer und bittet dich, danach ein neues Terminal zu öffnen, damit sie sichtbar wird.
  - Mit WSL: `llvm-21` in Ubuntu installieren und die Variable in derselben PowerShell-Sitzung setzen, in der du
    `make_game.py` startest:

    ```powershell
    $env:LLVM_MC = 'wsl -d Ubuntu -e /usr/lib/llvm-21/bin/llvm-mc'
    ```

    Mehr dazu in [`docs/design.md`](../../design.md#disassembling-shaders).
  - Unter Linux reicht `llvm-mc` im `PATH`.
- **Visual Studio 2022** mit der Workload *Desktopentwicklung mit C++* und dem **Windows SDK `10.0.26100` oder
  neuer** (für die Stufe `runtime`, die Root Signatures der Version 1.2 verwendet).
- [ffmpeg](https://ffmpeg.org/download.html) im `PATH` *(optional: dekodiert die XMA2-Streams und wird für `--wav`
  gebraucht)*.

## So baust du es

### Der einfache Weg (Windows)

1. Installiere die Voraussetzungen oben.
2. Klone das Repository **mit seinem Submodul**:
   ```powershell
   git clone --recursive https://github.com/ItsNotRexed/Leonida
   ```
3. Doppelklicke auf **`Build.bat`**.

Der Build findet deine Kopie selbst: Er sucht in Downloads, Desktop, Dokumente (auch dort, wo Windows sie ablegt,
etwa in OneDrive), Games, deinem Benutzerordner und oben auf jedem Laufwerk, jeweils zwei Ordner tief. Die Version
erkennt er an ein paar Dateigrößen, ohne große Dateien zu lesen. Findet er mehrere Kopien, fragt er, welche er nehmen
soll, findet er keine, bittet er dich, den Ordner einzufügen. Vor dem Start prüft er, ob alles installiert ist und
genug Platz frei ist, und listet alles Fehlende auf einmal auf. Das Ergebnis landet in `Documents\Leonida`. Ein
abgebrochener Build macht beim nächsten Start dort weiter, wo er aufgehört hat.

### Aus dem Terminal (`make_game.py`)

```powershell
python -I make_game.py --find                          # zeigt die auf diesem PC gefundenen Kopien
python -I make_game.py --pick                          # Spielordner in einem Ordnerdialog auswählen
python -I make_game.py --check                         # prüft deine Kopie, baut nichts
python -I make_game.py --check --full-check            # vergleicht zusätzlich die SHA-256 jeder Datei (langsam)
python -I make_game.py --game "D:\pfad\zum\paket" --out "D:\Games\Leonida" --work "E:\leonida-work"
python -I make_game.py --stages shaders,assets         # nur bestimmte Stufen
python -I make_game.py --game "D:\pfad\zum\paket" --wav   # dekodiert Opus zusätzlich zu PCM-WAV (groß)
python -I make_game.py --wave64-emulate auto           # auto (Standard), on oder off
```

Die Stufen laufen in dieser Reihenfolge: `check` → `shaders` → `assets` → `runtime` → `pipelines`.

- **`--wave64-emulate`**: emuliert Wave64-Compute- und -Mesh-Shader auf GPUs mit 32 breiten Subgroups (NVIDIA und
  Intel). Der Standard `auto` fragt `vulkaninfo` und schaltet die Emulation selbst ein, wenn sie gebraucht wird.
  AMD-Karten (RDNA) führen Wave64 nativ aus.
- **`--work`**: am besten ein Laufwerk mit mindestens 350 GB freiem Platz angeben. Die Stufe `shaders` dauert am
  längsten und nutzt standardmäßig die Hälfte der CPU-Threads (änderbar mit `--jobs`).
- **`pipelines`**: erstellt jeden übersetzten Shader auf deinem GPU-Treiber und speichert einen Pipeline-Cache,
  damit der erste Start sie nicht kompilieren muss.
- **Linux und Wine/Proton**: `make_game.py` läuft auch unter Linux. Um die Stufe `runtime` außerhalb von Windows zu
  nutzen, gib mit `--runtime-build <ordner>` einen Windows-Build an (`gta6rt.exe` und `gta6rt_tls_host.dll`). Das
  Skript legt dann `gta6rt.sh` an, das das Programm mit `wine` startet (oder mit dem Wine aus der Variable `WINE`,
  etwa von Proton).

## Häufige Probleme

- **Repository über „Download ZIP“ auf GitHub geladen**: Das ZIP enthält das Submodul `third_party/rpf9-explorer`
  nicht. Nimm `git clone --recursive` oder führe im Repository-Ordner `git submodule update --init --recursive` aus.
- **`llvm-mc` lehnt `gfx1013` / AMDGPU ab**: Du hast vermutlich den offiziellen LLVM-Installer für Windows. Nimm
  `scripts\build_llvm_mc.ps1` oder setze `LLVM_MC` auf das `llvm-mc` aus WSL (`llvm-21`).
- **Fehler `D3D12_ROOT_SIGNATURE_DESC2` beim Bauen der Runtime**: Aktualisiere das Windows SDK auf `10.0.26100` oder
  neuer (über den Visual Studio Installer oder `winget install Microsoft.WindowsSDK.10.0.26100`), lösche den
  Build-Ordner und konfiguriere neu.
- **Kein Platz auf `C:`**: Mit `--work "D:\leonida-work"` legst du die Arbeitsdateien auf ein anderes Laufwerk.

## Mithelfen und GPU-Tests ausführen

1. Schau dir die offenen Pull Requests und das [Koordinations-Issue][coord] an, damit keine Arbeit doppelt gemacht
   wird. Reserviere eine Aufgabe dort mit `claiming: <ID>` oder mit einer kurzen Beschreibung, wenn sie noch keine ID
   hat.
2. Führe die passenden Tests mit synthetischen Eingaben aus, zum Beispiel den Manifest-Prüfer mit
   `python -I tests/test_manifest_verifier.py` im Hauptordner des Repositorys.
3. Nenne im Pull Request die Aufgaben-ID, beschreibe, was du getestet hast, und bei GPU-Tests die Grafikkarte und die
   Treiberversion. Gib an, ob du KI-Unterstützung genutzt hast. Die Regeln stehen in
   [`CONTRIBUTING.md`](../../../CONTRIBUTING.md).

Die [Anleitung zu den GPU-Tests](../../testing.md) zeigt, wie sie ohne Spieldateien laufen.
Ergebnisse von GPU-Tests gehören in [Issue #166](https://github.com/ItsNotRexed/Leonida/issues/166).

[coord]: https://github.com/ItsNotRexed/Leonida/issues?q=is%3Aissue%20is%3Aopen%20in%3Atitle%20%22Coordinating%20who%20works%20on%20what%22
