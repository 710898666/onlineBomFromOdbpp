import math
import os
import re
import shlex
import tarfile
from collections import defaultdict
from datetime import datetime

from .common import EcadParser, Component, BoundingBox
from ..core.fontparser import FontParser


class OdbPlusPlusParser(EcadParser):
    """Best-effort ODB++ (.tgz/.tar.gz) parser for BOM + placement MVP."""

    DEFAULT_FOOTPRINT_SIZE = 0.08
    DEFAULT_PAD_SIZE = 0.6
    DEFAULT_TRACK_WIDTH = 0.01
    NET_POINT_TOLERANCE = 0.25
    _REF_RE = re.compile(r'^[A-Za-z]{1,8}[0-9][A-Za-z0-9._-]*$')
    _PROPERTY_HINTS = (
        "value", "val", "package", "pkg", "footprint", "mpn", "part",
        "description", "desc",
    )

    def parse(self):
        if not tarfile.is_tarfile(self.file_name):
            self.logger.error("File is not a valid tar archive: %s" %
                              self.file_name)
            return None, None

        with tarfile.open(self.file_name, mode='r:*') as archive:
            file_data = self._read_text_files(archive)
            if not file_data:
                self.logger.error("No readable files were found in archive.")
                return None, None
            self._file_data = file_data
            self._symbol_features_map = self._build_symbol_features_map(file_data)

            pkg_defs = self._parse_eda_packages(file_data)
            components, placements, pads_by_ref, refs_by_pkgid = self._parse_components(
                file_data, pkg_defs)
            pcbdata = self._build_pcbdata(
                file_data, components, placements, pads_by_ref, refs_by_pkgid, pkg_defs)

            if not pcbdata["footprints"]:
                self.logger.error("Failed to parse any ODB++ components.")
                return None, None

            self.logger.info("Parsed ODB++ archive %s", self.file_name)
            return pcbdata, components

    def _read_text_files(self, archive):
        data = {}
        for member in archive.getmembers():
            if not member.isfile():
                continue
            try:
                raw = archive.extractfile(member).read()
            except Exception:
                continue
            text = self._decode_bytes(raw)
            if text is None:
                continue
            data[member.name] = text
        return data

    @staticmethod
    def _decode_bytes(raw):
        for enc in ('utf-8', 'latin-1'):
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return None

    def _parse_components(self, file_data, pkg_defs):
        pad_symbol_map = self._load_pad_symbol_map(file_data)
        candidates = []
        for path in sorted(file_data.keys()):
            lower = path.lower()
            if lower.endswith('/eda/data'):
                candidates.append(path)
            elif lower.endswith('/components') and '/layers/comp_+_' in lower:
                candidates.append(path)

        by_ref = {}
        for path in candidates:
            source_layer = self._layer_from_path(path)
            scale = self._units_scale(file_data[path])
            current_ref = None
            for line in file_data[path].splitlines():
                line = line.strip()
                if not line or line.startswith(('#', '!', '//')):
                    continue

                upper = line.upper()
                comp = None
                if upper.startswith('CMP ') or upper.startswith('COMP '):
                    comp = self._parse_cmp_line(line, source_layer, scale)
                elif path.lower().endswith('/components'):
                    comp = self._parse_component_like_line(line, source_layer, scale)

                if comp:
                    if "pads" not in comp:
                        comp["pads"] = []
                    by_ref[comp["ref"]] = comp
                    current_ref = comp["ref"]
                    continue

                if current_ref is None:
                    continue
                pad = self._parse_component_pad(
                    line, source_layer, scale, pad_symbol_map)
                if pad:
                    by_ref[current_ref]["pads"].append(pad)
                    continue
                self._parse_property_line(line, by_ref[current_ref])

        components = []
        placements = {}
        pads_by_ref = {}
        refs_by_pkgid = defaultdict(list)
        for ref in sorted(by_ref.keys()):
            c = by_ref[ref]
            pkg_id = c.get("pkg_id")
            pkg_def = pkg_defs.get(pkg_id) if pkg_id is not None else None
            if pkg_def and c.get("footprint", "UNKNOWN") == "UNKNOWN":
                c["footprint"] = pkg_def.get("name", "UNKNOWN")
            placements[ref] = {
                "x": c["x"],
                "y": c["y"],
                "angle": c["angle"],
                "pkg_id": c.get("pkg_id"),
            }
            pads = c.get("pads", [])
            if not pads and pkg_def:
                pads = self._pads_from_package(pkg_def, c, placements[ref])
            else:
                pads = self._enrich_component_pads_from_package(
                    pads, pkg_def, c, placements[ref])
            pads_by_ref[ref] = pads
            if pkg_id is not None:
                refs_by_pkgid[pkg_id].append(ref)
            components.append(Component(
                ref=c["ref"],
                val=c["val"],
                footprint=c["footprint"],
                layer=c["layer"],
                extra_fields=c["extra_fields"],
            ))
        return components, placements, pads_by_ref, refs_by_pkgid

    def _parse_cmp_line(self, line, source_layer, scale):
        tokens = self._split_tokens(line)
        if len(tokens) < 2:
            return None

        # Common ODB++ cmp record:
        # CMP <id> <x> <y> <angle> <mirrorflag> <ref> <package> ...
        if (len(tokens) >= 8 and tokens[0].upper() == "CMP" and
                self._to_float(tokens[2]) is not None and
                self._to_float(tokens[3]) is not None):
            x = self._to_float(tokens[2]) * scale
            y = -self._to_float(tokens[3]) * scale
            angle = self._to_float(tokens[4]) or 0.0
            pkg_id = self._safe_int(tokens[1])
            ref = tokens[6]
            if not self._REF_RE.match(ref):
                ref = self._extract_ref(tokens[1:])
            if not ref:
                return None
            footprint = tokens[7] if len(tokens) > 7 else self._extract_footprint(tokens)
            return {
                "ref": ref,
                "x": x,
                "y": y,
                "angle": angle,
                "layer": self._extract_layer(tokens, source_layer),
                "footprint": footprint or "UNKNOWN",
                "val": "",
                "extra_fields": {},
                "pads": [],
                "pkg_id": pkg_id,
            }

        ref = self._extract_ref(tokens[1:])
        if not ref:
            return None

        idx = tokens.index(ref)
        x, y, angle = self._extract_xy_angle(tokens[idx + 1:], scale)
        if x is None or y is None:
            return None

        layer = self._extract_layer(tokens, source_layer)
        footprint = self._extract_footprint(tokens)

        return {
            "ref": ref,
            "x": x,
            "y": y,
            "angle": angle,
            "layer": layer,
            "footprint": footprint or "UNKNOWN",
            "val": "",
            "extra_fields": {},
            "pads": [],
            "pkg_id": None,
        }

    def _parse_component_like_line(self, line, source_layer, scale):
        tokens = self._split_tokens(line)
        if len(tokens) < 3:
            return None

        ref = self._extract_ref(tokens)
        if not ref:
            return None

        idx = tokens.index(ref)
        x, y, angle = self._extract_xy_angle(tokens[idx + 1:], scale)
        if x is None or y is None:
            return None

        return {
            "ref": ref,
            "x": x,
            "y": y,
            "angle": angle,
            "layer": self._extract_layer(tokens, source_layer),
            "footprint": self._extract_footprint(tokens) or "UNKNOWN",
            "val": "",
            "extra_fields": {},
            "pads": [],
            "pkg_id": None,
        }

    def _parse_property_line(self, line, comp):
        key = None
        value = None
        body = line

        if line.upper().startswith('PRP '):
            body = line[4:].strip()

        if '=' in body:
            k, v = body.split('=', 1)
            key = k.strip()
            value = v.strip().strip('"')
        else:
            tokens = self._split_tokens(body)
            if len(tokens) >= 2:
                key = tokens[0].strip()
                value = " ".join(tokens[1:]).strip().strip('"')

        if not key or value is None:
            return

        low = key.lower()
        if low in ("value", "val"):
            comp["val"] = value
        elif low in ("package", "pkg", "footprint"):
            comp["footprint"] = value
        elif low in self._PROPERTY_HINTS:
            comp["extra_fields"][key] = value
        elif len(key) < 32:
            comp["extra_fields"][key] = value

        # Fallbacks for packages that do not expose VALUE explicitly.
        if not comp["val"]:
            for hint in ("part_no", "devicename", "part label", "type"):
                if low == hint:
                    comp["val"] = value
                    break

    def _parse_eda_packages(self, file_data):
        eda_path = None
        for path in sorted(file_data.keys()):
            if path.lower().endswith("/eda/data"):
                eda_path = path
                break
        if not eda_path:
            return {}

        lines = file_data[eda_path].splitlines()
        scale = self._units_scale(file_data[eda_path])
        pkg_defs = {}
        pkg_index = -1
        pkg = None
        current_pin = None
        pin_geom_pending = False
        contour = None
        contour_target = None

        def finish_contour():
            nonlocal contour, contour_target, current_pin, pin_geom_pending
            if contour is None:
                return
            if len(contour) >= 3:
                if contour_target == "pin" and current_pin is not None:
                    current_pin.setdefault("polygons", []).append(contour)
                    pin_geom_pending = False
                elif pkg is not None:
                    pkg.setdefault("drawings", []).append({
                        "type": "polygon",
                        "filled": 0,
                        "width": 0.1,
                        "polygons": [contour],
                        "pos": [0, 0],
                        "angle": 0,
                    })
            contour = None
            contour_target = None

        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                m = re.match(r"#\s*PKG\s+(\d+)", line, re.I)
                if m:
                    finish_contour()
                    pkg_index = int(m.group(1))
                    current_pin = None
                    pin_geom_pending = False
                    pkg = None
                continue
            tokens = line.split()
            if not tokens:
                continue
            op = tokens[0].upper()
            if op == "PKG":
                finish_contour()
                pkg_index += 1
                current_pin = None
                pin_geom_pending = False
                name = tokens[1] if len(tokens) > 1 else "PKG_{}".format(pkg_index)
                pkg = {"id": pkg_index, "name": name, "pins": [], "drawings": []}
                pkg_defs[pkg_index] = pkg
                continue
            if pkg is None:
                continue
            if op == "PIN":
                finish_contour()
                current_pin = {
                    "name": tokens[1] if len(tokens) > 1 else "",
                    "kind": tokens[2] if len(tokens) > 2 else "S",
                    "x": (self._to_float(tokens[3]) or 0.0) * scale if len(tokens) > 3 else 0.0,
                    "y": -(self._to_float(tokens[4]) or 0.0) * scale if len(tokens) > 4 else 0.0,
                    "angle": self._to_float(tokens[5]) or 0.0 if len(tokens) > 5 else 0.0,
                }
                pkg["pins"].append(current_pin)
                pin_geom_pending = True
                continue
            if op == "CT":
                finish_contour()
                contour = None
                contour_target = "pin" if pin_geom_pending and current_pin is not None else "pkg"
                continue
            if op == "CE":
                finish_contour()
                continue
            if op == "OB":
                x, y = self._read_xy(tokens[1:], scale) or (None, None)
                if x is None:
                    continue
                contour = [[x, y]]
                continue
            if op == "OS":
                if contour is None:
                    continue
                x, y = self._read_xy(tokens[1:], scale) or (None, None)
                if x is None:
                    continue
                contour.append([x, y])
                continue
            if op == "OC":
                # Keep contour connectivity; approximate arc with end point.
                if contour is None:
                    continue
                x, y = self._read_xy(tokens[1:], scale) or (None, None)
                if x is None:
                    continue
                contour.append([x, y])
                continue
            if op == "OE":
                if contour and contour[0] != contour[-1]:
                    contour.append(contour[0])
                continue
            if op == "CR":
                nums = self._extract_numbers(tokens[1:])
                if len(nums) < 3:
                    continue
                cx = nums[0] * scale
                cy = -nums[1] * scale
                r = abs(nums[2]) * scale
                if current_pin is not None:
                    current_pin["shape"] = "circle"
                    current_pin["size"] = [max(2 * r, 0.08), max(2 * r, 0.08)]
                    pin_geom_pending = False
                else:
                    pkg["drawings"].append({
                        "type": "circle",
                        "start": [cx, cy],
                        "radius": r,
                        "width": 0.001,
                        "filled": 1,
                    })
                continue
            if op == "RC":
                nums = self._extract_numbers(tokens[1:])
                if len(nums) < 4:
                    continue
                x = nums[0] * scale
                y = -nums[1] * scale
                w = abs(nums[2] * scale)
                h = abs(nums[3] * scale)
                if current_pin is not None:
                    current_pin["shape"] = "rect"
                    current_pin["size"] = [max(w, 0.08), max(h, 0.08)]
                    pin_geom_pending = False
                else:
                    pkg["drawings"].append({
                        "type": "rect",
                        "start": [x, y],
                        "end": [x + w, y - h],
                        "width": 0.001,
                    })
                continue
        finish_contour()
        return pkg_defs

    def _pads_from_package(self, pkg_def, comp_info, placement):
        pads = []
        if not pkg_def:
            return pads
        for pin in pkg_def.get("pins", []):
            px, py = self._local_to_board(
                pin.get("x", 0.0), pin.get("y", 0.0),
                placement.get("x", 0.0), placement.get("y", 0.0),
                placement.get("angle", 0.0))
            pad = {
                "layers": [comp_info.get("layer", "F")],
                "pos": [px, py],
                "size": pin.get("size", [self.DEFAULT_PAD_SIZE, self.DEFAULT_PAD_SIZE]),
                "angle": (pin.get("angle", 0.0) + placement.get("angle", 0.0)) % 360,
                "shape": pin.get("shape", "custom" if pin.get("polygons") else "rect"),
                "type": "th" if pin.get("kind", "S").upper() == "T" else "smd",
                "name": pin.get("name", ""),
            }
            if pin.get("name", "") == "1":
                pad["pin1"] = 1
            if pin.get("polygons"):
                pad["shape"] = "custom"
                pad["polygons"] = self._to_pad_local_polygons(
                    pin["polygons"],
                    pin.get("x", 0.0),
                    pin.get("y", 0.0),
                    pin.get("angle", 0.0),
                )
                _, _, sx, sy = self._polygons_bbox_center_size(pad["polygons"])
                pad["size"] = [max(sx, 0.08), max(sy, 0.08)]
            pads.append(pad)
        return pads

    def _enrich_component_pads_from_package(self, pads, pkg_def, comp_info, placement):
        if not pkg_def or not pads:
            return pads
        pins_by_name = {p.get("name"): p for p in pkg_def.get("pins", []) if p.get("name")}
        out = []
        for pad in pads:
            pin_name = pad.get("name")
            pin = pins_by_name.get(pin_name)
            if pin:
                if pad.get("shape") in ("rect", None) and pin.get("shape"):
                    pad["shape"] = pin.get("shape")
                if "size" not in pad and pin.get("size"):
                    pad["size"] = pin["size"]
                if pin.get("polygons"):
                    pad["shape"] = "custom"
                    pad["polygons"] = self._to_pad_local_polygons(
                        pin["polygons"],
                        pin.get("x", 0.0),
                        pin.get("y", 0.0),
                        pin.get("angle", 0.0),
                    )
                    _, _, sx, sy = self._polygons_bbox_center_size(pad["polygons"])
                    pad["size"] = [max(sx, 0.08), max(sy, 0.08)]
            out.append(pad)
        return out

    def _package_drawings_to_footprint(self, pkg_def, cx, cy, angle, layer):
        if not pkg_def:
            return []
        drawings = []
        for d in pkg_def.get("drawings", []):
            dt = d.get("type")
            if dt == "polygon":
                # Package polygons in ODB++ data are often placement/courtyard
                # envelopes and look like oversized response boxes in iBOM.
                # Skip them to avoid clutter and misleading outlines.
                continue
            elif dt == "circle":
                px, py = self._local_to_board(d["start"][0], d["start"][1], cx, cy, angle)
                drawings.append({
                    "layer": layer,
                    "drawing": {
                        "type": "circle",
                        "start": [px, py],
                        "radius": d.get("radius", 0.0),
                        "filled": d.get("filled", 0),
                        "width": d.get("width", 0.001),
                    },
                })
            elif dt == "rect":
                x1, y1 = self._local_to_board(d["start"][0], d["start"][1], cx, cy, angle)
                x2, y2 = self._local_to_board(d["end"][0], d["end"][1], cx, cy, angle)
                drawings.append({
                    "layer": layer,
                    "drawing": {
                        "type": "rect",
                        "start": [x1, y1],
                        "end": [x2, y2],
                        "width": d.get("width", 0.001),
                    },
                })
        return drawings

    @staticmethod
    def _local_to_board(lx, ly, cx, cy, angle):
        rad = math.radians(angle or 0.0)
        ca = math.cos(rad)
        sa = math.sin(rad)
        return cx + lx * ca - ly * sa, cy + lx * sa + ly * ca

    def _transform_polygons(self, polygons, cx, cy, angle):
        out = []
        for poly in polygons:
            pts = []
            for x, y in poly:
                px, py = self._local_to_board(x, y, cx, cy, angle)
                pts.append([px, py])
            out.append(pts)
        return out

    def _to_pad_local_polygons(self, polygons, pinx, piny, pinangle):
        # Convert package-local polygons to pad-local polygons.
        # Rendering applies pad pos+angle transform later.
        out = []
        rad = math.radians(-(pinangle or 0.0))
        ca = math.cos(rad)
        sa = math.sin(rad)
        for poly in polygons:
            pts = []
            for x, y in poly:
                dx = x - pinx
                dy = y - piny
                lx = dx * ca - dy * sa
                ly = dx * sa + dy * ca
                pts.append([lx, ly])
            out.append(pts)
        return out

    @staticmethod
    def _polygons_bbox_center_size(polygons):
        xs = []
        ys = []
        for poly in polygons:
            for x, y in poly:
                xs.append(x)
                ys.append(y)
        if not xs:
            return 0.0, 0.0, 0.0, 0.0
        minx = min(xs)
        maxx = max(xs)
        miny = min(ys)
        maxy = max(ys)
        return (minx + maxx) / 2, (miny + maxy) / 2, (maxx - minx), (maxy - miny)

    def _build_pcbdata(self, file_data, components, placements, pads_by_ref,
                       refs_by_pkgid, pkg_defs):
        edges = self._parse_edges(file_data)
        footprints = []
        silk_f, silk_b = self._parse_layer_drawings(
            file_data, layer_type="silkscreen")
        fab_f, fab_b = self._parse_layer_drawings(
            file_data, layer_type="fabrication")
        font = FontParser()
        extent = BoundingBox()

        for comp in components:
            pkg_id = placements.get(comp.ref, {}).get("pkg_id")
            pkg_def = pkg_defs.get(pkg_id) if pkg_id is not None else None
            geom = self._component_footprint(
                comp, placements, pads_by_ref, pkg_def)
            footprints.append(geom)

            # Place very small reference text near footprint edge.
            b = geom["bbox"]
            max_side = max(b["size"][0], b["size"][1])
            text_size = max(min(max_side * 0.22, 0.028), 0.010)
            text = {
                "type": "text",
                "text": comp.ref,
                "pos": [
                    geom["center"][0] + b["size"][0] * 0.35,
                    geom["center"][1] - b["size"][1] * 0.35,
                ],
                "height": text_size,
                "width": text_size,
                "justify": [0, 0],
                "thickness": max(text_size * 0.14, 0.0025),
                "attr": [] if comp.layer == "F" else ["mirrored"],
                "angle": 0,
                "ref": 1,
            }
            target = silk_f if comp.layer == "F" else silk_b
            target.append(text)
            font.parse_font_for_string(comp.ref)

            bbox = geom["bbox"]
            extent.add_point(
                bbox["pos"][0] + bbox["relpos"][0],
                bbox["pos"][1] + bbox["relpos"][1],
            )
            extent.add_point(
                bbox["pos"][0] + bbox["relpos"][0] + bbox["size"][0],
                bbox["pos"][1] + bbox["relpos"][1] + bbox["size"][1],
            )

        edge_bbox = BoundingBox()
        for drawing in edges:
            self.add_drawing_bounding_box(drawing, edge_bbox)

        if edge_bbox.initialized():
            bbox = edge_bbox.to_dict()
        elif extent.initialized():
            extent.pad(2.0)
            bbox = extent.to_dict()
        else:
            bbox = {"minx": 0, "miny": 0, "maxx": 100, "maxy": 100}

        if not edges and extent.initialized():
            edges = self._bbox_to_edges(bbox)

        title = os.path.splitext(os.path.basename(self.file_name))[0]
        file_mtime = os.path.getmtime(self.file_name)
        file_date = datetime.fromtimestamp(file_mtime).strftime(
            '%Y-%m-%d %H:%M:%S')

        pcbdata = {
            "edges_bbox": bbox,
            "edges": edges,
            "drawings": {
                "silkscreen": {"F": silk_f, "B": silk_b},
                "fabrication": {"F": fab_f, "B": fab_b},
            },
            "footprints": footprints,
            "metadata": {
                "title": title,
                "revision": "",
                "company": "",
                "date": file_date,
            },
            "bom": {},
            "font_data": font.get_parsed_font(),
        }

        if self.config.include_tracks:
            tracks = self._parse_tracks(file_data)
            zones = self._parse_zones(file_data)
            pcbdata["tracks"] = tracks
            pcbdata["zones"] = zones
            if self.config.include_nets:
                net_names, net_points = self._parse_nets(file_data)
                self._assign_pad_nets(pcbdata["footprints"], net_points, net_names)
                self._assign_track_nets(pcbdata["tracks"], net_points, net_names)
                self._assign_zone_nets(pcbdata["zones"], net_points, net_names)
                pcbdata["nets"] = net_names

        self._normalize_units_if_needed(pcbdata)
        return pcbdata

    def _normalize_units_if_needed(self, pcbdata):
        bbox = pcbdata.get("edges_bbox") or {}
        bw = abs((bbox.get("maxx") or 0.0) - (bbox.get("minx") or 0.0))
        bh = abs((bbox.get("maxy") or 0.0) - (bbox.get("miny") or 0.0))
        max_dim = max(bw, bh)

        max_pad_dim = 0.0
        for fp in pcbdata.get("footprints", []):
            for pad in fp.get("pads", []):
                size = pad.get("size")
                if isinstance(size, list) and len(size) >= 2:
                    max_pad_dim = max(max_pad_dim, abs(size[0]), abs(size[1]))

        # Some ODB++ exports omit UNITS and provide inch coordinates. In those
        # files board geometry appears tiny while pads are disproportionately large.
        if max_dim >= 5.0 or max_pad_dim <= 1.0:
            return
        self._scale_pcbdata(pcbdata, 25.4)

    def _scale_pcbdata(self, pcbdata, scale):
        bbox = pcbdata.get("edges_bbox")
        if isinstance(bbox, dict):
            for k in ("minx", "miny", "maxx", "maxy"):
                if k in bbox:
                    bbox[k] *= scale

        for edge in pcbdata.get("edges", []):
            self._scale_shape(edge, scale)

        drawings = pcbdata.get("drawings", {})
        for drawing_type in ("silkscreen", "fabrication"):
            layer_map = drawings.get(drawing_type, {})
            for layer in ("F", "B"):
                for d in layer_map.get(layer, []):
                    self._scale_shape(d, scale)

        for fp in pcbdata.get("footprints", []):
            center = fp.get("center")
            if isinstance(center, list) and len(center) >= 2:
                center[0] *= scale
                center[1] *= scale
            for pad in fp.get("pads", []):
                self._scale_point_pair(pad.get("pos"), scale)
                self._scale_point_pair(pad.get("offset"), scale)
            for d in fp.get("drawings", []):
                if isinstance(d, dict):
                    self._scale_shape(d.get("drawing", d), scale)
            self._recompute_footprint_bbox(fp)

        tracks = pcbdata.get("tracks", {})
        for layer in ("F", "B"):
            for tr in tracks.get(layer, []):
                self._scale_shape(tr, scale)

        zones = pcbdata.get("zones", {})
        for layer in ("F", "B"):
            for z in zones.get(layer, []):
                polys = z.get("polygons")
                if isinstance(polys, list):
                    self._scale_polygons(polys, scale)

    @staticmethod
    def _scale_point_pair(v, scale):
        if isinstance(v, list) and len(v) >= 2:
            if isinstance(v[0], (int, float)):
                v[0] *= scale
            if isinstance(v[1], (int, float)):
                v[1] *= scale

    def _scale_polygons(self, polygons, scale):
        for poly in polygons:
            if not isinstance(poly, list):
                continue
            for pt in poly:
                self._scale_point_pair(pt, scale)

    def _scale_shape(self, shape, scale):
        if not isinstance(shape, dict):
            return
        self._scale_point_pair(shape.get("pos"), scale)
        self._scale_point_pair(shape.get("start"), scale)
        self._scale_point_pair(shape.get("end"), scale)
        self._scale_point_pair(shape.get("center"), scale)
        self._scale_point_pair(shape.get("cpa"), scale)
        self._scale_point_pair(shape.get("cpb"), scale)
        self._scale_point_pair(shape.get("size"), scale)
        if "radius" in shape and isinstance(shape["radius"], (int, float)):
            shape["radius"] *= scale
        if "drillsize" in shape:
            self._scale_point_pair(shape["drillsize"], scale)
        polys = shape.get("polygons")
        if isinstance(polys, list):
            self._scale_polygons(polys, scale)

    def _recompute_footprint_bbox(self, fp):
        pads = fp.get("pads", [])
        center = fp.get("center")
        if not isinstance(center, list) or len(center) < 2:
            return
        cx, cy = center[0], center[1]
        if not pads:
            return
        bbox_calc = BoundingBox()
        for pad in pads:
            pos = pad.get("pos")
            size = pad.get("size")
            if not (isinstance(pos, list) and len(pos) >= 2 and isinstance(size, list) and len(size) >= 2):
                continue
            w, h = size[0], size[1]
            bbox_calc.add_rectangle(pos[0], pos[1], w, h, pad.get("angle", 0.0))
        if not bbox_calc.initialized():
            return
        bbox_calc.pad(self.DEFAULT_PAD_SIZE * 0.25)
        raw = bbox_calc.to_dict()
        fp["bbox"] = {
            "pos": [cx, cy],
            "angle": 0,
            "relpos": [raw["minx"] - cx, raw["miny"] - cy],
            "size": [raw["maxx"] - raw["minx"], raw["maxy"] - raw["miny"]],
        }

    def _parse_tracks(self, file_data):
        top_signal, bot_signal = self._get_outer_signal_layers(file_data)
        tracks = {"F": [], "B": []}
        if not top_signal or not bot_signal:
            return tracks

        for path in sorted(file_data.keys()):
            lower = path.lower()
            if not lower.endswith('/features') or '/layers/' not in lower:
                continue
            layer_name = lower.split('/layers/', 1)[1].split('/', 1)[0]
            if layer_name == top_signal.lower():
                tracks["F"].extend(self._parse_track_features(file_data[path]))
            elif layer_name == bot_signal.lower():
                tracks["B"].extend(self._parse_track_features(file_data[path]))
        return tracks

    def _get_outer_signal_layers(self, file_data):
        matrix_path = None
        for path in file_data.keys():
            if path.lower().endswith('/matrix/matrix'):
                matrix_path = path
                break
        if not matrix_path:
            return "layer_1", "layer_2"

        text = file_data[matrix_path]
        signal_layers = []
        current_type = None
        current_name = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("LAYER {"):
                current_type = None
                current_name = None
            elif line.startswith("TYPE="):
                current_type = line.split("=", 1)[1].strip()
            elif line.startswith("NAME="):
                current_name = line.split("=", 1)[1].strip()
            elif line == "}":
                if current_type == "SIGNAL" and current_name:
                    signal_layers.append(current_name)
        if not signal_layers:
            return "layer_1", "layer_2"
        return signal_layers[0], signal_layers[-1]

    def _parse_track_features(self, text):
        scale = self._units_scale(text)
        symbols = self._parse_feature_symbols(text)
        tracks = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(('#', '$', '@', '!', '&')):
                continue
            tokens = line.split()
            if not tokens:
                continue
            op = tokens[0].upper()
            if op == "L":
                nums = self._extract_numbers(tokens[1:5])
                if len(nums) < 4:
                    continue
                sym_id = self._safe_int(tokens[5]) if len(tokens) > 5 else None
                width = self._symbol_width(symbols, sym_id)
                tracks.append({
                    "start": [nums[0] * scale, -nums[1] * scale],
                    "end": [nums[2] * scale, -nums[3] * scale],
                    "width": width,
                })
            elif op == "A":
                nums = self._extract_numbers(tokens[1:7])
                if len(nums) < 6:
                    continue
                sym_id = self._safe_int(tokens[7]) if len(tokens) > 7 else None
                width = self._symbol_width(symbols, sym_id)
                sx = nums[0] * scale
                sy = -nums[1] * scale
                ex = nums[2] * scale
                ey = -nums[3] * scale
                cx = nums[4] * scale
                cy = -nums[5] * scale
                arc = self._arc_from_points(sx, sy, ex, ey, cx, cy)
                if arc:
                    arc["width"] = width
                    tracks.append(arc)
        return tracks

    def _parse_zones(self, file_data):
        top_signal, bot_signal = self._get_outer_signal_layers(file_data)
        zones = {"F": [], "B": []}
        if not top_signal or not bot_signal:
            return zones

        for path in sorted(file_data.keys()):
            lower = path.lower()
            if not lower.endswith("/features") or "/layers/" not in lower:
                continue
            layer_name = lower.split('/layers/', 1)[1].split('/', 1)[0]
            if layer_name == top_signal.lower():
                zones["F"].extend(self._parse_zone_features(file_data[path]))
            elif layer_name == bot_signal.lower():
                zones["B"].extend(self._parse_zone_features(file_data[path]))
        return zones

    def _parse_zone_features(self, text):
        scale = self._units_scale(text)
        raw_polys = self._parse_surface_polygons(text, scale)
        zones = []
        for polys in raw_polys:
            if not polys:
                continue
            zones.append({
                "polygons": polys,
                "fillrule": "evenodd",
            })
        return zones

    def _parse_surface_polygons(self, text, scale, as_relative=False):
        zones = []
        in_surface = False
        current_zone = []
        contour = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(('#', '$', '@', '!', '&')):
                continue
            tokens = line.split()
            op = tokens[0].upper()
            if op == "S":
                in_surface = True
                current_zone = []
                contour = None
                continue
            if op == "SE":
                if in_surface and current_zone:
                    zones.append(current_zone)
                in_surface = False
                current_zone = []
                contour = None
                continue
            if not in_surface:
                continue
            if op == "OB":
                pt = self._read_xy(tokens[1:], scale)
                if pt is None:
                    continue
                contour = [[pt[0], pt[1]]]
                continue
            if op == "OS":
                if contour is None:
                    continue
                pt = self._read_xy(tokens[1:], scale)
                if pt is None:
                    continue
                contour.append([pt[0], pt[1]])
                continue
            if op == "OC":
                # Approximate contour arc by its endpoint for now.
                if contour is None:
                    continue
                pt = self._read_xy(tokens[1:], scale)
                if pt is None:
                    continue
                contour.append([pt[0], pt[1]])
                continue
            if op == "OE":
                if contour and len(contour) >= 3:
                    if contour[0] != contour[-1]:
                        contour.append(contour[0])
                    current_zone.append(contour)
                contour = None
        if as_relative:
            return zones
        return zones

    def _build_symbol_features_map(self, file_data):
        sym = {}
        for path in file_data.keys():
            lower = path.lower()
            if "/symbols/" not in lower or not lower.endswith("/features"):
                continue
            name = path.split("/symbols/", 1)[1].split("/", 1)[0]
            sym[name.lower()] = path
        return sym

    def _parse_layer_drawings(self, file_data, layer_type="silkscreen"):
        front = []
        back = []
        for path in sorted(file_data.keys()):
            lower = path.lower()
            if "/layers/" not in lower or not lower.endswith("/features"):
                continue
            layer_name = lower.split("/layers/", 1)[1].split("/", 1)[0]
            target = None
            if layer_type == "silkscreen":
                if "overlay" in layer_name or "silk" in layer_name or layer_name in ("sst", "ssb"):
                    target = front if any(k in layer_name for k in ("top", "_1", "_+_", "sst")) else back
                    if "bottom" in layer_name or layer_name == "ssb":
                        target = back
            elif layer_type == "fabrication":
                if "assembly" in layer_name or layer_name in ("assemt", "assemb"):
                    target = front if "top" in layer_name or layer_name == "assemt" else back
            if target is None:
                continue
            target.extend(self._parse_features(file_data[path]))
        return front, back

    def _parse_nets(self, file_data):
        netlist_path = None
        for path in sorted(file_data.keys()):
            if path.lower().endswith("/netlists/cadnet/netlist"):
                netlist_path = path
                break
        if not netlist_path:
            return [], []
        text = file_data[netlist_path]
        scale = self._units_scale(text)
        net_names = []
        net_lookup = {}
        points = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('$'):
                toks = line.split(maxsplit=1)
                nid = self._safe_int(toks[0][1:])
                if nid is None:
                    continue
                name = toks[1].strip() if len(toks) > 1 else ("NET_{}".format(nid))
                if name not in net_names:
                    net_names.append(name)
                net_lookup[nid] = name
                continue
            toks = line.split()
            if not toks or not toks[0].isdigit():
                continue
            nid = self._safe_int(toks[0])
            if nid is None or nid not in net_lookup:
                continue
            if len(toks) < 5:
                continue
            x = self._to_float(toks[2])
            y = self._to_float(toks[3])
            if x is None or y is None:
                continue
            layer_tok = toks[4].upper()
            layer = "F"
            if layer_tok == "B":
                layer = "B"
            elif layer_tok == "D":
                layer = "FB"
            points.append({
                "x": x * scale,
                "y": -y * scale,
                "layer": layer,
                "net": net_lookup[nid],
            })
        return net_names, points

    def _assign_pad_nets(self, footprints, net_points, net_names):
        if not net_points:
            return
        for fp in footprints:
            fplayer = fp.get("layer", "F")
            for pad in fp.get("pads", []):
                if pad.get("net"):
                    continue
                layer = "F" if "F" in pad.get("layers", []) else "B"
                net = self._nearest_net_for_point(
                    pad["pos"][0], pad["pos"][1], layer, net_points)
                if net:
                    pad["net"] = net
                    if net not in net_names:
                        net_names.append(net)

    def _assign_track_nets(self, tracks, net_points, net_names):
        if not net_points:
            return
        for layer in ("F", "B"):
            for t in tracks.get(layer, []):
                if t.get("net"):
                    continue
                sx, sy = t.get("start", [None, None])
                ex, ey = t.get("end", [None, None])
                if sx is None or ex is None:
                    continue
                tol = max(self.NET_POINT_TOLERANCE, (t.get("width", self.DEFAULT_TRACK_WIDTH) or 0) * 1.2)
                net = self._nearest_net_for_segment(sx, sy, ex, ey, layer, net_points, tol)
                if net:
                    t["net"] = net
                    if net not in net_names:
                        net_names.append(net)

    def _assign_zone_nets(self, zones, net_points, net_names):
        if not net_points:
            return
        for layer in ("F", "B"):
            for z in zones.get(layer, []):
                if z.get("net"):
                    continue
                polys = z.get("polygons", [])
                if not polys or not polys[0]:
                    continue
                x, y = polys[0][0]
                net = self._nearest_net_for_point(x, y, layer, net_points)
                if net:
                    z["net"] = net
                    if net not in net_names:
                        net_names.append(net)

    def _nearest_net_for_point(self, x, y, layer, net_points):
        best = None
        best_d2 = self.NET_POINT_TOLERANCE * self.NET_POINT_TOLERANCE
        for p in net_points:
            if p["layer"] != "FB" and p["layer"] != layer:
                continue
            dx = p["x"] - x
            dy = p["y"] - y
            d2 = dx * dx + dy * dy
            if d2 <= best_d2:
                best_d2 = d2
                best = p["net"]
        return best

    def _nearest_net_for_segment(self, x1, y1, x2, y2, layer, net_points, tol):
        best = None
        best_d2 = tol * tol
        for p in net_points:
            if p["layer"] != "FB" and p["layer"] != layer:
                continue
            d2 = self._point_to_segment_distance2(p["x"], p["y"], x1, y1, x2, y2)
            if d2 <= best_d2:
                best_d2 = d2
                best = p["net"]
        return best

    @staticmethod
    def _point_to_segment_distance2(px, py, x1, y1, x2, y2):
        vx = x2 - x1
        vy = y2 - y1
        wx = px - x1
        wy = py - y1
        c1 = vx * wx + vy * wy
        if c1 <= 0:
            dx = px - x1
            dy = py - y1
            return dx * dx + dy * dy
        c2 = vx * vx + vy * vy
        if c2 <= 0:
            dx = px - x1
            dy = py - y1
            return dx * dx + dy * dy
        if c1 >= c2:
            dx = px - x2
            dy = py - y2
            return dx * dx + dy * dy
        b = c1 / c2
        bx = x1 + b * vx
        by = y1 + b * vy
        dx = px - bx
        dy = py - by
        return dx * dx + dy * dy

    def _parse_feature_symbols(self, text):
        symbols = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("$"):
                continue
            tokens = line.split()
            if len(tokens) < 2:
                continue
            sym_id = self._safe_int(tokens[0][1:])
            if sym_id is None:
                continue
            symbols[sym_id] = tokens[1]
        return symbols

    def _symbol_width(self, symbols, sym_id):
        MIL_TO_MM = 0.0254
        if sym_id is None:
            return self.DEFAULT_TRACK_WIDTH
        name = symbols.get(sym_id, "")
        if name.startswith("r"):
            value = self._to_float(name[1:])
            if value is not None:
                return max(value * MIL_TO_MM, 0.02)
        return self.DEFAULT_TRACK_WIDTH

    @staticmethod
    def _safe_int(value):
        try:
            return int(value)
        except Exception:
            return None

    def _parse_edges(self, file_data):
        edges = []
        for path in sorted(file_data.keys()):
            lower = path.lower()
            if lower.endswith('/profile'):
                edges.extend(self._parse_profile(file_data[path]))
                continue

            if not lower.endswith('/features'):
                continue

            if '/layers/' not in lower:
                continue

            layer_name = lower.split('/layers/', 1)[1].split('/', 1)[0]
            if not any(k in layer_name for k in
                       ('outline', 'profile', 'dimension', 'board')):
                continue
            edges.extend(self._parse_features(file_data[path]))

        return edges

    def _parse_profile(self, text):
        scale = self._units_scale(text)
        drawings = []
        first = None
        current = None

        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            tokens = line.split()
            if not tokens:
                continue
            code = tokens[0].upper()

            if code in ('OB', 'OI'):
                pt = self._read_xy(tokens[1:], scale)
                if pt is None:
                    continue
                first = pt
                current = pt
            elif code == 'OS':
                pt = self._read_xy(tokens[1:], scale)
                if pt is None or current is None:
                    continue
                drawings.append({
                    "type": "segment",
                    "start": [current[0], current[1]],
                    "end": [pt[0], pt[1]],
                    "width": 0.1,
                })
                current = pt
            elif code == 'OE':
                if first is not None and current is not None and first != current:
                    drawings.append({
                        "type": "segment",
                        "start": [current[0], current[1]],
                        "end": [first[0], first[1]],
                        "width": 0.1,
                    })
                first = None
                current = None

        return drawings

    def _parse_features(self, text):
        scale = self._units_scale(text)
        drawings = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(('#', '$', '@', '!')):
                continue
            tokens = line.split()
            if not tokens:
                continue
            op = tokens[0].upper()
            if op == 'L':
                nums = self._extract_numbers(tokens[1:])
                if len(nums) < 4:
                    continue
                drawings.append({
                    "type": "segment",
                    "start": [nums[0] * scale, -nums[1] * scale],
                    "end": [nums[2] * scale, -nums[3] * scale],
                    "width": 0.1,
                })
            elif op == 'A':
                nums = self._extract_numbers(tokens[1:])
                if len(nums) < 6:
                    continue
                sx = nums[0] * scale
                sy = -nums[1] * scale
                ex = nums[2] * scale
                ey = -nums[3] * scale
                cx = nums[4] * scale
                cy = -nums[5] * scale
                arc = self._arc_from_points(sx, sy, ex, ey, cx, cy)
                if arc:
                    drawings.append(arc)
        for polys in self._parse_surface_polygons(text, scale):
            if not polys:
                continue
            drawings.append({
                "type": "polygon",
                "filled": 1,
                "polygons": polys,
                "pos": [0, 0],
                "angle": 0,
            })
        return drawings

    @staticmethod
    def _arc_from_points(sx, sy, ex, ey, cx, cy):
        r = math.hypot(sx - cx, sy - cy)
        if r <= 0:
            return None
        a0 = math.degrees(math.atan2(sy - cy, sx - cx))
        a1 = math.degrees(math.atan2(ey - cy, ex - cx))
        if a0 < 0:
            a0 += 360
        if a1 < 0:
            a1 += 360
        return {
            "type": "arc",
            "start": [cx, cy],
            "radius": r,
            "startangle": a0,
            "endangle": a1,
            "width": 0.1,
        }

    def _component_footprint(self, comp, placements, pads_by_ref, pkg_def=None):
        p = placements.get(comp.ref, {})
        x = p.get("x", 0.0)
        y = p.get("y", 0.0)
        angle = p.get("angle", 0.0)

        pads = pads_by_ref.get(comp.ref, [])
        if not pads:
            w = self.DEFAULT_FOOTPRINT_SIZE
            h = self.DEFAULT_FOOTPRINT_SIZE
            bbox = {
                "pos": [x, y],
                "angle": angle,
                "relpos": [-w / 2, -h / 2],
                "size": [w, h],
            }
            return {
                "ref": comp.ref,
                "center": [x, y],
                "bbox": bbox,
                "pads": [],
                "drawings": self._package_drawings_to_footprint(pkg_def, x, y, angle, comp.layer),
                "layer": comp.layer,
            }

        bbox_calc = BoundingBox()
        for pad in pads:
            w, h = pad["size"][0], pad["size"][1]
            bbox_calc.add_rectangle(
                pad["pos"][0], pad["pos"][1], w, h, pad.get("angle", 0.0))
        bbox_calc.pad(self.DEFAULT_PAD_SIZE * 0.25)
        bbox_raw = bbox_calc.to_dict()
        bbox = {
            "pos": [x, y],
            "angle": 0,
            "relpos": [bbox_raw["minx"] - x, bbox_raw["miny"] - y],
            "size": [
                bbox_raw["maxx"] - bbox_raw["minx"],
                bbox_raw["maxy"] - bbox_raw["miny"],
            ],
        }

        return {
            "ref": comp.ref,
            "center": [x, y],
            "bbox": bbox,
            "pads": pads,
            "drawings": self._package_drawings_to_footprint(pkg_def, x, y, angle, comp.layer),
            "layer": comp.layer,
        }

    def _parse_component_pad(self, line, layer_hint, scale, pad_symbol_map):
        tokens = self._split_tokens(line)
        if len(tokens) < 7:
            return None
        kind = tokens[0].upper()
        if kind not in ("TOP", "BOT"):
            return None
        x = self._to_float(tokens[2])
        y = self._to_float(tokens[3])
        if x is None or y is None:
            return None
        angle = self._to_float(tokens[4]) if len(tokens) > 4 else 0.0
        if angle is None:
            angle = 0.0
        symbol_id = self._safe_int(tokens[6]) if len(tokens) > 6 else None
        pin_num = tokens[-1] if len(tokens) > 1 else ""
        if kind == "TOP":
            layers = ['F']
            symbol_layer = "F"
        elif kind == "BOT":
            layers = ['B']
            symbol_layer = "B"
        else:
            layers = ['F'] if layer_hint == 'F' else ['B']
            symbol_layer = layer_hint
        geom = self._pad_geometry_from_symbol(symbol_layer, symbol_id, pad_symbol_map)
        pad = {
            "layers": layers,
            "pos": [x * scale, -y * scale],
            "size": geom.get("size", [self.DEFAULT_PAD_SIZE, self.DEFAULT_PAD_SIZE]),
            "angle": angle,
            "shape": geom.get("shape", "rect"),
            "type": "smd",
            "name": pin_num,
            "symbol_id": symbol_id,
        }
        if "radius" in geom:
            pad["radius"] = geom["radius"]
        if "polygons" in geom:
            # Custom pad polygons must stay in pad-local coordinates.
            pad["polygons"] = geom["polygons"]
            _, _, sx, sy = self._polygons_bbox_center_size(pad["polygons"])
            pad["size"] = [max(sx, 0.08), max(sy, 0.08)]
        if pin_num == "1":
            pad["pin1"] = 1
        return pad

    def _load_pad_symbol_map(self, file_data):
        top_signal, bot_signal = self._get_outer_signal_layers(file_data)
        maps = {"F": {}, "B": {}}
        for path, layer_key in ((top_signal, "F"), (bot_signal, "B")):
            p = self._find_layer_features_path(file_data, path)
            if not p:
                continue
            maps[layer_key] = self._parse_pad_symbols(file_data[p])
        return maps

    @staticmethod
    def _find_layer_features_path(file_data, layer_name):
        suffix = "/layers/{}/features".format(layer_name.lower())
        for p in file_data.keys():
            if p.lower().endswith(suffix):
                return p
        return None

    def _parse_pad_symbols(self, text):
        symbols = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("$"):
                continue
            tokens = line.split()
            if len(tokens) < 2:
                continue
            sid = self._safe_int(tokens[0][1:])
            if sid is None:
                continue
            symbols[sid] = tokens[1]
        return symbols

    def _pad_geometry_from_symbol(self, layer_hint, symbol_id, pad_symbol_map):
        # ODB++ symbol sizes are commonly encoded in mil.
        MIL_TO_MM = 0.0254
        default = {
            "size": [self.DEFAULT_PAD_SIZE, self.DEFAULT_PAD_SIZE],
            "shape": "rect",
        }
        if symbol_id is None:
            return default
        sym = pad_symbol_map.get(layer_hint, {}).get(symbol_id)
        if not sym:
            return default
        s = sym.lower()

        if s.startswith("rect"):
            m = re.match(r"rect([0-9.]+)x([0-9.]+)(?:xr([0-9.]+))?", s)
            if m:
                w = max(float(m.group(1)) * MIL_TO_MM, 0.08)
                h = max(float(m.group(2)) * MIL_TO_MM, 0.08)
                rr = self._to_float(m.group(3)) if m.group(3) else None
                if rr is not None and rr > 0:
                    return {
                        "size": [w, h],
                        "shape": "roundrect",
                        "radius": min(rr * MIL_TO_MM, min(w, h) / 2),
                    }
                return {"size": [w, h], "shape": "rect"}
        if s.startswith("oblong"):
            m = re.match(r"oblong([0-9.]+)x([0-9.]+)", s)
            if m:
                w = max(float(m.group(1)) * MIL_TO_MM, 0.08)
                h = max(float(m.group(2)) * MIL_TO_MM, 0.08)
                return {"size": [w, h], "shape": "oval"}
        if s.startswith("r"):
            v = self._to_float(s[1:])
            if v is not None:
                d = max(v * MIL_TO_MM, 0.08)
                return {"size": [d, d], "shape": "circle"}
        if s.startswith("s"):
            v = self._to_float(s[1:])
            if v is not None:
                d = max(v * MIL_TO_MM, 0.08)
                return {"size": [d, d], "shape": "rect"}

        custom = self._custom_symbol_polygons(sym)
        if custom:
            bbox = BoundingBox()
            for poly in custom:
                for x, y in poly:
                    bbox.add_point(x, y)
            b = bbox.to_dict()
            return {
                "size": [max(b["maxx"] - b["minx"], 0.08), max(b["maxy"] - b["miny"], 0.08)],
                "shape": "custom",
                "polygons": custom,
            }
        return default

    def _custom_symbol_polygons(self, symbol_name):
        if not hasattr(self, "_custom_symbol_cache"):
            self._custom_symbol_cache = {}
        if symbol_name in self._custom_symbol_cache:
            return self._custom_symbol_cache[symbol_name]
        if not hasattr(self, "_symbol_features_map"):
            self._symbol_features_map = {}
        path = self._symbol_features_map.get(symbol_name.lower())
        if not path or not hasattr(self, "_file_data"):
            self._custom_symbol_cache[symbol_name] = None
            return None
        text = self._file_data.get(path)
        if not text:
            self._custom_symbol_cache[symbol_name] = None
            return None
        polys = self._parse_surface_polygons(text, 1.0, as_relative=True)
        polygons = []
        for zone in polys:
            for poly in zone:
                if len(poly) >= 3:
                    polygons.append(poly)
        self._custom_symbol_cache[symbol_name] = polygons or None
        return self._custom_symbol_cache[symbol_name]

    @staticmethod
    def _bbox_to_edges(bbox):
        minx = bbox["minx"]
        miny = bbox["miny"]
        maxx = bbox["maxx"]
        maxy = bbox["maxy"]
        return [
            {"type": "segment", "start": [minx, miny], "end": [maxx, miny], "width": 0.1},
            {"type": "segment", "start": [maxx, miny], "end": [maxx, maxy], "width": 0.1},
            {"type": "segment", "start": [maxx, maxy], "end": [minx, maxy], "width": 0.1},
            {"type": "segment", "start": [minx, maxy], "end": [minx, miny], "width": 0.1},
        ]

    @staticmethod
    def _split_tokens(line):
        try:
            return shlex.split(line, posix=True)
        except Exception:
            return line.split()

    def _extract_ref(self, tokens):
        for t in tokens:
            if self._REF_RE.match(t):
                return t
        return None

    @staticmethod
    def _extract_numbers(tokens):
        nums = []
        for t in tokens:
            v = OdbPlusPlusParser._to_float(t)
            if v is not None:
                nums.append(v)
        return nums

    @staticmethod
    def _to_float(token):
        token = token.strip().strip(',;')
        if not token:
            return None
        try:
            return float(token)
        except Exception:
            return None

    def _extract_xy_angle(self, tokens, scale):
        nums = self._extract_numbers(tokens)
        if len(nums) < 2:
            return None, None, 0.0
        x = nums[0] * scale
        y = -nums[1] * scale
        angle = nums[2] if len(nums) > 2 else 0.0
        return x, y, angle

    @staticmethod
    def _layer_from_path(path):
        lower = path.lower()
        if 'comp_+_bot' in lower or '/bot/' in lower:
            return 'B'
        return 'F'

    def _extract_layer(self, tokens, fallback):
        for t in tokens:
            u = t.upper()
            if u in ('TOP', 'T', 'F'):
                return 'F'
            if u in ('BOT', 'BOTTOM', 'B'):
                return 'B'
        return fallback

    def _extract_footprint(self, tokens):
        best = None
        for t in tokens[::-1]:
            if self._to_float(t) is not None:
                continue
            low = t.lower()
            if low in ('cmp', 'comp', 'top', 'bot', 'bottom', 'layer'):
                continue
            if len(t) <= 1:
                continue
            best = t
            break
        return best

    @staticmethod
    def _units_scale(text):
        header = "\n".join(text.splitlines()[:30]).upper()
        if 'UNITS=INCH' in header or 'UNITS INCH' in header:
            return 25.4
        return 1.0

    def _read_xy(self, tokens, scale):
        nums = self._extract_numbers(tokens)
        if len(nums) < 2:
            return None
        return nums[0] * scale, -nums[1] * scale
