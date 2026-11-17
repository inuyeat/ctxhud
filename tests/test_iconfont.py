"""Synthetic artwork only: PNG decoding, grid operations and TrueType contract."""
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

MODULE = Path(__file__).resolve().parents[1] / "tools" / "iconfont.py"
SPEC = importlib.util.spec_from_file_location("ctxhud_iconfont_tool", MODULE)
iconfont = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(iconfont)


def encode_filtered(width, rows, channels, methods):
    previous = bytes(len(rows[0]))
    data = bytearray()
    for row, method in zip(rows, methods):
        encoded = bytearray()
        for index, value in enumerate(row):
            left = row[index-channels] if index >= channels else 0
            up = previous[index]
            corner = previous[index-channels] if index >= channels else 0
            predictors = [0, left, up, (left+up)//2]
            p = left + up - corner
            distances = [abs(p-left), abs(p-up), abs(p-corner)]
            predictors.append([left, up, corner][distances.index(min(distances))])
            encoded.append((value-predictors[method]) & 255)
        data.append(method)
        data.extend(encoded)
        previous = row
    header = struct.pack(">IIBBBBB", width, len(rows), 8, 2 if channels == 3 else 6, 0, 0, 0)
    return (iconfont.PNG_SIGNATURE + iconfont._chunk(b"IHDR", header) +
            iconfont._chunk(b"IDAT", zlib.compress(data)) + iconfont._chunk(b"IEND", b""))


def tables(font):
    count = struct.unpack_from(">H", font, 4)[0]
    result = {}
    for index in range(count):
        tag, check, offset, length = struct.unpack_from(">4sIII", font, 12+16*index)
        result[tag] = (check, offset, font[offset:offset+length])
    return result


class IconfontTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_all_png_filters_rgb_and_rgba(self):
        for channels in (3, 4):
            for method in range(5):
                with self.subTest(channels=channels, method=method):
                    rows = [bytes((17*x+43*y+11) % 256 for x in range(3*channels)) for y in range(3)]
                    path = self.root / "synthetic.png"
                    path.write_bytes(encode_filtered(3, rows, channels, [method]*3))
                    width, height, rgba = iconfont.read_png(path)
                    expected = bytearray()
                    for row in rows:
                        for x in range(3):
                            expected.extend(row[x*channels:(x+1)*channels])
                            if channels == 3:
                                expected.append(255)
                    self.assertEqual((width, height), (3, 3))
                    self.assertEqual(rgba, expected)

    def test_mixed_filters_and_multiple_idat(self):
        rows = [bytes((17*x+31*y) % 256 for x in range(12)) for y in range(5)]
        encoded = encode_filtered(3, rows, 4, list(range(5)))
        # Split the single IDAT at an arbitrary compressed byte boundary.
        length = struct.unpack_from(">I", encoded, 33)[0]
        payload = encoded[41:41+length]
        split = encoded[:33] + iconfont._chunk(b"IDAT", payload[:3]) + iconfont._chunk(b"IDAT", payload[3:]) + iconfont._chunk(b"IEND", b"")
        path = self.root / "mixed.png"
        path.write_bytes(split)
        self.assertEqual(iconfont.read_png(path)[2], b"".join(rows))

    def test_crc_truncation_signature_and_unsupported_format(self):
        path = self.root / "invalid.png"
        valid = encode_filtered(1, [bytes([1, 2, 3])], 3, [0])
        damaged = bytearray(valid)
        damaged[20] ^= 1
        for raw in (b"not png", valid[:-3], bytes(damaged)):
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                iconfont.read_png(path)
        for depth, color, interlace in ((16, 6, 0), (8, 3, 0), (8, 6, 1)):
            header = struct.pack(">IIBBBBB", 1, 1, depth, color, 0, 0, interlace)
            path.write_bytes(iconfont.PNG_SIGNATURE + iconfont._chunk(b"IHDR", header) +
                             iconfont._chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff")) + iconfont._chunk(b"IEND", b""))
            with self.assertRaises(ValueError):
                iconfont.read_png(path)

    def test_declared_dimensions_bound_decompression(self):
        header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        path = self.root / "oversized.png"
        path.write_bytes(iconfont.PNG_SIGNATURE + iconfont._chunk(b"IHDR", header) +
                         iconfont._chunk(b"IDAT", zlib.compress(b"x"*10000)) + iconfont._chunk(b"IEND", b""))
        with self.assertRaises(ValueError):
            iconfont.read_png(path)

    def test_transparency_is_composited_on_white(self):
        rgba = bytearray([0, 0, 0, 0, 0, 0, 0, 255, 255, 255, 255, 255, 220, 220, 220, 255])
        self.assertEqual(iconfont.threshold_mask(4, 1, rgba), bytearray([0, 1, 0, 0]))

    def test_png_writer_round_trip(self):
        path = self.root / "roundtrip.png"
        rgba = bytes([5, 10, 15, 255, 0, 0, 0, 0])
        iconfont.write_png(path, 2, 1, rgba)
        self.assertEqual(iconfont.read_png(path), (2, 1, rgba))

    def test_background_crop_and_speck_removal(self):
        mask = bytearray(30*30)
        for y in range(5, 25):
            for x in range(4, 24):
                mask[y*30+x] = 1
        mask[0] = 1
        width, height, result = iconfont.clean_and_crop(30, 30, mask)
        self.assertEqual((width, height), (20, 20))
        self.assertEqual(sum(result), 400)
        with self.assertRaises(ValueError):
            iconfont.clean_and_crop(2, 2, bytearray(4))

    def test_repeated_block_boundaries_find_pitch(self):
        mask = bytearray(16*16)
        for y in range(16):
            for x in range(16):
                mask[y*16+x] = ((x//2 + y//2) % 2 == 0)
        self.assertEqual(iconfont.estimate_grid(16, 16, mask), 2)
        grid, pitch = iconfont.quantize_grid(16, 16, mask)
        self.assertEqual((len(grid[0]), len(grid), pitch), (8, 8, 2))

    def test_fill_outline_keeps_internal_ink_as_negative_space(self):
        mask = bytearray(9*9)
        for x in range(1, 8):
            mask[9+x] = mask[7*9+x] = 1
        for y in range(1, 8):
            mask[y*9+1] = mask[y*9+7] = 1
        for x, y in ((3, 3), (4, 4), (3, 5), (5, 5)):
            mask[y*9+x] = 1
        filled = iconfont.fill_outline(9, 9, mask)
        self.assertEqual(filled[2*9+3], 1)
        self.assertEqual(filled[3*9+3], 0)
        self.assertEqual(filled[5*9+5], 0)
        self.assertEqual(filled[0], 0)
        self.assertEqual(filled[9+1], 1)
        mask[9+4] = 0
        with self.assertRaises(ValueError):
            iconfont.fill_outline(9, 9, mask)

    def test_two_cell_split_has_no_artificial_middle_gap(self):
        left, right = iconfont.grid_rectangles([[True]], cells=2)
        self.assertEqual(left, [(40, -50, 500, 650)])
        self.assertEqual(right, [(0, -50, 460, 650)])

    def test_single_cell_preserves_square_blocks_and_centers_wide_icon(self):
        grid = [[True] * 11 for _ in range(7)]
        rectangles = iconfont.single_cell_rectangles(grid)
        self.assertEqual(len(rectangles), 77)
        for x0, y0, x1, y1 in rectangles:
            self.assertEqual(x1-x0, 45)
            self.assertEqual(y1-y0, 45)
        x0, y0, x1, y1 = iconfont._bbox(rectangles)
        self.assertTrue(0 <= x0 < x1 <= 500)
        self.assertLessEqual(abs((x0+x1)/2 - 250), .5)
        self.assertLessEqual(abs((y0+y1)/2 - 300), .5)
        self.assertLess(y1-y0, x1-x0)

    def test_single_cell_keeps_empty_grid_cells_as_holes(self):
        grid = [[True, False, True], [True, True, True]]
        rectangles = iconfont.single_cell_rectangles(grid)
        self.assertEqual(len(rectangles), 5)
        self.assertNotEqual(iconfont._bbox(rectangles)[0], iconfont._bbox(rectangles)[2])

    def test_generate_adds_single_cell_without_removing_split_pair(self):
        # Deliberately generic synthetic rectangles, never private input artwork.
        rgba = bytes([0, 0, 0, 255]) * (12*8)
        for label in ("Claude", "Codex", "Agy"):
            iconfont.write_png(self.root / (label + ".png"), 12, 8, rgba)
        output = self.root / "output" / "synthetic.ttf"
        summary = iconfont.generate(self.root, output, grid_pitch=2)
        directory = tables(output.read_bytes())
        self.assertEqual(struct.unpack_from(">H", directory[b"maxp"][2], 4)[0], 6)
        self.assertEqual(summary["Claude"]["single_cell"]["codepoint"], "U+F8F4")
        cmap = directory[b"cmap"][2][12:]
        count = struct.unpack_from(">H", cmap, 6)[0] // 2
        ends = struct.unpack_from(">%dH" % count, cmap, 14)
        deltas = struct.unpack_from(">%dH" % count, cmap, 16+4*count)
        for index, code in enumerate(range(0xF8F0, 0xF8F5)):
            self.assertEqual(ends[index], code)
            self.assertEqual((code+deltas[index]) & 0xFFFF, index+1)
        self.assertEqual(iconfont.checksum(output.read_bytes()), 0xB1B0AFBA)
        for code in range(0xF8F0, 0xF8F5):
            width, height, _ = iconfont.read_png(output.parent / ("glyph-%04X.png" % code))
            self.assertEqual((width, height), (9, 18))
            self.assertTrue((output.parent / ("glyph-%04X.svg" % code)).is_file())
        glyph_offsets = struct.unpack(">7I", directory[b"loca"][2])
        glyph = directory[b"glyf"][2][glyph_offsets[5]:glyph_offsets[6]]
        bounds = struct.unpack_from(">hhhh", glyph, 2)
        self.assertLessEqual(abs((bounds[1]+bounds[3])/2 - 300), .5)

    def fake_font(self):
        return iconfont.build_ttf({code: [(20, -50, 480, 650)] for code in range(0xF8F0, 0xF8F4)})

    def test_true_type_tables_checksums_and_metrics(self):
        font = self.fake_font()
        directory = tables(font)
        self.assertEqual(set(directory), {b"head", b"hhea", b"maxp", b"OS/2", b"hmtx", b"cmap", b"loca", b"glyf", b"name", b"post"})
        self.assertEqual(iconfont.checksum(font), 0xB1B0AFBA)
        for tag, (check, offset, data) in directory.items():
            self.assertEqual(offset % 4, 0)
            if tag == b"head":
                data = data[:8] + b"\x00"*4 + data[12:]
            self.assertEqual(iconfont.checksum(data), check, tag)
        self.assertEqual(struct.unpack_from(">H", directory[b"maxp"][2], 4)[0], 5)
        self.assertEqual(struct.unpack_from(">H", directory[b"head"][2], 18)[0], 1000)
        self.assertEqual(len(directory[b"hhea"][2]), 36)
        self.assertEqual(len(directory[b"maxp"][2]), 32)
        self.assertEqual(len(directory[b"OS/2"][2]), 78)
        self.assertEqual(len(directory[b"post"][2]), 32)
        for index in range(5):
            self.assertEqual(struct.unpack_from(">H", directory[b"hmtx"][2], index*4)[0], 500)

    def test_cmap_four_private_use_characters_and_loca(self):
        directory = tables(self.fake_font())
        cmap = directory[b"cmap"][2]
        offset = struct.unpack_from(">I", cmap, 8)[0]
        subtable = cmap[offset:]
        self.assertEqual(struct.unpack_from(">H", subtable)[0], 4)
        count = struct.unpack_from(">H", subtable, 6)[0]//2
        ends = struct.unpack_from(">%dH" % count, subtable, 14)
        starts = struct.unpack_from(">%dH" % count, subtable, 16+2*count)
        deltas = struct.unpack_from(">%dH" % count, subtable, 16+4*count)
        self.assertEqual(ends[-1], 0xFFFF)
        for index, code in enumerate(range(0xF8F0, 0xF8F4)):
            self.assertEqual(starts[index], code)
            self.assertEqual(ends[index], code)
            self.assertEqual((code+deltas[index]) & 0xFFFF, index+1)
        loca = struct.unpack(">6I", directory[b"loca"][2])
        self.assertEqual(loca[0], 0)
        self.assertEqual(loca[-1], len(directory[b"glyf"][2]))
        for index in range(1, 5):
            self.assertEqual(struct.unpack_from(">h", directory[b"glyf"][2], loca[index])[0], 1)

    def test_font_generation_is_deterministic_and_named(self):
        first, second = self.fake_font(), self.fake_font()
        self.assertEqual(first, second)
        self.assertIn("ctxhud-icons".encode("utf-16-be"), tables(first)[b"name"][2])

    def test_preview_is_cell_sized_and_vertically_centered(self):
        iconfont.preview([(20, -50, 480, 650)], self.root / "preview")
        width, height, rgba = iconfont.read_png(self.root / "preview.png")
        self.assertEqual((width, height), (9, 18))
        self.assertEqual(sum(rgba[:9*3*4]), 9*3*4*255)
        self.assertEqual(sum(rgba[-9*3*4:]), 9*3*4*255)
        self.assertTrue((self.root / "preview.svg").is_file())
        self.assertTrue((self.root / "preview-zoom.png").is_file())


if __name__ == "__main__":
    unittest.main()
