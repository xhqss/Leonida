# Leonida — Guide de démarrage (`fr`)

> [!NOTE]
> La documentation en anglais ([`README.md`](../../../README.md), [`CONTRIBUTING.md`](../../../CONTRIBUTING.md) et
> [`docs/README.md`](../../README.md)) **fait foi**. Ce guide ne reprend que les instructions stables d'installation
> et de compilation (synchronisé avec le commit `e7584c52`). Pour l'état d'avancement du projet, qui change tous les
> jours, consultez le [`README.md`](../../../README.md).

## Ce qu'est Leonida

Leonida est un projet de recompilation statique de Grand Theft Auto VI (version Xbox Series) pour le faire tourner
nativement sous Windows. Il se construit **sur votre PC, à partir de votre propre copie du jeu**. Les shaders RDNA2 de
la console sont traduits en SPIR-V (Vulkan), les ressources sont lues directement dans votre paquet et le logiciel
système de la console est réimplémenté sous forme de shims.

> [!IMPORTANT]
> Ce dépôt **ne contient ni le jeu, ni clés, ni exécutable**, et n'explique pas comment les obtenir. Rien ici ne
> déchiffre ni ne contourne une protection. Tout ce que la compilation produit (shaders traduits, ressources
> extraites, dossier de travail, sauvegardes) provient de votre copie et **doit rester sur votre machine** : ne
> joignez et ne partagez jamais ces fichiers dans une issue ou une pull request (voir
> [`CONTRIBUTING.md`](../../../CONTRIBUTING.md)).

Le projet en est à ses débuts : **le jeu ne démarre pas encore**.

## Prérequis

- **Votre propre copie** du paquet Xbox Series, version `1.0.2.1` : le dossier qui contient `MicrosoftGame.Config` et
  les archives `.rpf`, sous forme de fichiers lisibles sur le disque.
- **Windows 10/11 x64**, avec environ **350 Go libres** pour les fichiers de travail. Ils peuvent aller sur un autre
  disque avec `--work`.
- [Python 3.12+](https://www.python.org/downloads/) (cochez « Add python.exe to PATH » à l'installation).
- [SDK .NET 8](https://dotnet.microsoft.com/download/dotnet/8.0).
- [SDK Vulkan](https://vulkan.lunarg.com/) (`spirv-as`, `spirv-val` et `vulkaninfo`).
- [LLVM 21+](https://github.com/llvm/llvm-project/releases) avec la **cible AMDGPU** (`llvm-mc`) :
  - La version officielle de LLVM pour Windows **n'inclut pas** la cible AMDGPU.
  - Pour compiler un `llvm-mc.exe` natif avec cette cible, lancez `.\scripts\build_llvm_mc.ps1` dans PowerShell
    depuis la racine du dépôt. Le script a besoin de `git` et des outils C++ de Visual Studio. Il définit `LLVM_MC`
    pour votre utilisateur : ouvrez ensuite un nouveau terminal pour que la variable soit prise en compte.
  - Avec WSL, installez `llvm-21` dans Ubuntu et donnez la commande entière à `LLVM_MC`, dans la session PowerShell
    où vous lancerez `make_game.py` :

    ```powershell
    $env:LLVM_MC = 'wsl -d Ubuntu -e /usr/lib/llvm-21/bin/llvm-mc'
    ```

    Voir [`docs/design.md`](../../design.md#disassembling-shaders) pour les détails.
  - Sous Linux, un `llvm-mc` présent dans le `PATH` suffit.
- **Visual Studio 2022** avec la charge de travail *Développement Desktop en C++* et le **SDK Windows `10.0.26100` ou
  plus récent**, pour l'étape `runtime` (le runtime utilise les root signatures 1.2, absentes des SDK plus anciens).
- [ffmpeg](https://ffmpeg.org/download.html) dans le `PATH` *(facultatif : décode les flux XMA2, et obligatoire avec
  `--wav`)*.

## Compiler

### La méthode simple (Windows)

1. Installez les prérequis ci-dessus.
2. Clonez le dépôt **avec son sous-module** :
   ```powershell
   git clone --recursive https://github.com/ItsNotRexed/Leonida
   ```
3. Double-cliquez sur **`Build.bat`**.

Le script trouve votre copie tout seul : il cherche dans Téléchargements, Bureau, Documents, Games, votre dossier
utilisateur et à la racine de chaque disque, sur deux niveaux de dossiers. S'il trouve plusieurs copies, il demande
laquelle utiliser ; s'il n'en trouve aucune, il vous demande de coller le chemin du dossier. Avant de commencer, il
vérifie les prérequis et l'espace libre, et liste d'un coup tout ce qui manque. Le résultat va dans
`Documents\Leonida`. Une compilation interrompue reprend là où elle s'était arrêtée.

### Depuis un terminal (`make_game.py`)

```powershell
python -I make_game.py --find                          # liste les copies trouvées sur ce PC
python -I make_game.py --pick                          # ouvre une fenêtre pour choisir le dossier du jeu
python -I make_game.py --check                         # vérifie votre copie, sans rien compiler
python -I make_game.py --check --full-check            # vérifie aussi le SHA-256 de chaque fichier (lent)
python -I make_game.py --game "D:\chemin\vers\le\paquet" --out "D:\Games\Leonida" --work "E:\leonida-work"
python -I make_game.py --stages shaders,assets         # seulement certaines étapes
python -I make_game.py --game "D:\chemin\vers\le\paquet" --wav  # décode aussi l'Opus en WAV PCM (volumineux)
python -I make_game.py --wave64-emulate off            # auto (par défaut), on ou off
```

Les étapes s'exécutent dans cet ordre : `check` → `shaders` → `assets` → `runtime` → `pipelines`.

- **`--wave64-emulate`** : traduit les shaders compute et mesh wave64 de façon exacte quelle que soit la taille des
  sous-groupes du GPU. Les GPU NVIDIA et Intel ont des sous-groupes de 32 et en ont besoin. La valeur par défaut,
  `auto`, interroge `vulkaninfo` et active l'émulation sauf si tous les GPU annoncent des sous-groupes de 64.
- **`--work`** : choisissez un disque avec au moins 350 Go libres. La traduction des shaders est l'étape la plus
  longue ; elle utilise par défaut la moitié des threads du processeur (réglable avec `--jobs`).
- **`--wav`** : désactivé par défaut, car le WAV PCM 16 bits est plusieurs fois plus volumineux que l'Ogg Opus.
- **Linux et Wine/Proton** : `make_game.py` fonctionne aussi sous Linux. Le chargeur est un programme Windows et
  tourne sous Wine ou Proton. Pour l'étape `runtime`, vous pouvez passer `--runtime-build <dossier>` avec une
  compilation Windows (`gta6rt.exe` et `gta6rt_tls_host.dll`) : le script écrit alors `gta6rt.sh`, qui lance
  `gta6rt.exe` avec `wine`, ou avec le Wine désigné par la variable `WINE` (pour Proton, son `files/bin/wine`).

## Problèmes courants

- **Vous avez utilisé « Download ZIP » sur GitHub** : le ZIP ne contient pas le sous-module
  `third_party/rpf9-explorer`. Utilisez `git clone --recursive`, ou lancez `git submodule update --init --recursive`
  dans le dossier du dépôt.
- **`llvm-mc` refuse `gfx1013` ou la cible AMDGPU** : vous utilisez probablement l'installateur officiel de LLVM pour
  Windows. Lancez `scripts\build_llvm_mc.ps1`, ou faites pointer `LLVM_MC` vers le `llvm-mc` de WSL (`llvm-21`).
- **CMake s'arrête sur « needs the Windows SDK 10.0.26100 or newer »**, ou MSVC signale que `Desc_1_2` n'est pas
  membre de `D3D12_VERSIONED_ROOT_SIGNATURE_DESC` : mettez à jour le SDK Windows en `10.0.26100` ou plus récent, par
  le Visual Studio Installer ou avec `winget install Microsoft.WindowsSDK.10.0.26100`, puis supprimez
  `runtime/build` et relancez la configuration.
- **Plus de place sur `C:`** : passez `--work "D:\leonida-work"` pour placer les fichiers temporaires sur un autre
  disque.
- **Windows refuse de lancer les programmes de test tout juste compilés** (« An Application Control policy has
  blocked this file ») : c'est le Contrôle intelligent des applications de Windows 11. Voir
  [`docs/testing.md`](../../testing.md#known-problems).

## Contribuer et lancer les tests GPU

1. Consultez les pull requests ouvertes et l'[issue de coordination][coord] pour ne pas refaire un travail en cours.
   Réservez la tâche avec `claiming: <ID>`, ou avec une description d'une ligne s'il n'y a pas encore d'identifiant.
2. Lancez les tests concernés, avec des entrées synthétiques. Par exemple, `python -I tests/test_manifest_verifier.py`
   depuis la racine du dépôt.
3. Dans la pull request, indiquez l'identifiant de la tâche, ce que vous avez testé, et le GPU et le pilote si vous
   avez lancé des tests GPU. Précisez si vous avez été assisté par une IA. Les règles sont dans
   [`CONTRIBUTING.md`](../../../CONTRIBUTING.md).

Le [guide des tests GPU](../../testing.md) explique comment lancer les tests sans aucun fichier du jeu. Publiez vos
résultats dans l'[issue #166](https://github.com/ItsNotRexed/Leonida/issues/166), un commentaire par GPU et par
pilote.

[coord]: https://github.com/ItsNotRexed/Leonida/issues?q=is%3Aissue%20is%3Aopen%20in%3Atitle%20%22Coordinating%20who%20works%20on%20what%22
