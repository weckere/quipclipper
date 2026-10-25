"""Shoot the README screenshots (docs/screenshots/) against a fixture dev server.

    FIXTURE=<make-fixture.sh OUT_DIR> OUT=<dir> python shoot.py [shot ...]

The server runs with QC_MEDIA_ROOTS=$FIXTURE/Movies:$FIXTURE/Shows on :8017 and
Jellyfin pointed at fake_jellyfin.py (see README.md here). Needs Playwright and
Google Chrome (Playwright's own Chromium can't play H.264). Compress with
pngquant afterwards.
"""
import asyncio, json, sys, urllib.request
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
    # The waveform strip: wait for its peaks (the overview is built on first open).
    await page.wait_for_function("wf.data !== null && document.getElementById('wf-note').textContent === ''", timeout=60000)
    await settle(page, 1500)


def api(method, path, body=None):
    """Call the fixture server's API as the app (pairing) or a person would."""
    req = urllib.request.Request(BASE + "api/" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Quipclipper": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


async def bottom(page, selector, pad=16):
    """The page y just below the last element matching `selector`, plus `pad`."""
    box = await page.locator(selector).last.bounding_box()
    return round(box["y"] + box["height"] + pad)


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
        # Down to the home's Dialogue index card and its Index the whole library button.
        await page.wait_for_selector("#lib-index-btn:not([hidden])")
        await shot(page, "now-playing", clip={"x": 0, "y": 0, "width": 1600,
                                            "height": await bottom(page, "#index-card", 10)})
        await page.evaluate("p => browse(p)", SHOWS + "/The Next Leporation/Season 01")
        await settle(page)
        if await page.locator("#index-btn").is_visible():
            await page.click("#index-btn")
            await page.wait_for_function("!document.getElementById('index-btn') || document.getElementById('index-btn').hidden || /indexed/i.test(document.getElementById('index-card-text').textContent)", timeout=60000)
            await settle(page, 1500)
        # One episode's subtitles "changed" since indexing (a newer mtime), so the folder
        # shows the changed chip and the Re-index them banner; the old mtime goes back after.
        srt = SHOWS + "/The Next Leporation/Season 01/The Next Leporation - S01E03 - Q Who Hops.srt"
        st = os.stat(srt)
        os.utime(srt, (st.st_atime, st.st_mtime + 3600))
        await page.evaluate("p => browse(p)", SHOWS + "/The Next Leporation/Season 01")
        await page.wait_for_selector("#changed-banner:not([hidden])", timeout=30000)
        await settle(page, 1000)
        await shot(page, "library", clip={"x": 0, "y": 0, "width": 1600,
                                        "height": await bottom(page, "#entries li")})
        os.utime(srt, (st.st_atime, st.st_mtime))

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

        # Send to LeadStinger: the button beside Make clip, hovered so its tooltip shows, at 2x.
        hi = await b.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=2, color_scheme="dark")
        page = await home(hi)
        await open_movie_from_now_playing(page)
        await page.fill("#clip-before", "2")
        await page.fill("#clip-after", "2")
        await page.evaluate("document.activeElement.blur()")
        await page.evaluate("document.querySelector('.ws-clip').scrollIntoView({block: 'center'})")
        await page.hover("#ls-send")
        await settle(page, 700)
        box = await page.locator(".ws-clip").bounding_box()
        tip = await page.locator("#ls-send-tip").bounding_box()
        top = min(box["y"], tip["y"]) - 16
        await shot(page, "send-to-leadstinger", clip={"x": box["x"] - 16, "y": top, "width": box["width"] + 32, "height": box["y"] + box["height"] + 16 - top})
        await hi.close()

        # Machine transcription (on where whisper is installed, D-053; the fixture has a stand-in
        # and seed_transcript.py's lines): the side panel with the slim transcript bar closed,
        # then open, at 2x, cropped to the bar and the script lines below it.
        hi = await b.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=2, color_scheme="dark")
        page = await home(hi)
        await open_movie_from_now_playing(page)
        await page.wait_for_selector("#stt-bar:not([hidden])")
        await page.wait_for_selector("#script-list .script-machine")
        await page.evaluate("document.activeElement.blur()")
        side = await page.locator(".ws-side").bounding_box()
        crop = {"x": side["x"], "y": side["y"], "width": side["width"], "height": min(side["height"], 600)}
        await shot(page, "transcript", clip=crop)
        await page.click("#stt-toggle")
        await page.mouse.move(0, 0)  # no hover tooltip over the opened bar
        await settle(page, 700)
        await shot(page, "transcript-open", clip=crop)
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
        # Scroll the waveform strip up to just above the selection pinned to the bottom,
        # but never past the title.
        await page.evaluate("""() => {
            const wave = document.getElementById('waveform').getBoundingClientRect();
            const bar = document.querySelector('.ws-clip').getBoundingClientRect();
            const title = document.getElementById('item-name').getBoundingClientRect();
            window.scrollBy(0, Math.min(wave.bottom + 12 - bar.top, title.top - 8));
        }""")
        await settle(page, 800)
        await shot(page, "phone")
        await phone.close()

        # Pairing: LeadStinger on another computer asks for access (the banner), and
        # Settings > Connected apps lists one already let in (shot at 2x, cropped). Shot last: the request
        # is denied afterwards, so it shows on no other page.
        if not any(c["host"] == "booth-mini" for c in api("GET", "clients")["clients"]):
            done = api("POST", "pair", {"name": "LeadStinger", "host": "booth-mini"})
            api("POST", f"pair/{done['id']}/approve")
        ask = api("POST", "pair", {"name": "LeadStinger", "host": "studio-macbook"})
        try:
            dark = await b.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=2,
                                       color_scheme="dark")
            page = await home(dark)
            await page.wait_for_selector("#pair-banner:not([hidden])", timeout=30000)
            await settle(page, 700)
            ban = await page.locator("#pair-banner").bounding_box()
            await shot(page, "pairing", clip={"x": ban["x"] - 16, "y": ban["y"] - 16,
                                            "width": ban["width"] + 32, "height": ban["height"] + 32})
            # Connected apps, at the foot of the Settings popover, cropped to the popover.
            await page.click("#settings-btn")
            await page.wait_for_selector("#apps-list li")
            await page.evaluate("document.getElementById('apps-setting').scrollIntoView({block: 'end'})")
            await settle(page, 700)
            pop = await page.locator("#settings-popover").bounding_box()
            await shot(page, "connected-apps", clip={"x": pop["x"] - 16, "y": pop["y"] - 16,
                                                   "width": pop["width"] + 32, "height": pop["height"] + 32})
            await dark.close()
        finally:
            api("POST", f"pair/{ask['id']}/deny")
        await b.close()

asyncio.run(main())
