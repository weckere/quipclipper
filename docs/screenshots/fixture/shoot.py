"""Shoot the README screenshots (docs/screenshots/) against a fixture dev server.

    FIXTURE=<make-fixture.sh OUT_DIR> OUT=<dir> python shoot.py [shot ...]

The server runs with QC_MEDIA_ROOTS=$FIXTURE/Movies:$FIXTURE/Shows on :8017 and
Jellyfin pointed at fake_jellyfin.py (see README.md here). Needs Playwright and
Google Chrome (Playwright's own Chromium can't play H.264). Compress with
pngquant afterwards.
"""
import asyncio, sys
from playwright.async_api import async_playwright

import os
OUT = os.environ.get("OUT", ".") + "/"
FIXTURE = os.environ["FIXTURE"]
BASE = "http://localhost:8017/"
SHOWS = FIXTURE + "/Shows"
ONLY = set(sys.argv[1:])


async def settle(page, ms=800):
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(ms)


async def home(ctx):
    page = await ctx.new_page()
    page.on("console", lambda m: print("  console:", m.type, m.text) if m.type == "error" else None)
    await page.goto(BASE)
    await page.wait_for_selector("#nowplaying-list button")
    await settle(page)
    return page


async def open_movie_from_now_playing(page):
    # The card's second session is the movie, paused at 0:40.
    btn = page.locator("#nowplaying-list button", has_text="0:40")
    await btn.click()
    await page.wait_for_function("document.querySelector('#script-list .sel, #script-list [aria-selected=true], #script-list .selected') !== null || (typeof clipFirst !== 'undefined' && clipFirst >= 0)")
    await page.wait_for_function("(() => { const v = document.getElementById('player'); return v && v.readyState >= 2 && Math.abs(v.currentTime - 40.1) < 0.6; })()", timeout=20000)
    await page.evaluate("document.getElementById('player').pause()")
    await settle(page, 1500)


async def shot(page, name, clip=None, full=False):
    if ONLY and name not in ONLY:
        return
    kw = {"path": OUT + name + ".png"}
    if clip:
        kw["clip"] = clip
    await page.screenshot(**kw)
    print("shot", name)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        dark = await b.new_context(viewport={"width": 1600, "height": 1000}, color_scheme="dark")

        # Index the season first, so the card says so and search is quick.
        page = await home(dark)
        await shot(page, "now-playing", clip={"x": 0, "y": 0, "width": 1600, "height": 310})
        await page.evaluate("p => browse(p)", SHOWS + "/The Next Leporation/Season 01")
        await settle(page)
        if await page.locator("#index-btn").is_visible():
            await page.click("#index-btn")
            await page.wait_for_function("!document.getElementById('index-btn') || document.getElementById('index-btn').hidden || /indexed/i.test(document.getElementById('index-card-text').textContent)", timeout=60000)
            await settle(page, 1500)
        await shot(page, "library", clip={"x": 0, "y": 0, "width": 1600, "height": 425})

        # Dialogue search over the whole library (from a folder, so the Now
        # playing card is out of the way), tick two hits, preview the first.
        # As on a server without Jellyfin: no Now playing card over the results.
        await page.evaluate("appConfig.jellyfin = false")
        await page.evaluate("p => browse(p)", SHOWS)
        await settle(page)
        await page.click("#search-scope")
        await page.click("#scope-menu [data-scope=library]")
        await page.fill("#global-search-input", "resistance is futile")
        await page.press("#global-search-input", "Enter")
        await page.wait_for_selector("#entries .hit, #entries [data-hit], #entries li", timeout=60000)
        await settle(page, 1500)
        first_hit = page.locator("#entries .hit-row, #entries .hit").first
        boxes = page.locator("#entries input[type=checkbox]")
        for i in (1, 2):
            await boxes.nth(i).check()
        if await first_hit.count():
            await first_hit.click()
            await settle(page, 1500)
        await shot(page, "search-results")
        await page.close()

        # The workspace, opened from Now playing.
        page = await home(dark)
        await open_movie_from_now_playing(page)
        await shot(page, "workspace-dark")
        await page.close()
        await dark.close()

        # Send to LeadStinger: the toggle beside Make clip, ticked, at 2x.
        hi = await b.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=2, color_scheme="dark")
        page = await home(hi)
        await open_movie_from_now_playing(page)
        await page.locator("#ls-send").check(force=True)
        await page.fill("#clip-before", "2")
        await page.fill("#clip-after", "2")
        await page.evaluate("document.activeElement.blur()")
        await settle(page, 500)
        box = await page.locator(".ws-clip").bounding_box()
        await shot(page, "send-to-leadstinger", clip={"x": box["x"] - 16, "y": box["y"] - 16, "width": box["width"] + 32, "height": box["height"] + 32})
        await hi.close()

        light = await b.new_context(viewport={"width": 1600, "height": 1000}, color_scheme="light")
        page = await home(light)
        await open_movie_from_now_playing(page)
        await shot(page, "workspace-light")
        await light.close()

        phone = await b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                    is_mobile=True, has_touch=True, color_scheme="dark")
        page = await home(phone)
        await open_movie_from_now_playing(page)
        await shot(page, "phone")
        await phone.close()
        await b.close()

asyncio.run(main())
