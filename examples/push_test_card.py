"""Drive the panel: build a frame, encode it, push it over our own stack.

Listens on TCP 11113, waits for a sign to dial in, pushes a test card, then
waits for the device to report a ``DisplayStateCRC`` equal to the checksum we
computed -- i.e. the device confirming, pixel for pixel, what it drew.

Verified against a 31.2" Place&Play 32 on firmware 7.4.4407.
Point the sign at this host first: ``server_tcp_set <ip> 11113`` over USB.
"""
import asyncio, os, sys, time, datetime
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pyvisionect.io.tcp import VisionectServer
from pyvisionect.session import DeviceStateStore
from pyvisionect.imaging import encode_frame
from pyvisionect import devices
from pyvisionect.packets.status import decode_status_fields

W, H = 1440, 2560
FONT = os.environ.get("PYVISIONECT_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

def font(sz):
    try: return ImageFont.truetype(FONT, sz)
    except Exception: return ImageFont.load_default()

def build():
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W - 1, 300], fill=0)
    d.text((60, 70), "pyvisionect", font=font(130), fill=255)
    d.text((64, 215), "driving this panel directly", font=font(44), fill=200)

    y = 380
    for label, val in [
        ("rendered",      datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("server",        "pyvisionect  (no VSS)"),
        ("panel",         '31.2" PP32 v1.1 - 2 x 2880x640'),
        ("encoding",      "4 bpp, 16 levels (n*17)"),
        ("dither",        "blue noise (void-and-cluster)"),
        ("firmware",      "7.4.4407  crc 652be4ad"),
    ]:
        d.text((60, y), f"{label}", font=font(38), fill=90)
        d.text((430, y), val, font=font(38), fill=0)
        y += 62

    # 16-level ramp: every grey the panel can actually show
    y += 40
    d.text((60, y), "16 grey levels", font=font(38), fill=90); y += 58
    bw = (W - 120) // 16
    for i in range(16):
        d.rectangle([60 + i * bw, y, 60 + (i + 1) * bw - 2, y + 150], fill=i * 17)
    y += 200

    # continuous gradient -> exercises the dither
    d.text((60, y), "continuous gradient", font=font(38), fill=90); y += 58
    grad = np.linspace(0, 255, W - 120, dtype=np.uint8)[None, :].repeat(260, 0)
    img.paste(Image.fromarray(grad), (60, y)); y += 310

    # radial, the classic dither torture test
    yy, xx = np.mgrid[0:420, 0:(W - 120)]
    cx, cy = (W - 120) / 2, 210
    r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    img.paste(Image.fromarray((127 + 120 * np.cos(r / 26)).astype(np.uint8)), (60, y))
    return img

async def main():
    panel = devices.panel_for(0xC2050128)
    img = build()
    print(f"encoding {img.size} for {panel}", flush=True)
    t0 = time.time()
    frame = encode_frame(img, panel=panel, encoding=4, dithering=4)
    print(f"encoded in {time.time()-t0:.2f}s: {len(frame.rectangles)} rects, "
          f"state_checksum={frame.state_checksum}", flush=True)
    for r in frame.rectangles:
        print(f"   rect screen={r.screen_id} {r.w}x{r.h} @({r.x},{r.y}) {len(r.data)} bytes", flush=True)

    store = DeviceStateStore()
    done = asyncio.Event()
    sent = {"id": None}

    def on_events(conn, events):
        for e in events:
            name = type(e).__name__
            st = getattr(e, "status", None)
            if st is not None:
                f = decode_status_fields(dict(st.records))
                print(f"[{time.strftime('%H:%M:%S')}] {name}: "
                      f"crc={f.get('DisplayStateCRC')} updates={f.get('DisplayUpdateCount')} "
                      f"reason={f.get('ConnectReason',{}).get('name')}", flush=True)
                if f.get("DisplayStateCRC") == frame.state_checksum:
                    print("*** DEVICE CONFIRMS OUR CHECKSUM — IMAGE IS ON THE GLASS ***", flush=True)
                    done.set(); return
            else:
                print(f"[{time.strftime('%H:%M:%S')}] {name}", flush=True)
            if sent["id"] is None and conn is not None:
                try:
                    sent["id"] = conn.send_image(frame)
                    print(f"--> pushed image, packet id {sent['id']}", flush=True)
                except Exception as exc:
                    print(f"!!! send_image failed: {exc!r}", flush=True)

    srv = VisionectServer(on_events=on_events, store=store, host="0.0.0.0", port=11113)
    await srv.start()
    print("listening; waiting for the sign to connect ...", flush=True)
    try:
        await asyncio.wait_for(done.wait(), timeout=420)
    except asyncio.TimeoutError:
        print("timed out waiting for the device to confirm", flush=True)
    finally:
        await srv.close()

asyncio.run(main())
