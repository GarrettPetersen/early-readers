#!/usr/bin/env python3
"""Utility to assemble illustrated early-reader PDFs from YAML specs."""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Frame, Paragraph

POINTS_PER_INCH = 72
ALIGNMENT_MAP = {
    "left": TA_LEFT,
    "center": TA_CENTER,
    "right": TA_RIGHT,
    "justify": TA_JUSTIFY,
}


@dataclass
class RegionLayout:
    name: str
    folder: Optional[Path]
    style: ParagraphStyle
    box_height: float
    insets: Dict[str, Optional[float]]


@dataclass
class PageSpec:
    slug: str
    sequence_index: int
    page_number: int
    kind: str
    image_path: Path
    image_scale: float
    image_offset: Dict[str, float]
    text_refs: Dict[str, Optional["TextSource"]]


@dataclass
class TextSource:
    mode: str  # "file" or "inline"
    value: str


class BookBuilder:
    def __init__(self, config_path: Path) -> None:
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        self.config_path = config_path
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        if not raw or "book" not in raw:
            raise ValueError("Config must contain a 'book' section")
        self.raw = raw
        self.book_cfg = raw["book"]
        self.base_dir = config_path.parent

        trim = self.book_cfg.get("trim_size_in", {})
        bleed_in = float(self.book_cfg.get("bleed_in", 0))
        self.trim_width_in = float(trim.get("width", 6))
        self.trim_height_in = float(trim.get("height", 9))
        self.page_width_pt = (self.trim_width_in + bleed_in * 2) * POINTS_PER_INCH
        self.page_height_pt = (self.trim_height_in + bleed_in * 2) * POINTS_PER_INCH

        self.output_pdf = self._resolve_path(self.book_cfg.get("output_pdf", "book.pdf"))
        self.output_pdf.parent.mkdir(parents=True, exist_ok=True)

        image_folder = self.book_cfg.get("image_folder")
        if not image_folder:
            raise ValueError("'book.image_folder' is required")
        self.image_folder = self._resolve_path(image_folder)

        font_cfg = self.book_cfg.get("font") or {}
        font_name = font_cfg.get("name", "Lexend")
        font_path = font_cfg.get("path")
        if font_path:
            font_path = self._resolve_path(font_path)
            if not font_path.exists():
                raise FileNotFoundError(f"Font file not found: {font_path}")
            pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
        self.font_name = font_name

        text_library_path = self.book_cfg.get("text_library")
        self.text_library: Dict[str, Any] = {}
        self.library_pages: List[Dict[str, Any]] = []
        if text_library_path:
            lib_path = self._resolve_path(text_library_path)
            if not lib_path.exists():
                raise FileNotFoundError(f"Text library file not found: {lib_path}")
            self.text_library, self.library_pages = self._load_text_library(lib_path)

        text_layout_cfg = self.book_cfg.get("text_layout", {})
        self.text_regions: Dict[str, RegionLayout] = {}
        for name in ("top", "bottom"):
            cfg = text_layout_cfg.get(name)
            if not cfg:
                continue
            folder_path = None
            folder_value = cfg.get("folder")
            if folder_value:
                folder_path = self._resolve_path(folder_value)
                folder_path.mkdir(parents=True, exist_ok=True)

            font_size = float(cfg.get("font_size_pt", 26))
            leading = float(cfg.get("leading_pt", font_size * 1.2))
            color_value = cfg.get("color", "#111111")
            alignment = ALIGNMENT_MAP.get(cfg.get("align", "center"), TA_CENTER)
            style = ParagraphStyle(
                name=f"{self.font_name}-{name}",
                fontName=self.font_name,
                fontSize=font_size,
                leading=leading,
                alignment=alignment,
                textColor=colors.HexColor(color_value),
                spaceBefore=0,
                spaceAfter=0,
            )

            box_height = self._inches_to_points(cfg.get("box_height_in", 1.5))
            inset_cfg = cfg.get("inset_in", {})
            insets: Dict[str, Optional[float]] = {
                "left": self._inches_to_points(inset_cfg.get("left", 0.5)),
                "right": self._inches_to_points(inset_cfg.get("right", 0.5)),
                "top": self._inches_to_points(inset_cfg.get("top", 0.5)),
                "bottom": self._inches_to_points(inset_cfg.get("bottom", 0.5)),
            }
            if "inner" in inset_cfg:
                insets["inner"] = self._inches_to_points(inset_cfg["inner"])
            if "outer" in inset_cfg:
                insets["outer"] = self._inches_to_points(inset_cfg["outer"])

            self.text_regions[name] = RegionLayout(
                name=name,
                folder=folder_path,
                style=style,
                box_height=box_height,
                insets=insets,
            )

        defaults = self.book_cfg.get("defaults", {})
        self.default_scale = float(defaults.get("image_scale", 1.0))
        offset_in = defaults.get("image_offset_in", {})
        self.default_offset = {
            "x": self._inches_to_points(offset_in.get("x", 0.0)),
            "y": self._inches_to_points(offset_in.get("y", 0.0)),
        }

    def build(self) -> None:
        c = canvas.Canvas(str(self.output_pdf), pagesize=(self.page_width_pt, self.page_height_pt))
        for page in self._expand_pages():
            self._draw_page(c, page)
        c.save()
        print(f"Created {self.output_pdf}")

    def _expand_pages(self) -> Iterable[PageSpec]:
        pages = self.library_pages or self.raw.get("pages", [])
        using_library_pages = bool(self.library_pages)
        page_number = 1
        for block in pages:
            kind = str(block.get("kind", "page")).strip().lower() or "page"
            requested_span = block.get("span")
            if requested_span is not None:
                span = int(requested_span)
            else:
                span = 2 if kind == "spread" else 1
            if span < 1:
                raise ValueError(f"Page span must be >= 1 (slug={block.get('slug')})")
            for index in range(span):
                slug = block.get("slug") or f"page-{len(pages)}"
                image_name = block.get("image")
                if not image_name:
                    raise ValueError(f"Every page needs an image (slug={slug})")
                image_path = self._resolve_media_path(image_name)
                scale = float(block.get("image_scale", self.default_scale))
                offset_cfg = block.get("image_offset_in", {})
                offset = {
                    "x": self._inches_to_points(offset_cfg["x"]) if "x" in offset_cfg else self.default_offset["x"],
                    "y": self._inches_to_points(offset_cfg["y"]) if "y" in offset_cfg else self.default_offset["y"],
                }

                text_refs: Dict[str, Optional[TextSource]] = {}
                block_text = block.get("text", {})
                for region_name in self.text_regions.keys():
                    ref = block_text.get(region_name)
                    resolved = self._resolve_text_reference(
                        ref, index, slug, region_name, prefer_inline=using_library_pages
                    )
                    text_refs[region_name] = resolved

                yield PageSpec(
                    slug=slug,
                    sequence_index=index,
                    page_number=page_number,
                    kind=kind,
                    image_path=image_path,
                    image_scale=scale,
                    image_offset=offset,
                    text_refs=text_refs,
                )
                page_number += 1

    def _draw_page(self, canv: canvas.Canvas, page: PageSpec) -> None:
        self._draw_background(canv, page)
        for region_name, layout in self.text_regions.items():
            text_ref = page.text_refs.get(region_name)
            if not text_ref:
                continue
            self._draw_text(canv, layout, text_ref, page.page_number)
        canv.showPage()

    def _draw_background(self, canv: canvas.Canvas, page: PageSpec) -> None:
        if not page.image_path.exists():
            raise FileNotFoundError(f"Missing image for page '{page.slug}': {page.image_path}")
        image = ImageReader(str(page.image_path))
        img_w, img_h = image.getSize()
        cover_scale = max(self.page_width_pt / img_w, self.page_height_pt / img_h)
        final_scale = cover_scale * page.image_scale
        draw_w = img_w * final_scale
        draw_h = img_h * final_scale
        x = (self.page_width_pt - draw_w) / 2 + page.image_offset["x"]
        y = (self.page_height_pt - draw_h) / 2 + page.image_offset["y"]
        canv.drawImage(image, x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")

    def _draw_text(
        self, canv: canvas.Canvas, layout: RegionLayout, text_ref: TextSource, page_number: int
    ) -> None:
        if text_ref.mode == "inline":
            content = text_ref.value.strip()
        else:
            if not layout.folder:
                raise ValueError(
                    f"Region '{layout.name}' is not configured with a folder, "
                    "so file-based text sources are unavailable."
                )
            text_path = layout.folder / text_ref.value
            if not text_path.exists():
                raise FileNotFoundError(f"Text file '{text_ref.value}' not found in {layout.folder}")
            content = text_path.read_text(encoding="utf-8").strip()
        if not content:
            return
        left_inset = layout.insets.get("left", 0.0) or 0.0
        right_inset = layout.insets.get("right", 0.0) or 0.0
        inner_inset = layout.insets.get("inner")
        outer_inset = layout.insets.get("outer")
        is_recto = page_number % 2 == 1  # odd-numbered pages are on the right (recto)
        if inner_inset is not None:
            if is_recto:
                left_inset = inner_inset
            else:
                right_inset = inner_inset
        if outer_inset is not None:
            if is_recto:
                right_inset = outer_inset
            else:
                left_inset = outer_inset

        paragraph = Paragraph(content.replace("\n", "<br/>"), layout.style)
        width = self.page_width_pt - (left_inset + right_inset)
        height = layout.box_height
        if layout.name == "top":
            y = self.page_height_pt - layout.insets.get("top", 0.0) - height
        else:
            y = layout.insets.get("bottom", 0.0)
        frame = Frame(left_inset, y, width, height, showBoundary=False)
        frame.addFromList([paragraph], canv)

    def _resolve_text_reference(
        self,
        ref: Any,
        index: int,
        page_slug: str,
        region_name: str,
        prefer_inline: bool = False,
    ) -> Optional[TextSource]:
        source = self._coerce_text_source(ref, index, page_slug, region_name, prefer_inline=prefer_inline)
        if source:
            return source
        if ref is None:
            return self._text_from_library(page_slug, region_name, index)
        return None

    def _coerce_text_source(
        self,
        value: Any,
        index: int,
        page_slug: str,
        region_name: str,
        prefer_inline: bool = False,
    ) -> Optional[TextSource]:
        if value is None:
            return None
        if isinstance(value, list):
            if not value:
                return None
            target = value[index] if index < len(value) else value[-1]
            return self._coerce_text_source(
                target, index, page_slug, region_name, prefer_inline=prefer_inline
            )
        if isinstance(value, dict):
            if "inline" in value:
                return TextSource("inline", str(value["inline"]))
            if "file" in value:
                return TextSource("file", str(value["file"]))
            if "library" in value:
                key = str(value["library"] or page_slug)
                return self._text_from_library(key, region_name, index)
            return None
        if isinstance(value, str):
            if value.startswith("@library"):
                _, _, key = value.partition(":")
                key = key or page_slug
                return self._text_from_library(key, region_name, index)
            if prefer_inline:
                return TextSource("inline", value)
            return TextSource("file", value)
        return TextSource("inline", str(value))

    def _text_from_library(
        self, library_key: str, region_name: str, index: int
    ) -> Optional[TextSource]:
        if not self.text_library:
            return None
        entry = self.text_library.get(library_key)
        if not entry:
            return None
        region_value = entry.get(region_name)
        return self._coerce_text_source(
            region_value, index, library_key, region_name, prefer_inline=True
        )

    def _resolve_media_path(self, image_name: str) -> Path:
        candidate = Path(image_name)
        if not candidate.is_absolute():
            candidate = self.image_folder / candidate
        return candidate

    def _resolve_path(self, value: Optional[str]) -> Path:
        if value is None:
            return self.base_dir
        path = Path(value)
        if not path.is_absolute():
            path = (self.base_dir / path).resolve()
        return path

    @staticmethod
    def _inches_to_points(value: Any) -> float:
        try:
            return float(value) * POINTS_PER_INCH
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _load_text_library(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        loader = json.load if path.suffix.lower() == ".json" else yaml.safe_load
        with path.open("r", encoding="utf-8") as handle:
            data = loader(handle) or {}

        pages: List[Dict[str, Any]] = []
        texts: Dict[str, Any] = {}

        if isinstance(data, list):
            pages = [entry for entry in data if isinstance(entry, dict)]
        elif isinstance(data, dict):
            if "pages" in data:
                raw_pages = data.get("pages") or []
                if not isinstance(raw_pages, list):
                    raise ValueError("'pages' inside the text library must be a list.")
                pages = [entry for entry in raw_pages if isinstance(entry, dict)]
                if "texts" in data and isinstance(data["texts"], dict):
                    texts = data["texts"]
                else:
                    texts = {k: v for k, v in data.items() if k not in {"pages", "texts"}}
            else:
                texts = data
        else:
            raise ValueError("Text library file must be a mapping, list, or contain a 'pages' list.")

        if not isinstance(texts, dict):
            raise ValueError("Text entries inside the library must form a mapping.")

        for entry in pages:
            slug = entry.get("slug")
            page_text = entry.get("text")
            if slug and page_text is not None and slug not in texts:
                texts[slug] = page_text

        return texts, pages


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a PDF for the early-reader book.")
    parser.add_argument(
        "--config",
        default="content/pages.yaml",
        type=Path,
        help="Path to the YAML config that describes the book",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    builder = BookBuilder(args.config.resolve())
    builder.build()


if __name__ == "__main__":
    main(sys.argv[1:])
