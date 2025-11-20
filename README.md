# Early Readers PDF Builder

This repo is a lightweight pipeline for turning scanned watercolor spreads and short sentences into a press-ready PDF (6"×9" trim with 0.125" bleed on every edge).

## Project layout

```
assets/images/              # legacy sandbox artwork
books/
  └── protestant-reformation/
        images/             # drop 1.tif, 2.tif, ... for that title
        pages.yaml          # layout + metadata for that book
        text-library.json   # per-book text + page metadata
content/pages.yaml          # original sample layout
content/text/library.json   # original sample text database
fonts/                      # supplied Lexend family (already included)
build/                      # generated PDFs land here (git-ignored)
Makefile                    # convenience targets (sample/reformation/all)
# After each build you also get per-page images under build/<bookname>-pages/
```

## Workflow

1. **Add art**: copy each final TIFF into `assets/images/`. Name them however you like; reference those names inside `content/pages.yaml`.
2. **Add text** (pick any mix of styles):
   - **Central file**: populate `content/text/library.json` (or swap to YAML) with entries keyed by page slug, e.g. `"cover": { "top": "…", "bottom": "…" }`. Spreads can use arrays so the left/right sides differ.
   - **Per-file**: if you prefer loose `.txt` snippets, create folders anywhere you like (e.g. `content/snippets/top`) and point `book.text_layout.top.folder` to that path. Those folders are not created by default.
   - **Inline**: embed copy directly in `content/pages.yaml` using `inline:` blocks for one-off tweaks.
3. **Describe the page order**: edit `content/pages.yaml` to list every page or spread in reading order. Use the included sample as a template.
4. **Generate the PDF**: install dependencies once, then run `python generate_book.py`. The finished book ends up at `build/sample-book.pdf` (or whatever you set in the YAML).

## YAML reference (`content/pages.yaml`)

Key fields you will edit most often:

- `book.trim_size_in`, `book.bleed_in`: control page math (defaults already match 6×9 with 0.125" bleed).
- `book.output_pdf`: path for the finished book (relative to `content/` unless absolute). The default writes to `../build/sample-book.pdf`.
- `book.image_folder`: where the script looks for your TIFFs. Default points to `../assets/images`.
- `book.font`: the Lexend file that gets embedded. Swap to another weight if you like.
- `book.text_library`: optional JSON or YAML file that stores every sentence in one place. If a page does not specify `text.top` / `text.bottom`, the generator automatically looks up the entry whose key matches the page's `slug`.
- `book.text_library.pages`: if the text file includes a `pages` list (or is itself a list), every entry inside it is treated as a page definition (`slug`, `kind`, `image`, `text`, etc.). In that case you can leave `pages:` empty inside the YAML after the initial setup.
- `book.image_output`:
  - Optional block to control the per-page image export. By default images land in `build/<output_pdf_stem>-pages/` as PNGs at 300 dpi.
  - Override with `folder`, `format` (`png` or `jpg`), and `dpi`. Set the whole value to `false` to turn image export off.
- `book.text_layout.top` / `.bottom`:
  - `folder` (optional): path to look for `.txt` snippets (only needed if you plan to use file-based copy).
  - `font_size_pt`, `box_height_in`, `inset_in`: control the safe area and scale for the text block.
  - `inset_in.inner` / `.outer`: override the gutter vs outer margin independently (inner = spine edge, which flips automatically between left/right based on page number).
  - Add additional regions such as `middle` by defining another block (e.g. `text_layout.middle`). Set `origin: top|bottom|center` to control the vertical anchor and use `inset_in.center` to nudge centered copy up/down.
  - Optional `color` or `align` keys let you style copy (`align: left|center|right|justify`).
- `book.defaults`: fallback `image_scale` and `image_offset_in` values so you only override when a specific page needs tweaks.
- `pages[].kind`: set to `spread` when one painting should stretch across two facing pages; default is `page`. Spreads must begin on a left-hand (even-numbered) page so the art lines up correctly in print—otherwise the generator raises an error asking you to insert a blank/filler page first. You can still override `span` manually for special cases.

Each entry under `pages:` represents either a single page or a spread:

```yaml
- slug: stone-bridge-spread   # just an identifier for you
  kind: spread                # renders twice (left + right page)
  image: spread-river.tiff    # pulled from assets/images/
  image_scale: 1.1            # optional override of the default scaling
  image_offset_in:            # optional x/y nudge in inches
    x: 0.0
    y: 0.1
  text:
    top:
      left: >
        Builders raised stone arches that met in the middle like clasped hands.
      right: >
        Across the plaza, new arches started to mirror the old.
    middle:
      inline: "THE END"
    bottom:
      library: stone-bridge-spread   # fetches left/right entries from the central file
```

If you omit `kind` (and `span`), the entry renders once. Set `kind: spread` to automatically crop a single painting across the left/right pages. For text, you can:

- Provide arrays (legacy style) and the generator will pick the entry matching each side.
- Provide directional keys (`left`/`right`) to control each half explicitly (and omit whichever side should stay blank).
- Mix and match with `inline`, `file`, or `library` sources for maximum flexibility.

The generator enforces that every spread starts on a left-hand (even) page so the printed book lines up correctly.

### Driving everything from JSON

When `book.text_library` points to a JSON/YAML file that contains a `pages` list (or the entire file is a list), each entry in that list behaves exactly like a row in `pages.yaml`. A typical JSON entry looks like:

```json
{
  "slug": "reformation-title",
  "kind": "page",
  "image": "1.tif",
  "text": {
    "top": "The Protestant Reformation",
    "middle": "THE END",
    "bottom": "A journey through questions, courage, and new ideas."
  }
}
```

With this setup the YAML only holds book-level settings; all per-page updates (including spreads and left/right text) happen inside the JSON file.

### Spreads & printing alignment

- Spread images are scaled once for the full 12"×9" (plus bleed) canvas, then clipped so page 1 shows the left half and page 2 shows the right half—what you see in the PDF is exactly what will line up on press.
- Because of that clipping, spreads must begin on a left-hand (even-numbered) page. If you accidentally schedule a spread on a right-hand/odd page the generator fails fast and tells you to insert a blank/filler page first.
- Text regions accept directional keys. For example, `text.top.left` renders only on the left page, `text.bottom.right` renders only on the right, and you can mix in `inline`, `file`, or `library` references at any level. Leave a side undefined if it should stay blank.
- Inner vs outer insets automatically flip based on page parity, keeping the gutter margin wider than the trim edge.

## Generating the PDF

```bash
make install            # sets up the virtualenv & dependencies (do this once)
make sample             # rebuilds build/sample-book.pdf (+ per-page PNGs)
make reformation        # rebuilds build/protestant-reformation.pdf (+ PNGs)
```

Prefer manual commands? Activate `.venv` and run `python generate_book.py --config <path>` like before. The script prints the path to the finished PDF and overwrites the previous file each time you run it.

## Troubleshooting

- **Missing image/text**: the script stops with a helpful error that tells you which filename was missing (or which slug could not be found in the text library).
- **Spread on the wrong side**: spreads must start on a left-hand (even-numbered) page. If you see this error, insert a blank/filler page or reorder your layout so the spread begins on an even page number.
- **Multiple books**: duplicate `content/pages.yaml` (e.g. `books/mythic/pages.yaml`) or copy the `books/protestant-reformation/` pattern. Pass the desired file via `python generate_book.py --config books/mythic/pages.yaml`. Each config can point to its own text library, assets folder, and output path, so the repo can host any number of books.
- **Scale & offset**: every page auto-scales background art to cover the full bleed area. Use `image_scale` (>1 to zoom in, <1 to zoom out) plus `image_offset_in` (`x`/`y` in inches) for fine positioning.
- **Different fonts or colors**: point `book.font.path` to another `.ttf` and add `color: "#C71585"` inside the relevant text layout block.

Feel free to duplicate `content/pages.yaml` for multiple dummies (e.g. `pages-agent.yaml`, `pages-final.yaml`) and pass a different `--config` path to the generator.
