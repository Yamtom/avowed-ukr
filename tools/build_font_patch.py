"""Extend the game's original Latin faces with Cyrillic from its Dante eText faces.

Requires fontTools (tested with 4.39.2). Never modifies the original game archives.
Outputs a separate UkrFonts_P trio, so translation rebuilds cannot remove fonts.
"""
import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import struct

from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen

import build_iostore_patch as bp
import extract_to_sqlite as ex
import font_pak

REQUIRED = set(range(0x410, 0x450)) | {0x401, 0x451, 0x404, 0x454, 0x406, 0x456, 0x407, 0x457, 0x490, 0x491}
NAME = 'Alabama-WinGDK_UkrFonts_P'


def bounds(font, name):
    glyphs = font.getGlyphSet()
    pen = BoundsPen(glyphs)
    glyphs[name].draw(pen)
    return pen.bounds


def drawing(font, name):
    glyphs = font.getGlyphSet()
    pen = DecomposingRecordingPen(glyphs)
    glyphs[name].draw(pen)
    return pen.value


def extend(original, donor_bytes):
    font, donor = TTFont(BytesIO(original)), TTFont(BytesIO(donor_bytes))
    assert 'CFF ' in font and 'glyf' in donor
    cmap, dmap = font.getBestCmap(), donor.getBestCmap()
    old_order = list(font.getGlyphOrder())
    old_metrics = dict(font['hmtx'].metrics)
    old_drawings = {n: drawing(font, n) for n in old_order}
    top = font['CFF '].cff.topDictIndex[0]
    order = list(old_order)
    scales = {}
    for kind, ref in [('upper', 'H'), ('lower', 'x')]:
        sy = bounds(font, cmap[ord(ref)])[3] / bounds(donor, dmap[ord(ref)])[3]
        width_ref = 'H' if kind == 'upper' else 'n'
        sx = font['hmtx'][cmap[ord(width_ref)]][0] / donor['hmtx'][dmap[ord(width_ref)]][0]
        scales[kind] = (sx, sy)
    added = {}
    # Include every Cyrillic character present in the shipped donor, plus Ukrainian Ghe.
    for cp in sorted(set(range(0x400, 0x460)) | {0x490, 0x491}):
        if cp in cmap or cp not in dmap:
            continue
        name = f'uni{cp:04X}'
        if name in order:
            name += '.uk'
        source = dmap[cp]
        sx, sy = scales['upper' if chr(cp).isupper() else 'lower']
        width = round(donor['hmtx'][source][0] * sx)
        pen = T2CharStringPen(width - top.Private.nominalWidthX, None)
        recording = DecomposingRecordingPen(donor.getGlyphSet())
        donor.getGlyphSet()[source].draw(recording)
        recording.replay(TransformPen(pen, (sx, 0, 0, sy, 0, 0)))
        charstring = pen.getCharString(private=top.Private, globalSubrs=font['CFF '].cff.GlobalSubrs)
        top.CharStrings.charStrings[name] = len(top.CharStrings.charStringsIndex)
        top.CharStrings.charStringsIndex.append(charstring)
        order.append(name)
        box = bounds(donor, source)
        font['hmtx'][name] = (width, round(box[0] * sx) if box else 0)
        added[cp] = name
    font.setGlyphOrder(order)
    top.charset = order
    font['maxp'].numGlyphs = len(order)
    for table in font['cmap'].tables:
        if table.isUnicode() and table.format in (4, 12):
            table.cmap.update(added)
    font['OS/2'].ulUnicodeRange1 |= 1 << 9
    font['OS/2'].ulCodePageRange1 |= 1 << 2
    for name in added.values():
        box = bounds(font, name)
        if box:
            font['OS/2'].usWinAscent = max(font['OS/2'].usWinAscent, round(box[3] + 0.5))
            font['OS/2'].usWinDescent = max(font['OS/2'].usWinDescent, round(-box[1] + 0.5))
    if 'DSIG' in font:
        del font['DSIG']
    output = BytesIO()
    font.recalcTimestamp = False
    font.save(output)
    data = output.getvalue()
    verified = TTFont(BytesIO(data), checkChecksums=2)
    assert REQUIRED <= verified.getBestCmap().keys()
    assert verified.getGlyphOrder()[:len(old_order)] == old_order
    for name in old_order:
        assert verified['hmtx'][name] == old_metrics[name], name
        assert drawing(verified, name) == old_drawings[name], name
    for cp in REQUIRED:
        name = verified.getBestCmap()[cp]
        assert bounds(verified, name) is not None
        assert verified['CFF '].cff.topDictIndex[0].CharStrings[name].width == verified['hmtx'][name][0]
    return data, {'added': len(added), 'scale': scales, 'original_glyphs_unchanged': len(old_order)}


def patch_composite(data):
    """Remove ONLY Espinosa's forced U+0400..04FF Dante subfont.

    Keep the default Espinosa typeface and the three CJK subfonts byte-identical.
    Guard every byte of the expected unversioned first-subfont structure.
    """
    marker = bytes.fromhex('000901000000000500050100040000000501ff04000000000000000304000000')
    assert data.count(marker) == 1, 'unexpected Cyrillic subfont layout'
    start = data.index(marker)
    assert struct.unpack_from('<I', data, start - 4)[0] == 4
    # Header/range/culture/typeface header + 4 entries (22 bytes each) + scale.
    end = start + 32 + 4 * 22 + 4
    assert data[end:end + 6] == bytes.fromhex('000908000000'), 'CJK subfont boundary changed'
    removed = data[start:end]
    expected = bytearray(marker)
    for name, ref in [(2, -8), (0, -5), (3, -7), (1, -6)]:
        expected += b'\0\x05' + struct.pack('<II', name, 0) + struct.pack('<IiI', 1, ref, 0)
    expected += struct.pack('<f', 0.89)
    assert removed == expected, f'Cyrillic typeface references changed ({len(marker)}, {len(expected)}, {len(removed)})'
    result = bytearray(data[:start - 4] + struct.pack('<I', 3) + data[end:])
    export_map = struct.unpack_from('<I', result, 0x20)[0]
    assert struct.unpack_from('<I', result, 0x24)[0] - export_map == 72
    old_size = struct.unpack_from('<Q', result, export_map + 8)[0]
    struct.pack_into('<Q', result, export_map + 8, old_size - len(removed))
    assert result[-12:] == data[-12:]
    return bytes(result)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--game-paks', type=Path, default=Path(bp.GAME_PAKS))
    ap.add_argument('--oodle', default=ex.DEFAULT_OODLE)
    ap.add_argument('--out-dir', type=Path, default=ex.REPO / 'build' / 'fonts')
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    original_dir = ex.REPO / 'raw_assets' / 'fonts' / 'original'
    original_dir.mkdir(parents=True, exist_ok=True)
    oodle = ex.Oodle(args.oodle)
    pak = font_pak.Reader(args.game_paks / 'Alabama-WinGDK.pak', oodle)
    files, report = {}, []
    donors = {}
    for path in pak.entries:
        if '/DanteeText_Faces/' in path and path.endswith('.ufont'):
            donors[Path(path).stem] = pak.read(path)
    for path in sorted(pak.entries):
        if not path.endswith(('.ufont', '.ttf', '.otf')):
            continue
        raw = pak.read(path)
        font = TTFont(BytesIO(raw))
        cm = font.getBestCmap()
        entry = {'path': path, 'sha256_original': hashlib.sha256(raw).hexdigest(),
                 'missing_before': [f'U+{cp:04X}' for cp in sorted(REQUIRED - cm.keys())]}
        if any(part in path for part in ('/EspinosaNova_Faces/', '/UiSystem/Content/Fonts/Celestia')):
            stem = Path(path).stem
            style = 'BoldItalic' if 'BoldItalic' in stem else 'Italic' if 'Italic' in stem else 'Bold' if ('Bold' in stem or 'Semibold' in stem) else ''
            donor = 'DanteeText' + ('-' + style if style else '')
            data, stats = extend(raw, donors[donor])
            (original_dir / Path(path).name).write_bytes(raw)
            (args.out_dir / (stem + '.otf')).write_bytes(data)
            files[path] = data
            entry.update(stats, donor=donor, sha256_modified=hashlib.sha256(data).hexdigest(), missing_after=[])
            print(stem, stats)
        report.append(entry)
    assert len(files) == 8
    # Espinosa explicitly maps Cyrillic to Dante; remove that override to use the extended face.
    toc = bp.TocReader(args.game_paks / 'Alabama-WinGDK.utoc', oodle)
    index = 156984
    original = toc.read(index)
    assert ex.parse_names(original) == ['Bold', 'BoldItalic', 'Default', 'Italic', 'EspinosaNova', '/Game/UI/Fonts/EspinosaNova']
    composite = patch_composite(original)
    cid = toc.chunk_id(index)
    container_id = int.from_bytes(hashlib.sha256(NAME.encode()).digest()[:8], 'little')
    header_id = struct.pack('<Q', toc.container_id) + b'\0\0\0\x06'
    header = bp.parse_container_header(toc.read(toc.lookup(header_id)))
    new_header = bp.carve_container_header(header, container_id, [struct.unpack_from('<Q', cid)[0]])
    chunks = [(cid, composite), (struct.pack('<Q', container_id) + b'\0\0\0\x06', bp.serialize_container_header(new_header))]
    base = args.out_dir / NAME
    bp.write_container(chunks, container_id, base)
    bp.verify_container(base, chunks, container_id, new_header)
    font_pak.write(base.with_suffix('.pak'), files)
    (args.out_dir / 'font_report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Verified 8 extended faces, unchanged original outlines/advances, full Cyrillic + Ukrainian coverage, PAK payloads and IoStore chunks.')


if __name__ == '__main__':
    main()
