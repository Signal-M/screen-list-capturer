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
On HiDPI/Retina displays the tool auto-detects the scale factor and converts
coordinates for you.

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
    from PIL import Image, ImageGrab, ImageTk
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")


# ============================================================
# Configuration
# ============================================================
CAPTURE_SECONDS = 5        # countdown before each calibration point is recorded
SCROLL_PAUSE = 1.5         # seconds to wait for the list to settle after scrolling
MAX_PAGES = 200            # hard cap on pages, to avoid an endless loop
DEFAULT_OVERLAP = 50       # % of the region kept as overlap between two pages

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
        self.scale = 1.0            # HiDPI / Retina scale factor

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
        self._detect_scale()
        self.log(f"Region ready: {w}x{h} points")
        self._test_capture()

    def _detect_scale(self):
        """On HiDPI screens the screenshot pixel grid is larger than the logical
        point grid used by mouse coordinates. Cache the ratio once."""
        try:
            full = ImageGrab.grab()
            self.scale = round(full.width / float(pyautogui.size().width), 3) or 1.0
        except Exception as e:  # noqa: BLE001
            self.log(f"Scale detection failed, assuming 1x: {e}")
            self.scale = 1.0
        if self.scale > 1.0:
            self.log(f"HiDPI display detected ({self.scale}x) — capture box scaled accordingly.")

    # ── capture ──
    def _capture_region(self):
        """Screenshot the calibrated region. Returns a PIL Image or None."""
        if not self.region_tl or not self.region_br:
            return None
        try:
            s = self.scale
            bbox = (int(self.region_tl[0] * s), int(self.region_tl[1] * s),
                    int(self.region_br[0] * s), int(self.region_br[1] * s))
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
        prev_bottom_hash = None
        unchanged = 0
        page_idx = 0

        try:
            while page_idx < MAX_PAGES and not self.stop_flag:
                page_idx += 1
                img = self._capture_region()
                if img is None:
                    self.log("Capture failed, skipping this page")
                    continue

                path = os.path.join(PAGES_DIR, f"page_{page_idx:03d}.png")
                img.save(path)
                self.page_count = page_idx
                self.root.after(0, lambda i=img: self._show_image(i))
                self.root.after(0, self._set_stage_label, f"{page_idx} pages")
                self.log(f"Saved page {page_idx} -> {os.path.basename(path)}")

                # stop when the bottom strip stops changing (= end of list)
                cur_hash = self._bottom_hash(img)
                if cur_hash is not None and cur_hash == prev_bottom_hash:
                    unchanged += 1
                else:
                    unchanged = 0
                prev_bottom_hash = cur_hash
                if page_idx > 3 and unchanged >= 3:
                    self.log("Bottom unchanged for 3 pages — end of list reached.")
                    break

                if self.stop_flag or page_idx >= MAX_PAGES:
                    break
                self._scroll(cx, cy, scroll_dist, method, area_h)
                time.sleep(SCROLL_PAUSE)

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

    def _bottom_hash(self, img):
        """Hash the bottom 35% of the image to detect 'no more content'."""
        try:
            strip = img.convert("L").crop(
                (0, int(img.height * 0.65), img.width, img.height))
            return strip.resize((32, 8)).tobytes()
        except Exception:  # noqa: BLE001
            return None

    def _scroll(self, cx, cy, dist, method, area_h):
        if method == "Wheel":
            pyautogui.moveTo(cx, cy)
            time.sleep(0.2)
            pyautogui.scroll(-dist)
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
