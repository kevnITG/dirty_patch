from __future__ import annotations

import re
import shutil
import struct
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image


class LzException(Exception):
    pass
class Lz77Decompress:
    RING_LENGTH = 0x1000
    FLAG_COPY = 1
    FLAG_BACKREF = 0

    def __init__(self, data: bytes, backref: Optional[int] = None) -> None:
        self.eof = False
        self.data = data
        self.read_pos = 0
        self.left = len(self.data)
        self.flags = 1
        self.write_pos = 0
        self.pending_copy_amount = 0
        self.pending_copy_pos = 0
        self.pending_copy_max = 0
        self.ringlength = backref or self.RING_LENGTH
        self.ring = b"\x00" * self.ringlength

    def _ring_read(self, copy_pos, copy_len):
        while copy_len > 0:
            if copy_pos + copy_len > self.ringlength:
                amount = self.ringlength - copy_pos
            else:
                amount = copy_len
            ret = self.ring[copy_pos:(copy_pos + amount)]
            self._ring_write(ret)
            yield ret
            copy_pos = (copy_pos + amount) % self.ringlength
            copy_len -= amount

    def _ring_write(self, bytedata):
        while True:
            amount = len(bytedata)
            if amount == 0:
                return
            if amount > (self.ringlength - self.write_pos):
                amount = self.ringlength - self.write_pos
            self.ring = self.ring[: self.write_pos] + bytedata[:amount] + self.ring[(self.write_pos + amount):]
            bytedata = bytedata[amount:]
            self.write_pos = (self.write_pos + amount) % self.ringlength

    def decompress_bytes(self):
        while not self.eof:
            if self.pending_copy_amount > 0:
                amount = min(self.pending_copy_amount, self.pending_copy_max)
                yield from self._ring_read(self.pending_copy_pos, amount)
                self.pending_copy_amount -= amount
                self.pending_copy_max = amount
            else:
                if self.flags == 1:
                    if self.left == 0:
                        return
                    else:
                        self.flags = 0x100 | self.data[self.read_pos]
                        self.read_pos += 1
                        self.left -= 1
                flag = self.flags & 1
                self.flags >>= 1
                if flag == self.FLAG_COPY:
                    amount = 1
                    while self.flags != 1 and (self.flags & 1) == self.FLAG_COPY:
                        self.flags >>= 1
                        amount += 1
                    b = self.data[self.read_pos:(self.read_pos + amount)]
                    self._ring_write(b)
                    yield b
                    self.read_pos += amount
                    self.left -= amount
                elif flag == self.FLAG_BACKREF:
                    yield from self._read_backref()
                else:
                    raise Exception("Logic error!")

    def _read_backref(self):
        if self.left == 0:
            self.eof = True
            return
        if self.left == 1:
            raise LzException("Unexpected EOF mid-backref")
        hi = self.data[self.read_pos]
        lo = self.data[self.read_pos + 1]
        self.read_pos += 2
        self.left -= 2
        copy_len = lo & 0xF
        copy_pos = (hi << 4) | (lo >> 4)
        if copy_pos > 0:
            copy_len += 3
            if copy_len > copy_pos:
                self.pending_copy_amount = copy_len - copy_pos
                self.pending_copy_pos = self.write_pos
                self.pending_copy_max = copy_pos
                copy_len = copy_pos
            copy_pos = self.write_pos - copy_pos
            while copy_pos < 0:
                copy_pos += self.ringlength
            copy_pos = copy_pos % self.ringlength
            yield from self._ring_read(copy_pos, copy_len)
        else:
            self.eof = True
            return


class Lz77Compress:
    RING_LENGTH = 0x1000
    LOOSE_COMPRESS_THRESHOLD = 1024 * 512
    FLAG_COPY = 1
    FLAG_BACKREF = 0

    def __init__(self, data: bytes, backref: Optional[int] = None) -> None:
        self.data = data
        self.read_pos = 0
        self.left = len(self.data)
        self.eof = False
        self.bytes_written = 0
        self.ringlength = backref or self.RING_LENGTH
        self.locations = defaultdict(set)
        self.starts = defaultdict(set)
        self.last_start = (0, 0, 0)
        if len(data) > self.LOOSE_COMPRESS_THRESHOLD:
            self._ring_write = self._ring_write_starts_only
        else:
            self._ring_write = self._ring_write_both

    def _ring_write_starts_only(self, bytedata):
        for byte in bytedata:
            self.last_start = (self.last_start[1], self.last_start[2], byte)
            if self.bytes_written >= 2:
                self.starts[bytes(self.last_start)].add(self.bytes_written - 2)
            self.bytes_written += 1

    def _ring_write_both(self, bytedata):
        for byte in bytedata:
            self.last_start = (self.last_start[1], self.last_start[2], byte)
            if self.bytes_written >= 2:
                self.starts[bytes(self.last_start)].add(self.bytes_written - 2)
            self.locations[byte].add(self.bytes_written)
            self.bytes_written += 1

    def compress_bytes(self):
        while not self.eof:
            if self.left == 0:
                self.eof = True
                yield b"\x00\x00\x00"
            else:
                flags = 0x0
                flagpos = -1
                data = [b""] * 8
                for _ in range(8):
                    flagpos += 1
                    if self.left == 0:
                        flags |= self.FLAG_BACKREF << flagpos
                        data[flagpos] = b"\x00\x00"
                        self.eof = True
                        break
                    elif self.left < 3 or self.bytes_written < 3:
                        flags |= self.FLAG_COPY << flagpos
                        chunk = self.data[self.read_pos:(self.read_pos + 1)]
                        data[flagpos] = chunk
                        self._ring_write(chunk)
                        self.read_pos += 1
                        self.left -= 1
                        continue
                    backref_amount = min(self.left, 18)
                    earliest = max(0, self.bytes_written - (self.ringlength - 1))
                    index = self.data[self.read_pos:(self.read_pos + 3)]
                    updated_backref_locations = set(
                        absolute_pos for absolute_pos in self.starts[index] if absolute_pos >= earliest
                    )
                    self.starts[index] = updated_backref_locations
                    possible_backref_locations = list(updated_backref_locations)
                    if not possible_backref_locations:
                        flags |= self.FLAG_COPY << flagpos
                        chunk = self.data[self.read_pos:(self.read_pos + 1)]
                        data[flagpos] = chunk
                        self._ring_write(chunk)
                        self.read_pos += 1
                        self.left -= 1
                        continue
                    start_write_size = self.bytes_written
                    self._ring_write(index)
                    copy_amount = 3
                    while copy_amount < backref_amount:
                        index = self.data[(self.read_pos + copy_amount):(self.read_pos + copy_amount + 3)]
                        locations = self.starts[index]
                        new_backref_locations = [
                            absolute_pos for absolute_pos in possible_backref_locations
                            if absolute_pos + copy_amount in locations
                        ]
                        if new_backref_locations:
                            self._ring_write(index)
                            copy_amount += 3
                            possible_backref_locations = new_backref_locations
                        else:
                            while copy_amount < backref_amount:
                                locations = self.locations[self.data[self.read_pos + copy_amount]]
                                new_backref_locations = [
                                    absolute_pos for absolute_pos in possible_backref_locations
                                    if absolute_pos + copy_amount in locations
                                ]
                                if not new_backref_locations:
                                    break
                                self._ring_write(
                                    self.data[(self.read_pos + copy_amount):(self.read_pos + copy_amount + 1)]
                                )
                                copy_amount += 1
                                possible_backref_locations = new_backref_locations
                            break
                    absolute_pos = possible_backref_locations[0]
                    backref_pos = start_write_size - absolute_pos
                    lo = (copy_amount - 3) & 0xF | ((backref_pos & 0xF) << 4)
                    hi = (backref_pos >> 4) & 0xFF
                    flags |= self.FLAG_BACKREF << flagpos
                    data[flagpos] = bytes([hi, lo])
                    self.read_pos += copy_amount
                    self.left -= copy_amount
                yield bytes([flags]) + b"".join(data)


class Lz77:
    def __init__(self, backref: Optional[int] = None) -> None:
        self.backref = backref

    def decompress(self, data: bytes) -> bytes:
        lz = Lz77Decompress(data, backref=self.backref)
        return b"".join(lz.decompress_bytes())

    def compress(self, data: bytes) -> bytes:
        lz = Lz77Compress(data, backref=self.backref)
        return b"".join(lz.compress_bytes())


class ARC:
    def __init__(self, data: bytes) -> None:
        self.__files: dict = {}
        self.__data = data
        self.__parse_file(data)

    def __parse_file(self, data: bytes) -> None:
        if data[0:4] != bytes([0x20, 0x11, 0x75, 0x19]):
            raise Exception("Unknown file format!")
        (_, numfiles, _) = struct.unpack("<III", data[4:16])
        for fno in range(numfiles):
            start = 16 + (16 * fno)
            end = start + 16
            (nameoffset, fileoffset, uncompressedsize, compressedsize) = struct.unpack("<IIII", data[start:end])
            name = ""
            while data[nameoffset] != 0:
                name = name + data[nameoffset:(nameoffset + 1)].decode("ascii")
                nameoffset += 1
            self.__files[name] = (fileoffset, uncompressedsize, compressedsize)

    @property
    def filenames(self) -> list:
        return [f for f in self.__files]

    def read_file(self, filename: str) -> bytes:
        (fileoffset, uncompressedsize, compressedsize) = self.__files[filename]
        if compressedsize == uncompressedsize:
            return self.__data[fileoffset:(fileoffset + compressedsize)]
        return Lz77().decompress(self.__data[fileoffset:(fileoffset + compressedsize)])


class DXTBuffer:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.block_countx = self.width // 4
        self.block_county = self.height // 4
        self.decompressed_buffer = [None] * ((width * height) * 2)

    def unpackRGB(self, packed: int):
        R = (packed >> 11) & 0x1F
        G = (packed >> 5) & 0x3F
        B = (packed) & 0x1F
        R = (R << 3) | (R >> 2)
        G = (G << 2) | (G >> 4)
        B = (B << 3) | (B >> 2)
        return (R, G, B)

    def swapbytes(self, data: bytes, swap: bool) -> bytes:
        if swap:
            return b"".join([data[(x + 1):(x + 2)] + data[x:(x + 1)] for x in range(0, len(data), 2)])
        return data

    def DXT1Decompress(self, filedata: bytes, swap: bool = False) -> bytes:
        import io
        file = io.BytesIO(filedata)
        for row in range(self.block_county):
            for col in range(self.block_countx):
                c0, c1, ctable = struct.unpack("<HHI", self.swapbytes(file.read(8), swap))
                for j in range(4):
                    for i in range(4):
                        self.getColors(col * 4, row * 4, i, j, ctable, c0, c1, 255)
        return b"".join([x for x in self.decompressed_buffer if x is not None])

    def getColors(self, x, y, i, j, ctable, c0, c1, alpha):
        code = (ctable >> (2 * ((4 * j) + i))) & 0x03
        pixel_color = None
        r0, g0, b0 = self.unpackRGB(c0)
        r1, g1, b1 = self.unpackRGB(c1)
        if code == 0:
            pixel_color = (r0, g0, b0, alpha)
        if code == 1:
            pixel_color = (r1, g1, b1, alpha)
        if code == 2:
            if c0 > c1:
                pixel_color = ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, alpha)
            else:
                pixel_color = ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, alpha)
        if code == 3:
            if c0 > c1:
                pixel_color = ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, alpha)
            else:
                pixel_color = (0, 0, 0, alpha)
        if pixel_color is not None and (x + i) < self.width and (y + j) < self.height:
            self.decompressed_buffer[(y + j) * self.width + (x + i)] = struct.pack("<BBBB", *pixel_color)


MAGIC_PMAR = b"PMAR\x00\x00\x00\x00"
MAGIC_ARC = struct.pack("<II", 0x19751120, 1)


def _parse_arc_like(src: bytes):
    if src[:8] == MAGIC_PMAR:
        pass
    elif src[:8] == MAGIC_ARC:
        pass
    else:
        raise RuntimeError(f"unknown startup.arc magic: {src[:8].hex()}")
    numfiles = struct.unpack("<I", src[8:12])[0]
    flag4 = src[12:16]
    lz = Lz77()
    files, order, raw_set = {}, [], set()
    for fno in range(numfiles):
        off = 16 + 16 * fno
        nameoff, fileoff, ucomp, comp = struct.unpack("<IIII", src[off:off + 16])
        end = src.find(b"\x00", nameoff)
        name = src[nameoff:end].decode("ascii")
        order.append(name)
        raw = (ucomp == comp)
        blob = src[fileoff:fileoff + comp]
        if raw:
            raw_set.add(name)
            files[name] = blob
        else:
            files[name] = lz.decompress(blob)
    return files, order, raw_set, flag4


def _pack_arc_like(files: dict, order: list, raw_set: set, flag4: bytes, magic_hdr: bytes) -> bytes:
    lz = Lz77()
    blobs = {}
    for name in order:
        data = files[name]
        if name in raw_set:
            blobs[name] = (data, len(data))
        else:
            blobs[name] = (lz.compress(data), len(data))
    N = len(order)
    header_size = 16 + 16 * N
    names_blob = bytearray()
    name_offs = {}
    for name in order:
        name_offs[name] = header_size + len(names_blob)
        names_blob += name.encode("ascii") + b"\x00"
    data_start = header_size + len(names_blob)

    entries = []
    data_blob = bytearray()
    cur = data_start
    for name in order:
        comp_bytes, ucomp_size = blobs[name]
        entries.append((name_offs[name], cur, ucomp_size, len(comp_bytes)))
        data_blob += comp_bytes
        cur += len(comp_bytes)

    out = bytearray()
    out += magic_hdr
    out += struct.pack("<I", N)
    out += flag4
    for (no, fo, uc, cs) in entries:
        out += struct.pack("<IIII", no, fo, uc, cs)
    out += names_blob
    out += data_blob
    return bytes(out)


def next_thumbnail_index(install_root: Path) -> int:
    thumb_dir = install_root / "data/arc/thumbnail"
    existing = []
    if thumb_dir.is_dir():
        for p in thumb_dir.glob("jacket_thumbnails_ja_*.arc"):
            m = re.match(r"jacket_thumbnails_ja_(\d+)\.arc$", p.name)
            if m:
                existing.append(int(m.group(1)))
    return (max(existing) + 1) if existing else 0


def write_combined_thumbnail_arc(install_root: Path, entries: list, out_dir: Path) -> Path:
    next_n = next_thumbnail_index(install_root)
    arc_name = f"jacket_thumbnails_ja_{next_n}.arc"
    arc_bytes = make_thumbnail_arc(entries)
    out = out_dir / arc_name
    out.write_bytes(arc_bytes)
    return out


def update_filepath_info(install_root: Path, new_lines: list,
                         backup_suffix: str = ".bak_preSpecialSSQImporter") -> int:

    fp = install_root / "prop" / "filepath.info"
    bak = fp.with_name(fp.name + backup_suffix)
    if not bak.exists() and fp.exists():
        shutil.copy(fp, bak)
    text = fp.read_text(encoding="utf-8") if fp.exists() else ""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("#downcase_path"):
        raise RuntimeError(
            f"unexpected filepath.info format: first line is not `#downcase_path N` ({lines[:1]})")
    content = set(lines[1:])
    added = 0
    for line in new_lines:
        if line not in content:
            lines.append(line)
            content.add(line)
            added += 1
    lines[0] = f"#downcase_path {len(lines) - 1}"
    fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return added


def read_filepath_info_header(install_root: Path):
    fp = install_root / "prop" / "filepath.info"
    if not fp.is_file():
        return None
    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or not lines[0].startswith("#downcase_path"):
        return None
    try:
        declared = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return None
    return declared, len(lines) - 1


def repair_filepath_info_header(install_root: Path,
                                backup_suffix: str = ".bak_preSpecialSSQImporter") -> bool:
    state = read_filepath_info_header(install_root)
    if state is None:
        return False
    declared, actual = state
    if declared == actual:
        return False
    fp = install_root / "prop" / "filepath.info"
    bak = fp.with_name(fp.name + backup_suffix)
    if not bak.exists():
        shutil.copy(fp, bak)
    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    lines[0] = f"#downcase_path {actual}"
    fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


SSQ_ROUTING_TABLE_OFFSET = 0x19DD80
_SSQ_ROUTING_MARKER = b"acef\x00\x12\x34\x50"
ROUTING_ENTRY_SIZE = 8
ROUTING_NAME_LEN = 5

THUMBNAIL_PATH_FORMAT = b"jacket_thumbnails_%s_%d.arc"
THUMBNAIL_LIMIT_HEADROOM = 99

MUSICDB_WARN_BYTES = 1024 * 1024
MUSICDB_MAX_BYTES = 4 * 1024 * 1024
MUSICDB_ARC_PATH = "data/gamedata/musicdb.xml"


def encode_routing_nibbles(nibbles: list) -> bytes:
    n = list(nibbles) + [0] * (6 - len(nibbles))
    for v in n:
        if not 0 <= v <= 5:
            raise ValueError(f"routing nibble out of range 0..5: {v}")
    return bytes([(n[0] << 4) | n[1], (n[2] << 4) | n[3], (n[4] << 4) | n[5]])


def decode_routing_nibbles(route: bytes) -> list:
    return [
        route[0] >> 4, route[0] & 0xF,
        route[1] >> 4, route[1] & 0xF,
        route[2] >> 4, route[2] & 0xF,
    ]


def read_routing_table(data: bytes) -> dict:
    off = SSQ_ROUTING_TABLE_OFFSET
    if data[off:off + ROUTING_ENTRY_SIZE] != _SSQ_ROUTING_MARKER:
        return {}
    out = {}
    while True:
        entry = data[off:off + ROUTING_ENTRY_SIZE]
        if len(entry) < ROUTING_ENTRY_SIZE or entry == b"\x00" * ROUTING_ENTRY_SIZE:
            break
        name = entry[:ROUTING_NAME_LEN].rstrip(b"\x00").decode("ascii", "replace")
        out[name] = (off, bytes(entry[ROUTING_NAME_LEN:]))
        off += ROUTING_ENTRY_SIZE
    return out


SP_CHART_TYPES = {0: 0x0414, 1: 0x0114, 2: 0x0214, 3: 0x0314, 4: 0x0614}
DP_CHART_TYPES = {0: 0x0418, 1: 0x0118, 2: 0x0218, 3: 0x0318, 4: 0x0618}

DIFFICULTY_NAMES = ["BEGINNER", "BASIC", "DIFFICULT", "EXPERT", "CHALLENGE"]


def parse_ssq_chart_types(path) -> set:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return set()
    out = set()
    off = 0
    while off + 12 <= len(data):
        length = struct.unpack_from("<I", data, off)[0]
        if length == 0 or length > len(data) - off:
            break
        if struct.unpack_from("<H", data, off + 4)[0] == 3:
            out.add(struct.unpack_from("<H", data, off + 6)[0])
        off += length
    return out


def plan_routing(ssq_files: dict, difflv: list) -> tuple:
    contents = {tier: parse_ssq_chart_types(p) for tier, p in ssq_files.items()}
    bare = contents.get(None)
    tiers = {t: c for t, c in contents.items() if t is not None}

    nibbles = [0] * 6
    for i in range(5):
        want = SP_CHART_TYPES[i]
        want_dp = DP_CHART_TYPES[i] if len(difflv) > 5 + i and difflv[5 + i] else None
        if bare is not None and want in bare and (want_dp is None or want_dp in bare):
            nibbles[i] = 0
            continue
        candidates = [t for t in sorted(tiers) if want in tiers[t]]
        if want_dp is not None:
            both = [t for t in candidates if want_dp in tiers[t]]
            if both:
                candidates = both
        if bare is not None and want in bare and not candidates:
            nibbles[i] = 0
        elif (i + 1) in candidates:
            nibbles[i] = i + 1
        elif candidates:
            nibbles[i] = candidates[0]
        else:
            nibbles[i] = 0

    problems = []
    for i in range(5):
        served = bare if nibbles[i] == 0 else tiers.get(nibbles[i])
        target = "the plain .ssq" if nibbles[i] == 0 else f"_{nibbles[i]}.ssq"
        if difflv[i]:
            if served is None or SP_CHART_TYPES[i] not in served:
                problems.append(f"{DIFFICULTY_NAMES[i]} is declared in musicdb "
                                f"but no supplied .ssq contains that chart")
        if len(difflv) > 5 + i and difflv[5 + i]:
            if served is None or DP_CHART_TYPES[i] not in served:
                problems.append(f"{DIFFICULTY_NAMES[i]}-DP is declared in musicdb "
                                f"but {target} has no doubles chart for it")
    return nibbles, problems


def _pe_sections(data: bytes):
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    optsz = struct.unpack_from("<H", data, pe + 20)[0]
    out = []
    tbl = pe + 24 + optsz
    for i in range(nsec):
        s = tbl + 40 * i
        out.append((struct.unpack_from("<I", data, s + 12)[0],
                    struct.unpack_from("<I", data, s + 16)[0],
                    struct.unpack_from("<I", data, s + 20)[0],
                    data[s:s + 8].rstrip(b"\0").decode("ascii", "replace")))
    return out


def find_thumbnail_loop_limit(data: bytes):
    """Locate the `cmp rsi, N` that bounds the thumbnail-arc enumeration loop.

    The game builds `data/arc/thumbnail/jacket_thumbnails_%s_%d.arc` inside a
    loop `for rsi in 0..N`, so N is the highest arc index it will ever open.
    A fixed file offset only holds for one build, so find it structurally:
    the format string -> the rip-relative lea that loads it -> the loop's
    `inc rsi / cmp rsi, imm8 / jbe` tail.

    Returns (file_offset_of_imm, limit) or None.
    """
    try:
        secs = _pe_sections(data)
    except Exception:
        return None

    def f2r(f):
        for va, sz, rp, _ in secs:
            if rp <= f < rp + sz:
                return va + (f - rp)
        return None

    hit = data.find(THUMBNAIL_PATH_FORMAT)
    if hit < 0:
        return None
    start = hit
    while start > 0 and data[start - 1] not in (0, 0xFF):
        start -= 1
    srva = f2r(start)
    if srva is None:
        return None

    text = next((s for s in secs if s[3] == ".text"), None)
    if text is None:
        return None
    _tva, tsz, tp, _ = text

    for i in range(tp, tp + tsz - 7):
        # lea r64, [rip+disp32] -- any REX, so `lea r8` (4C) counts too
        if not (0x48 <= data[i] <= 0x4F and data[i + 1] == 0x8D
                and (data[i + 2] & 0xC7) == 0x05):
            continue
        disp = struct.unpack_from("<i", data, i + 3)[0]
        if f2r(i) + 7 + disp != srva:
            continue
        # loop tail: inc rsi (48 FF C6); cmp rsi, imm8 (48 83 FE ib); jbe
        for j in range(i, min(i + 0x400, tp + tsz - 10)):
            if data[j:j + 3] != b"\x48\xff\xc6":
                continue
            if data[j + 3:j + 6] != b"\x48\x83\xfe":
                continue
            tail = data[j + 7:j + 9]
            if tail[:1] == b"\x76" or tail == b"\x0f\x86":
                return j + 6, data[j + 6]
    return None


def read_thumbnail_loop_limit(data: bytes):
    found = find_thumbnail_loop_limit(data)
    return found[1] if found is not None else None


def raise_thumbnail_loop_limit(dll_path: Path, new_limit: int = THUMBNAIL_LIMIT_HEADROOM,
                               backup_suffix: str = ".bak_preSpecialSSQImporter"):
    """Widen the thumbnail enumeration loop so added arcs are actually opened.

    Returns (old_limit, new_limit). Raises RuntimeError if the loop cannot be
    located -- never guesses at an offset.
    """
    if not 1 <= new_limit <= 0x7F:
        raise ValueError(f"limit must be 1..127 (imm8), got {new_limit}")
    data = dll_path.read_bytes()
    found = find_thumbnail_loop_limit(data)
    if found is None:
        raise RuntimeError(
            f"cannot locate the thumbnail enumeration loop in {dll_path.name}; "
            "refusing to patch a guessed offset")
    off, old = found
    if old >= new_limit:
        return old, old
    bak = dll_path.with_name(dll_path.name + backup_suffix)
    if not bak.exists():
        shutil.copy2(dll_path, bak)
    patched = bytearray(data)
    patched[off] = new_limit
    dll_path.write_bytes(bytes(patched))
    check = find_thumbnail_loop_limit(dll_path.read_bytes())
    if check is None or check[1] != new_limit:
        dll_path.write_bytes(data)
        raise RuntimeError("patch did not verify; original restored")
    return old, new_limit


DDS_HEADER = bytes.fromhex(
    "444453207c0000000710000000020000000200000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000200000004000000000000000200000000000ff0000ff0000ff0000"
    "00000000000010000000000000000000000000000000000000"
)


def _tile_to_512(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    sw, sh = img.size
    if sw == sh:
        return img.resize((512, 512), Image.LANCZOS)
    if sw > sh:
        scale = 512 / sw
        tw, th = 512, max(1, round(sh * scale))
        tile = img.resize((tw, th), Image.LANCZOS)
        out = Image.new("RGB", (512, 512))
        y = 0
        while y < 512:
            out.paste(tile, (0, y))
            y += th
    else:
        scale = 512 / sh
        tw, th = max(1, round(sw * scale)), 512
        tile = img.resize((tw, th), Image.LANCZOS)
        out = Image.new("RGB", (512, 512))
        x = 0
        while x < 512:
            out.paste(tile, (x, 0))
            x += tw
    return out


def make_dds(img: Image.Image) -> bytes:
    img = _tile_to_512(img)
    rgb = img.tobytes()
    bgrx = bytearray(512 * 512 * 4)
    for i in range(512 * 512):
        r, g, b = rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2]
        bgrx[i * 4] = b
        bgrx[i * 4 + 1] = g
        bgrx[i * 4 + 2] = r
        bgrx[i * 4 + 3] = 0
    return DDS_HEADER + bytes(bgrx)


def make_arc(dds_path: str, dds_data: bytes) -> bytes:
    name_bytes = dds_path.encode('ascii') + b'\x00'
    name_padded = name_bytes.ljust(32, b'\x00')
    assert len(name_padded) <= 32, f"Filename too long: {dds_path}"
    nameoffset = 32
    fileoffset = 64
    size = len(dds_data)
    header = struct.pack('<IIII', 0x19751120, 1, 1, 2)
    entry = struct.pack('<IIII', nameoffset, fileoffset, size, size)
    return header + entry + name_padded + dds_data


def _build_dds_tn_header() -> bytes:
    out = bytearray(128)
    struct.pack_into('<4s', out, 0, b'DDS ')
    struct.pack_into('<I', out, 4, 124)
    struct.pack_into('<I', out, 8, 0x00081007)
    struct.pack_into('<I', out, 12, 192)
    struct.pack_into('<I', out, 16, 192)
    struct.pack_into('<I', out, 20, 384)
    struct.pack_into('<I', out, 24, 0)
    struct.pack_into('<I', out, 28, 1)
    struct.pack_into('<I', out, 76, 32)
    struct.pack_into('<I', out, 80, 0x00000004)
    struct.pack_into('<4s', out, 84, b'DXT1')
    struct.pack_into('<I', out, 108, 0x00001000)
    return bytes(out)


DDS_TN_HEADER = _build_dds_tn_header()
assert len(DDS_TN_HEADER) == 128

DXT1_DATA_SIZE = 18432


def read_jacket_as_rgb(arc_path: str):
    with open(arc_path, 'rb') as f:
        raw = f.read()
    try:
        arc = ARC(raw)
        names = arc.filenames
        if not names:
            return None
        dds_data = arc.read_file(names[0])
    except Exception:
        return None

    if len(dds_data) < 128:
        return None

    header = dds_data[:128]
    pf_flags = struct.unpack_from('<I', header, 80)[0]
    pf_fourcc = header[84:88]
    pf_bitcount = struct.unpack_from('<I', header, 88)[0]
    width = struct.unpack_from('<I', header, 16)[0]
    height = struct.unpack_from('<I', header, 12)[0]

    pixel_data = dds_data[128:]

    if pf_flags & 0x4 and pf_fourcc == b'DXT1':
        buf = DXTBuffer(width, height)
        rgba = buf.DXT1Decompress(pixel_data)
        img = Image.frombytes('RGBA', (width, height), rgba)
        return img.convert('RGB')
    elif pf_bitcount == 32:
        expected = width * height * 4
        if len(pixel_data) < expected:
            return None
        arr = np.frombuffer(pixel_data[:expected], dtype=np.uint8).reshape(height, width, 4)
        rgb = arr[:, :, [2, 1, 0]]
        return Image.fromarray(rgb.astype(np.uint8), 'RGB')
    else:
        import io
        try:
            return Image.open(io.BytesIO(dds_data)).convert('RGB')
        except Exception:
            return None


def encode_dxt1(img: Image.Image) -> bytes:
    img192 = img.resize((192, 192), Image.LANCZOS).convert('RGB')
    arr = np.array(img192, dtype=np.int32)

    blocks = arr.reshape(48, 4, 48, 4, 3).transpose(0, 2, 1, 3, 4).reshape(2304, 16, 3)

    max_c = blocks.max(axis=1)
    min_c = blocks.min(axis=1)

    def to_565(rgb):
        r5 = (rgb[:, 0] >> 3) & 31
        g6 = (rgb[:, 1] >> 2) & 63
        b5 = (rgb[:, 2] >> 3) & 31
        return ((r5 << 11) | (g6 << 5) | b5).astype(np.uint16)

    c0_565 = to_565(max_c)
    c1_565 = to_565(min_c)

    swap = c0_565 < c1_565
    c0_new = np.where(swap, c1_565, c0_565)
    c1_new = np.where(swap, c0_565, c1_565)
    c0_565, c1_565 = c0_new.astype(np.uint16), c1_new.astype(np.uint16)

    def from_565(v):
        r = ((v >> 11) & 31).astype(np.int32) * 255 // 31
        g = ((v >> 5) & 63).astype(np.int32) * 255 // 63
        b = (v & 31).astype(np.int32) * 255 // 31
        return np.stack([r, g, b], axis=1)

    c0_rgb = from_565(c0_565)
    c1_rgb = from_565(c1_565)
    c2_rgb = (2 * c0_rgb + c1_rgb) // 3
    c3_rgb = (c0_rgb + 2 * c1_rgb) // 3

    palette = np.stack([c0_rgb, c1_rgb, c2_rgb, c3_rgb], axis=1)

    diff = blocks[:, :, np.newaxis, :] - palette[:, np.newaxis, :, :]
    dists = (diff * diff).sum(axis=3)
    indices = dists.argmin(axis=2).astype(np.uint32)

    shifts = np.arange(16, dtype=np.uint32) * 2
    packed = (indices << shifts).sum(axis=1).astype(np.uint32)

    c0_b = c0_565.astype('<u2').view(np.uint8).reshape(2304, 2)
    c1_b = c1_565.astype('<u2').view(np.uint8).reshape(2304, 2)
    pk_b = packed.astype('<u4').view(np.uint8).reshape(2304, 4)

    out = np.concatenate([c0_b, c1_b, pk_b], axis=1)
    result = out.tobytes()
    assert len(result) == DXT1_DATA_SIZE
    return result


def make_thumbnail_arc(thumbnails: list) -> bytes:
    n = len(thumbnails)
    header_size = 16
    entries_size = n * 16

    name_strs = [f"data/jacket/thumbnail/{song_id}_tn.dds" for song_id, _ in thumbnails]

    name_offsets = []
    name_area = bytearray()
    base = header_size + entries_size
    for name in name_strs:
        name_offsets.append(base + len(name_area))
        name_area.extend(name.encode('ascii') + b'\x00')

    file_base = base + len(name_area)
    file_offsets = []
    cur = file_base
    for _, dds in thumbnails:
        file_offsets.append(cur)
        cur += len(dds)

    out = bytearray()
    out.extend(struct.pack('<IIII', 0x19751120, 1, n, 2))
    for i in range(n):
        size = len(thumbnails[i][1])
        out.extend(struct.pack('<IIII', name_offsets[i], file_offsets[i], size, size))
    out.extend(name_area)
    for _, dds in thumbnails:
        out.extend(dds)
    return bytes(out)
