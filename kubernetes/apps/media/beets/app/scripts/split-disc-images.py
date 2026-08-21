"""Split single-file album rips into per-track FLAC using their CUE sheets.

Some albums entered the library as one big file -- a whole CD ripped without
splitting -- so Navidrome can only offer them as a single 40-minute track. The
torrent that supplied them usually shipped a CUE sheet, which carries exact
sample offsets and per-track titles.

The split is additive: new files are written to a staging directory and the
originals are left alone. Removing the originals afterwards is a separate,
deliberate step, because it deletes existing library content.

Two kinds of long file must NOT be split, and both are detected from the CUE
rather than from a hardcoded list: a cue with one TRACK is a continuous DJ mix,
and a cue with several FILE entries describes an album that is already split.
"""

import argparse
import json
import os
import re
import subprocess
import sys

FRAME_SAMPLES = 588  # 44100 / 75, so CUE frames convert without rounding


def read_cue(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_cue(text):
    album = {"title": None, "performer": None, "date": None, "genre": None}
    tracks, cur, in_track, files = [], None, False, 0
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^FILE\s", s, re.I):
            files += 1
            continue
        m = re.match(r"^TRACK\s+(\d+)\s+AUDIO", s, re.I)
        if m:
            cur = {"num": int(m.group(1)), "title": None, "performer": None, "start": None}
            tracks.append(cur)
            in_track = True
            continue
        m = re.match(r'^TITLE\s+"?(.*?)"?$', s, re.I)
        if m:
            (cur if in_track else album)["title"] = m.group(1).strip()
            continue
        m = re.match(r'^PERFORMER\s+"?(.*?)"?$', s, re.I)
        if m:
            (cur if in_track else album)["performer"] = m.group(1).strip()
            continue
        m = re.match(r'^REM\s+DATE\s+"?(.*?)"?$', s, re.I)
        if m:
            album["date"] = m.group(1).strip()
            continue
        m = re.match(r'^REM\s+GENRE\s+"?(.*?)"?$', s, re.I)
        if m:
            album["genre"] = m.group(1).strip()
            continue
        m = re.match(r"^INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)", s, re.I)
        if m and in_track:
            idx, mm, ss, ff = (int(g) for g in m.groups())
            samples = ((mm * 60 + ss) * 75 + ff) * FRAME_SAMPLES
            # INDEX 01 is the track proper; INDEX 00 is its pregap.
            if idx == 1 or cur["start"] is None:
                cur["start"] = samples
    return album, tracks, files


def norm_key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").casefold())


def safe(name):
    name = re.sub(r"[/\x00]", "_", name or "")
    return name.strip(" .") or "Unknown"


def split_one(entry, outroot, apply_):
    src = entry["path"]
    cue = entry["cues"][0]
    album, tracks, files = parse_cue(read_cue(cue))

    if len(tracks) < 2:
        return "hoppar över (kontinuerlig mix, 1 spår i cue)", 0
    if files > 1:
        return f"hoppar över (cue har {files} FILE-poster, redan styckat)", 0

    total = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration_ts", "-of", "csv=p=0", src],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        total_samples = int(total.split(",")[0])
    except ValueError:
        return f"hoppar över (kunde inte läsa längd: {total!r})", 0

    aa = entry.get("albumartist") or album["performer"] or "Unknown"
    alb = entry.get("album") or album["title"] or "Unknown"
    outdir = os.path.join(outroot, safe(aa), safe(alb))

    made = 0
    for i, t in enumerate(tracks):
        start = t["start"] or 0
        end = tracks[i + 1]["start"] if i + 1 < len(tracks) else total_samples
        title = t["title"] or f"Track {t['num']:02d}"
        dest = os.path.join(outdir, f"{t['num']:02d} - {safe(title)}.flac")
        if not apply_:
            made += 1
            continue
        os.makedirs(outdir, exist_ok=True)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
            "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1",
            "-af", f"atrim=start_sample={start}:end_sample={end},asetpts=N/SR",
            "-c:a", "flac", "-compression_level", "8",
            "-metadata", f"TITLE={title}",
            "-metadata", f"ARTIST={t['performer'] or aa}",
            "-metadata", f"ALBUM={alb}",
            "-metadata", f"ALBUMARTIST={aa}",
            "-metadata", f"TRACKNUMBER={t['num']}",
            "-metadata", f"TRACKTOTAL={len(tracks)}",
        ]
        if album["date"] or entry.get("year"):
            cmd += ["-metadata", f"DATE={album['date'] or entry['year']}"]
        if album["genre"]:
            cmd += ["-metadata", f"GENRE={album['genre']}"]
        cmd.append(dest)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return f"FEL på spår {t['num']}: {r.stderr.strip()[:120]}", made
        made += 1

    if apply_ and made:
        bad = subprocess.run(
            ["bash", "-c", f'flac -t -s "{outdir}"/*.flac 2>&1'],
            capture_output=True, text=True,
        )
        if bad.returncode != 0:
            return f"FEL: flac -t underkände utdata ({bad.stdout.strip()[:120]})", made
    return f"{made} spår", made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--matched", default="/tmp/split-audit/matched.json")
    ap.add_argument("--out", default="/data/staging/cuesplit")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    entries = [e for e in json.load(open(args.matched)) if e.get("cues")]
    print(f"kandidater med cue: {len(entries)}")

    best = {}
    for e in entries:
        try:
            _, tracks, files = parse_cue(read_cue(e["cues"][0]))
        except Exception:
            tracks, files = [], 1
        key = (norm_key(e.get("albumartist")), norm_key(e.get("album")))
        score = (len(tracks), e.get("size") or 0)
        if key not in best or score > best[key][0]:
            best[key] = (score, e)
    dropped = len(entries) - len(best)
    if dropped:
        print(f"dubblettpressningar utan bäst cue: {dropped} (behåller den mest kompletta per album)")
    entries = [e for _, e in best.values()]
    print()

    total_tracks = albums = skipped = 0
    for e in entries:
        label = f"{e.get('albumartist')} — {e.get('album')}"
        msg, n = split_one(e, args.out, args.apply)
        print(f"  {label}: {msg}")
        if n:
            albums += 1
            total_tracks += n
        else:
            skipped += 1

    print()
    print(f"album styckade: {albums}, spår: {total_tracks}, överhoppade: {skipped}")
    if not args.apply:
        print("TORRKÖRNING - inget skrivet. Kör med --apply.")
    else:
        print(f"utdata: {args.out}")
        print("originalen är ORÖRDA; att ta bort dem är ett separat beslut.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
