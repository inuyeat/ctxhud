"""Build a personal monochrome pixel-icon TrueType font, using only stdlib.

Input artwork and generated assets are intentionally separate from this MIT
tool. Supports non-interlaced 8-bit RGB/RGBA PNGs, without Pillow/fontTools.
"""
from __future__ import annotations

import argparse
from array import array
from collections import deque
import json
import math
from pathlib import Path
import statistics
import struct
import sys
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PIXELS = 16_000_000
EM, ADVANCE, ASCENT, DESCENT = 1000, 500, 800, -200
FONT_NAME = "ctxhud-icons"


def paeth(a, b, c):
    estimate = a + b - c
    distances = (abs(estimate - a), abs(estimate - b), abs(estimate - c))
    return (a, b, c)[distances.index(min(distances))]


def read_png(path):
    """Return (width, height, RGBA bytes); reject unsupported/malformed PNGs."""
    data = Path(path).read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")
    cursor, image_header, compressed, ended = 8, None, bytearray(), False
    while cursor < len(data):
        if cursor + 12 > len(data):
            raise ValueError("truncated PNG chunk")
        length, tag = struct.unpack_from(">I4s", data, cursor)
        if length > 64 * 1024 * 1024 or cursor + 12 + length > len(data):
            raise ValueError("invalid PNG chunk length")
        payload = data[cursor + 8:cursor + 8 + length]
        crc = struct.unpack_from(">I", data, cursor + 8 + length)[0]
        if zlib.crc32(tag + payload) & 0xFFFFFFFF != crc:
            raise ValueError("PNG CRC mismatch")
        if image_header is None and tag != b"IHDR":
            raise ValueError("IHDR must be first")
        if tag == b"IHDR":
            if image_header is not None or length != 13:
                raise ValueError("invalid IHDR")
            image_header = struct.unpack(">IIBBBBB", payload)
        elif tag == b"IDAT":
            compressed.extend(payload)
        elif tag == b"IEND":
            if length:
                raise ValueError("invalid IEND")
            ended = True
            break
        elif not (tag[0] & 32) and tag != b"PLTE":
            raise ValueError("unsupported critical PNG chunk")
        cursor += length + 12
    if image_header is None or not ended or not compressed:
        raise ValueError("incomplete PNG")
    width, height, depth, color, compression, filtering, interlace = image_header
    if not width or not height or width * height > MAX_PIXELS:
        raise ValueError("PNG dimensions out of bounds")
    if depth != 8 or color not in (2, 6) or compression or filtering or interlace:
        raise ValueError("requires non-interlaced 8-bit RGB or RGBA PNG")
    channels = 3 if color == 2 else 4
    stride = width * channels
    expected = height * (stride + 1)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(bytes(compressed), expected + 1)
    if len(raw) != expected or not decoder.eof or decoder.unused_data:
        raise ValueError("invalid PNG image data length")
    previous = bytearray(stride)
    rgba = bytearray(width * height * 4)
    offset = 0
    for y in range(height):
        method = raw[offset]
        offset += 1
        if method > 4:
            raise ValueError("unsupported PNG row filter")
        row = bytearray(raw[offset:offset + stride])
        offset += stride
        if method:
            for index in range(stride):
                left = row[index - channels] if index >= channels else 0
                up = previous[index]
                upper_left = previous[index - channels] if index >= channels else 0
                predictor = (left if method == 1 else up if method == 2 else
                             (left + up) // 2 if method == 3 else paeth(left, up, upper_left))
                row[index] = (row[index] + predictor) & 255
        destination = y * width * 4
        if channels == 4:
            rgba[destination:destination + width * 4] = row
        else:
            for x in range(width):
                rgba[destination + 4*x:destination + 4*x + 4] = row[3*x:3*x + 3] + b"\xff"
        previous = row
    return width, height, rgba


def _chunk(tag, payload):
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)


def write_png(path, width, height, rgba):
    if len(rgba) != width * height * 4:
        raise ValueError("RGBA buffer size mismatch")
    raw = b"".join(b"\x00" + bytes(rgba[y*width*4:(y+1)*width*4]) for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    Path(path).write_bytes(PNG_SIGNATURE + _chunk(b"IHDR", header) +
                           _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


def threshold_mask(width, height, rgba, threshold=220, alpha_min=128):
    """Composite onto white before luminance thresholding; ignore faint alpha."""
    result = bytearray(width * height)
    for index in range(width * height):
        r, g, b, alpha = rgba[4*index:4*index + 4]
        if alpha < alpha_min:
            continue
        luminance = (54*r + 183*g + 19*b) // 256
        on_white = (luminance * alpha + 255 * (255 - alpha) + 127) // 255
        result[index] = on_white < threshold
    return result


def components(width, height, mask):
    labels = array("I", [0]) * len(mask)
    areas = [0]
    for start, value in enumerate(mask):
        if not value or labels[start]:
            continue
        label = len(areas)
        areas.append(0)
        labels[start] = label
        queue = deque([start])
        while queue:
            index = queue.popleft()
            areas[label] += 1
            x = index % width
            for neighbor in ((index - 1 if x else -1),
                             (index + 1 if x + 1 < width else -1),
                             (index - width if index >= width else -1),
                             (index + width if index + width < len(mask) else -1)):
                if neighbor >= 0 and mask[neighbor] and not labels[neighbor]:
                    labels[neighbor] = label
                    queue.append(neighbor)
    return labels, areas


def clean_and_crop(width, height, mask):
    labels, areas = components(width, height, mask)
    maximum = max(areas)
    if not maximum:
        raise ValueError("image has no foreground after thresholding")
    keep = {index for index, area in enumerate(areas) if index and area >= max(2, maximum / 200)}
    # A tiny valid icon can contain a single foreground pixel.
    if not keep:
        keep = {areas.index(maximum)}
    clean = bytearray(label in keep for label in labels)
    xs, ys = [], []
    for index, value in enumerate(clean):
        if value:
            xs.append(index % width)
            ys.append(index // width)
    x0, x1, y0, y1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
    cropped = bytearray()
    for y in range(y0, y1):
        cropped.extend(clean[y*width + x0:y*width + x1])
    return x1 - x0, y1 - y0, cropped


def fill_outline(width, height, mask):
    """Fill the largest closed outline; enclosed separate ink becomes holes.

    The detached original inner marks are cut out, rather than filling all
    original background indiscriminately. Open outlines are rejected.
    """
    labels, areas = components(width, height, mask)
    if len(areas) < 2:
        raise ValueError("no outline")
    outline = max(range(1, len(areas)), key=areas.__getitem__)
    exterior = bytearray(len(mask))
    queue = deque()
    for index in list(range(width)) + list(range((height-1)*width, height*width)):
        if labels[index] != outline and not exterior[index]:
            exterior[index] = 1
            queue.append(index)
    for y in range(height):
        for index in (y*width, y*width + width - 1):
            if labels[index] != outline and not exterior[index]:
                exterior[index] = 1
                queue.append(index)
    while queue:
        index = queue.popleft()
        x = index % width
        for neighbor in ((index - 1 if x else -1), (index + 1 if x + 1 < width else -1),
                         (index - width if index >= width else -1),
                         (index + width if index + width < len(mask) else -1)):
            if neighbor >= 0 and labels[neighbor] != outline and not exterior[neighbor]:
                exterior[neighbor] = 1
                queue.append(neighbor)
    interior = sum(not outside and labels[index] != outline for index, outside in enumerate(exterior))
    if not interior:
        raise ValueError("outline is open: cannot flood-fill its interior")
    # Separate enclosed ink components are the original symbol cutouts.
    return bytearray(not exterior[index] and labels[index] in (0, outline)
                     for index in range(len(mask)))


def _boundaries(histogram, extent):
    threshold = max(1, int(extent * .01))
    points = [index for index, count in enumerate(histogram) if count >= threshold]
    groups = []
    jitter = 1 if extent < 64 else 2
    for point in points:
        if groups and point - groups[-1][-1] <= jitter:
            groups[-1].append(point)
        else:
            groups.append([point])
    return [max(group, key=lambda index: histogram[index]) for group in groups]


def estimate_grid(width, height, mask):
    """Estimate block pitch from repeated, long axis-aligned ink boundaries."""
    vertical, horizontal = [0] * (width + 1), [0] * (height + 1)
    for y in range(height):
        previous = 0
        for x in range(width):
            value = mask[y*width + x]
            if value != previous:
                vertical[x] += 1
            previous = value
        vertical[width] += previous
    for x in range(width):
        previous = 0
        for y in range(height):
            value = mask[y*width + x]
            if value != previous:
                horizontal[y] += 1
            previous = value
        horizontal[height] += previous
    gaps = []
    for boundaries in (_boundaries(vertical, height), _boundaries(horizontal, width)):
        gaps.extend(b - a for a, b in zip(boundaries, boundaries[1:]) if b - a >= max(1, min(width, height)//128))
    if not gaps:
        return max(1, round(max(width, height) / 32))
    candidates = sorted(set(gaps))
    def support(pitch):
        nearby = sum(abs(gap - pitch) <= max(1, pitch*.16) for gap in gaps)
        return nearby / math.sqrt(pitch)
    winner = max(candidates, key=support)
    matching = [gap for gap in gaps if abs(gap - winner) <= max(1, winner*.16)]
    return max(1, round(statistics.median(matching)))


def quantize_grid(width, height, mask, pitch=None):
    pitch = estimate_grid(width, height, mask) if pitch is None else pitch
    if pitch <= 0:
        raise ValueError("grid pitch must be positive")
    columns = min(64, max(1, round(width / pitch)))
    rows = min(64, max(1, round(height / pitch)))
    grid = []
    for row in range(rows):
        y0, y1 = row * height // rows, (row + 1) * height // rows
        cells = []
        for column in range(columns):
            x0, x1 = column * width // columns, (column + 1) * width // columns
            ink = sum(mask[y*width + x0:y*width + x1].count(1) for y in range(y0, y1))
            cells.append(ink * 2 >= max(1, (y1-y0)*(x1-x0)))
        grid.append(cells)
    return grid, pitch


def grid_rectangles(grid, cells=1):
    """Fit to a 2:1 terminal cell with 15% vertical and 4% outer side margins."""
    rows, columns = len(grid), len(grid[0])
    total_width = ADVANCE * cells
    left, right = round(total_width*.04), round(total_width*.96)
    bottom, top = DESCENT + 150, ASCENT - 150
    result = [[] for _ in range(cells)]
    for row in range(rows):
        y0 = round(top - (row + 1) * (top-bottom) / rows)
        y1 = round(top - row * (top-bottom) / rows)
        for column in range(columns):
            if not grid[row][column]:
                continue
            x0 = round(left + column*(right-left)/columns)
            x1 = round(left + (column+1)*(right-left)/columns)
            for cell in range(cells):
                a, b = max(x0, cell*ADVANCE), min(x1, (cell+1)*ADVANCE)
                if a < b and y0 < y1:
                    result[cell].append((a-cell*ADVANCE, y0, b-cell*ADVANCE, y1))
    return result


def single_cell_rectangles(grid):
    """Width-fit a complete icon using equal integer pitch on both axes.

    Keep source grid cells square and center the resulting silhouette in one
    terminal cell. Raster previews sample these rectangles with nearest pixels.
    """
    rows, columns = len(grid), len(grid[0])
    # Use the whole advance: an extra inset can discard one-cell features
    # (such as eyes) when a wide source grid is sampled at terminal sizes.
    pitch = min(ADVANCE // columns, 700 // rows)
    if pitch < 1:
        raise ValueError("grid is too large for an integer-pitch terminal cell")
    left = (ADVANCE - columns*pitch) // 2
    bottom = (ASCENT + DESCENT - rows*pitch) // 2
    return [(left + column*pitch, bottom + (rows-row-1)*pitch,
             left + (column+1)*pitch, bottom + (rows-row)*pitch)
            for row in range(rows) for column in range(columns) if grid[row][column]]


def _bbox(rectangles):
    if not rectangles:
        return (0, 0, 0, 0)
    return (min(r[0] for r in rectangles), min(r[1] for r in rectangles),
            max(r[2] for r in rectangles), max(r[3] for r in rectangles))


def _glyf(rectangles):
    if len(rectangles) > 16383:
        raise ValueError("too many pixel contours")
    header = struct.pack(">hhhhh", len(rectangles), *_bbox(rectangles))
    endpoints = b"".join(struct.pack(">H", 4*i + 3) for i in range(len(rectangles)))
    points = []
    for x0, y0, x1, y1 in rectangles:
        points.extend(((x0, y0), (x0, y1), (x1, y1), (x1, y0)))
    flags = b"\x01" * len(points)  # Every point on-curve, signed 16-bit deltas.
    axes = []
    for axis in (0, 1):
        last = 0
        coordinates = bytearray()
        for point in points:
            coordinates.extend(struct.pack(">h", point[axis] - last))
            last = point[axis]
        axes.append(bytes(coordinates))
    return header + endpoints + b"\x00\x00" + flags + b"".join(axes)


def checksum(data):
    padded = data + b"\x00" * (-len(data) % 4)
    return sum(struct.unpack(">%dI" % (len(padded)//4), padded)) & 0xFFFFFFFF if padded else 0


def build_ttf(glyphs):
    """glyphs: {BMP codepoint: rectangle list}. Glyph zero is an empty .notdef."""
    codes = sorted(glyphs)
    if not codes or codes[0] < 0 or codes[-1] >= 0xFFFF:
        raise ValueError("format 4 requires non-sentinel BMP codepoints")
    rectangles = [[]] + [glyphs[code] for code in codes]
    glyph_data, offsets = bytearray(), [0]
    for shape in rectangles:
        glyph = _glyf(shape)
        glyph_data.extend(glyph + b"\x00" * (-len(glyph) % 4))
        offsets.append(len(glyph_data))
    bounds = _bbox([rectangle for shape in rectangles for rectangle in shape])
    head = struct.pack(">IIIIHHQQhhhhHHhhh", 0x10000, 0x10000, 0, 0x5F0F3CF5,
                       3, EM, 2082844800, 2082844800, *bounds, 0, 8, 2, 1, 0)
    bearings = [_bbox(shape)[0] for shape in rectangles]
    rights = [ADVANCE - _bbox(shape)[2] for shape in rectangles]
    hhea = struct.pack(">IhhhH11hH", 0x10000, ASCENT, DESCENT, 0, ADVANCE,
                       min(bearings), min(rights), max(_bbox(s)[2] for s in rectangles),
                       1, 0, 0, 0, 0, 0, 0, 0, len(rectangles))
    max_points, max_contours = max(map(len, rectangles))*4, max(map(len, rectangles))
    maxp = struct.pack(">I14H", 0x10000, len(rectangles), max_points, max_contours,
                       0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0)
    os2 = bytearray(78)
    struct.pack_into(">HhHHH", os2, 0, 0, ADVANCE, 400, 5, 0)
    struct.pack_into(">11h", os2, 10, 650, 600, 0, 75, 650, 600, 0, 350, 50, 250, 0)
    struct.pack_into(">I", os2, 46, 1 << 28)  # Unicode range: BMP private-use area.
    os2[58:62] = b"ctxh"
    struct.pack_into(">HHHhhhHH", os2, 62, 0x40, codes[0], codes[-1],
                     ASCENT, DESCENT, 0, ASCENT, -DESCENT)
    hmtx = b"".join(struct.pack(">Hh", ADVANCE, bearing) for bearing in bearings)
    segments = len(codes) + 1
    selector = segments.bit_length() - 1
    ends = codes + [0xFFFF]
    deltas = [(index + 1 - code) & 0xFFFF for index, code in enumerate(codes)] + [1]
    cmap4 = (struct.pack(">7H", 4, 16 + 8*segments, 0, 2*segments,
                         2*(1 << selector), selector, 2*segments-2*(1 << selector)) +
             struct.pack(">%dH" % segments, *ends) + b"\x00\x00" +
             struct.pack(">%dH" % segments, *ends) +
             struct.pack(">%dH" % segments, *deltas) + b"\x00\x00" * segments)
    cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + cmap4
    names = {0: "Generated personal artwork font", 1: FONT_NAME, 2: "Regular",
             3: FONT_NAME + ":1.0", 4: FONT_NAME, 5: "Version 1.0", 6: FONT_NAME}
    strings, records = bytearray(), bytearray()
    for name_id, value in sorted(names.items()):
        encoded = value.encode("utf-16-be")
        records.extend(struct.pack(">6H", 3, 1, 0x0409, name_id, len(encoded), len(strings)))
        strings.extend(encoded)
    name = struct.pack(">3H", 0, len(names), 6+12*len(names)) + records + strings
    tables = {b"head": head, b"hhea": hhea, b"maxp": maxp, b"OS/2": bytes(os2),
              b"hmtx": hmtx, b"cmap": cmap, b"loca": struct.pack(">%dI" % len(offsets), *offsets),
              b"glyf": bytes(glyph_data), b"name": bytes(name),
              b"post": struct.pack(">IIhhIIIII", 0x30000, 0, -100, 50, 1, 0, 0, 0, 0)}
    count = len(tables)
    selector = count.bit_length() - 1
    offset = 12 + 16*count
    directory, body, head_offset = bytearray(), bytearray(), None
    for tag, data in sorted(tables.items()):
        directory.extend(struct.pack(">4sIII", tag, checksum(data), offset, len(data)))
        if tag == b"head":
            head_offset = offset
        padded = data + b"\x00" * (-len(data) % 4)
        body.extend(padded)
        offset += len(padded)
    font = bytearray(struct.pack(">I4H", 0x10000, count, (1 << selector)*16,
                                 selector, count*16-(1 << selector)*16) + directory + body)
    struct.pack_into(">I", font, head_offset + 8, (0xB1B0AFBA-checksum(font)) & 0xFFFFFFFF)
    return bytes(font)


def preview(rectangles, path, width=9, height=18, cells=1, zoom=16):
    """Render the actual font-space rectangles, without claiming OS raster QA."""
    rgba = bytearray()
    for y in range(height):
        fy = ASCENT - (y+.5)*EM/height
        for x in range(width):
            fx = (x+.5)*ADVANCE*cells/width
            on = any(x0 <= fx < x1 and y0 <= fy < y1 for x0, y0, x1, y1 in rectangles)
            rgba.extend((0, 0, 0, 255) if on else (255, 255, 255, 255))
    path = Path(path)
    write_png(path.with_suffix(".png"), width, height, rgba)
    enlarged = bytearray()
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row.extend(rgba[4*(y*width+x):4*(y*width+x)+4] * zoom)
        enlarged.extend(row * zoom)
    write_png(path.with_name(path.name + "-zoom").with_suffix(".png"), width*zoom, height*zoom, enlarged)
    shapes = "".join('<rect x="%d" y="%d" width="%d" height="%d"/>' %
                     (x0, ASCENT-y1, x1-x0, y1-y0) for x0, y0, x1, y1 in rectangles)
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
           'width="%d" height="%d" shape-rendering="crispEdges">'
           '<rect width="100%%" height="100%%" fill="white"/><g fill="black">%s</g></svg>') % (
               ADVANCE*cells, EM, width*zoom, height*zoom, shapes)
    path.with_suffix(".svg").write_text(svg, encoding="utf-8")


def generate(input_dir, output, threshold=220, alpha_min=128, fill=False, grid_pitch=None):
    input_dir, output = Path(input_dir), Path(output)
    glyphs, summary = {}, {}
    for label, codes in (("Claude", (0xF8F0, 0xF8F1)), ("Codex", (0xF8F2,)), ("Agy", (0xF8F3,))):
        width, height, rgba = read_png(input_dir / (label + ".png"))
        mask = threshold_mask(width, height, rgba, threshold, alpha_min)
        width, height, mask = clean_and_crop(width, height, mask)
        if label == "Codex" and fill:
            mask = fill_outline(width, height, mask)
        grid, pitch = quantize_grid(width, height, mask, grid_pitch)
        shapes = grid_rectangles(grid, len(codes))
        for code, shape in zip(codes, shapes):
            glyphs[code] = shape
        if label == "Claude":
            glyphs[0xF8F4] = single_cell_rectangles(grid)
        summary[label] = {"crop": [width, height], "pitch": pitch,
                          "grid": [len(grid[0]), len(grid)], "contours": list(map(len, shapes))}
        if label == "Claude":
            summary[label]["single_cell"] = {"codepoint": "U+F8F4",
                                              "contours": len(glyphs[0xF8F4]),
                                              "bbox": list(_bbox(glyphs[0xF8F4]))}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(build_ttf(glyphs))
    for code, shape in sorted(glyphs.items()):
        preview(shape, output.parent / ("glyph-%04X" % code))
    combined = glyphs[0xF8F0] + [(a+ADVANCE, b, c+ADVANCE, d) for a, b, c, d in glyphs[0xF8F1]]
    preview(combined, output.parent / "claude-combined", width=18, cells=2)
    (output.parent / "iconfont-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path(__file__).resolve().parents[1] / "assets" / "personal")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--threshold", type=int, default=220)
    parser.add_argument("--alpha-min", type=int, default=128)
    parser.add_argument("--grid-size", type=int, help="override automatically detected source block pitch")
    parser.add_argument("--fill-outline", action="store_true", help="fill Codex's closed outline and cut out its original inner marks")
    args = parser.parse_args(argv)
    if not 1 <= args.threshold <= 255 or not 0 <= args.alpha_min <= 255 or (args.grid_size is not None and args.grid_size < 1):
        parser.error("invalid threshold, alpha or grid size")
    try:
        summary = generate(args.input_dir, args.output or args.input_dir / (FONT_NAME + ".ttf"),
                           args.threshold, args.alpha_min, args.fill_outline, args.grid_size)
    except (OSError, ValueError, struct.error, zlib.error) as exc:
        parser.exit(1, "iconfont: %s\n" % exc)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
