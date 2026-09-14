# Screen List Capturer

**Turn any on-screen list into a spreadsheet — without writing a scraper.**

Mark a region once. The tool auto-scrolls, screenshots every screen, packs them
into a zip, and hands you a ready-to-paste prompt. You feed both to any
vision-capable LLM; it returns CSV; one command turns that into `.xlsx`.

[中文说明](./README.zh-CN.md)

---

## Why not just scrape it?

Because a lot of lists live somewhere you cannot reach with code: a WeChat mini
program, a native app, an Electron client, a page behind login. Reverse
engineering their APIs is slow and breaks on every release.

This tool takes the opposite route — it works on pixels instead of protocols:

| | DOM / API scraper | Screen List Capturer |
|---|---|---|
| Needs API or selectors | Yes | No |
| Breaks when UI changes | Often | No — it just screenshots |
| Setup time | Hours per target | ~1 min per target |
| Extraction accuracy | Exact | Depends on the vision model |
| Cost | Free | LLM tokens |

The trade-off is deliberate: you trade exactness for reach. Anything a human can
scroll through, this can capture.

## How it works

```
1. Calibrate   point at the top-left and bottom-right of the list (countdown guided)
2. Capture     auto-scroll + screenshot -> pages/page_001.png, page_002.png ...
3. Pack        pages.zip + LLM_PROMPT.md (prompt also copied to clipboard)
4. Extract     upload the zip + prompt to any vision LLM -> CSV
5. Convert     python csv_to_xlsx.py llm_output.csv
```

### Stop detection

Every frame is compared with the previous one in grayscale, allowing for a few
pixels of vertical offset so that elastic-scroll bounce does not read as new
content. When the screen stops changing, capture ends immediately and the
unchanged frame is **not saved**. A near-identical frame (bounce in progress)
triggers one extra settle-and-recheck before being judged.

Measured separation on synthetic lists: jitter ≈ 0.0, a real new page ≈ 27
(mean per-pixel difference on a 64×64 downscale).

If scrolling has no effect at all, it stops after the first page and says so —
usually a permissions or scroll-mode problem. Run the simulation without a
display:

```bash
python tests/test_capture_loop.py
```

## Install

```bash
git clone https://github.com/m2290526022-boop/screen-list-capturer.git
cd screen-list-capturer
pip install -r requirements.txt
python screen_list_capturer.py
```

Python 3.9+ is required. `tkinter` ships with most Python builds (see
`requirements.txt` if yours lacks it).

### macOS permissions

The tool drives the real mouse and reads the real screen, so grant your
terminal (or Python launcher):

- **System Settings → Privacy & Security → Accessibility**
- **System Settings → Privacy & Security → Screen Recording**

HiDPI / Retina displays are handled: the scale factor is detected automatically
and the capture box is converted from points to pixels.

### Windows / Linux

Works as-is. On Linux, `scrot` or `gnome-screenshot` may be needed for
`ImageGrab` to function.

## Configuration

| What | Where |
|---|---|
| Region, overlap %, scroll mode | In the app (top panel) |
| Extracted fields, prompt wording | `prompt_template.md` — edit freely, `{n_pages}` is substituted at pack time |
| Output location | `pages/`, `pages.zip`, `LLM_PROMPT.md` next to the script |

Overlap matters: a higher overlap produces more pages (more tokens) but loses
fewer rows that straddle a page boundary. 50% is a safe default.

## Limitations

Honest list of what this does *not* do:

- **No OCR, no local parsing.** Extraction quality is entirely the vision
  model's. Small text and dense layouts still trip it up.
- **No dedup across pages.** Overlap means the same item can appear on two
  pages; the prompt tells the model to list everything, so dedupe afterwards.
  A large scroll bounce at the very bottom can leave one extra duplicate frame —
  kept on purpose, because dropping a real page would be worse.
- **Fixed region.** If the list's position changes while scrolling (e.g. a
  sticky header that resizes), recalibrate.
- **You must watch it once.** `pyautogui` FailSafe aborts if you throw the
  mouse into a screen corner — that is the intended escape hatch.

## Responsible use

This is a general-purpose screen capture utility. Use it on your own screens,
your own accounts, and data you are allowed to collect. Before capturing a
third-party app or website, check its terms of service and applicable law, and
do not use the output to build a competing dataset. The author is not
responsible for how it is used.

## License

[MIT](./LICENSE)
