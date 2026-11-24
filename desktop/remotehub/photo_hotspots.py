"""Find candidate key frames on a rectified remote photo.

Pure functions only: a straightened ``QImage`` goes in, a list of normalized
frames with a confidence comes out. Nothing here touches widgets, files or the
network, and only numpy and Qt are used.

Outline of the method, in working pixels (the photo is scaled so its long edge
is at most ``WORK_EDGE``; ``S`` below is the short edge):

1. Estimate the panel colour locally. Each window of the photo takes the
   median of the pixels close to the photo's dominant colour, or its own
   dominant colour when there are none (a two-tone panel or a hard shadow).
   The windows are interpolated, so gradual shading follows the panel.
2. Pixels that differ from the local panel colour form the foreground. A small
   closing joins fragments of one key; small specks are opened away.
3. Connected regions are filled (icons and text printed on a key become part
   of it) and filtered: regions touching the photo edge are background left by
   the corner selection, thin or tiny ones are text and icon fragments, huge
   ones are panel areas. Nearby small fragments are merged into one key when
   the result is key sized.
4. A thick ring with a centred hole is a direction pad and becomes five frames
   (up, down, left, right, OK). Five separate keys laid out as a cross with no
   diagonal neighbours are labelled the same way.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from .key_roles import DPAD_ROLES, KEYPAD_ROLES, ROLE_NAMES


WORK_EDGE = 640
MIN_KEYS = 3
MAX_KEYS = 128

# All sizes below are fractions of the short edge S.
_CLOSE = .008           # closing radius that joins fragments of one key
_BORDER = .012          # regions this close to the photo edge are background
_MIN_SIDE = .055        # thinner than this is a text line or an icon stroke
_MIN_AREA = .075 ** 2   # smaller than this is a fragment
_SPECK_AREA = .012 ** 2  # below this a fragment is noise, not worth merging
_MAX_SIDE = .55         # wider or taller than this is a panel area, not a key
_MAX_TALL = 1.1         # a tall rocker key may be this long
_MAX_RING = .9          # a direction pad may be larger than a key
_MERGE_GAP = .022       # fragments closer than this may belong to one key
_MAX_ASPECT = 4.5
_MIN_PAD = .26          # a direction pad is at least this wide
_PAD_OVER_KEY = 1.6     # a direction pad is at least this much larger than a typical key
_ROCKER = 1.9           # a capsule this much taller than wide is two keys (+ and −)


@dataclass(frozen=True)
class Hotspot:
    """One candidate key frame, normalized to the photo (0–1 on both axes)."""

    x: float
    y: float
    width: float
    height: float
    confidence: float
    shape: str = "rect"     # "round", "rect" or "capsule"
    role: str = ""          # "up", "down", "left", "right" or "ok" inside a direction pad

    @property
    def rect(self) -> list[float]:
        return [self.x, self.y, self.width, self.height]

    @property
    def name(self) -> str:
        return ROLE_NAMES.get(self.role, "")


@dataclass
class _Region:
    x0: int
    y0: int
    x1: int          # exclusive
    y1: int          # exclusive
    area: int        # foreground pixels
    filled: int      # foreground plus enclosed holes
    contrast: float  # mean difference from the panel, in threshold units
    hole: tuple | None = None   # (area, x0, y0, x1, y1) of the largest enclosed hole
    merged: bool = False

    @property
    def w(self):
        return self.x1 - self.x0

    @property
    def h(self):
        return self.y1 - self.y0

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2

    @property
    def fill(self):
        return self.filled / max(1, self.w * self.h)


# -- public -----------------------------------------------------------------

def detect_hotspots(image: QImage, *, min_keys: int = MIN_KEYS) -> list[Hotspot]:
    """Return candidate key frames in reading order, or [] when too few are found."""
    if image is None or image.isNull() or min(image.width(), image.height()) < 48:
        return []
    work = image
    if max(image.width(), image.height()) > WORK_EDGE:
        work = image.scaled(WORK_EDGE, WORK_EDGE, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
    rgb, inside = _pixels(work)
    height, width = inside.shape
    short = min(width, height)
    if short < 48:
        return []
    sharp = _ycc(rgb)
    ycc = _box_blur(sharp, 1)
    panel = _panel_colour(ycc, inside, short)
    diff = np.sqrt(((ycc - panel) ** 2).sum(axis=2))
    threshold = _threshold(diff, inside)
    contrast = diff / threshold
    mask = _open(_close((diff > threshold) & inside, max(1, round(short * _CLOSE))), 1)
    outlines = _close(_edges(sharp[..., 0], inside) & inside, 1)
    spots = _hotspots(_regions(mask, contrast, inside, short), _regions(outlines, contrast, inside, short), short)
    if len(spots) < min_keys:
        return []
    if len(spots) > MAX_KEYS:
        spots = sorted(spots, key=lambda s: -s[1])[:MAX_KEYS]
    result = [
        Hotspot(
            x=_clip(x0 / width), y=_clip(y0 / height),
            width=min((x1 - x0) / width, 1 - _clip(x0 / width)),
            height=min((y1 - y0) / height, 1 - _clip(y0 / height)),
            confidence=round(float(confidence), 3), shape=shape, role=role,
        )
        for (x0, y0, x1, y1), confidence, shape, role in spots
    ]
    return _reading_order(result)


# -- pixels -----------------------------------------------------------------

def _pixels(image: QImage):
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = converted.width(), converted.height()
    raw = np.frombuffer(converted.constBits(), np.uint8, count=converted.sizeInBytes())
    rows = raw.reshape(height, converted.bytesPerLine())[:, :width * 4].reshape(height, width, 4)
    rgba = rows.astype(np.float32)
    inside = rgba[..., 3] >= 128
    return rgba[..., :3], inside


def _ycc(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = .299 * r + .587 * g + .114 * b
    return np.stack([y, (b - y) * .6, (r - y) * .7], axis=2)


def _box_blur(values, radius):
    if radius <= 0:
        return values
    out = values
    for axis in (0, 1):
        pad = [(0, 0)] * out.ndim
        pad[axis] = (radius + 1, radius)
        cumulative = np.cumsum(np.pad(out, pad, mode="edge"), axis=axis, dtype=np.float64)
        size = out.shape[axis]
        upper = np.take(cumulative, range(2 * radius + 1, 2 * radius + 1 + size), axis=axis)
        lower = np.take(cumulative, range(0, size), axis=axis)
        out = ((upper - lower) / (2 * radius + 1)).astype(np.float32)
    return out


def _dominant(pixels):
    """Median colour of the most common coarse colour bin."""
    if len(pixels) == 0:
        return None
    bins = np.clip((pixels + [0, 128, 128]) // 12, 0, 31).astype(np.int64)
    keys = bins[:, 0] * 1024 + bins[:, 1] * 32 + bins[:, 2]
    winner = np.bincount(keys).argmax()
    near = (np.abs(bins - [winner // 1024, winner // 32 % 32, winner % 32]) <= 1).all(axis=1)
    return np.median(pixels[near], axis=0)


def _panel_colour(ycc, inside, short):
    height, width = inside.shape
    margin = max(1, round(short * .04))
    core = inside.copy()
    core[:margin], core[-margin:], core[:, :margin], core[:, -margin:] = False, False, False, False
    overall = _dominant(ycc[core] if core.any() else ycc[inside])
    if overall is None:
        return np.zeros_like(ycc)
    block = max(8, short // 6)
    rows, cols = math.ceil(height / block), math.ceil(width / block)
    grid = np.zeros((rows, cols, 3), np.float32)
    for row in range(rows):
        for col in range(cols):
            y0, y1 = max(0, (row - 1) * block), min(height, (row + 2) * block)
            x0, x1 = max(0, (col - 1) * block), min(width, (col + 2) * block)
            window = ycc[y0:y1, x0:x1][inside[y0:y1, x0:x1]]
            if len(window) == 0:
                grid[row, col] = overall
                continue
            delta = window - overall
            close = (delta[:, 0] * .5) ** 2 + delta[:, 1] ** 2 + delta[:, 2] ** 2 < 30 ** 2
            if close.sum() >= max(12, .05 * len(window)):
                grid[row, col] = np.median(window[close], axis=0)
            else:
                grid[row, col] = _dominant(window)
    centres_y = (np.arange(rows) + .5) * block
    centres_x = (np.arange(cols) + .5) * block
    ys, xs = np.arange(height) + .5, np.arange(width) + .5
    across = np.stack([
        np.stack([np.interp(xs, centres_x, grid[row, :, channel]) for channel in range(3)], axis=1)
        for row in range(rows)
    ])                                         # rows × width × 3
    return np.stack([
        np.stack([np.interp(ys, centres_y, across[:, col, channel]) for channel in range(3)], axis=1)
        for col in range(width)
    ], axis=1).astype(np.float32)              # height × width × 3


def _threshold(diff, inside):
    values = diff[inside]
    if len(values) == 0:
        return 12.
    low = float(np.percentile(values, 30))
    return float(min(40., max(10., 4.5 * low)))


def _edges(luma, inside):
    """Outlines and rims: keys the same colour as the panel still have a closed edge."""
    gx = np.zeros_like(luma)
    gy = np.zeros_like(luma)
    gx[:, 1:-1] = luma[:, 2:] - luma[:, :-2]
    gy[1:-1] = luma[2:] - luma[:-2]
    magnitude = np.hypot(gx, gy) / 2
    values = magnitude[inside]
    if len(values) == 0:
        return np.zeros_like(inside)
    return magnitude > max(4., 4 * float(np.median(values)))


# -- binary morphology and labelling ------------------------------------------

def _shift_or(mask, radius, combine):
    """Square structuring element, applied separably; pixels past the edge are ignored."""
    out = mask.copy()
    for step in range(1, radius + 1):
        out[step:] = combine(out[step:], mask[:-step])
        out[:-step] = combine(out[:-step], mask[step:])
    rows = out.copy()
    for step in range(1, radius + 1):
        out[:, step:] = combine(out[:, step:], rows[:, :-step])
        out[:, :-step] = combine(out[:, :-step], rows[:, step:])
    return out


def _dilate(mask, radius):
    return _shift_or(mask, radius, np.logical_or)


def _erode(mask, radius):
    return _shift_or(mask, radius, np.logical_and)


def _close(mask, radius):
    return _erode(_dilate(mask, radius), radius)


def _open(mask, radius):
    return _dilate(_erode(mask, radius), radius)


def label(mask, *, diagonal=True):
    """Connected components of a boolean array: (labels, count), labels start at 1."""
    height, width = mask.shape
    padded = np.zeros((height, width + 2), np.int8)
    padded[:, 1:-1] = mask
    edges = np.diff(padded, axis=1)
    run_rows, run_starts = np.nonzero(edges == 1)
    _, run_ends = np.nonzero(edges == -1)
    labels = np.zeros((height, width), np.int32)
    count = len(run_rows)
    if count == 0:
        return labels, 0
    parent = list(range(count))

    def find(i):
        root = i
        while parent[root] != root:
            root = parent[root]
        while parent[i] != root:
            parent[i], i = root, parent[i]
        return root

    rows, starts, ends = run_rows.tolist(), run_starts.tolist(), run_ends.tolist()
    first = np.searchsorted(run_rows, np.arange(height + 1)).tolist()
    reach = 1 if diagonal else 0
    for row in range(1, height):
        i, i_end, j, j_end = first[row - 1], first[row], first[row], first[row + 1]
        while i < i_end and j < j_end:
            if ends[i] + reach <= starts[j]:
                i += 1
            elif ends[j] + reach <= starts[i]:
                j += 1
            else:
                a, b = find(i), find(j)
                if a != b:
                    parent[max(a, b)] = min(a, b)
                if ends[i] < ends[j]:
                    i += 1
                else:
                    j += 1
    ids, next_id = {}, 0
    for run in range(count):
        root = find(run)
        if root not in ids:
            next_id += 1
            ids[root] = next_id
        labels[rows[run], starts[run]:ends[run]] = ids[root]
    return labels, next_id


def _component_boxes(labels, count):
    ys, xs = np.nonzero(labels)
    ids = labels[ys, xs]
    area = np.bincount(ids, minlength=count + 1)
    x0 = np.full(count + 1, labels.shape[1]); np.minimum.at(x0, ids, xs)
    y0 = np.full(count + 1, labels.shape[0]); np.minimum.at(y0, ids, ys)
    x1 = np.zeros(count + 1, int); np.maximum.at(x1, ids, xs + 1)
    y1 = np.zeros(count + 1, int); np.maximum.at(y1, ids, ys + 1)
    return area, x0, y0, x1, y1


def _holes(piece):
    """Enclosed background of one component crop: (filled pixel count, largest hole)."""
    padded = np.pad(~piece, 1, constant_values=True)
    labels, count = label(padded, diagonal=False)
    if count <= 1:
        return int(piece.sum()), None
    outside = labels[0, 0]
    area, x0, y0, x1, y1 = _component_boxes(labels, count)
    enclosed = [i for i in range(1, count + 1) if i != outside]
    largest = max(enclosed, key=lambda i: area[i])
    hole = (int(area[largest]), int(x0[largest]) - 1, int(y0[largest]) - 1,
            int(x1[largest]) - 1, int(y1[largest]) - 1)
    return int(piece.sum() + sum(int(area[i]) for i in enclosed)), hole


# -- regions to frames ---------------------------------------------------------

def _regions(mask, contrast, inside, short):
    labels, count = label(mask)
    if count == 0:
        return []
    height, width = mask.shape
    area, x0, y0, x1, y1 = _component_boxes(labels, count)
    border = max(1, round(short * _BORDER))
    outside = _dilate(~inside, border) if not inside.all() else None
    speck = _SPECK_AREA * short * short
    regions = []
    for i in range(1, count + 1):
        if area[i] < speck:
            continue
        if x0[i] < border or y0[i] < border or x1[i] > width - border or y1[i] > height - border:
            continue
        if max(x1[i] - x0[i], y1[i] - y0[i]) > _MAX_RING * short:
            continue
        piece = labels[y0[i]:y1[i], x0[i]:x1[i]] == i
        if outside is not None and outside[y0[i]:y1[i], x0[i]:x1[i]][piece].any():
            continue
        filled, hole = _holes(piece)
        strength = float(contrast[y0[i]:y1[i], x0[i]:x1[i]][piece].mean())
        regions.append(_Region(int(x0[i]), int(y0[i]), int(x1[i]), int(y1[i]), int(area[i]), filled, strength,
                               hole=hole))
    return regions


def _gap(a, b):
    dx = max(0, max(a.x0, b.x0) - min(a.x1, b.x1))
    dy = max(0, max(a.y0, b.y0) - min(a.y1, b.y1))
    return math.hypot(dx, dy)


def _is_key(region, short):
    side_min, side_max = min(region.w, region.h), max(region.w, region.h)
    return (
        side_min >= _MIN_SIDE * short
        and region.filled >= _MIN_AREA * short * short
        # A tall rocker (volume, channel) can be longer than the remote is wide.
        and (side_max <= _MAX_SIDE * short
             or (side_max <= _MAX_TALL * short and side_min <= _MAX_SIDE * short * .7
                 and side_max >= 1.6 * side_min))
        and side_max / max(1, side_min) <= _MAX_ASPECT
        and region.fill >= (.45 if region.merged else .55)
    )


def _merge_fragments(regions, short):
    """Join small neighbouring pieces whose union is key sized."""
    small = [r for r in regions if not _is_key(r, short) and max(r.w, r.h) <= _MAX_SIDE * short]
    rest = [r for r in regions if r not in small]
    parent = list(range(len(small)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    gap = _MERGE_GAP * short
    for i, a in enumerate(small):
        for j in range(i + 1, len(small)):
            if _gap(a, small[j]) <= gap:
                parent[find(i)] = find(j)
    groups = {}
    for i, region in enumerate(small):
        groups.setdefault(find(i), []).append(region)
    merged = []
    for members in groups.values():
        if len(members) == 1:
            merged.append(members[0])
            continue
        union = _Region(
            min(r.x0 for r in members), min(r.y0 for r in members),
            max(r.x1 for r in members), max(r.y1 for r in members),
            sum(r.area for r in members), sum(r.filled for r in members),
            sum(r.contrast * r.area for r in members) / sum(r.area for r in members), merged=True,
        )
        # Pieces of one key cover most of their joint box; a word does not.
        merged.append(union if _is_key(union, short) else members[0])
        if not _is_key(union, short):
            merged.extend(members[1:])
    return rest + merged


def _squarish(w, h, low=.75):
    return low <= w / max(1, h) <= 1 / low


def _pad(region, regions, short):
    """Inner box of a direction pad, or None.

    Two styles are recognised: a thick ring around a centred hole (the OK key sits
    in the hole), and a large round outline with a separate centred OK key.
    """
    side = (region.w + region.h) / 2
    if not _MIN_PAD * short <= max(region.w, region.h) <= _MAX_RING * short or not _squarish(region.w, region.h):
        return None
    if region.fill < .6:
        return None
    inner = []
    if region.hole is not None and region.area / max(1, region.filled) >= .3:
        hole_area, hx0, hy0, hx1, hy1 = region.hole
        if .1 <= hole_area / max(1, region.filled) <= .5:
            inner.append((region.x0 + hx0, region.y0 + hy0, region.x0 + hx1, region.y0 + hy1))
    for other in regions:
        if other is not region and region.x0 < other.x0 and other.x1 < region.x1 \
                and region.y0 < other.y0 and other.y1 < region.y1 and other.fill >= .55:
            inner.append((other.x0, other.y0, other.x1, other.y1))
    for x0, y0, x1, y1 in sorted(inner, key=lambda box: -(box[2] - box[0]) * (box[3] - box[1])):
        w, h = x1 - x0, y1 - y0
        if not _squarish(w, h, .6) or not .25 <= (w + h) / 2 / side <= .72:
            continue
        if abs((x0 + x1) / 2 - region.cx) <= .12 * side and abs((y0 + y1) / 2 - region.cy) <= .12 * side:
            return (x0, y0, x1, y1)
    # A round outline holding several marks (arrows, a label, an OK rim merged
    # with the arrows): still a pad, with the OK key at its usual size.
    outline = region.hole is not None and region.area / max(1, region.filled) < .3
    marks = [r for r in regions if r is not region and _within(r, (region.x0, region.y0, region.x1, region.y1))]
    if outline and region.fill >= .7 and len(marks) >= 2:
        radius = side * .25
        return (region.cx - radius, region.cy - radius, region.cx + radius, region.cy + radius)
    return None


def _shape(region):
    aspect = max(region.w, region.h) / max(1, min(region.w, region.h))
    if aspect >= 1.6:
        return "capsule"
    return "rect" if region.fill >= .88 else "round"


def _confidence(region, short):
    aspect = max(region.w, region.h) / max(1, min(region.w, region.h))
    score = .35 + .25 * min(1., (region.contrast - 1) / 1.5) + .25 * min(1., (region.fill - .5) / .35)
    score += .15 * (1 - min(1., max(0., aspect - 2.5) / 2))
    if region.merged:
        score *= .75
    return max(.05, min(.99, score))


def _pad_frames(region, inner):
    """Four frames in the band between the OK key and the pad edge."""
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    outer = (region.w + region.h) / 4
    radius = ((inner[2] - inner[0]) + (inner[3] - inner[1])) / 4
    band = max(2., outer - radius)
    half = max(band * .55, outer * .38)
    edge = band * .06
    return {
        "up": (cx - half, cy - outer + edge, cx + half, cy - radius - edge),
        "down": (cx - half, cy + radius + edge, cx + half, cy + outer - edge),
        "left": (cx - outer + edge, cy - half, cx - radius - edge, cy + half),
        "right": (cx + radius + edge, cy - half, cx + outer - edge, cy + half),
    }


def _within(inner, box):
    return box[0] <= inner.cx <= box[2] and box[1] <= inner.cy <= box[3]


def _cross(keys):
    """Label five separate keys laid out as a plus sign with no diagonal neighbours."""
    best = None
    for centre in keys:
        found = {}
        size = (centre.w + centre.h) / 2
        for role, (dx, dy) in (("up", (0, -1)), ("down", (0, 1)), ("left", (-1, 0)), ("right", (1, 0))):
            options = []
            for other in keys:
                if other is centre:
                    continue
                vx, vy = other.cx - centre.cx, other.cy - centre.cy
                along, across = vx * dx + vy * dy, abs(vx * dy - vy * dx)
                if along > size * .5 and across <= along * .35 and along <= size * 3:
                    options.append((along, other))
            if not options:
                break
            found[role] = min(options, key=lambda item: item[0])
        if len(found) != 4:
            continue
        up, down, left, right = (found[r][0] for r in ("up", "down", "left", "right"))
        if max(up, down) > 1.5 * min(up, down) or max(left, right) > 1.5 * min(left, right):
            continue
        reach_x, reach_y = (left + right) / 2, (up + down) / 2
        diagonal = any(
            abs(other.cx - (centre.cx + sx * reach_x)) < reach_x * .45
            and abs(other.cy - (centre.cy + sy * reach_y)) < reach_y * .45
            for other in keys for sx in (-1, 1) for sy in (-1, 1)
        )
        if diagonal:
            continue
        spread = max(up, down, left, right) / min(up, down, left, right)
        if best is None or spread < best[0]:
            best = (spread, centre, {role: item[1] for role, item in found.items()})
    if best is None:
        return {}
    roles = {id(best[1]): "ok"}
    roles.update({id(region): role for role, region in best[2].items()})
    return roles


def _key_frames(region, short, role):
    """One frame per key; a tall capsule is a rocker with an upper and a lower key."""
    box = (region.x0, region.y0, region.x1, region.y1)
    confidence, shape = _confidence(region, short), _shape(region)
    if not role and region.h >= _ROCKER * region.w:
        middle = (region.y0 + region.y1) / 2
        gap = region.h * .02
        return [((region.x0, region.y0, region.x1, middle - gap), confidence * .9, "capsule", ""),
                ((region.x0, middle + gap, region.x1, region.y1), confidence * .9, "capsule", "")]
    return [(box, confidence, shape, role)]


def _pads(regions, short, pads):
    for region in sorted(regions, key=lambda r: -r.filled):
        if any(_within(region, (p.x0, p.y0, p.x1, p.y1)) for p, _ in pads):
            continue
        inner = _pad(region, regions, short)
        if inner is not None:
            pads.append((region, inner))
    return pads


def _one_pad(pads, regions, short):
    """A remote has one direction pad: the largest candidate, and only when it is
    clearly bigger than the ordinary keys. Round keys with an icon in the middle
    look like small pads; they stay single keys."""
    if not pads:
        return []
    side = lambda r: (r.w + r.h) / 2
    region, inner = max(pads, key=lambda item: side(item[0]))
    others = [side(r) for r in _keys([r for r in regions if r is not region
                                       and not _within(r, (region.x0, region.y0, region.x1, region.y1))], short)]
    others += [side(r) for r, _ in pads if r is not region]
    if others and side(region) < _PAD_OVER_KEY * sorted(others)[len(others) // 2]:
        return []
    return [(region, inner)]


def _keys(regions, short):
    keys = sorted((r for r in _merge_fragments(regions, short) if _is_key(r, short)), key=lambda r: -r.filled)
    kept = []
    for region in keys:
        # Pieces sitting inside a bigger key are its icons or reflections.
        if not any(_within(region, (k.x0, k.y0, k.x1, k.y1)) for k in kept):
            kept.append(region)
    return kept


def _keypad(keys, excluded_ids):
    """Label at most one regular 4-row × 3-column numeric keypad block (1-9, *, 0, #)."""
    candidates = [r for r in keys if id(r) not in excluded_ids and r.h < _ROCKER * r.w]
    if len(candidates) < 12:
        return {}
    med_h = sorted(r.h for r in candidates)[len(candidates) // 2]
    rows = []
    for region in sorted(candidates, key=lambda r: r.cy):
        if rows and abs(region.cy - rows[-1][0]) <= med_h * .45:
            rows[-1][1].append(region)
            rows[-1][0] = sum(item.cy for item in rows[-1][1]) / len(rows[-1][1])
        else:
            rows.append([region.cy, [region]])
    if len(rows) < 4:
        return {}

    best = None
    for start in range(len(rows) - 3):
        window = [sorted(row, key=lambda r: r.cx) for _, row in rows[start:start + 4]]
        if any(len(row) != 3 for row in window):
            continue
        flat = [item for row in window for item in row]
        ws = sorted(item.w for item in flat)
        hs = sorted(item.h for item in flat)
        if ws[-1] > 1.8 * max(1, ws[0]) or hs[-1] > 1.8 * max(1, hs[0]):
            continue
        grid_w, grid_h = ws[len(ws) // 2], hs[len(hs) // 2]
        col_cxs = [[window[r][c].cx for r in range(4)] for c in range(3)]
        col_means = [sum(xs) / 4 for xs in col_cxs]
        dx01, dx12 = col_means[1] - col_means[0], col_means[2] - col_means[1]
        if min(dx01, dx12) <= grid_w * .8 or max(dx01, dx12) > 1.35 * min(dx01, dx12):
            continue
        col_step = (dx01 + dx12) / 2
        col_spread = max(max(xs) - min(xs) for xs in col_cxs)
        if col_spread > col_step * .35:
            continue
        row_cys = [sum(item.cy for item in row) / 3 for row in window]
        dys = [row_cys[r + 1] - row_cys[r] for r in range(3)]
        if min(dys) < grid_h * .7 or max(dys) > grid_h * 2.8 or max(dys) > 1.4 * min(dys):
            continue
        irregularity = (
            (max(dx01, dx12) / min(dx01, dx12) - 1.0)
            + (max(dys) / min(dys) - 1.0)
            + (col_spread / max(1.0, col_step))
        )
        mean_y = sum(row_cys) / 4
        # Prefer the tightest 4×3 grid; break near-ties toward the bottom (keypad area).
        score = (round(irregularity, 2), -mean_y)
        if best is None or score < best[0]:
            best = (score, flat)
    if best is None:
        return {}
    return {id(region): role for region, role in zip(best[1], KEYPAD_ROLES)}


def _hotspots(regions, outlines, short):
    """Combine colour regions with closed outlines into frames.

    Colour regions are the main source. An outline adds a key the colour pass
    missed (a key the colour of the panel with a rim), or replaces a colour region
    that covers only part of the key it encloses. An outline around several keys
    is a printed group frame and is ignored.
    """
    pads = _one_pad(_pads(outlines, short, _pads(regions, short, [])), regions, short)
    inside_pad = lambda r: any(_within(r, (p.x0, p.y0, p.x1, p.y1)) for p, _ in pads)
    kept = _keys([r for r in regions if not inside_pad(r)], short)
    for outline in _keys([r for r in outlines if not inside_pad(r)], short):
        covered = [k for k in kept if _within(k, (outline.x0, outline.y0, outline.x1, outline.y1))]
        if len(covered) == 1 and outline.filled <= 3 * covered[0].filled:
            outline.contrast = max(outline.contrast, covered[0].contrast)
            kept[kept.index(covered[0])] = outline
        elif not covered and not any(_within(outline, (k.x0, k.y0, k.x1, k.y1)) for k in kept):
            kept.append(outline)
    spots = []
    for region, inner in pads:
        quality = .55 + .4 * min(1., max(0., region.contrast - 1) / 1.5)
        for role, box in _pad_frames(region, inner).items():
            spots.append((box, quality, "rect", role))
        spots.append((inner, quality, "round", "ok"))
    roles = _cross(kept) if not pads else {}
    keypad_roles = _keypad(kept, set(roles))
    roles.update(keypad_roles)
    for region in kept:
        spots.extend(_key_frames(region, short, roles.get(id(region), "")))
    return spots


def _clip(value):
    return max(0., min(.999, float(value)))


def _reading_order(spots):
    remaining = sorted(spots, key=lambda s: s.y + s.height / 2)
    rows = []
    for spot in remaining:
        centre = spot.y + spot.height / 2
        if rows and abs(centre - rows[-1][0]) <= min(spot.height, rows[-1][1]) * .5:
            rows[-1][2].append(spot)
        else:
            rows.append([centre, spot.height, [spot]])
    return [spot for _, _, row in rows for spot in sorted(row, key=lambda s: s.x)]
