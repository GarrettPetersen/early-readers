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
from reportlab.platypus import Paragraph

import fitz  # PyMuPDF

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
    vertical_anchor: str


@dataclass
class PageSpec:
    slug: str
    sequence_index: int
    page_number: int
    kind: str
    spread_side: Optional[str]
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
        total_bleed = bleed_in * 2
        self.page_width_pt = (self.trim_width_in + total_bleed) * POINTS_PER_INCH
        self.page_height_pt = (self.trim_height_in + total_bleed) * POINTS_PER_INCH

        self.output_pdf = self._resolve_path(
            self.book_cfg.get("output_pdf", "book.pdf")
        )
        self.output_pdf.parent.mkdir(parents=True, exist_ok=True)

        image_output_cfg = self.book_cfg.get("image_output", {})
        self.output_image_dir: Optional[Path] = None
        self.image_export_format = "png"
        self.image_export_dpi = 300
        self.image_export_enabled = image_output_cfg is not False
        if self.image_export_enabled:
            default_dir = (
                self.output_pdf.parent / f"{self.output_pdf.stem}-pages"
            ).resolve()
            folder_value = None
            if isinstance(image_output_cfg, dict):
                folder_value = image_output_cfg.get("folder")
            if folder_value:
                image_dir = self._resolve_path(folder_value)
            else:
                image_dir = default_dir
            image_dir.mkdir(parents=True, exist_ok=True)
            self.output_image_dir = image_dir
            if isinstance(image_output_cfg, dict):
                fmt = image_output_cfg.get("format", "png").lower()
                if fmt in {"jpg", "jpeg"}:
                    self.image_export_format = "jpg"
                elif fmt == "png":
                    self.image_export_format = "png"
                else:
                    self.image_export_format = "png"
                self.image_export_dpi = int(image_output_cfg.get("dpi", 300))
        else:
            self.output_image_dir = None

        manuscript_cfg = self.book_cfg.get("manuscript", {})
        self.manuscript_path: Optional[Path] = None
        self.manuscript_format = "md"
        self.manuscript_enabled = manuscript_cfg is not False
        if self.manuscript_enabled:
            default_manuscript = self.output_pdf.with_suffix(".md")
            path_value = None
            if isinstance(manuscript_cfg, dict):
                path_value = manuscript_cfg.get("path")
            if path_value:
                manuscript_path = self._resolve_path(path_value)
            else:
                manuscript_path = default_manuscript
            manuscript_path.parent.mkdir(parents=True, exist_ok=True)
            self.manuscript_path = manuscript_path
            if isinstance(manuscript_cfg, dict):
                fmt = manuscript_cfg.get("format", "md").lower()
                if fmt in {"md", "markdown"}:
                    self.manuscript_format = "md"
                else:
                    self.manuscript_format = "txt"
        else:
            self.manuscript_path = None

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

        text_layout_cfg = self.book_cfg.get("text_layout", {}) or {}
        self.text_regions: Dict[str, RegionLayout] = {}
        for name, cfg in text_layout_cfg.items():
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
            if "center" in inset_cfg:
                insets["center"] = self._inches_to_points(inset_cfg["center"])

            origin = (
                cfg.get("origin") or ("top" if name == "top" else "bottom")
            ).lower()
            if origin not in {"top", "bottom", "center"}:
                raise ValueError(
                    "text_layout."
                    f"{name}.origin must be one of "
                    "'top', 'bottom', or 'center' "
                    f"(got {origin})."
                )

            self.text_regions[name] = RegionLayout(
                name=name,
                folder=folder_path,
                style=style,
                box_height=box_height,
                insets=insets,
                vertical_anchor=origin,
            )

        defaults = self.book_cfg.get("defaults", {})
        self.default_scale = float(defaults.get("image_scale", 1.0))
        offset_in = defaults.get("image_offset_in", {})
        self.default_offset = {
            "x": self._inches_to_points(offset_in.get("x", 0.0)),
            "y": self._inches_to_points(offset_in.get("y", 0.0)),
        }

        self.missing_images: Dict[str, Path] = {}
        self.total_pages = 0
        self.pages_with_images = 0
        self.word_count = 0
        self.manuscript_entries: List[Dict[str, Any]] = []

    def build(self) -> None:
        page_size = (self.page_width_pt, self.page_height_pt)
        c = canvas.Canvas(str(self.output_pdf), pagesize=page_size)
        for page in self._expand_pages():
            has_image = page.image_path.exists()
            if not has_image:
                self.missing_images.setdefault(page.slug, page.image_path)
            self._draw_page(c, page, has_image)
            self.total_pages += 1
            if has_image:
                self.pages_with_images += 1
        c.save()
        print(f"Created {self.output_pdf}")
        print(f"  Pages rendered: {self.total_pages}")
        print(f"  Pages with art: {self.pages_with_images}")
        print(f"  Estimated words: {self.word_count}")
        self._export_page_images()
        self._write_manuscript()
        if self.missing_images:
            print("Skipped pages because images were missing:")
            for slug, path in self.missing_images.items():
                print(f"  - {slug}: {path}")

    def _export_page_images(self) -> None:
        if not self.output_image_dir:
            return
        try:
            doc = fitz.open(str(self.output_pdf))
        except Exception as exc:
            print(f"  Skipping page image export: {exc}")
            return
        try:
            pattern = f"*.{self.image_export_format}"
            for existing in self.output_image_dir.glob(pattern):
                existing.unlink()
            for idx, page in enumerate(doc, start=1):
                pix = page.get_pixmap(dpi=self.image_export_dpi)
                if self.image_export_format == "jpg" and pix.alpha:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                out_path = (
                    self.output_image_dir / f"{idx:03d}.{self.image_export_format}"
                )
                pix.save(str(out_path))
        finally:
            doc.close()
        print(f"  Page images written to {self.output_image_dir}")

    def _write_manuscript(self) -> None:
        if not self.manuscript_path:
            return
        lines: List[str] = []
        title = self.book_cfg.get("title", "Manuscript")
        if self.manuscript_format == "md":
            lines.append(f"# {title}")
            lines.append("")
        else:
            lines.append(title)
            lines.append("=" * len(title))
            lines.append("")
        current_page = None
        region_order = {"top": 0, "middle": 1, "bottom": 2}

        def sort_key(entry):
            region_rank = region_order.get(entry["region"], 99)
            spread_marker = entry["spread_side"] or ""
            return (
                entry["page_number"],
                spread_marker,
                region_rank,
            )

        for entry in sorted(self.manuscript_entries, key=sort_key):
            key = (entry["page_number"], entry["spread_side"], entry["slug"])
            if key != current_page:
                current_page = key
                page_label = f"Page {entry['page_number']}"
                if entry["spread_side"]:
                    page_label += f" ({entry['spread_side']})"
                page_label += f" — {entry['slug']}"
                if self.manuscript_format == "md":
                    lines.append(f"## {page_label}")
                else:
                    lines.append(page_label)
                lines.append("")
            region_label = entry["region"].capitalize()
            content = entry["content"]
            if self.manuscript_format == "md":
                lines.append(f"- **{region_label}:** {content}")
            else:
                lines.append(f"{region_label}: {content}")
            lines.append("")
        self.manuscript_path.write_text(
            "\n".join(lines).rstrip() + "\n", encoding="utf-8"
        )
        print(f"  Manuscript written to {self.manuscript_path}")

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
                raise ValueError(
                    "Page span must be >= 1 " f"(slug={block.get('slug')})"
                )
            if kind == "spread" and page_number % 2 == 1:
                raise ValueError(
                    "Spread '"
                    f"{block.get('slug')}' must start on a left-hand page. "
                    "Insert a blank page before it so it begins on an "
                    "even page number."
                )
            for index in range(span):
                slug = block.get("slug") or f"page-{len(pages)}"
                image_name = block.get("image")
                if not image_name:
                    raise ValueError(f"Every page needs an image (slug={slug})")
                image_path = self._resolve_media_path(image_name)
                scale = float(block.get("image_scale", self.default_scale))
                offset_cfg = block.get("image_offset_in", {})
                offset = {
                    "x": (
                        self._inches_to_points(offset_cfg["x"])
                        if "x" in offset_cfg
                        else self.default_offset["x"]
                    ),
                    "y": (
                        self._inches_to_points(offset_cfg["y"])
                        if "y" in offset_cfg
                        else self.default_offset["y"]
                    ),
                }

                spread_side: Optional[str] = None
                if kind == "spread":
                    spread_side = "left" if index == 0 else "right"

                text_refs: Dict[str, Optional[TextSource]] = {}
                block_text = block.get("text", {})
                for region_name in self.text_regions.keys():
                    ref = block_text.get(region_name)
                    resolved = self._resolve_text_reference(
                        ref,
                        index,
                        slug,
                        region_name,
                        spread_side=spread_side,
                        prefer_inline=using_library_pages,
                    )
                    text_refs[region_name] = resolved

                yield PageSpec(
                    slug=slug,
                    sequence_index=index,
                    page_number=page_number,
                    kind=kind,
                    spread_side=spread_side,
                    image_path=image_path,
                    image_scale=scale,
                    image_offset=offset,
                    text_refs=text_refs,
                )
                page_number += 1

    def _draw_page(self, canv: canvas.Canvas, page: PageSpec, has_image: bool) -> None:
        self._draw_background(canv, page, has_image)
        for region_name, layout in self.text_regions.items():
            text_ref = page.text_refs.get(region_name)
            if not text_ref:
                continue
            self._draw_text(
                canv,
                layout,
                text_ref,
                page.page_number,
                page.spread_side,
                page.slug,
            )
        canv.showPage()

    def _draw_background(
        self, canv: canvas.Canvas, page: PageSpec, has_image: bool
    ) -> None:
        if not has_image:
            self._draw_blank_background(canv)
            return
        image = ImageReader(str(page.image_path))
        if page.kind == "spread":
            self._draw_spread_background(canv, image, page)
            return
        img_w, img_h = image.getSize()
        cover_scale = max(self.page_width_pt / img_w, self.page_height_pt / img_h)
        final_scale = cover_scale * page.image_scale
        draw_w = img_w * final_scale
        draw_h = img_h * final_scale
        x = (self.page_width_pt - draw_w) / 2 + page.image_offset["x"]
        y = (self.page_height_pt - draw_h) / 2 + page.image_offset["y"]
        canv.drawImage(
            image,
            x,
            y,
            width=draw_w,
            height=draw_h,
            preserveAspectRatio=True,
            mask="auto",
        )

    def _draw_blank_background(self, canv: canvas.Canvas) -> None:
        canv.saveState()
        canv.setFillColor(colors.white)
        canv.rect(0, 0, self.page_width_pt, self.page_height_pt, stroke=0, fill=1)
        canv.setStrokeColor(colors.Color(0.85, 0.85, 0.85))
        canv.line(0, 0, self.page_width_pt, self.page_height_pt)
        canv.line(0, self.page_height_pt, self.page_width_pt, 0)
        canv.restoreState()

    def _draw_spread_background(
        self, canv: canvas.Canvas, image: ImageReader, page: PageSpec
    ) -> None:
        img_w, img_h = image.getSize()
        spread_width = self.page_width_pt * 2
        spread_height = self.page_height_pt
        cover_scale = max(spread_width / img_w, spread_height / img_h)
        final_scale = cover_scale * page.image_scale
        draw_w = img_w * final_scale
        draw_h = img_h * final_scale
        x = (spread_width - draw_w) / 2 + page.image_offset["x"]
        y = (spread_height - draw_h) / 2 + page.image_offset["y"]

        canv.saveState()
        clip_path = canv.beginPath()
        clip_path.rect(0, 0, self.page_width_pt, self.page_height_pt)
        canv.clipPath(clip_path, stroke=0)

        offset_x = x
        if page.spread_side == "right":
            offset_x -= self.page_width_pt

        canv.drawImage(
            image,
            offset_x,
            y,
            width=draw_w,
            height=draw_h,
            preserveAspectRatio=True,
            mask="auto",
        )
        canv.restoreState()

    def _draw_text(
        self,
        canv: canvas.Canvas,
        layout: RegionLayout,
        text_ref: TextSource,
        page_number: int,
        spread_side: Optional[str],
        slug: str,
    ) -> None:
        if text_ref.mode == "inline":
            content = text_ref.value.strip()
        else:
            if not layout.folder:
                raise ValueError(
                    "Region '"
                    f"{layout.name}' is not configured with a folder, "
                    "so file-based text sources are unavailable."
                )
            text_path = layout.folder / text_ref.value
            if not text_path.exists():
                raise FileNotFoundError(
                    "Text file " f"'{text_ref.value}' not found in {layout.folder}"
                )
            content = text_path.read_text(encoding="utf-8").strip()
        if not content:
            return
        left_inset = layout.insets.get("left", 0.0) or 0.0
        right_inset = layout.insets.get("right", 0.0) or 0.0
        inner_inset = layout.insets.get("inner")
        outer_inset = layout.insets.get("outer")
        # odd-numbered pages are on the right (recto)
        is_recto = page_number % 2 == 1
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
        max_height = layout.box_height
        _, paragraph_height = paragraph.wrap(width, max_height)
        paragraph_height = min(max_height, paragraph_height)
        if paragraph_height <= 0:
            paragraph_height = layout.style.leading

        anchor = layout.vertical_anchor
        if anchor == "top":
            y = (
                self.page_height_pt
                - (layout.insets.get("top", 0.0) or 0.0)
                - paragraph_height
            )
        elif anchor == "center":
            center_offset = layout.insets.get("center", 0.0) or 0.0
            y = (self.page_height_pt - paragraph_height) / 2 + center_offset
        else:  # bottom
            y = layout.insets.get("bottom", 0.0) or 0.0

        paragraph.drawOn(canv, left_inset, y)
        self.word_count += len(content.split())
        self.manuscript_entries.append(
            {
                "page_number": page_number,
                "slug": slug,
                "spread_side": spread_side,
                "region": layout.name,
                "content": content,
            }
        )

    def _resolve_text_reference(
        self,
        ref: Any,
        index: int,
        page_slug: str,
        region_name: str,
        spread_side: Optional[str],
        prefer_inline: bool = False,
    ) -> Optional[TextSource]:
        source = self._coerce_text_source(
            ref,
            index,
            page_slug,
            region_name,
            spread_side=spread_side,
            prefer_inline=prefer_inline,
        )
        if source:
            return source
        if ref is None:
            return self._text_from_library(
                page_slug, region_name, index, spread_side=spread_side
            )
        return None

    def _coerce_text_source(
        self,
        value: Any,
        index: int,
        page_slug: str,
        region_name: str,
        spread_side: Optional[str],
        prefer_inline: bool = False,
    ) -> Optional[TextSource]:
        if value is None:
            return None
        if isinstance(value, list):
            if not value:
                return None
            target = value[index] if index < len(value) else value[-1]
            return self._coerce_text_source(
                target,
                index,
                page_slug,
                region_name,
                spread_side,
                prefer_inline=prefer_inline,
            )
        if isinstance(value, dict):
            if "inline" in value:
                return TextSource("inline", str(value["inline"]))
            if "file" in value:
                return TextSource("file", str(value["file"]))
            if "library" in value:
                key = str(value["library"] or page_slug)
                return self._text_from_library(
                    key, region_name, index, spread_side=spread_side
                )
            directional_keys = {"left", "right"}
            if directional_keys.intersection(value.keys()):
                candidate = None
                if spread_side and spread_side in value:
                    candidate = value.get(spread_side)
                elif "default" in value:
                    candidate = value.get("default")
                if candidate is None:
                    return None
                return self._coerce_text_source(
                    candidate,
                    index,
                    page_slug,
                    region_name,
                    spread_side,
                    prefer_inline=prefer_inline,
                )
            return None
        if isinstance(value, str):
            if value.startswith("@library"):
                _, _, key = value.partition(":")
                key = key or page_slug
                return self._text_from_library(
                    key, region_name, index, spread_side=spread_side
                )
            if prefer_inline:
                return TextSource("inline", value)
            return TextSource("file", value)
        return TextSource("inline", str(value))

    def _text_from_library(
        self,
        library_key: str,
        region_name: str,
        index: int,
        spread_side: Optional[str],
    ) -> Optional[TextSource]:
        if not self.text_library:
            return None
        entry = self.text_library.get(library_key)
        if not entry:
            return None
        region_value = entry.get(region_name)
        return self._coerce_text_source(
            region_value,
            index,
            library_key,
            region_name,
            spread_side,
            prefer_inline=True,
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
    def _load_text_library(
        path: Path,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        if path.suffix.lower() == ".json":
            loader = json.load
        else:
            loader = yaml.safe_load
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
                    texts = {
                        k: v for k, v in data.items() if k not in {"pages", "texts"}
                    }
            else:
                texts = data
        else:
            raise ValueError(
                "Text library file must be a mapping, list, "
                "or contain a 'pages' list."
            )

        if not isinstance(texts, dict):
            raise ValueError("Text entries inside the library must form a mapping.")

        for entry in pages:
            slug = entry.get("slug")
            page_text = entry.get("text")
            if slug and page_text is not None and slug not in texts:
                texts[slug] = page_text

        return texts, pages


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a PDF for the early-reader book."
    )
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
