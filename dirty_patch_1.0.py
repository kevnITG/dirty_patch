from __future__ import annotations

import os
import queue
import re
import shutil
import struct
import sys
import tempfile
import threading
import traceback
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

EXPECTED_GAMEMDX_SIZE = 4_995_736

ROUTING_PAD_ENTRY = bytes.fromhex("66666666666666cc")

STARTUP_ARC_VARIANTS = [
    "data/arc/startup.arc",
    "data/arc/startup.arc.omnimix_filtered",
    "data/arc/startup.arc.omnimix_backup",
]

A3_CONTENTS_CANDIDATES: list[Path] = []

BACKUP_SUFFIX = ".bak_preSpecialSSQImporter"

SSQ_STEM_RE = re.compile(r"^(?P<base>[a-z0-9]{2,6})(?:_(?P<tier>[1-5]))?$", re.IGNORECASE)


add_song = None
convert_jackets = None
thumbmod = None


MIN_PYTHON = (3, 9)


def check_environment() -> Optional[str]:
    if sys.version_info < MIN_PYTHON:
        return (
            f"This tool needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.\n\n"
            f"You're running {sys.version.split()[0]} from:\n  {sys.executable}\n\n"
            "Install a newer Python from https://www.python.org/downloads/"
        )

    missing = []
    for module, package in (("numpy", "numpy"), ("PIL", "Pillow")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        return (
            "Missing required package(s): " + ", ".join(missing) + "\n\n"
            "Install them with:\n"
            f'  "{sys.executable}" -m pip install ' + " ".join(missing) + "\n\n"
            "(py7zr is also needed if you want to import straight from a .7z)"
        )

    toolkit = script_dir() / "arc_toolkit.py"
    if not toolkit.is_file():
        return (
            f"Couldn't find arc_toolkit.py next to this script.\n\n"
            f"Looked in:\n  {toolkit.parent}\n\n"
            "dirty_patch_1.0.bat, dirty_patch_1.0.py and arc_toolkit.py all "
            "need to sit together in the same folder."
        )
    return None


def load_dependencies():
    global add_song, convert_jackets, thumbmod

    problem = check_environment()
    if problem:
        raise RuntimeError(problem)

    sys.path.insert(0, str(script_dir()))
    try:
        import arc_toolkit
    except Exception as e:
        raise RuntimeError(
            f"Failed to load arc_toolkit.py.\n\n{e}\n\n{traceback.format_exc()}"
        )
    add_song = arc_toolkit
    convert_jackets = arc_toolkit
    thumbmod = arc_toolkit


@dataclass
class SongAssets:
    ssq_files: dict = field(default_factory=dict)
    xsb: Optional[Path] = None
    xwb: Optional[Path] = None
    jacket_arc: Optional[Path] = None
    entry_xml: Optional[str] = None
    source_route: Optional[bytes] = None

    @property
    def tiers(self) -> list[int]:
        return sorted(t for t in self.ssq_files if t is not None)

    @property
    def has_bare(self) -> bool:
        return None in self.ssq_files


def parse_music_blocks(xml_text: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"<music>.*?</music>", xml_text, re.DOTALL):
        block = m.group(0)
        bm = re.search(r"<basename>([^<]+)</basename>", block)
        if bm:
            out[bm.group(1).strip().lower()] = block
    return out


def parse_difflv(entry_xml: Optional[str]) -> list[int]:
    if not entry_xml:
        return [0] * 10
    m = re.search(r"<diffLv[^>]*>([^<]*)</diffLv>", entry_xml)
    if not m:
        return [0] * 10
    vals = []
    for tok in m.group(1).split():
        try:
            vals.append(int(tok))
        except ValueError:
            vals.append(0)
    return (vals + [0] * 10)[:10]


def parse_mcode(entry_xml: Optional[str]) -> Optional[int]:
    if not entry_xml:
        return None
    m = re.search(r"<mcode[^>]*>(\d+)</mcode>", entry_xml)
    return int(m.group(1)) if m else None


def normalize_music_block(block: str) -> str:
    return re.sub(r">\s+<", "><", block.strip())


MUSIC_CHILD_RE = re.compile(r"<([a-zA-Z_0-9]+)((?:\s[^>]*)?)>(.*?)</\1>", re.DOTALL)


def synth_limited_ary(difflv: list[int]) -> str:
    return " ".join("0" if d else "-1" for d in difflv)


def target_tag_vocabulary(install_root: Path, log) -> set[str]:
    p = install_root / "data" / "arc" / "startup.arc"
    if not p.is_file():
        return set()
    try:
        files, _, _, _ = add_song._parse_arc_like(p.read_bytes())
        xml = files.get(add_song.MUSICDB_ARC_PATH)
        if not xml:
            return set()
        tags = set()
        for block in parse_music_blocks(xml.decode("utf-8", "replace")).values():
            for m in MUSIC_CHILD_RE.finditer(block):
                tags.add(m.group(1))
        tags.discard("music")
        if len(tags) < 8:
            return set()
        return tags
    except Exception as e:
        log(f"  WARN: couldn't read target musicdb tag vocabulary: {e}")
        return set()


def sanitize_entry(entry_xml: str, known_tags: set, difflv: list[int]) -> tuple[str, list[str], bool]:
    flat = normalize_music_block(entry_xml)
    m = re.match(r"<music>(.*)</music>\s*$", flat, re.DOTALL)
    if not m:
        return flat, [], False
    inner = m.group(1)

    kept: list[str] = []
    names: list[str] = []
    dropped: list[str] = []
    for cm in MUSIC_CHILD_RE.finditer(inner):
        tag = cm.group(1)
        if known_tags and tag not in known_tags:
            dropped.append(tag)
            continue
        kept.append(cm.group(0))
        names.append(tag)

    added = False
    if "limited_ary" not in names and (not known_tags or "limited_ary" in known_tags):
        tag = ('<limited_ary __type="s32" __count="10">'
               + synth_limited_ary(difflv) + "</limited_ary>")
        if "diffLv" in names:
            kept.insert(names.index("diffLv"), tag)
        else:
            kept.append(tag)
        added = True

    return "<music>" + "".join(kept) + "</music>", dropped, added


def scan_source(root: Path, log) -> dict[str, SongAssets]:
    songs: dict[str, SongAssets] = {}
    startup_arcs: list[Path] = []
    loose_musicdbs: list[Path] = []
    source_dlls: list[Path] = []

    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            low = fn.lower()
            if low == "startup.arc":
                startup_arcs.append(p)
            elif low == "musicdb.xml":
                loose_musicdbs.append(p)
            elif low.startswith("gamemdx") and low.endswith(".dll"):
                source_dlls.append(p)
            elif low.endswith(".ssq"):
                m = SSQ_STEM_RE.match(p.stem)
                if not m:
                    continue
                base = m.group("base").lower()
                tier = int(m.group("tier")) if m.group("tier") else None
                songs.setdefault(base, SongAssets()).ssq_files[tier] = p
            elif low.endswith(".xsb"):
                base = p.stem.lower()
                songs.setdefault(base, SongAssets()).xsb = p
            elif low.endswith(".xwb"):
                base = p.stem.lower()
                songs.setdefault(base, SongAssets()).xwb = p
            elif low.endswith("_jk.arc"):
                base = fn[: -len("_jk.arc")].lower()
                songs.setdefault(base, SongAssets()).jacket_arc = p

    musicdb_blocks: dict[str, str] = {}
    for arc_path in startup_arcs:
        try:
            src = arc_path.read_bytes()
            files, _, _, _ = add_song._parse_arc_like(src)
            xml = files.get("data/gamedata/musicdb.xml")
            if xml:
                for bn, block in parse_music_blocks(xml.decode("utf-8", "replace")).items():
                    musicdb_blocks.setdefault(bn, block)
        except Exception as e:
            log(f"  WARN: couldn't read {arc_path}: {e}")
    for mdb_path in loose_musicdbs:
        try:
            text = mdb_path.read_text(encoding="utf-8", errors="replace")
            for bn, block in parse_music_blocks(text).items():
                musicdb_blocks.setdefault(bn, block)
        except Exception as e:
            log(f"  WARN: couldn't read {mdb_path}: {e}")

    source_routes: dict[str, bytes] = {}
    for dll_path in sorted(source_dlls, key=lambda p: (p.name.lower() != "gamemdx.dll", str(p).lower())):
        try:
            table = add_song.read_routing_table(dll_path.read_bytes())
        except Exception as e:
            log(f"  WARN: couldn't read routing table from {dll_path}: {e}")
            continue
        if table:
            if source_routes:
                log(f"  (ignoring additional routing table in {dll_path.name})")
                continue
            for name, (_off, route) in table.items():
                source_routes[name.lower()] = route
            log(f"  found Special-SSQ routing table in {dll_path} ({len(table)} entries)")

    for bn, sa in songs.items():
        sa.entry_xml = musicdb_blocks.get(bn)
        sa.source_route = source_routes.get(bn)

    songs = {bn: sa for bn, sa in songs.items() if sa.ssq_files}
    return songs


def extract_archive(path: Path, log) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="ssqimport_src_"))
    suffix = path.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
    elif suffix == ".7z":
        try:
            import py7zr
        except ImportError:
            raise RuntimeError(
                "This source is a .7z archive but the 'py7zr' package isn't "
                "installed.\n\nInstall it with:\n    pip install py7zr\n\n"
                "...or extract the archive yourself and pick the extracted "
                "folder instead."
            )
        with py7zr.SevenZipFile(path, mode="r") as z:
            z.extractall(path=tmp)
    else:
        raise RuntimeError(f"Unsupported archive type: {suffix}")
    log(f"  extracted to {tmp}")
    return tmp


def script_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def validate_target(install_root: Path) -> dict:
    gamemdx = install_root / "modules" / "gamemdx.dll"
    startup = install_root / "data" / "arc" / "startup.arc"
    info = {
        "install_root": install_root,
        "looks_like_install": (install_root / "modules").is_dir() and startup.is_file(),
        "startup_exists": startup.is_file(),
        "gamemdx_exists": gamemdx.is_file(),
        "gamemdx_size_ok": False,
        "gamemdx_marker_ok": False,
        "gamemdx_path": gamemdx,
    }
    if gamemdx.is_file():
        try:
            size = gamemdx.stat().st_size
            info["gamemdx_size_ok"] = size == EXPECTED_GAMEMDX_SIZE
            data = gamemdx.read_bytes()
            offset = add_song.SSQ_ROUTING_TABLE_OFFSET
            marker = data[offset : offset + 8]
            info["gamemdx_marker_ok"] = marker == add_song._SSQ_ROUTING_MARKER
        except Exception:
            pass
    return info


def existing_target_basenames(install_root: Path, log) -> set[str]:
    p = install_root / "data" / "arc" / "startup.arc"
    if not p.is_file():
        return set()
    try:
        files, _, _, _ = add_song._parse_arc_like(p.read_bytes())
        xml = files.get("data/gamedata/musicdb.xml")
        if not xml:
            return set()
        return set(parse_music_blocks(xml.decode("utf-8", "replace")).keys())
    except Exception as e:
        log(f"  WARN: couldn't read target musicdb for dup-check: {e}")
        return set()


def _replace_music_block(xml: str, basename: str, entry: str) -> tuple[str, bool]:
    pattern = re.compile(
        r"<music>(?:(?!</music>).)*?<basename>" + re.escape(basename) +
        r"</basename>.*?</music>", re.DOTALL)
    new_xml, n = pattern.subn(lambda _m: entry, xml, count=1)
    return new_xml, bool(n)


def patch_all_startup_variants(install_root: Path, new_entries: list[str], log,
                               overwrite: bool = False) -> int:
    touched = 0
    for rel in STARTUP_ARC_VARIANTS:
        p = install_root / rel
        if not p.is_file():
            log(f"  (skip {rel} — not present)")
            continue
        src = p.read_bytes()
        magic = add_song.MAGIC_PMAR if src[:8] == add_song.MAGIC_PMAR else add_song.MAGIC_ARC
        files, order, raw_set, flag4 = add_song._parse_arc_like(src)
        mdb_name = "data/gamedata/musicdb.xml"
        if mdb_name not in files:
            log(f"  WARN: {rel} has no embedded musicdb.xml, skipping")
            continue
        xml = files[mdb_name].decode("utf-8")
        added_here = 0
        for entry in new_entries:
            m = re.search(r"<basename>([^<]+)</basename>", entry)
            bn = m.group(1) if m else None
            flat = normalize_music_block(entry)
            if bn and f"<basename>{bn}</basename>" in xml:
                if not overwrite:
                    continue
                xml, replaced = _replace_music_block(xml, bn, flat)
                if replaced:
                    log(f"  {rel}: replaced existing <music> entry for '{bn}'")
                    added_here += 1
                else:
                    log(f"  WARN: {rel}: couldn't locate '{bn}' block to replace; left as-is")
                continue
            head, sep, tail = xml.rpartition("</mdb>")
            xml = head + flat + sep + tail
            added_here += 1
        if added_here == 0:
            log(f"  {rel}: no new entries needed (already present)")
            continue
        projected = len(xml.encode("utf-8"))
        if projected > add_song.MUSICDB_MAX_BYTES:
            log(
                f"  ABORT: {rel} musicdb would reach {projected:,} bytes, over "
                f"the {add_song.MUSICDB_MAX_BYTES:,}-byte ceiling even with the "
                f"song-count patch applied. The game would crash at boot. No "
                f"changes written to this file."
            )
            continue
        if projected > add_song.MUSICDB_WARN_BYTES:
            log(
                f"  note: {rel} musicdb is {projected:,} bytes, over the stock "
                f"{add_song.MUSICDB_WARN_BYTES:,}-byte parse buffer — fine on an "
                f"install with the song-count patch, a boot crash without it."
            )
        bak = p.with_name(p.name + BACKUP_SUFFIX)
        if not bak.exists():
            shutil.copy2(p, bak)
            log(f"  backed up {rel} -> {bak.name}")
        files[mdb_name] = xml.encode("utf-8")
        out = add_song._pack_arc_like(files, order, raw_set, flag4, magic)
        p.write_bytes(out)
        log(f"  {rel}: +{added_here} song(s) ({len(src):,} -> {len(out):,} bytes)")
        touched += 1
    return touched


def sync_loose_musicdb(install_root: Path, new_entries: list[str], log,
                       overwrite: bool = False) -> None:
    p = install_root / "data" / "gamedata" / "musicdb.xml"
    if not p.is_file():
        return
    text = p.read_text(encoding="utf-8", errors="replace")
    if "</mdb>" not in text:
        return
    changed = False
    for entry in new_entries:
        m = re.search(r"<basename>([^<]+)</basename>", entry)
        bn = m.group(1) if m else None
        flat = normalize_music_block(entry)
        if bn and f"<basename>{bn}</basename>" in text:
            if not overwrite:
                continue
            text, replaced = _replace_music_block(text, bn, flat)
            changed = changed or replaced
            continue
        head, sep, tail = text.rpartition("</mdb>")
        text = head + flat + sep + tail
        changed = True
    if changed:
        bak = p.with_name(p.name + BACKUP_SUFFIX)
        if not bak.exists():
            shutil.copy2(p, bak)
        p.write_text(text, encoding="utf-8")
        log("  synced loose data/gamedata/musicdb.xml (startup.arc is what the "
            "game reads; keeping both in sync avoids confusing later edits)")


def patch_routing_table_safe(dll_path: Path, entries: list[tuple[str, bytes]], log) -> bool:
    if not dll_path.exists():
        log(f"  WARN: {dll_path} not found; skipping routing-table patch.")
        return False

    offset = add_song.SSQ_ROUTING_TABLE_OFFSET
    marker = add_song._SSQ_ROUTING_MARKER
    data = bytearray(dll_path.read_bytes())

    found = bytes(data[offset : offset + 8])
    if found != marker:
        log(
            f"  WARN: routing-table marker mismatch at 0x{offset:X} "
            f"(expected {marker.hex(' ')}, got {found.hex(' ')}).\n"
            f"  This tool's DLL patch only supports the exact gamemdx.dll "
            f"shipped in 'Latest Patch (rev. 7-25-26).zip'. Skipping — "
            f"apply that patch first if you need tiered-SSQ songs to load "
            f"the correct chart per difficulty."
        )
        return False

    fo = offset
    existing = {}
    while True:
        e = bytes(data[fo : fo + 8])
        if len(e) < 8:
            log(f"  ABORT: routing table runs to the end of the file without a "
                f"terminator; refusing to touch gamemdx.dll.")
            return False
        if e == b"\x00" * 8:
            break
        name = e[:5].rstrip(b"\x00").decode("ascii", "replace")
        existing[name] = fo
        fo += 8
    term_fo = fo

    new_names = [bn for bn, _ in entries if bn not in existing and len(bn) <= 5]
    end_fo = term_fo + 8 * len(new_names) + 8

    scan = term_fo
    while scan < end_fo:
        chunk = bytes(data[scan : scan + 8])
        if chunk != b"\x00" * 8 and chunk != ROUTING_PAD_ENTRY:
            log(
                f"  ABORT: unexpected bytes at 0x{scan:X} while extending the "
                f"routing table (got {chunk.hex(' ')}); refusing to risk "
                f"corrupting gamemdx.dll. No changes written."
            )
            return False
        scan += 8

    bak = dll_path.with_name(dll_path.name + BACKUP_SUFFIX)
    if not bak.exists():
        shutil.copy2(dll_path, bak)
        log(f"  backed up gamemdx.dll -> {bak.name}")

    touched = False
    for bn, route in entries:
        if len(bn) > 5:
            log(
                f"  WARN: basename '{bn}' is longer than 5 chars; the routing "
                f"table stores names in a fixed 5-byte field and compares all "
                f"5 bytes, so a longer name can't be represented (and would "
                f"risk false-matching a shorter entry). Skipping its DLL entry "
                f"— every difficulty will load the plain {bn}.ssq."
            )
            continue
        if len(route) != 3 or any(n > 5 for n in add_song.decode_routing_nibbles(route)):
            log(
                f"  WARN: refusing to write routing {route.hex(' ')} for '{bn}' — "
                f"nibbles must each be 0..5 and this isn't, so it came from a "
                f"corrupt or unrecognised source table. Skipping its DLL entry."
            )
            continue
        name_bytes = bn.encode("ascii").ljust(5, b"\x00")
        entry = name_bytes + route
        pretty = "".join(str(n) for n in add_song.decode_routing_nibbles(route)[:5])
        if bn in existing:
            slot = existing[bn]
            if bytes(data[slot : slot + 8]) != entry:
                data[slot : slot + 8] = entry
                log(f"  routing table: updated '{bn}' @0x{slot:X} "
                    f"({route.hex(' ')} -> SP slots {pretty})")
                touched = True
            else:
                log(f"  routing table: '{bn}' already correct ({route.hex(' ')})")
        else:
            data[term_fo : term_fo + 8] = entry
            data[term_fo + 8 : term_fo + 16] = b"\x00" * 8
            log(f"  routing table: added '{bn}' @0x{term_fo:X} "
                f"({route.hex(' ')} -> SP slots {pretty})")
            existing[bn] = term_fo
            term_fo += 8
            touched = True

    if touched:
        dll_path.write_bytes(bytes(data))
        log(f"  wrote gamemdx.dll ({len(data):,} bytes)")
    return True


def _backup_once(dst: Path, log) -> None:
    if not dst.exists():
        return
    bak = dst.with_name(dst.name + BACKUP_SUFFIX)
    if not bak.exists():
        shutil.copy2(dst, bak)
        log(f"    backed up existing {dst.name} -> {bak.name}")


SSQ_TICKS_PER_MEASURE = 4096
SSQ_BEATS_PER_MEASURE = 4


def bpm_range_from_ssq(path: Path):
    """Derive (min, max) BPM from an .ssq tempo chunk, or None if it doesn't decode.

    Tempo chunk (type 0x01) holds N tick offsets followed by N cumulative
    millisecond values, so BPM between two entries is just how many beats
    elapsed over how much time.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    off = 0
    while off + 12 <= len(data):
        length = struct.unpack_from("<I", data, off)[0]
        if length == 0 or length > len(data) - off:
            break
        tid, _a, count, _c = struct.unpack_from("<Hhhh", data, off + 4)
        if tid == 1 and count > 1:
            pl = data[off + 12: off + length]
            if len(pl) < count * 8:
                return None
            ticks = struct.unpack_from(f"<{count}i", pl, 0)
            ms = struct.unpack_from(f"<{count}i", pl, count * 4)
            bpms = []
            for i in range(count - 1):
                dt, dm = ticks[i + 1] - ticks[i], ms[i + 1] - ms[i]
                if dt <= 0 or dm <= 0:
                    continue
                beats = dt / SSQ_TICKS_PER_MEASURE * SSQ_BEATS_PER_MEASURE
                bpm = beats * 60000.0 / dm
                if 30 <= bpm <= 1000:
                    bpms.append(bpm)
            if bpms:
                return int(round(min(bpms))), int(round(max(bpms)))
            return None
        off += length
    return None


def synthesize_entry(bn: str, sa: SongAssets, mcode: int) -> str:
    """Build a <music> block for a song the source has charts for but no metadata.

    Everything derivable from the files is filled in; title and artist are left
    as the basename for the user to edit. diffLv is a placeholder 1 on exactly
    the slots whose charts really exist, and limited_ary mirrors it -- that tag
    is not optional, a song without it never unlocks and stays off the wheel.
    """
    ids = set()
    for p in sa.ssq_files.values():
        ids |= add_song.parse_ssq_chart_types(p)
    difflv = [0] * 10
    for i in range(5):
        if add_song.SP_CHART_TYPES[i] in ids:
            difflv[i] = 1
        if add_song.DP_CHART_TYPES[i] in ids:
            difflv[5 + i] = 1
    limited = [0 if v else -1 for v in difflv]

    bpm = None
    for key in (None, 1, 2, 3, 4, 5):
        if key in sa.ssq_files:
            bpm = bpm_range_from_ssq(sa.ssq_files[key])
            if bpm:
                break

    parts = [f'<mcode __type="u32">{mcode}</mcode>',
             f"<basename>{bn}</basename>",
             f"<title>{bn}</title>",
             f"<title_yomi>{bn}</title_yomi>",
             f"<artist>{bn}</artist>"]
    if bpm:
        lo, hi = bpm
        if lo != hi:
            parts.append(f'<bpmmin __type="u16">{lo}</bpmmin>')
        parts.append(f'<bpmmax __type="u16">{hi}</bpmmax>')
    parts.append('<series __type="u8">21</series>')
    parts.append('<limited_ary __type="s32" __count="10">'
                 + " ".join(map(str, limited)) + "</limited_ary>")
    parts.append('<diffLv __type="u8" __count="10">'
                 + " ".join(map(str, difflv)) + "</diffLv>")
    return "<music>" + "".join(parts) + "</music>"


def write_musicdb_template(source_root: Path, songs: dict, install_root: Path, log) -> Optional[Path]:
    """Write a musicdb.xml into the source folder for every song missing metadata.

    This is the step people otherwise have to do by hand: the importer needs a
    <music> record and cannot invent titles, so previously a song with charts
    but no musicdb was simply skipped.
    """
    missing = {bn: sa for bn, sa in songs.items() if not sa.entry_xml}
    if not missing:
        log("  every song in this source already has a musicdb entry — nothing to template.")
        return None

    used = set(existing_target_mcodes(install_root, log))
    for sa in songs.values():
        mc = parse_mcode(sa.entry_xml)
        if mc:
            used.add(mc)

    dst = source_root / "musicdb.xml"
    if dst.exists():
        bak = dst.with_name(dst.name + BACKUP_SUFFIX)
        if not bak.exists():
            shutil.copy2(dst, bak)
            log(f"  backed up existing {dst.name} -> {bak.name}")

    next_mcode = 90000
    blocks = []
    for bn in sorted(missing):
        while next_mcode in used:
            next_mcode += 1
        used.add(next_mcode)
        blocks.append(synthesize_entry(bn, missing[bn], next_mcode))
        log(f"    + {bn}  mcode {next_mcode}")
        next_mcode += 1

    dst.write_text('<?xml version="1.0" encoding="UTF-8"?><mdb>\r\n'
                   + "".join(blocks) + "</mdb>", encoding="utf-8")
    log(f"  wrote {dst} with {len(blocks)} entry template(s).")
    log("  Edit the <title>, <artist> and <diffLv> values, then re-scan this "
        "folder and the songs will import.")
    log("  Note: mcodes are allocated in the 90000 range to avoid colliding "
        "with official ones; they are not official values.")
    return dst


def deploy_song(install_root: Path, basename: str, sa: SongAssets, log) -> Optional[bytes]:
    ssq_dir = install_root / "data" / "mdb_apx" / "ssq"
    ssq_dir.mkdir(parents=True, exist_ok=True)
    for tier, src in sorted(sa.ssq_files.items(), key=lambda kv: (kv[0] is not None, kv[0])):
        name = f"{basename}.ssq" if tier is None else f"{basename}_{tier}.ssq"
        dst = ssq_dir / name
        _backup_once(dst, log)
        shutil.copy2(src, dst)
        log(f"    + data/mdb_apx/ssq/{name} ({dst.stat().st_size:,} B)")

    if sa.xsb and sa.xwb:
        dance_dir = install_root / "data" / "sound" / "win" / "dance"
        dance_dir.mkdir(parents=True, exist_ok=True)
        for src, ext in ((sa.xsb, "xsb"), (sa.xwb, "xwb")):
            dst = dance_dir / f"{basename}.{ext}"
            _backup_once(dst, log)
            shutil.copy2(src, dst)
            log(f"    + data/sound/win/dance/{basename}.{ext} ({dst.stat().st_size:,} B)")
    else:
        log(
            f"    WARN: no audio (.xsb/.xwb pair) found in source for "
            f"'{basename}' — skipped; the song will be silent unless the "
            f"target install already has audio for it."
        )

    tn_dds = None
    if sa.jacket_arc:
        img = thumbmod.read_jacket_as_rgb(str(sa.jacket_arc))
        if img is None:
            log(f"    WARN: couldn't decode jacket art for '{basename}', skipping jacket/thumbnail")
        else:
            dds = convert_jackets.make_dds(img)
            arc_bytes = convert_jackets.make_arc(f"data/jacket/{basename}_jk.dds", dds)
            jk_dir = install_root / "data" / "arc" / "jacket"
            jk_dir.mkdir(parents=True, exist_ok=True)
            jk_dst = jk_dir / f"{basename}_jk.arc"
            _backup_once(jk_dst, log)
            jk_dst.write_bytes(arc_bytes)
            log(f"    + data/arc/jacket/{basename}_jk.arc")
            tn_dds = thumbmod.DDS_TN_HEADER + thumbmod.encode_dxt1(img)
    else:
        log(f"    WARN: no jacket art (_jk.arc) found in source for '{basename}'")

    return tn_dds


SP_SLOT_NAMES = ["BEGINNER", "BASIC", "DIFFICULT", "EXPERT", "CHALLENGE"]


def existing_target_mcodes(install_root: Path, log) -> dict[int, str]:
    p = install_root / "data" / "arc" / "startup.arc"
    if not p.is_file():
        return {}
    try:
        files, _, _, _ = add_song._parse_arc_like(p.read_bytes())
        xml = files.get(add_song.MUSICDB_ARC_PATH)
        if not xml:
            return {}
        out = {}
        for bn, block in parse_music_blocks(xml.decode("utf-8", "replace")).items():
            mc = parse_mcode(block)
            if mc is not None:
                out.setdefault(mc, bn)
        return out
    except Exception as e:
        log(f"  WARN: couldn't read target musicdb for mcode check: {e}")
        return {}


def resolve_routing(bn: str, sa: SongAssets, log) -> tuple[Optional[bytes], list[str]]:
    difflv = parse_difflv(sa.entry_xml)

    if sa.source_route is not None:
        route = sa.source_route
        nibs = add_song.decode_routing_nibbles(route)
        problems = []
        contents = {t: add_song.parse_ssq_chart_types(p) for t, p in sa.ssq_files.items()}
        for i in range(5):
            served = contents.get(None if nibs[i] == 0 else nibs[i])
            target = f"{bn}.ssq" if nibs[i] == 0 else f"{bn}_{nibs[i]}.ssq"
            if difflv[i] and (served is None or add_song.SP_CHART_TYPES[i] not in served):
                problems.append(f"{SP_SLOT_NAMES[i]} routes to {target}, which the "
                                f"source didn't provide or which has no such chart")
            if difflv[5 + i] and (served is None or add_song.DP_CHART_TYPES[i] not in served):
                problems.append(f"{SP_SLOT_NAMES[i]}-DP routes to {target}, which has "
                                f"no doubles chart for it")
        log(f"  routing: {route.hex(' ')} (copied verbatim from the source install's gamemdx.dll)")
        return route, problems

    nibs, problems = add_song.plan_routing(sa.ssq_files, difflv)
    route = add_song.encode_routing_nibbles(nibs)
    if sa.tiers:
        log(f"  routing: {route.hex(' ')} (chosen by reading which chart each "
            f"supplied .ssq actually contains)")
    return route, problems


def _target_thumbnail_ids(install_root: Path) -> set:
    ids = set()
    thumb_dir = install_root / "data" / "arc" / "thumbnail"
    if not thumb_dir.is_dir():
        return ids
    limit = None
    try:
        dll = install_root / "modules" / "gamemdx.dll"
        if dll.is_file():
            limit = add_song.read_thumbnail_loop_limit(dll.read_bytes())
    except Exception:
        limit = None
    for p in thumb_dir.glob("jacket_thumbnails_ja_*.arc"):
        m = re.match(r"jacket_thumbnails_ja_(\d+)\.arc$", p.name)
        if not m:
            continue
        if limit is not None and int(m.group(1)) > limit:
            continue
        try:
            for name in add_song.ARC(p.read_bytes()).filenames:
                base = name.rsplit("/", 1)[-1]
                if base.endswith("_tn.dds"):
                    ids.add(base[: -len("_tn.dds")].lower())
        except Exception:
            continue
    return ids


def diagnose_target(install_root: Path, log) -> dict:
    p = install_root / "data" / "arc" / "startup.arc"
    if not p.is_file():
        return {}
    try:
        files, _, _, _ = add_song._parse_arc_like(p.read_bytes())
        xml = files.get(add_song.MUSICDB_ARC_PATH)
        if not xml:
            return {}
        blocks = parse_music_blocks(xml.decode("utf-8", "replace"))
    except Exception as e:
        log(f"  WARN: couldn't read target musicdb: {e}")
        return {}

    try:
        dll_bytes = (install_root / "modules" / "gamemdx.dll").read_bytes()
    except Exception:
        dll_bytes = b""
    routes = add_song.read_routing_table(dll_bytes) if dll_bytes else {}
    # Not every build stores routing in a readable table; some resolve it by
    # other means that this cannot inspect. "No table" therefore does not mean
    # "no routing", and treating it that way reports working songs as broken.
    # When nothing is readable, accept a difficulty as served by any supplied
    # chart file rather than inventing a failure.
    routing_known = bool(routes)

    ssq_dir = install_root / "data" / "mdb_apx" / "ssq"
    dance_dir = install_root / "data" / "sound" / "win" / "dance"
    jacket_dir = install_root / "data" / "arc" / "jacket"
    thumbs = _target_thumbnail_ids(install_root)

    chart_cache: dict = {}

    def charts_of(name):
        if name not in chart_cache:
            f = ssq_dir / name
            chart_cache[name] = add_song.parse_ssq_chart_types(f) if f.is_file() else None
        return chart_cache[name]

    out = {}
    for bn, block in blocks.items():
        broken = []
        partial = []
        difflv = parse_difflv(block)

        if "<limited_ary" not in block:
            broken.append("no <limited_ary> — this build never unlocks the charts, so the song stays off the wheel")

        route = routes.get(bn)
        nibs = add_song.decode_routing_nibbles(route[1]) if route else [0] * 6
        declared = 0
        unservable = 0
        anywhere = set()
        if not routing_known:
            for cand in [f"{bn}.ssq"] + [f"{bn}_{t}.ssq" for t in range(1, 6)]:
                got = charts_of(cand)
                if got:
                    anywhere |= got

        for i in range(5):
            fname = f"{bn}.ssq" if nibs[i] == 0 else f"{bn}_{nibs[i]}.ssq"
            have = charts_of(fname)
            for want, label in ((add_song.SP_CHART_TYPES[i], add_song.DIFFICULTY_NAMES[i]),
                                (add_song.DP_CHART_TYPES[i], add_song.DIFFICULTY_NAMES[i] + "-DP")):
                slot = i if want == add_song.SP_CHART_TYPES[i] else 5 + i
                if not difflv[slot]:
                    continue
                declared += 1
                if not routing_known:
                    # no readable routing table: the stock hardcoded chain may send
                    # this difficulty to any tier file, so only complain when the
                    # chart exists in none of them
                    if want not in anywhere:
                        unservable += 1
                        partial.append(f"{label} is declared but no {bn} .ssq contains that chart")
                    continue
                if have is None:
                    unservable += 1
                    partial.append(f"{label} needs {fname}, which is missing")
                elif want not in have:
                    unservable += 1
                    partial.append(f"{label} routes to {fname}, which has no such chart")

        if declared == 0:
            broken.append("musicdb declares no playable difficulty")
        elif unservable == declared:
            broken.extend(partial)
            broken.append("every difficulty it declares is missing its chart, so the song is hidden entirely")
            partial = []

        if not (dance_dir / f"{bn}.xsb").is_file() or not (dance_dir / f"{bn}.xwb").is_file():
            partial.append("no .xsb/.xwb audio — the song will be silent")
        if not (jacket_dir / f"{bn}_jk.arc").is_file():
            partial.append("no jacket arc — shows a purple square")
        elif bn not in thumbs:
            partial.append("no thumbnail in a loadable jacket_thumbnails_ja_*.arc — purple tile on the wheel")

        if broken:
            out[bn] = ("broken", broken + partial)
        elif partial:
            out[bn] = ("partial", partial)
    return out


def write_thumbnail_arc(install_root: Path, thumb_entries: list, build_dir: Path, log) -> None:
    log("\nBuilding thumbnail arc...")
    next_n = add_song.next_thumbnail_index(install_root)
    dll = install_root / "modules" / "gamemdx.dll"
    if not dll.is_file():
        log(f"  ABORT: {dll} not found, so the arc index bound cannot be "
            f"checked. Skipping the thumbnail step — the songs are still "
            f"imported and playable.")
        return
    found = add_song.find_thumbnail_loop_limit(dll.read_bytes())
    if found is None:
        log("  ABORT: could not locate the thumbnail enumeration loop in "
            "gamemdx.dll, so there is no way to tell which arc indexes the "
            "game will open. Writing one anyway risks silently invisible "
            "(purple) jackets, so the thumbnail step is skipped — the songs "
            "are still imported and playable.")
        return
    _off, limit = found
    if next_n > limit:
        # A stock install ships enough arcs to fill every slot the loop
        # enumerates, so it is already at the cap before the first import.
        # Widen the loop rather than silently dropping the thumbnails.
        try:
            old, new = add_song.raise_thumbnail_loop_limit(dll)
        except Exception as exc:
            log(f"  ABORT: arc {next_n} is past the enumeration bound 0..{limit} "
                f"and the bound could not be raised ({exc}). Skipping the "
                f"thumbnail step — the songs are still imported and playable.")
            return
        log(f"  gamemdx.dll only enumerated arcs 0..{old}, which is already "
            f"full; raised the bound to 0..{new} (backup: "
            f"gamemdx.dll{BACKUP_SUFFIX})")
        limit = new
    log(f"  writing arc {next_n} (gamemdx enumerates 0..{limit})")
    arc_path = add_song.write_combined_thumbnail_arc(install_root, thumb_entries, build_dir)
    dst = install_root / "data" / "arc" / "thumbnail" / arc_path.name
    shutil.copy2(arc_path, dst)
    log(f"  + data/arc/thumbnail/{arc_path.name} ({len(thumb_entries)} thumbnail(s))")
    added = add_song.update_filepath_info(
        install_root, [f"data/arc/thumbnail/{arc_path.name}"], BACKUP_SUFFIX)
    log(f"  updated prop/filepath.info (+{added} line(s))")


def run_import(install_root: Path, songs: dict[str, SongAssets], selected: list[str], log,
               overwrite_existing: bool = False) -> None:
    build_dir = Path(tempfile.mkdtemp(prefix="ssqimport_build_"))
    entries: list[str] = []
    thumb_entries: list[tuple[str, bytes]] = []
    routing_entries: list[tuple[str, bytes]] = []
    skipped = 0

    existing_bn = existing_target_basenames(install_root, log)
    known_tags = target_tag_vocabulary(install_root, log)
    existing_mcodes = existing_target_mcodes(install_root, log)
    target_routes: dict = {}
    try:
        dllp = install_root / "modules" / "gamemdx.dll"
        if dllp.is_file():
            target_routes = add_song.read_routing_table(dllp.read_bytes())
    except Exception:
        target_routes = {}
    selected = list(dict.fromkeys(selected))

    try:
        for bn in selected:
            sa = songs[bn]
            log(f"[{bn}] importing...")
            if not sa.entry_xml:
                log(f"  SKIP: no musicdb entry found in source for '{bn}'. Click "
                    f"'Write musicdb template' to generate one (title/artist/levels "
                    f"pre-filled for you to edit), then re-scan this folder.")
                skipped += 1
                continue

            if bn in existing_bn and not overwrite_existing:
                log(
                    f"  SKIP: '{bn}' is already in the target musicdb. Importing "
                    f"it would overwrite that song's chart, audio and jacket. "
                    f"Tick 'Overwrite songs already in the target' if that's what "
                    f"you want."
                )
                skipped += 1
                continue

            mcode = parse_mcode(sa.entry_xml)
            if mcode is not None and mcode in existing_mcodes and existing_mcodes[mcode] != bn:
                log(
                    f"  WARN: mcode {mcode} is already used by '{existing_mcodes[mcode]}' "
                    f"in the target. mcode is the scores/unlocks key, so the two songs "
                    f"will share save data. Importing anyway."
                )

            route, problems = resolve_routing(bn, sa, log)
            for prob in problems:
                log(f"  WARN: {prob}.")
            if problems:
                log(
                    f"  ^ the game validates every difficulty the musicdb declares "
                    f"against the actual chart files and hides the entire song from "
                    f"song-select when one is missing. '{bn}' will very likely not "
                    f"appear until either the missing .ssq is supplied or that slot "
                    f"is zeroed in its diffLv."
                )

            tn_dds = deploy_song(install_root, bn, sa, log)
            clean, dropped, added_la = sanitize_entry(
                sa.entry_xml, known_tags, parse_difflv(sa.entry_xml))
            if added_la:
                log(f"  added <limited_ary> (the source's musicdb format omits it; "
                    f"without it this game build never unlocks the charts and the "
                    f"song stays off the wheel)")
            if dropped:
                log(f"  dropped tag(s) this game build doesn't use: {', '.join(sorted(set(dropped)))}")
            entries.append(clean)
            existing_bn.add(bn)
            if mcode is not None:
                existing_mcodes.setdefault(mcode, bn)
            if tn_dds:
                thumb_entries.append((bn, tn_dds))
            if sa.tiers or bn in target_routes:
                if not sa.tiers and bn in target_routes:
                    log(f"  routing: {route.hex(' ')} (clearing the target's stale "
                        f"{target_routes[bn][1].hex(' ')} entry — this import supplies "
                        f"no tier files)")
                routing_entries.append((bn, route))

        if entries:
            log("\nPatching startup.arc (all variants on disk)...")
            n = patch_all_startup_variants(install_root, entries, log, overwrite_existing)
            log(f"  patched {n} startup.arc variant(s)")
            try:
                sync_loose_musicdb(install_root, entries, log, overwrite_existing)
            except Exception as e:
                log(f"  (non-critical) failed to sync loose musicdb.xml: {e}")

        if thumb_entries:
            try:
                write_thumbnail_arc(install_root, thumb_entries, build_dir, log)
            except Exception as e:
                log(f"  WARN: thumbnail step failed ({e}); songs are still imported, "
                    f"they'll just show a purple tile on the wheel.")

        if routing_entries:
            log("\nPatching Special-SSQ routing table in gamemdx.dll...")
            try:
                ok = patch_routing_table_safe(
                    install_root / "modules" / "gamemdx.dll", routing_entries, log)
            except Exception as e:
                ok = False
                log(f"  ERROR: routing patch failed: {e}")
            if not ok:
                log(
                    "  Routing table NOT patched (see above). Tiered-SSQ songs will "
                    "look for a plain .ssq that may not exist, which hides them from "
                    "song-select. Re-run with 'Overwrite songs already in the target' "
                    "ticked once the problem is fixed."
                )

        try:
            state = add_song.read_filepath_info_header(install_root)
            if state and state[0] != state[1]:
                if add_song.repair_filepath_info_header(install_root, BACKUP_SUFFIX):
                    log(f"\n  repaired prop/filepath.info header ({state[0]} -> {state[1]}); "
                        f"a stale count here can hide songs from the wheel.")
        except Exception as e:
            log(f"  WARN: couldn't check prop/filepath.info header: {e}")

        log(f"\nDone. Imported {len(entries)}/{len(selected)} song(s)"
            + (f", skipped {skipped}." if skipped else ".")
            + " Restart the game to see them.")
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


APP_TITLE = "dirty patch by kevinnnn"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("980x700")

        self.install_root = script_dir()
        self.songs: dict[str, SongAssets] = {}
        self.existing_basenames: set[str] = set()
        self.target_problems: dict = {}
        self.checked: dict[str, bool] = {}
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.busy = False
        self._filter_after_id = None

        self._build_widgets()
        self._refresh_target_status()
        self.root.after(100, self._drain_log_queue)

    def _build_widgets(self):
        pad = {"padx": 8, "pady": 4}

        title_frame = ttk.Frame(self.root)
        title_frame.pack(fill="x")
        ttk.Label(title_frame, text=APP_TITLE, font=("Segoe UI", 16, "bold")).pack(pady=(10, 2))
        ttk.Label(title_frame, text="special ssq importer", font=("Segoe UI", 9)).pack(pady=(0, 8))
        ttk.Separator(self.root, orient="horizontal").pack(fill="x")

        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill="x", **pad)
        self.install_ok_label = ttk.Label(status_frame, text="", justify="left")
        self.install_ok_label.pack(fill="x")
        self.gamemdx_ok_label = ttk.Label(status_frame, text="", justify="left")
        self.gamemdx_ok_label.pack(fill="x")
        self.target_health_label = ttk.Label(status_frame, text="", justify="left")
        self.target_health_label.pack(fill="x")

        source_frame = ttk.LabelFrame(self.root, text="Songs to import")
        source_frame.pack(fill="x", **pad)
        ttk.Button(source_frame, text="ddr a3", command=self._pick_a3).pack(side="left", padx=6, pady=6)
        ttk.Button(source_frame, text=".zip/.7z", command=self._pick_archive).pack(side="left", padx=6, pady=6)
        ttk.Button(source_frame, text="raw ssq/xsb etc.", command=self._pick_folder).pack(
            side="left", padx=6, pady=6
        )
        self.source_label = ttk.Label(source_frame, text="(none selected)")
        self.source_label.pack(side="left", padx=10)

        filter_frame = ttk.Frame(self.root)
        filter_frame.pack(fill="x", **pad)
        ttk.Label(filter_frame, text="Find song (basename or title):").pack(side="left", padx=(6, 4))
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", self._on_filter_changed)
        filter_entry = ttk.Entry(filter_frame, textvariable=self.filter_var, width=40)
        filter_entry.pack(side="left", padx=4)
        self.special_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            filter_frame,
            text="Special-SSQ (multi-tier) songs only",
            variable=self.special_only_var,
            command=self._populate_tree,
        ).pack(side="left", padx=10)
        self.broken_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            filter_frame,
            text="Only broken in target",
            variable=self.broken_only_var,
            command=self._populate_tree,
        ).pack(side="left", padx=4)
        ttk.Button(
            filter_frame, text="Report target problems", command=self._report_target_problems
        ).pack(side="left", padx=6)
        self.count_label = ttk.Label(filter_frame, text="")
        self.count_label.pack(side="right", padx=10)

        list_frame = ttk.LabelFrame(self.root, text="Songs found")
        list_frame.pack(fill="both", expand=True, **pad)

        columns = ("import", "basename", "title", "tiers", "audio", "jacket", "status")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="none")
        headers = {
            "import": "Import",
            "basename": "Basename",
            "title": "Title",
            "tiers": "SSQ tiers",
            "audio": "Audio",
            "jacket": "Jacket",
            "status": "Status",
        }
        widths = {"import": 60, "basename": 80, "title": 260, "tiers": 90, "audio": 60, "jacket": 60, "status": 190}
        for col in columns:
            self.tree.heading(col, text=headers[col])
            self.tree.column(col, width=widths[col], anchor="w")
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)

        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", **pad)
        ttk.Button(btn_frame, text="Select all visible", command=self._select_all_visible).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Select all Special-SSQ songs", command=self._select_all_special_ssq).pack(
            side="left", padx=4
        )
        ttk.Button(btn_frame, text="Select none", command=self._select_none).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Write musicdb template",
                   command=self._write_template).pack(side="left", padx=10)
        self.overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            btn_frame,
            text="Overwrite songs already in the target",
            variable=self.overwrite_var,
        ).pack(side="left", padx=14)
        self.import_btn = ttk.Button(btn_frame, text="Import selected", command=self._start_import)
        self.import_btn.pack(side="right", padx=4)

        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def log(self, msg: str):
        self.log_queue.put(str(msg))

    def _refresh_target_status(self):
        info = validate_target(self.install_root)
        if info["looks_like_install"]:
            self.install_ok_label.configure(text="✅ Valid DDR install folder", foreground="#1a9c1a")
        else:
            self.install_ok_label.configure(
                text="❌ Valid DDR install folder — move this to your base game folder (i.e. next to spice)",
                foreground="#cc2222",
            )
        if info["gamemdx_marker_ok"]:
            self.gamemdx_ok_label.configure(text="✅ Supported gamemdx version", foreground="#1a9c1a")
        else:
            self.gamemdx_ok_label.configure(text="❌ Unsupported gamemdx version", foreground="#cc2222")
        self.existing_basenames = existing_target_basenames(self.install_root, self.log)
        if not info["looks_like_install"]:
            self.target_problems = {}
            self.target_health_label.configure(text="", foreground="#777777")
            return
        try:
            self.target_problems = diagnose_target(self.install_root, self.log)
        except Exception as e:
            self.target_problems = {}
            self.log(f"  WARN: couldn't scan the target for broken songs: {e}")
        n_broken = sum(1 for sev, _ in self.target_problems.values() if sev == "broken")
        n_partial = len(self.target_problems) - n_broken
        if n_broken:
            self.target_health_label.configure(
                text=f"⚠ {n_broken} song(s) in this install won't appear in game"
                     + (f", {n_partial} more have minor issues" if n_partial else "")
                     + " — tick 'Only broken in target' to find them",
                foreground="#cc6600",
            )
        elif n_partial:
            self.target_health_label.configure(
                text=f"{n_partial} song(s) have minor issues (audio/jacket/thumbnail or an unused difficulty)",
                foreground="#777777",
            )
        else:
            self.target_health_label.configure(text="✅ No broken songs detected in this install",
                                               foreground="#1a9c1a")

    def _pick_folder(self):
        d = filedialog.askdirectory(title="Select a folder of songs to import")
        if d:
            self._load_source(Path(d))

    def _pick_archive(self):
        f = filedialog.askopenfilename(
            title="Select a .zip or .7z of songs to import",
            filetypes=[("Archives", "*.zip *.7z"), ("All files", "*.*")],
        )
        if f:
            self._load_source(Path(f))

    def _pick_a3(self):
        for cand in A3_CONTENTS_CANDIDATES:
            if cand.is_dir():
                self._load_source(cand)
                return
        messagebox.showinfo(
            "ddr a3 not found",
            "Couldn't find your ddr a3 installation; point me to your ddr a3 /contents/ folder",
        )
        d = filedialog.askdirectory(
            title="Point me to your ddr a3 /contents/ folder",
            initialdir=r"C:/Program Files (x86)",
        )
        if d:
            self._load_source(Path(d))

    def _load_source(self, path: Path):
        self.source_label.configure(text=f"Scanning {path}...")
        self.root.update_idletasks()
        try:
            root = path
            if path.is_file():
                root = extract_archive(path, self.log)
            songs = scan_source(root, self.log)
        except Exception as e:
            messagebox.showerror("Scan failed", str(e))
            self.source_label.configure(text="(none selected)")
            return
        self.songs = songs
        self.source_root = root
        self.checked = {bn: False for bn in songs}
        self.filter_var.set("")
        n_missing = sum(1 for sa in songs.values() if not sa.entry_xml)
        extra = f", {n_missing} without musicdb data" if n_missing else ""
        self.source_label.configure(text=f"{path}  ({len(songs)} song(s) found{extra})")
        if n_missing:
            self.log(f"\n{n_missing} song(s) here have charts but no musicdb entry, so they "
                     f"cannot be imported yet. Click 'Write musicdb template' to generate "
                     f"one with the levels and BPM already filled in from the charts.")
        self._populate_tree()

    def _write_template(self):
        if not self.songs:
            messagebox.showinfo("No source loaded", "Pick a source folder or archive first.")
            return
        root = getattr(self, "source_root", None)
        if root is None or not Path(root).is_dir():
            messagebox.showinfo("No source folder",
                                "The template is written next to the source files, so this "
                                "needs a folder source rather than an archive.")
            return
        try:
            dst = write_musicdb_template(Path(root), self.songs, self.install_root, self.log)
        except Exception as e:
            self.log(f"  ERROR writing template: {e}\n{traceback.format_exc()}")
            messagebox.showerror("Template failed", str(e))
            return
        if dst:
            messagebox.showinfo(
                "musicdb template written",
                f"Wrote:\n{dst}\n\nEdit the <title>, <artist> and <diffLv> values, then "
                f"re-scan this folder. The songs will then import normally.")
            self._load_source(Path(root))

    def _visible_basenames(self) -> list[str]:
        needle = self.filter_var.get().strip().lower()
        special_only = self.special_only_var.get()
        broken_only = self.broken_only_var.get()
        out = []
        for bn in sorted(self.songs):
            sa = self.songs[bn]
            if special_only and not sa.tiers:
                continue
            if broken_only and bn not in self.target_problems:
                continue
            if needle:
                title_m = re.search(r"<title>([^<]*)</title>", sa.entry_xml or "")
                title = (title_m.group(1) if title_m else "").lower()
                if needle not in bn.lower() and needle not in title:
                    continue
            out.append(bn)
        return out

    def _status_for(self, bn: str) -> str:
        entry = self.target_problems.get(bn)
        if entry:
            severity, problems = entry
            if severity == "broken":
                return f"BROKEN — re-import ({len(problems)})"
            return f"partial — {len(problems)} issue(s)"
        if bn in self.existing_basenames:
            return "already in target"
        return "new"

    def _report_target_problems(self):
        problems = self.target_problems
        if not problems:
            self.log("\nTarget scan: no broken songs found in "
                     f"{self.install_root}.")
            return
        broken = {b: p for b, (s, p) in problems.items() if s == "broken"}
        partial = {b: p for b, (s, p) in problems.items() if s == "partial"}
        self.log(f"\n=== Problems in {self.install_root} ===")
        self.log(f"{len(broken)} song(s) will NOT appear in game:")
        for bn in sorted(broken):
            here = " (in the loaded source — tick it and re-import with Overwrite)" \
                if bn in self.songs else " (not in the loaded source; point the tool at a source that has it)"
            self.log(f"  [{bn}]{here}")
            for pr in broken[bn]:
                self.log(f"      - {pr}")
        if partial:
            self.log(f"\n{len(partial)} song(s) appear but have issues:")
            for bn in sorted(partial):
                self.log(f"  [{bn}] " + "; ".join(partial[bn]))
        self.log("=== end of report ===")

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        visible = self._visible_basenames()
        for bn in visible:
            sa = self.songs[bn]
            title_m = re.search(r"<title>([^<]*)</title>", sa.entry_xml or "")
            title = title_m.group(1) if title_m else "(no musicdb entry found — will be skipped)"
            tiers = ",".join(str(t) for t in sa.tiers) if sa.tiers else ("bare" if sa.has_bare else "-")
            audio = "yes" if (sa.xsb and sa.xwb) else "no"
            jacket = "yes" if sa.jacket_arc else "no"
            status = self._status_for(bn)
            mark = "[x]" if self.checked.get(bn) else "[ ]"
            self.tree.insert("", "end", iid=bn, values=(mark, bn, title, tiers, audio, jacket, status))
        n_checked = sum(1 for v in self.checked.values() if v)
        self.count_label.configure(text=f"{len(visible)} shown / {len(self.songs)} total — {n_checked} selected")

    def _on_filter_changed(self, *_args):
        if self._filter_after_id is not None:
            self.root.after_cancel(self._filter_after_id)
        self._filter_after_id = self.root.after(200, self._populate_tree)

    def _on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row or col != "#1":
            return
        self.checked[row] = not self.checked.get(row, False)
        vals = list(self.tree.item(row, "values"))
        vals[0] = "[x]" if self.checked[row] else "[ ]"
        self.tree.item(row, values=vals)
        n_checked = sum(1 for v in self.checked.values() if v)
        self.count_label.configure(
            text=f"{len(self.tree.get_children())} shown / {len(self.songs)} total — {n_checked} selected"
        )

    def _select_all_visible(self):
        for bn in self._visible_basenames():
            self.checked[bn] = True
        self._populate_tree()

    def _select_all_special_ssq(self):
        for bn, sa in self.songs.items():
            if sa.tiers:
                self.checked[bn] = True
        self._populate_tree()

    def _select_none(self):
        for bn in self.songs:
            self.checked[bn] = False
        self._populate_tree()

    def _start_import(self):
        if self.busy:
            return
        selected = [bn for bn, v in self.checked.items() if v]
        if not selected:
            messagebox.showinfo("Nothing selected", "Check at least one song to import.")
            return
        info = validate_target(self.install_root)
        if not info["looks_like_install"]:
            if not messagebox.askyesno(
                "Unrecognized folder",
                f"{self.install_root} doesn't look like a valid DDR install.\nImport anyway?",
            ):
                return
        if not messagebox.askyesno(
            "Confirm import",
            f"This will modify files in:\n  {self.install_root}\n\n"
            f"Importing {len(selected)} song(s):\n  " + ", ".join(selected) + "\n\n"
            "Backups of anything overwritten will be made as "
            f"'*{BACKUP_SUFFIX}' the first time. Continue?",
        ):
            return

        self.busy = True
        self.import_btn.configure(state="disabled")
        t = threading.Thread(
            target=self._run_import_thread,
            args=(selected, self.overwrite_var.get()),
            daemon=True,
        )
        t.start()

    def _run_import_thread(self, selected: list[str], overwrite: bool = False):
        try:
            run_import(self.install_root, self.songs, selected, self.log, overwrite)
        except Exception as e:
            self.log(f"\nFATAL ERROR: {e}\n{traceback.format_exc()}")
            self.log_queue.put("__ERROR__:" + str(e))
        finally:
            self.log_queue.put("__DONE__")

    def _drain_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if msg == "__DONE__":
                    self.busy = False
                    self.import_btn.configure(state="normal")
                    self._refresh_target_status()
                    self._populate_tree()
                    continue
                if msg.startswith("__ERROR__:"):
                    messagebox.showerror("Import failed", msg[len("__ERROR__:") :])
                    continue
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)


def report_fatal(summary: str, detail: str) -> None:
    saved = None
    try:
        path = script_dir() / "dirty_patch_error.log"
        path.write_text(f"{summary}\n\n{detail}", encoding="utf-8")
        saved = path
    except Exception:
        saved = None
    body = summary
    if saved:
        body += f"\n\nA full report was saved to:\n{saved}"
    try:
        messagebox.showerror(f"{APP_TITLE} — startup error", body)
    except Exception:
        print(summary)
        print(detail)


def main():
    try:
        root = tk.Tk()
    except Exception:
        print("FATAL: could not start the GUI (Tkinter failed to initialize).")
        traceback.print_exc()
        try:
            input("\nPress Enter to close...")
        except Exception:
            pass
        sys.exit(1)

    try:
        load_dependencies()
        App(root)
    except Exception as e:
        root.withdraw()
        report_fatal(str(e), traceback.format_exc())
        sys.exit(1)

    root.mainloop()


if __name__ == "__main__":
    main()
