# Music imports — Session Handoff (uppdaterad 2026-08-07 ~07:40 CEST)

Silvercheek-importen (svärfars samling) är **KLAR**. Kvar i repot finns två uppföljningar:
FLAC-styckningen och Bowie-dumpen. Bakgrund i auto-memory:
`project_music_library_tagging_pending.md` + `project_beets_music_tagger.md`.

## ✅ Silvercheek-importen — KLAR 2026-08-07

Svärfar (Nextcloud-konto `silvercheek`, Jochen Klug) delade `Silvercheek Muzik`,
190,5 GiB / 19 687 filer i Nextclouds Ceph-RBD-PVC.

| Steg | Resultat |
|---|---|
| Staging | 19 424 ljudfiler / 189,9 GiB på 53 min (263 cruft-filer exkluderade) |
| Ljudböcker → ABS | 820 filer / 11,3 GB (Charlaine Harris 729, Tomas Bolme 91) |
| Lossless → FLAC | 461 filer, 189G → 170G |
| **Importerat** | **17 727 spår** (beets 23 665 → **41 391**) |
| Kvar i staging | 877 filer / 86 album (dubbletter + Beatles, se nedan) |
| Navidrome | **41 391 present / exakt 130 missing** (baseline höll hela vägen) |

Slutläget är verifierat 2026-08-07 (full scan, 8 min): **0 casing-kollisioner bland
levande filer**, 0 blanksteg, 0 `[Unknown Album]`, 0 `[Unknown Artist]`, och exakt 1 av
17 727 importerade spår saknade albumartist (en iTunes LP-artefakt som togs bort).

Enda kvarvarande kollision är avsiktlig: `Gloria Estefan And Miami Sound Machine` på
`Various Artists/NOW That's What I Call An 80s Summer (2026)/14 - Conga.flac`, som är
hardlinkad till en **RED/OPS-torrent**. Seeding-regeln vinner över kosmetiken — rör den
inte utan copy-then-write.

### ⚠️ LÄS DETTA FÖRE NÄSTA TAGGJOBB — `beet modify` matchar DELSTRÄNGAR
`beet modify albumartist:"Blue "` tappade sitt efterföljande blanksteg i skalet och
degraderade till en delsträngsfråga som skrev om **allt som innehöll "Blue"** →
Bluesology (127 spår), Blues Brothers (11), Jonas Blue (7), The Bluebells, Ilan
Bluestone, Bread And Beer Band. **169 rader korrumperade.**

Räddningen: `beet modify` skriver bara DB:n, inte filerna — filtaggarna var orörda,
så återställningen var att läsa tillbaka sanningen ur filerna
(`/config/tag-job/repair-blue.py`, 162 spår + 12 omräknade album).

**Regel framåt: använd ALDRIG `beet modify` med frågesträngar för taggfixar.**
Kör via beets Python-API och iterera över `lib.items()` med explicita jämförelser.
Färdiga, korrekta skript finns i podden: `fix-casing-final.py` (kanonisk stavning +
whitespace-trim, med RED/OPS-skydd) och `repair-blue.py` (återställ från filtaggar).

### ⚠️ beets lagrar RELATIVA sökvägar
`items.path` är relativ mot `directory` (`Blue/Best Of Blue (2004)/01 - ...mp3`).
Öppnar man biblioteket i Python måste man passa katalogen explicit:
```python
lib = library.Library("/config/library.db", directory="/data/media/musik")
```
Annars resolveras allt mot beets standard `/config/Music` och **varje fil ser ut att
saknas**. Detta gav mig flera falska larm om "borttagna" filer. `os.path.exists()` på
en rå DB-sökväg ljuger — kontrollera med `ls` i podden vid tvekan.

### Overlays (i podden, `/config/tag-job/`)
- `silvercheek-import-overlay.yaml` — `move: yes`, `write: yes`, `duplicate_action: skip`.
  Vänder baskonfigens seeding-profil (`hardlink: yes` + `write: no`). Verifierat före
  start att alla staged-filer hade `nlink=1`. Sätt ALDRIG `copy: yes` här.
- `silvercheek-final-overlay.yaml` — samma men `incremental: no`, för slutpasset.
- `silvercheek-gaps-overlay.yaml` — `duplicate_action: keep`, för att importera album
  som medvetet ska samexistera med en befintlig version.

### ⚠️ OOM: importera ALDRIG hela trädet i ett `beet`-anrop
Första försöket blev **OOMKilled (exit 137)** mot 2Gi efter 54 min och 1 311 spår —
beets buffrar varje albums MusicBrainz-kandidater (`search_limit: 10`) obegränsat.
Fixen är `/config/tag-job/batch-import.sh`: ett `beet import` per artistmapp, vilket
höll minnet på 240–560 MiB. Minnesgränsen är sedan höjd till 6Gi (commit `b158d485`)
som takhöjd, men **batchningen är den riktiga fixen** — höj inte gränsen som "lösning".

Ingen dataförlust vid OOM:en: `move`-semantiken gör att filer antingen är i biblioteket
eller kvar i staging, aldrig däremellan (verifierat: 1 311 importerade = 1 311 borta).

### Ljudbokssvep — längd ensam RÄCKER INTE
Första svepet ffprobe:ade en fil per artistmapp och flaggade > 40 min. Det hittade
Charlaine Harris (4,5 h-spår) men **missade Tomas Bolme**, vars kapitel är 6 min och
vars första fil är 6 sekunder. Använd flera signaler vid nästa externa samling:
1. **genre** per albummapp (`Spoken & Audio` fällde Bolme; sök även ljudbok/talbok/audiobook)
2. **generiska spårtitlar** (`Spår 10c`, `Kapitel N`)
3. **längd** > 20 min
4. **>40 filer i ett album** (mest false positives — Zappas 182-spåriga box-set)

Kolumnen heter `items.genres` (plural) i beets 2.x, inte `genre`.

## 🔜 UPPFÖLJNING 1: stycka FLAC disc-images med CUE (beslutad, ej påbörjad)

**377 av 3 417 album i biblioteket är 1-spårs disc-images** — en enda stor FLAC per
album, alltså inte spelbara låt för låt i Navidrome. Beatles är värst: **45 av 48 album**.
"Nowhere Man" och "Ticket To Ride" finns inte som sökbara spår.

**Torrenten har 60 CUE-filer**, t.ex.
`/data/torrents/music/The Beatles -  Discography (1963-2013) [FLAC]/Original Masters/.../*.cue`

Planen (användarens val 2026-08-07): stycka de egna FLAC-imagesen med CUE → per-spår
lossless, i stället för att importera Jochens mp3. Styckning skapar NYA filer och rör
inte originalen, så seeding är opåverkat.

De 877 filerna i `/data/staging/silvercheek-import` **behålls tills detta är klart** —
de är fallback för album som saknar CUE. Radera dem först därefter (`rm -rf`, INTE via
något som routar till `/data/.Trash-1000`; kolla att `/data/.zfs/snapshot` är tom).

## ⏳ UPPFÖLJNING 2: David Bowie-dumpen (användaren raderar själv)
293 mp3 utan albumtaggar, lösa i `David Bowie/00 - *.mp3` (267 Bowie + 26 Tin Machine,
alla `[Unknown Album]`). Källtorrenten är en platt dump — beets kunde inte göra bättre.

**EFTER radering:** `beet update` → Navidrome-scan → kirurgisk ghost-städning (DB-backup
först, radera ENDAST missing=1-rader vars path matchar `David Bowie/00 - %`, ALDRIG
baseline-130) → verifiera genre-gaps 0, unknown-album 0.

## Import-pipelinen ("konstens alla regler")

Allt körs via `kubectl exec` i `deploy/beets` (ns media, LSIO: exec=root, kör beets/filer
som `s6-setuidgid abc`, uid 568). ALDRIG skriva/radera under `/data/torrents`.
Långa jobb: `setsid nohup ... &` + loggfil i podden (lokala watchdogs dör om datorn sover).

1. **Baseline:** `beet stats` + Navidrome present/missing-count.
2. **Import:** batchat per artistmapp, detached. Aldrig hela trädet på en gång.
3. **Slutpass** med `incremental: no` — fångar mappar som en krasch bokfört som klara.
4. **Granska resterna:** för varje kvarvarande album, verifiera på SPÅRNIVÅ att
   biblioteket har motsvarigheten. Mappnamn ljuger (staging säger "Unknown Album" medan
   taggarna har det riktiga namnet; "TEY 1965-1967" i staging = "The Early Years:
   1965–1967" i biblioteket). Skript: `/config/tag-job/uniqueness-audit.py`.
5. **Tracker-karta:** `/config/tag-job/refresh_tracker_map.py` — qBit-API via tracker-URL-HOST
   (flacsfor.me/redacted=RED, opsfet.ch/orpheus=OPS). **Bara RED/OPS skyddas** mot
   taggskrivning; andra trackers får skrivas in-place även när `nlink>=2`.
6. **Casing-audit:** kör kollisionsquery på **BÅDE `albumartist` OCH `artist`** — de ger
   olika träffar. Jag kollade först bara albumartist och missade a-ha/a-Ha,
   Frankie Goes to Hollywood/To och Gloria Estefan And/and.
   ⚠️ **Filtrera ALLTID på `missing=0`.** Baseline-130-spökena har gamla stavningar kvar
   och ser ut som kollisioner annars — de kostade mig en onödig full scan och en lång
   felsökning. Navidrome döljer saknade filer i UI:t, så de påverkar inga tiles.
   Kanonisk stavning =
   **MusicBrainz, ALDRIG nuvarande tile-visning** (Jason Derulo-incidenten). Bevisat värde:
   MB säger `Bo Kaspers orkester` med litet o — den BEFINTLIGA stavningen var fel.
   Fastställda 2026-08-07: `Charli xcx` (gemener), `LeAnn Rimes`, `Middle of the Road`,
   `The Art of Noise`, `Prince and The Revolution`, `Bruce Springsteen & The E Street Band`,
   `Frankie Goes to Hollywood`, `Gloria Estefan and Miami Sound Machine`, `a-ha`.
   ⚠️ **Medveten avvikelse:** MB skriver `a‐ha` och `The B‐52's` med typografiskt
   bindestreck U+2010. Vi använder ASCII-bindestreck för a-ha — kollisionen gällde
   versalen i "a-Ha", och U+2010 försämrar sökbarheten. (B‐52s kom in med U+2010 via
   beets MB-matchning vid import; lämnat som det är.)
   Äldre: Jason Derulo, KISS, MIKA, Tiësto, will.i.am, The Jimi Hendrix Experience.
   **Taggarna måste skrivas till FILERNA** — Navidrome läser filtaggar, inte beets-DB:n.
7. **Genre-backfill:** gap-query på `items.genres`; OBS tomsträngs-gotchan i mutagen
   (`genre=['']` är truthy — kolla `any(v.strip() ...)`). MB release-group → artist-fallback
   (1 req/s, UA `solen-ops-tagfix/1.0 (mattias@imbox.se)`).
8. **Navidrome-scan:** `navidrome scan` (inkrementell, ~10 s) räcker för nya/ändrade filer.
   Verifiera present = baseline + importerat, missing = **exakt 130**.
9. `[Unknown Album/Artist]`: platta dumpar eller taggllösa filer — tagga från mappnamn där
   det är entydigt, annars fråga. Single-file disc-images utan CUE: 1-spårs-album
   (prejudikat: Gary Moore .ape, Zeppelin III, Mothership, MMT, LIBN).

## Infrastrukturfakta
- `/data/staging`, `/data/media/musik`, `/data/torrents` är **vanliga mappar på ETT
  ZFS-dataset** (`rust/data`, 144T/37T ledigt) — bevisat via identisk `st_dev`, `.zfs` bara
  på `/data`, och levande hardlinks tvärs `musik`↔`torrents`. Därför är beets `move` en
  `rename()` och staging dräneras utan dubbellagring.
- TrueNAS-exporten har **`maproot=apps`** → en pod som kör som root får skriva på NFS och
  filerna landar som uid 568 automatiskt.
- Nextcloud (ns `default`) ligger på **ceph-block RWO**. En andra pod kan bara montera
  PVC:n **på samma nod** som Nextcloud-podden. PVC-roten monteras med subPaths
  (`root`, `html`, `data`, …), så användardata är `data/<user>/files/`. Scopa monteringen
  med `subPath` så jobbet inte kan se andra användares filer, och sätt `readOnly: true`.
- ABS indexerar INTE filer som en annan pod skrivit (inotify korsar inte NFS-klienter).
  Trigga scan via API — se auto-memory `project_abs_scan_trigger.md`. `rollout restart`
  fungerar inte OCH avbryter pågående lyssning.
- Nextcloud-användare: `mattias`, `susanna`, `silvercheek` (Jochen Klug, svärfar).
- `beet update -p` betyder `--pretend`. Kolla `beet stats` efteråt att något hände.

## Övrigt öppet (lågprio)
- Maroon 5 JORDI har all-caps låtskrivarkrediter — kosmetiskt, fixa bara på begäran.
- 3 Phil Collins `(loose)`-filer (nära-dubbletter) för granskning.
- 2 Gary Moore `.ape` utan track# + 10 Human League-år (ingen MB-träff).
- 1 149 befintliga FLAC-spår saknar albumartist på spårnivå (albumnivån är satt, så
  sökvägarna är korrekta). Kosmetiskt; fanns före denna import.

**Radera denna fil när FLAC-styckningen OCH Bowie-punkten är klara.**
