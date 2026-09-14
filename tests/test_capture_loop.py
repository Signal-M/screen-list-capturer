#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Headless simulation of the capture loop — no display, no mouse, no permissions.

End-of-list detection is the part of this tool most likely to break, and it is
impossible to eyeball: it only shows up after a real list has run out. So it is
simulated here. A long list image is captured the way a real scroll would:
page i is the window at y = i * step, clamped at the bottom, so once the list is
exhausted the frames repeat — exactly the "scrolled to the end" condition.

Run:
    python tests/test_capture_loop.py
"""

import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import screen_list_capturer as m  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

W, H, STEP, WIN = 400, 2600, 400, 800


def long_list():
    random.seed(7)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    y = 0
    while y < H:
        d.rectangle([8, y + 6, W - 8, y + 110], fill=(245, 245, 245), outline=(220, 220, 220))
        d.rectangle([20, y + 18, 20 + random.randint(90, 220), y + 36], fill=(40, 40, 40))
        d.rectangle([20, y + 48, 20 + random.randint(60, 150), y + 62], fill=(120, 120, 120))
        d.rectangle([20, y + 72, 20 + random.randint(40, 110), y + 84], fill=(180, 180, 180))
        y += 120
    return img


LIST = long_list()


def frame(i, jitter=0):
    top = min(i * STEP, H - WIN)
    img = LIST.crop((0, top, W, top + WIN))
    if jitter:
        img = img.transform(img.size, Image.AFFINE, (1, 0, 0, 0, 1, jitter))
    return img.copy()


class FakeRoot:
    def after(self, ms, fn, *args):
        if callable(fn):
            fn(*args)
        return "id"


class FakeWidget:
    def config(self, **kw):
        pass


def run(build_feed, label, expected):
    tmp = tempfile.mkdtemp(prefix="slc-sim-")
    m.PAGES_DIR = tmp

    app = object.__new__(m.ListCapturerApp)
    app.root = FakeRoot()
    app.btn_stage1 = FakeWidget()
    app.btn_stop = FakeWidget()
    app.lbl_stage = FakeWidget()
    app.region_tl = (0, 0)
    app.region_br = (W, WIN)
    app.stop_flag = False
    app.is_running = True
    app.page_count = 0
    app.scale = 1.0
    app._show_image = lambda img: None
    app._scroll = lambda *a, **k: None

    feed = build_feed()
    app._capture_region = lambda: feed.pop(0) if feed else frame(0)

    logs = []
    app.log = logs.append

    real_sleep = m.time.sleep
    m.time.sleep = lambda s: None
    try:
        app._stage1_worker(STEP, "Wheel", WIN)
    finally:
        m.time.sleep = real_sleep

    saved = len([f for f in os.listdir(tmp) if f.startswith("page_")])
    shutil.rmtree(tmp, ignore_errors=True)
    m.PAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(m.__file__)), "pages")
    reason = [l for l in logs if "end of list" in l or "aborting" in l
              or "Nothing moved" in l] or ["NONE — DID NOT STOP"]
    print(f"  {'PASS' if saved == expected else 'FAIL'}  {label}: "
          f"{saved} page(s) saved (expected {expected}) — {reason[-1]}")
    return saved == expected


def main():
    print("End-of-list detection")
    ok = True
    ok &= run(lambda: [frame(i) for i in range(12)],
              "scrolls to bottom, then frames repeat", 6)
    ok &= run(lambda: [frame(i) for i in range(5)] + [frame(4, jitter=6), frame(4), frame(4)],
              "6px scroll bounce at the bottom", 6)
    ok &= run(lambda: [frame(i) for i in range(5)] + [frame(4, jitter=20)] + [frame(4)] * 3,
              "20px scroll bounce at the bottom (keeps 1 duplicate)", 7)
    ok &= run(lambda: [frame(0)] * 6,
              "scrolling has no effect at all", 1)
    print("\nOK" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
