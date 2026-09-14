# On-screen list screenshots — vision extraction task

You receive a zip archive containing {n_pages} screenshots of a scrollable list
(page_001.png ... in top-to-bottom order).
Look at **every image** and list **all** items you can see. Do not skip an item,
and do not merge two items into one.

> Edit this file freely — `screen_list_capturer.py` reads it at pack time and
> only replaces `{n_pages}`. Change the fields below to match your own list.

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
