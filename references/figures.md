# Figures

Read before extracting figures or changing how they are tied to sections.

A manual's diagrams carry what its prose leaves out, and a text extractor turns
them into label soup — `<!-- Start of picture text -->SoC<br>CPU<br>…` — or into
nothing at all when the figure is a raster image. Extract them as images and
let the model look.

`scripts/extract_figures.py <collection>` reads each source PDF with the layout
model pymupdf4llm itself uses, crops every figure into `docs/<slug>/figures/`,
and records page, box, caption and drawn labels in `figures.json`. It touches
nothing the converter wrote.

**A figure belongs to a section only on evidence**, and which evidence there is
depends on what the converter left behind. Its own caption appearing as a line
of the section is exact: 331 of 331 captioned figures in one Tessent guide, 454
of 454 in a Synopsys one. Failing that:

- **Its page, where the chunks carry page numbers** (every converter here) and
  the page belongs to one section. Checked against the
  caption: 100% agreement on an unshared page, 76.7% where one entry ends and
  the next begins on it — so a shared page ties nothing.
- **The paragraph above it**, where the page ties nothing or the chunks carry
  none: found just after the previous figure's section, or else exactly once in
  the document. Checked
  against the caption, that is 96–99% right on documents chunked by heading —
  and 5.8% on a command reference rebuilt into one chunk per entry, where a bold
  caption starts its own chunk and strands the paragraph above it in the
  previous one. So it is never used on a reference rebuilt one chunk per entry.

Otherwise the figure stays unattached and is served by page — a guessed section
would present a diagram as illustrating text it does not.

Traps, each met on a real corpus:

- A sentence citing a figure ("Figure 59 illustrates …") starts like a caption.
  A caption follows its number with punctuation or a capitalised title.
- Synopsys sets "Figure 1" and its title apart with a tab, so a caption box's
  first line is the bare label; join the next line.
- The layout model's raw box clips vector drawings at their edges. Grow it the
  way pymupdf4llm does before cropping.
- Note icons (~15 pt) and horizontal rules (~3 pt tall) come back as pictures.
- The model costs ~0.2 s a page, so skip pages with no sizeable image and no
  drawing in the body — ignoring the panel Synopsys paints behind every page's
  text area, which otherwise sends every page through.
- Its ONNX sessions take a thread per core, so parallel jobs fight: one guide
  took 283 s alone and 1,830 s as one of four jobs. `--jobs` caps each job's
  share, installed before the model is imported — the sessions are created on
  import, and importing pymupdf4llm triggers it. Installed after, the cap
  silently did nothing; installed first, another guide went from 1,552 s to 82 s.

The server shows a figure with `get_figure` and any page with `get_page_image`,
and `get_section` lists a section's figures by id.

Captions and drawn labels are indexed with their section, and measured they add
nothing: the caption is already in the section's text, and so, as picture
soup, are most vector figures' labels. What a raster figure says is invisible to
search until `scripts/ocr_figures.py` reads it — that took questions answered by
a figure from 14 of 19 in the top five to 18 of 19, and moved no text question
out of it. Noisy OCR is enough; search needs only some of the words. With that
in place, written figure descriptions had one question left to win and were not
generated — the model looks at the figure itself through `get_figure`.
