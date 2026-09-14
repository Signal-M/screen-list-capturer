#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Screen List Capturer
====================
Turn any on-screen scrollable list (desktop app, mini-program, web page) into a
structured table — without writing a single scraper.

How it works
------------
  Stage 1  Mark the list region on screen once. The tool auto-scrolls, takes a
           screenshot of every screen, and saves them as page_001.png ...
  Stage 2  The screenshots are packed into pages.zip, and a ready-to-paste
           prompt is generated (LLM_PROMPT.md) for any vision-capable LLM.
  Stage 3  Feed the zip + prompt to your LLM. It returns CSV; convert it with
           csv_to_xlsx.py.

No OCR, no selectors, no reverse engineering — the vision LLM does the reading.
That is the whole point: brittle DOM scrapers break on every UI change, a
screenshot + a vision model does not.

Requirements
------------
  pip install -r requirements.txt

macOS: grant Accessibility (to move the mouse / scroll) and Screen Recording
(to capture screenshots) permissions to your terminal or Python launcher.
On HiDPI/Retina displays nothing special is needed: the capture box is given in
logical points (the same units the mouse uses) and the screenshots simply come
back at 2x resolution.

Usage
-----
  python screen_list_capturer.py
"""

import os
import sys
import glob
import time
import zipfile
import threading
import subprocess
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext
except ImportError:
    sys.exit("tkinter is required. On macOS use a Python build with tkinter "
             "enabled (python.org installer or `brew install python-tk`).")

try:
    import pyautogui
    from PIL import Image, ImageChops, ImageGrab, ImageStat, ImageTk
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")


# ============================================================
# Configuration
# ============================================================
CAPTURE_SECONDS = 5        # countdown before each calibration point is recorded
SCROLL_PAUSE = 1.5         # seconds to wait for the list to settle after scrolling
MAX_PAGES = 200            # hard cap on pages, to avoid an endless loop
DEFAULT_OVERLAP = 50       # % of the region kept as overlap between two pages

# End-of-list detection. Consecutive pages are compared in grayscale, allowing
# for a vertical offset of up to MAX_SHIFT px (scroll bounce / elastic overscroll
# moves the whole page by a few pixels without showing anything new).
# The value is the mean per-pixel difference, 0-255, measured on a 64x64 downscale:
#   < DIFF_IDENTICAL : nothing moved at all      -> stop immediately
#   < DIFF_NEAR      : only jitter / bounce      -> not saved, stop after NEAR_LIMIT
#   >= DIFF_NEAR     : real new content          -> save and keep going
# Measured separation on synthetic lists: jitter ~0.0, a real new page ~27.
DIFF_IDENTICAL = 1.0
DIFF_NEAR = 6.0
NEAR_LIMIT = 2
MAX_SHIFT = 4
SETTLE_SECONDS = 1.0   # extra wait before confirming a page "did not change"
MOVE_DIFF_MAX = 20.0   # residual above which two frames are not "the same content shifted"
SHIFT_WINDOW_FRAC = 0.45   # height of the comparison window used to measure a shift

# Wheel scrolling. pyautogui.scroll() takes wheel units, not pixels, and its own
# source warns that values outside roughly -10..10 per event have
# application-dependent results — dumping one huge event at WeChat gets merged
# into a single gesture or truncated. So: send small bursts, measure how far the
# page actually moved from the screenshots, and top up until the target is met.
WHEEL_UNITS_PER_BURST = 5
WHEEL_UNITS_MAX = 10
WHEEL_SETTLE = 0.35
WHEEL_MAX_BURSTS = 25

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
PAGES_DIR = os.path.join(OUTPUT_DIR, "pages")
ZIP_PATH = os.path.join(OUTPUT_DIR, "pages.zip")
PROMPT_PATH = os.path.join(OUTPUT_DIR, "LLM_PROMPT.md")
TEMPLATE_PATH = os.path.join(OUTPUT_DIR, "prompt_template.md")

# {n_pages} is replaced with the number of captured screenshots.
# Edit prompt_template.md to extract different fields — no code change needed.
DEFAULT_TEMPLATE = """# On-screen list screenshots — vision extraction task

You receive a zip archive containing {n_pages} screenshots of a scrollable list
(page_001.png ... in top-to-bottom order).
Look at **every image** and list **all** items you can see. Do not skip an item,
and do not merge two items into one.

## What each item usually contains
- Name (the item's main title)
- Price (e.g. "¥150"; OCR often renders ¥ as a look-alike glyph — read it as ¥)
- Type / category (e.g. indoor / outdoor)
- Feature tags (zero or more, e.g. booking-online / free-parking / floodlit)
- Address (small text under the title)
- Distance (e.g. "2km" / "1.5km")

## Output rules (follow exactly)
Output **CSV** (or a Markdown table), one row per item, with this exact header:

number,name,price,type,tag1,tag2,address,distance_km,source_page

- price: write as `¥<digits>`; leave empty if absent.
- tag1 / tag2: the first two tags; leave empty if fewer.
- distance_km: digits only (e.g. `2` or `1.5`).
- source_page: the page_xxx.png file the item came from.
- **Never** invent a value, merge items, or de-duplicate across images.
- If an image is unreadable or has no items, say so in one comment row.

## Example output
number,name,price,type,tag1,tag2,address,distance_km,source_page
1,Sample Club - Downtown,¥150,outdoor,booking-online,paid-parking,88 Sample Rd,2,page_001.png
2,Sample Sports - East,¥200,indoor,booking-online,,100 Sample North Rd,1.2,page_001.png
"""


def open_path(path):
    """Open a file or folder with the OS default handler (cross-platform)."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif os.name == "nt":
            os.startfile(path)  # noqa: S606 - Windows only
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:  # noqa: BLE001 - never let this crash the UI
        print(f"Could not open {path}: {e}")


class ListCapturerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Screen List Capturer")
        self.root.geometry("760x720")
        self.root.resizable(True, True)

        # state
        self.region_tl = None       # region top-left, in logical screen points
        self.region_br = None       # region bottom-right, in logical screen points
        self.is_running = False
        self.stop_flag = False
        self.page_count = 0

        # calibration state machine, driven by Tk's after() loop
        self._calib_state = None    # None / "tl" / "br"
        self._calib_after_id = None
        self._cd_win = None

        self._build_ui()
        self.log("Ready. Step 1: calibrate the region. Step 2: capture. Step 3: pack.")

    # ── UI ──
    def _build_ui(self):
        frame_top = ttk.LabelFrame(self.root, text="1  Region calibration", padding=10)
        frame_top.pack(fill=tk.X, padx=10, pady=(10, 5))

        row1 = ttk.Frame(frame_top)
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="Top-left:").pack(side=tk.LEFT)
        self.lbl_tl = ttk.Label(row1, text="(not set)", width=18)
        self.lbl_tl.pack(side=tk.LEFT, padx=5)
        ttk.Label(row1, text="Bottom-right:").pack(side=tk.LEFT, padx=(20, 0))
        self.lbl_br = ttk.Label(row1, text="(not set)", width=18)
        self.lbl_br.pack(side=tk.LEFT, padx=5)

        btn_frame = ttk.Frame(frame_top)
        btn_frame.pack(fill=tk.X, pady=(5, 0))
        self.btn_calibrate = ttk.Button(btn_frame, text="Calibrate region (countdown)",
                                        command=self._teach_region)
        self.btn_calibrate.pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Test capture",
                   command=self._test_capture).pack(side=tk.LEFT, padx=5)
        ttk.Label(btn_frame, text="Scroll mode:").pack(side=tk.LEFT, padx=(20, 0))
        self.cmb_scroll = ttk.Combobox(btn_frame, values=["Wheel", "Drag"],
                                       width=8, state="readonly")
        self.cmb_scroll.set("Wheel")
        self.cmb_scroll.pack(side=tk.LEFT)

        stage_frame = ttk.LabelFrame(self.root, text="2  Capture and pack", padding=8)
        stage_frame.pack(fill=tk.X, padx=10, pady=(2, 5))
        self.btn_stage1 = ttk.Button(stage_frame, text="Capture all pages",
                                     command=self._stage1_capture_all)
        self.btn_stage1.pack(side=tk.LEFT)
        self.btn_stop = ttk.Button(stage_frame, text="Stop",
                                   command=self._stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=5)
        ttk.Button(stage_frame, text="Pack for LLM",
                   command=self._stage2_pack).pack(side=tk.LEFT, padx=5)
        ttk.Button(stage_frame, text="Open pages folder",
                   command=lambda: open_path(PAGES_DIR)).pack(side=tk.LEFT, padx=5)
        ttk.Label(stage_frame, text="Overlap %:").pack(side=tk.LEFT, padx=(15, 2))
        self.spin_overlap = ttk.Spinbox(stage_frame, from_=30, to=80, width=5)
        self.spin_overlap.set(DEFAULT_OVERLAP)
        self.spin_overlap.pack(side=tk.LEFT)
        self.lbl_stage = ttk.Label(stage_frame, text="")
        self.lbl_stage.pack(side=tk.LEFT, padx=(15, 0))

        frame_mid = ttk.LabelFrame(self.root, text="Latest screenshot", padding=10)
        frame_mid.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.lbl_image = tk.Label(frame_mid, text="[no screenshot yet]",
                                  anchor=tk.CENTER, relief=tk.SUNKEN)
        self.lbl_image.pack(fill=tk.BOTH, expand=True)

        frame_bot = ttk.LabelFrame(self.root, text="Log", padding=5)
        frame_bot.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.txt_log = scrolledtext.ScrolledText(frame_bot, height=10,
                                                 font=("Consolas", 9))
        self.txt_log.pack(fill=tk.BOTH, expand=True)

    # ── logging ──
    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_log.insert(tk.END, f"[{ts}] {msg}\n")
        self.txt_log.see(tk.END)
        self.root.update_idletasks()

    # ── calibration ──
    def _teach_region(self):
        if self._calib_state is not None:
            self.log("Calibration already running — wait for the countdown.")
            return
        self.btn_calibrate.config(state=tk.DISABLED)
        self._calib_state = "tl"
        self.log("Move the mouse to the TOP-LEFT corner of the list and hold still.")
        self._calib_after_id = self.root.after(0, self._calib_tick, CAPTURE_SECONDS)

    def _calib_tick(self, remaining):
        if self._calib_state is None:
            return
        label = "Top-left" if self._calib_state == "tl" else "Bottom-right"
        if remaining > 0:
            self._show_countdown(label, remaining)
            self._calib_after_id = self.root.after(1000, self._calib_tick, remaining - 1)
            return

        self._show_countdown(label, "OK")
        pos = pyautogui.position()
        self._destroy_countdown()
        if self._calib_state == "tl":
            self.region_tl = (pos.x, pos.y)
            self.lbl_tl.config(text=f"({pos.x}, {pos.y})")
            self.log(f"Top-left recorded: ({pos.x}, {pos.y})")
            self._calib_state = "br"
            self.log("Now move to the BOTTOM-RIGHT corner of the list.")
            self._calib_after_id = self.root.after(1200, self._calib_tick, CAPTURE_SECONDS)
        else:
            self.region_br = (pos.x, pos.y)
            self.lbl_br.config(text=f"({pos.x}, {pos.y})")
            self.log(f"Bottom-right recorded: ({pos.x}, {pos.y})")
            self._finish_calib()

    def _show_countdown(self, label, text):
        if self._cd_win is None or not self._cd_win.winfo_exists():
            self._cd_win = tk.Toplevel(self.root)
            self._cd_win.overrideredirect(True)
            self._cd_win.attributes("-topmost", True)
            self._cd_label = tk.Label(self._cd_win, font=("Arial", 36, "bold"),
                                      fg="#B3261E", bg="#FFF6CC", padx=30, pady=15)
            self._cd_label.pack()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self._cd_win.geometry(f"240x120+{(sw - 240) // 2}+{(sh - 120) // 2}")
        self._cd_label.config(text=f"{label}\n{text}")
        self._cd_win.update()

    def _destroy_countdown(self):
        if self._cd_win is not None and self._cd_win.winfo_exists():
            self._cd_win.destroy()
        self._cd_win = None

    def _finish_calib(self):
        self._calib_state = None
        self.btn_calibrate.config(state=tk.NORMAL)
        if self.region_br[0] <= self.region_tl[0] or self.region_br[1] <= self.region_tl[1]:
            messagebox.showerror("Error", "Bottom-right must be below and to the "
                                          "right of top-left. Please calibrate again.")
            return
        w = self.region_br[0] - self.region_tl[0]
        h = self.region_br[1] - self.region_tl[1]
        self.log(f"Region ready: {w}x{h} points")
        self._test_capture()

    # ── capture ──
    def _capture_region(self):
        """Screenshot the calibrated region. Returns a PIL Image or None."""
        if not self.region_tl or not self.region_br:
            return None
        try:
            # ImageGrab.grab() takes logical points (verified on macOS Retina:
            # it returns a 2x pixel image for a logical box), so the calibrated
            # point coordinates can be passed straight through.
            bbox = (self.region_tl[0], self.region_tl[1],
                    self.region_br[0], self.region_br[1])
            return ImageGrab.grab(bbox=bbox)
        except Exception as e:  # noqa: BLE001
            self.log(f"Capture failed: {e}")
            return None

    def _test_capture(self):
        if not self.region_tl or not self.region_br:
            messagebox.showwarning("Warning", "Calibrate the region first.")
            return
        img = self._capture_region()
        if img is None:
            return
        self._show_image(img)
        self.log(f"Test capture OK: {img.size[0]}x{img.size[1]} px")

    def _show_image(self, pil_img):
        img = pil_img.copy()
        img.thumbnail((520, 420), Image.Resampling.LANCZOS)
        self.tk_img = ImageTk.PhotoImage(img)
        self.lbl_image.config(image=self.tk_img, text="")

    # ── stage 1: capture every page ──
    def _stage1_capture_all(self):
        if self.is_running:
            self.log("A capture is already running.")
            return
        if not self.region_tl or not self.region_br:
            messagebox.showwarning("Warning", "Calibrate the region first.")
            return

        try:
            overlap = max(30, min(80, int(self.spin_overlap.get())))
        except ValueError:
            overlap = DEFAULT_OVERLAP
        method = self.cmb_scroll.get() or "Wheel"
        area_h = self.region_br[1] - self.region_tl[1]
        # scroll just under one screen, so consecutive pages always overlap
        scroll_dist = max(60, int(area_h * (1 - overlap / 100.0)))

        os.makedirs(PAGES_DIR, exist_ok=True)
        for f in glob.glob(os.path.join(PAGES_DIR, "page_*.png")):
            try:
                os.remove(f)
            except OSError:
                pass

        self.is_running = True
        self.stop_flag = False
        self.page_count = 0
        self.btn_stage1.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.log("=" * 50)
        self.log(f"Capture started (overlap={overlap}%, step={scroll_dist}px, mode={method})")
        threading.Thread(target=self._stage1_worker,
                         args=(scroll_dist, method, area_h), daemon=True).start()

    def _stop(self):
        if self.is_running:
            self.stop_flag = True
            self.log("Stopping after the current page...")

    def _stage1_worker(self, scroll_dist, method, area_h):
        cx = (self.region_tl[0] + self.region_br[0]) // 2
        cy = (self.region_tl[1] + self.region_br[1]) // 2
        prev_img = None
        near_count = 0
        fail_count = 0
        page_idx = 0

        def scroll_and_wait():
            self._scroll(cx, cy, scroll_dist, method, area_h)
            time.sleep(SCROLL_PAUSE)

        try:
            while page_idx < MAX_PAGES and not self.stop_flag:
                img = self._capture_region()
                if img is None:
                    fail_count += 1
                    if fail_count >= 5:
                        self.log("Capture failed 5 times in a row — aborting.")
                        break
                    scroll_and_wait()
                    continue
                fail_count = 0

                # end-of-list detection: compare against the previous screen
                if prev_img is not None:
                    diff = self._frame_diff(prev_img, img)
                    if diff is not None and diff < DIFF_NEAR:
                        # could be mid-bounce — let it settle and look again
                        time.sleep(SETTLE_SECONDS)
                        settled = self._capture_region()
                        if settled is not None:
                            diff2 = self._frame_diff(prev_img, settled)
                            if diff2 is not None and diff2 >= DIFF_NEAR:
                                self.log("  bounce settled into new content")
                                img, diff = settled, diff2
                    if diff is not None:
                        self.log(f"  change vs previous page: {diff:.2f}")
                        if diff < DIFF_IDENTICAL:
                            if page_idx <= 1:
                                self.log("Nothing moved after the first scroll — check "
                                         "scroll mode and macOS Accessibility permission.")
                            else:
                                self.log("Page unchanged after scrolling — end of list reached.")
                            break
                        if diff < DIFF_NEAR:
                            near_count += 1
                            self.log(f"  near-identical page {near_count}/{NEAR_LIMIT} — not saved")
                            if near_count >= NEAR_LIMIT:
                                self.log("Content stopped changing — end of list reached.")
                                break
                        else:
                            near_count = 0

                if near_count:
                    # only jitter or scroll bounce — keep going, but do not save it
                    prev_img = img
                    scroll_and_wait()
                    continue

                page_idx += 1
                path = os.path.join(PAGES_DIR, f"page_{page_idx:03d}.png")
                img.save(path)
                self.page_count = page_idx
                self.root.after(0, lambda i=img: self._show_image(i))
                self.root.after(0, self._set_stage_label, f"{page_idx} pages")
                self.log(f"Saved page {page_idx} -> {os.path.basename(path)}")
                prev_img = img

                if self.stop_flag or page_idx >= MAX_PAGES:
                    break
                scroll_and_wait()

            self.log(f"Capture finished: {page_idx} pages in {PAGES_DIR}")
            self.root.after(0, self._set_stage_label, f"captured {page_idx}")
        except pyautogui.FailSafeException:
            self.log("FailSafe triggered (mouse hit a screen corner) — aborted.")
        except Exception as e:  # noqa: BLE001
            import traceback
            self.log(f"Capture error: {e}\n{traceback.format_exc()}")
        finally:
            self.is_running = False
            self.root.after(0, self._set_stage_label, f"captured {page_idx}")
            self.root.after(0, lambda: self.btn_stage1.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.btn_stop.config(state=tk.DISABLED))

    def _set_stage_label(self, text):
        self.lbl_stage.config(text=text)

    def _frame_diff(self, a, b):
        """Mean per-pixel difference between two pages, 0-255.

        The best (smallest) difference over vertical offsets in
        [-MAX_SHIFT, MAX_SHIFT] is returned, so a scroll bounce that shifts the
        whole page by a few pixels reads as ~0 while a genuinely new page of
        content stays high. Returns None if the frames cannot be compared.
        """
        try:
            ga, gb = a.convert("L"), b.convert("L")
            w, h = ga.size
            margin = MAX_SHIFT
            if h <= 2 * margin + 8:
                return None
            best = None
            for dy in range(-MAX_SHIFT, MAX_SHIFT + 1):
                ca = ga.crop((0, margin, w, h - margin))
                cb = gb.crop((0, margin + dy, w, h - margin + dy))
                ca = ca.resize((64, 64))
                cb = cb.resize((64, 64))
                diff = ImageStat.Stat(ImageChops.difference(ca, cb)).mean[0]
                best = diff if best is None else min(best, diff)
            return best
        except Exception:  # noqa: BLE001
            return None

    def _best_shift(self, a, b, max_dy=None):
        """How many pixels b is scrolled up relative to a.

        Uses a fixed-size window anchored at the top of the frame, so no dy is
        favoured by stretching. Returns (dy, residual); dy is 0 when the frames
        are not the same content shifted.
        """
        try:
            ga, gb = a.convert("L"), b.convert("L")
            w, h = ga.size
            win = max(16, int(h * SHIFT_WINDOW_FRAC))
            search_max = h - win
            if win < 16 or search_max < 1:
                return 0, None
            if max_dy is None:
                max_dy = search_max
            max_dy = max(0, min(max_dy, search_max))

            def score(dy):
                if dy < 0 or dy > search_max:
                    return None
                ca = ga.crop((0, dy, w, dy + win)).resize((96, 160))
                cb = gb.crop((0, 0, w, win)).resize((96, 160))
                return ImageStat.Stat(ImageChops.difference(ca, cb)).mean[0]

            best_diff = score(0)
            best_dy = 0
            for dy in range(0, max_dy + 1, 8):                    # coarse
                d = score(dy)
                if d is not None and d < best_diff:
                    best_diff, best_dy = d, dy
            for dy in range(max(0, best_dy - 8),                   # refine
                            min(max_dy, best_dy + 8) + 1):
                d = score(dy)
                if d is not None and d < best_diff:
                    best_diff, best_dy = d, dy
            if best_diff is not None and best_diff > MOVE_DIFF_MAX:
                return 0, best_diff
            return best_dy, best_diff
        except Exception:  # noqa: BLE001
            return 0, None

    def _wheel_scroll_to(self, cx, cy, target_px):
        """Scroll about target_px points; return how far it actually scrolled.

        Screenshots are in physical pixels while the target is in points, so the
        measured shift is converted back with the same ratio.
        """
        pyautogui.moveTo(cx, cy)
        time.sleep(0.2)
        prev = self._capture_region()
        if prev is None:
            pyautogui.scroll(-WHEEL_UNITS_PER_BURST)
            return 0.0

        moved = 0.0
        units_total = 0
        px_per_unit = None
        for i in range(WHEEL_MAX_BURSTS):
            if target_px - moved <= 8:
                break
            units = (WHEEL_UNITS_PER_BURST if not px_per_unit
                     else max(1, min(WHEEL_UNITS_MAX,
                                     int((target_px - moved) / px_per_unit))))
            pyautogui.scroll(-units)
            units_total += units
            time.sleep(WHEEL_SETTLE)
            now = self._capture_region()
            if now is None:
                break
            dy, _ = self._best_shift(prev, now)
            if dy <= 0:
                self.log(f"  wheel burst {i + 1}: no movement — end of list or "
                         "the app ignored the event")
                break
            scale = max(1.0, now.width / float(max(1, self.region_br[0] - self.region_tl[0])))
            moved += dy / scale
            prev = now
            px_per_unit = max(1.0, moved / float(units_total))

        self.log(f"  wheel: asked {target_px:.0f}pt, moved {moved:.0f}pt "
                 f"({units_total} units)")
        return moved

    def _scroll(self, cx, cy, dist, method, area_h):
        if method == "Wheel":
            moved = self._wheel_scroll_to(cx, cy, dist)
            if moved <= 0:
                for _ in range(3):                 # last resort, in bursts
                    pyautogui.scroll(-WHEEL_UNITS_MAX)
                    time.sleep(WHEEL_SETTLE)
        else:
            # drag up, keeping both endpoints inside the region
            safe = min(dist, max(60, area_h - 40))
            y_start = min(cy + safe // 2, self.region_br[1] - 10)
            y_end = max(y_start - safe, self.region_tl[1] + 10)
            pyautogui.moveTo(cx, y_start)
            time.sleep(0.2)
            pyautogui.mouseDown()
            pyautogui.moveTo(cx, y_end, duration=0.35)
            pyautogui.mouseUp()

    # ── stage 2: pack and build the prompt ──
    def _stage2_pack(self):
        files = sorted(glob.glob(os.path.join(PAGES_DIR, "page_*.png")))
        if not files:
            messagebox.showwarning("Warning",
                                   f"No page_*.png found in {PAGES_DIR}.\n"
                                   "Run 'Capture all pages' first.")
            return

        try:
            with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.write(f, os.path.basename(f))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Pack failed", str(e))
            return

        prompt_md = self._build_prompt(len(files))
        try:
            with open(PROMPT_PATH, "w", encoding="utf-8") as fh:
                fh.write(prompt_md)
        except Exception as e:  # noqa: BLE001
            self.log(f"Could not write prompt file: {e}")

        self.log("=" * 50)
        self.log(f"Packed {len(files)} screenshots -> {ZIP_PATH}")
        self.log(f"Prompt written -> {PROMPT_PATH}")
        self._set_stage_label(f"packed {len(files)}")

        self._copy_to_clipboard(prompt_md)
        messagebox.showinfo(
            "Packed",
            f"{len(files)} screenshots -> pages.zip\n\n"
            "Upload that zip to any vision-capable LLM together with\n"
            "LLM_PROMPT.md. It returns CSV; convert it with csv_to_xlsx.py.\n\n"
            "(The prompt text was also copied to your clipboard.)")

    def _build_prompt(self, n_pages):
        template = DEFAULT_TEMPLATE
        if os.path.exists(TEMPLATE_PATH):
            try:
                with open(TEMPLATE_PATH, encoding="utf-8") as fh:
                    template = fh.read()
                self.log("Using custom prompt_template.md")
            except Exception as e:  # noqa: BLE001
                self.log(f"Could not read prompt_template.md, using default: {e}")
        return template.replace("{n_pages}", str(n_pages))

    def _copy_to_clipboard(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log("Prompt copied to clipboard.")
        except Exception:  # noqa: BLE001
            pass


def main():
    root = tk.Tk()
    ListCapturerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
