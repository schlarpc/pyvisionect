# Imaging: encodings, packing, geometry and the state checksum

Everything in this file was first recovered statically from the vendor binaries, then
cross-checked against a real 1.84 MB image push captured from a live sign and against the
server's own preview reconstruction of the same frame. The byte-exact confirmations are:

- `ImageHeader` and `RectangleHeader` field values on the wire [W]
- the 4 bpp nibble order, the `n*17` grey ramp, the horizontal mirror, and the complete
  interlace plane→display map — **100.0000% exact pixel match** against the server's own
  reconstruction [W]
- the block-chain flag polarity [W]
- the `DisplayStateCRC` ⇄ `ImageHeader.Checksum` identity [W]

All of it was then confirmed a second way, by pushing a frame to the same sign from a
from-scratch implementation with no vendor software in the path: the device acked it and,
on the next heartbeat, reported back a `DisplayStateCRC` exactly equal to the checksum the
reimplementation had computed, with `DisplayUpdateCount` advancing by one. [W]

## Payload layout (packet type 5)

```
ImageHeader (20 B)
   0   4  Checksum        uint32 LE   the state tag — see below
   4   4  NrPrimitives    uint32 LE   number of rectangles
   8   4  Options         uint32 LE
  12   4  PayloadLength   uint32 LE   bytes after this 20-byte header
  16   4  Reserved        uint32 LE   not written; stays whatever the buffer held (0)

then NrPrimitives x:
RectangleHeader (24 B)
   0   2  ImageType                uint16 LE   1 = Gray
   2   2  ScreenID                 uint16 LE
   4   2  X                        uint16 LE
   6   2  Y                        uint16 LE
   8   2  Width                    uint16 LE
  10   2  Height                   uint16 LE
  12   2  RectangleUpdateOptions   uint16 LE
  14   2  Options                  uint16 LE
  16   2  Encoding                 uint16 LE
  18   2  Reserved                 uint16 LE
  20   4  PayloadLength            uint32 LE
  24   N  data
```

- The `ImageHeader` is written **last** by the vendor marshaller, which back-patches
  `NrPrimitives = len(rectangles)` and `PayloadLength = totalBytes - 20`. [D]
- Total size is `20 + sum(24 + len(payload))`. [D]
- `ImageHeader.Reserved` is never written. [D]

### `RectangleUpdateOptions` is auto-defaulted at marshal time

If the field is zero when the rectangle is marshalled, the marshaller substitutes a value
based on the encoding: [D]

| `Encoding` | substituted `RectangleUpdateOptions` |
|---|---|
| 1 (1 bpp) | `0x0101` |
| 4 (4 bpp) | `0x0102` |
| anything else | `0` |

So a wire rectangle for a 4 bpp device normally carries `0x0102`, and the capture confirms
exactly that. [W] **[I]** the low byte is a waveform/LUT selector and the high byte `0x01`
is a "mode present" marker; no decoder for the individual bits exists on the server side —
the field is opaque to the server and simply forwarded from the rendering backend. [D]

### `ImageHeader.Options` and `RectangleHeader.Options`

Both come straight from server configuration knobs and are written verbatim. [D] Both
default to **0**, which is what the capture shows. [W]

**No code in any of the four vendor binaries tests any individual bit of
`ImageHeader.Options`** — the server only ever copies it. [D] **[I]** the bits are
device-firmware flags, and the sole purpose of the config knob is to let an integrator
poke firmware behaviour. The per-bit meanings are a **[GAP]**.

`RectangleHeader.Options` has exactly **one** bit the server manipulates: [D]

```go
if isFullScreen && nextFullScreenIsInverseUpdate {
    rect.Options &= ^uint16(0x0002)
}
```

So **bit `0x0002` is the "normal update" bit, and clearing it requests an inverse /
ghost-clearing refresh.** The firmware reads this bit, and the polarity is **verified on
hardware** — a controlled A/B on the live sign, driving the vendor server and watching
both the wire and the sign's serial console: [W]

| session `Options["RectangleFlags"]` | wire | firmware log |
|---|---|---|
| unset or `0` (the shipped default) | `Rectangle(..., options=0, update_options=258)` | `u: (0   0   0 2880 640), wfn: 2, dum: 1, inv: 1`, then `Force Inverse and full area update`, `border (0 1)`, `UPD_FULL` |
| `"2"` | `options=2` | `u: (0   0   0 2880 640), wfn: 2, dum: 1, inv: 0`, no `Force Inverse` line, and `UPD_FULL_AREA` instead of `UPD_FULL` |

The firmware's own profiling line differs with it too: `ImgOpt=0x00003024` with the bit
clear, against `ImgOpt=0x00000124` with it set.

The vendor's own UI settles the semantics independently. The admin web app's per-session
**"Inverse updates"** dropdown writes exactly this key (`data/admin/all.js`,
`<select name="sessionInverseUpdates">`), with options `1` = "Server default" (which
deletes the key), **`0` = "Enable"** and **`2` = "Disable"**. [D]

**So `RectangleFlags = 0`, the shipped default, means inverse updates are *enabled*.** The
bit is clear on **every** full-screen push, and a default deployment therefore inverts on
every push rather than never signalling one. That is the reverse of the earlier reading
here, which assumed the signal was a rare one-shot. To get a non-inverting push, set bit 1
— `RectangleFlags = 2`.

The only trigger for the server's *one-shot* inverse update is a **timer**, not a command:
a 120 s ticker that fires a full-screen inverse re-render when the session has been idle
longer than a per-session timeout, and it is only started for firmware older than a
compiled-in constant (newer firmware is assumed to manage its own ghosting). [D] **There
is no API or command route to request one** — the sole caller of the inverse-update entry
point is that timer. [D] On a default configuration that one-shot is moot: it clears a bit
that is already clear.

## Encodings

There are exactly **two** real encodings. [D]

| `Encoding` | meaning |
|---:|---|
| 0 | `undefined` — invalid; the server rejects it |
| **1** | **1 bpp, 2 levels** |
| **4** | **4 bpp, 16 grey levels** |
| other | `unknown` — rejected |

The vendor's level count is literally `2 ** (1 if enc == 1 else 4)`, i.e. 2 or 16. [D]
`Encoding: 4` is the shipped default. There is no 2 bpp, no 8 bpp, no colour value in
this enum.

`ImageType` has only one meaningful value: [D]

| `ImageType` | meaning |
|---:|---|
| 0 | `Unknown` |
| **1** | **`Gray`** |

## Bit and nibble packing

The packers are compiled C in the vendor binary. All four operate on a **flat, row-major,
stride == width buffer for the whole rectangle** — the packing runs *across row
boundaries*, and **there is no per-row padding**. [D]

### 8 bpp → 4 bpp

```c
for (c = 0, o = 0; len > c; c += 2, o++)
    dst[o] = (src[c+1] & 0xF0) | (src[c] >> 4);
```

```
 src:  [ P0 ][ P1 ][ P2 ][ P3 ] ...        8-bit grey
 dst:  byte0 = (P1>>4)<<4 | (P0>>4)        byte1 = (P3>>4)<<4 | (P2>>4)

        bit  7 6 5 4   3 2 1 0
              P1          P0         <-- EVEN pixel in the LOW nibble
                                         ODD pixel in the HIGH nibble
```

Each nibble is the **top 4 bits of the 8-bit grey value — truncation, not rounding**. `len`
must be even; an odd length makes the loop read one byte past the end. [D]

> **This is a trap.** Verified at **100.0000% exact pixel match** against the server's own
> reconstruction of a real frame: even pixel in the **low** nibble. [W] Note that a naive
> "which nibble order looks more like a natural image" heuristic gets this **wrong** — the
> content is dithered, so adjacent pixels are deliberately anti-correlated and a
> horizontal-gradient test prefers the wrong answer by about 5%. Only a byte-exact
> comparison is conclusive. If your decoded image looks almost right but subtly wrong,
> this is the first thing to check.

The inverse expands nibble `n` to `n * 17`: [D]

```c
for (si = 0, di = 0; len > di; si++, di += 2) {
    b = src[si];
    dst[di]   = (b << 4) + (b & 0x0F);   // low nibble  n -> n*0x11
    dst[di+1] = (b & 0xF0) + (b >> 4);   // high nibble n -> n*0x11
}
```

### The grey ramp is `n * 17`

Nibble `n` means grey level `n * 17`: 0, 17, 34, …, 255. A **uniform 16-level ramp**,
confirmed independently by the decoder above and by the 16-entry greyscale palette in the
vendor's own preview output. [D][W]

This matters for dithering: the vendor's quantiser is given a *level count*, but the wire
ramp is uniform, so a reimplementation should dither against `{0, 17, 34, …, 255}` rather
than against a histogram-derived palette.

### 8 bpp → 1 bpp

```c
for (j = 0, o = 0; len > j; j += 8, o++) {
    dst[o] =  (src[j+0]      & 0x80)
           |  ((src[j+1]>>1) & 0x40)
           |  ((src[j+2]>>2) & 0x20)
           |  ((src[j+3]>>3) & 0x10)
           |  ((src[j+4]>>4) & 0x08)
           |  ((src[j+5]>>5) & 0x04)
           |  ((src[j+6]>>6) & 0x02)
           |   (src[j+7]>>7);
}
```

```
 dst byte:  bit7 bit6 bit5 bit4 bit3 bit2 bit1 bit0
             P0   P1   P2   P3   P4   P5   P6   P7     <-- MSB-FIRST
```

**Each pixel contributes only its MSB**: `src >= 0x80 → 1`. This is a hard threshold at
128 and is *not* a dither — dithering has to happen earlier. The inverse maps `1 → 0xFF`,
`0 → 0x00`. [D]

> **A bug in the vendor's 1 bpp wrapper, for the record.** Its output allocation is
> `(len + (len & 7)) / 8` rather than `ceil(len / 8)`, which under-allocates for any
> length that is not a multiple of 8, and the C call then writes past the buffer. In
> practice the rectangle sizer guarantees `W*H % 16 == 0` for 1 bpp so it never fires. [D]
> **Use `ceil(len / 8)`.**

### Payload size

```
payload bytes = (Width * Height) / (8 / bpp)
              = W*H / 2   for Encoding 4
              = W*H / 8   for Encoding 1
```

and `RectangleHeader.PayloadLength` is set to exactly that. [D] Confirmed:
`2880 * 640 / 2 = 921600`, exactly the captured value. [W]

## The alignment rule is on `W*H`, not on width

```go
func noSubpixelsIn16BitQuant(enc) uint16 {
    bpp := 1 if enc == 1 else 4 if enc == 4 else error
    return 16 / bpp         // enc 1 -> 16 pixels, enc 4 -> 4 pixels per 16-bit word
}
func isDividableTo16BitQuants(w, h, enc) bool {
    q := noSubpixelsIn16BitQuant(enc)
    total := w * h
    if q > total { return false }
    return total % q == 0
}
```

**The constraint is on `Width * Height` — total sub-pixels — not on width alone**, because
the packer runs linearly over the whole rectangle with no row padding. [D]

| encoding | pixels per 16-bit word | requirement |
|---:|---:|---|
| 1 | 16 | `W*H % 16 == 0` |
| 4 | 4 | `W*H % 4 == 0` |

When a rectangle does not satisfy it, the vendor grows the rectangle **outward**, clamped
to the display and canvas borders, erroring with `"bad display/image borders"` or
`"rectangle is outside boundaries"`. [D] **[GAP]** the growth *order* — right-then-left?
symmetric? height first? — was not recovered, and it is unreachable on the reference
hardware, where every push is full-screen anyway. Settling it needs a capture of one
partial push from a device that supports rectangles.

A 1440×640 rectangle at 4 bpp has `921600 % 4 == 0`, so it is already aligned.

## Dithering

| value | name | implementation |
|---:|---|---|
| **0** | `default` | **invalid at the encoder** — the vendor's own rectangle setup rejects 0 |
| 1 | `none` | no dithering; plain truncation (4 bpp) or MSB threshold (1 bpp) |
| 2 | `bayer` | GraphicsMagick `OrderedDitherImage` |
| 3 | `floyd-steinberg` | GraphicsMagick `QuantizeImage` with `dither = 1` |

The string parser maps `"default"→0`, `"none"→1`, `"bayer"→2`, `"floyd-steinberg"→3`, and
**anything unrecognised silently becomes 0** — which is the invalid value. [D]

> **One of the source reports listed this enum as `0 = none, 1 = default`**, i.e. with 0
> and 1 swapped. The table above is the one supported by the encoder-side evidence (the
> encoder rejecting 0, and a `"none"` configuration producing 1) and by the vendor's
> configuration serialiser. Treat the swapped version as an error.

Back-end details: [D]

- Floyd-Steinberg goes through GraphicsMagick's `QuantizeImage` with
  `number_colors = encodingLevels(encoding)` (**2 for 1 bpp, 16 for 4 bpp**),
  `tree_depth = 0`, `dither = 1`, `measure_error = 1`, and `colorspace` left at the
  library default.
- Bayer goes through `OrderedDitherImage`, which takes **no level parameter** —
  GraphicsMagick's ordered dither is fixed **bi-level**. So `bayer` only really makes
  sense with `Encoding: 1`; with `Encoding: 4` it still produces a 2-level image, which
  then gets nibble-packed as `0x0` / `0xF`. **[I]** based on the documented library
  semantics, not on reading the library code inside the binary.

### Nothing about grey-level reduction happens on the device

The division of labour, each row traced to its implementation: [D]

| step | where |
|---|---|
| render content → RGB(A) | **server** |
| image enhancement (auto-levels, gamma) | **server** |
| change detection → rectangle list | **server** |
| region merge and count limit | **server** |
| rectangle alignment to 16-bit quanta | **server** |
| crop and rotate per display | **server** |
| RGB → 8-bit grey | **server** |
| **dither / quantise to 2 or 16 levels** | **server** |
| **bit packing to 1 bpp / 4 bpp** | **server** |
| interlace the half-panels | **server** |
| LZ4 block compression | **server** |
| waveform / LUT selection, the actual EPD drive | **device** |
| ghost-clearing waveform | **device**, triggered by the server's `Options` bit |
| framebuffer compositing of partial rectangles | **device** (mirrored server-side) |

**The device receives already-quantised 1-bit or 4-bit data.** A reimplementation owns
the entire pixel pipeline.

A reimplementation is **not** obliged to match GraphicsMagick bit-for-bit, and in practice
cannot: a from-scratch implementation with its own Bayer matrix and Floyd-Steinberg
weights, dithering against the uniform `n*17` ramp instead of a histogram-derived palette,
drives the panel correctly. [W] Blue-noise dithering — which the vendor does not offer at
all — also works and looks better on this panel.

## Geometry

### The display model

```go
type Display struct {
    ID         uint16   // off 0
    Width      uint16   // off 2
    Height     uint16   // off 4
    X          uint16   // off 6   position in the session canvas
    Y          uint16   // off 8
    Rotation   uint16   // off 10  0, 1, 2, 3 => 0, 90, 180, 270 degrees
    Driver     string   // off 16
    IsDisabled bool     // off 32
}
```

A rotation of 1 or 3 swaps the reported width and height. The canvas is the bounding box
of all enabled displays after rotation. [D]

> **One sentinel to know about**: the vendor server treats a `Rotation` value of **42** as
> a 42-inch-flip-panel marker rather than an angle, and it fails with
> `couldn't rotate 42" device display - not enough displays` if the display list is too
> short. [D] It is not a rotation.

Translating a canvas rectangle into a display-local, rotation-applied rectangle: [D]

```go
Wd, Hd := display.DimensionsRotated()
rctX    = clamp(in.X,            display.X, display.X + Wd)
rctY    = clamp(in.Y,            display.Y, display.Y + Hd)
rctFarX = clamp(in.X + in.Width,  rctX,     display.X + Wd)
rctFarY = clamp(in.Y + in.Height, rctY,     display.Y + Hd)
if rctFarX <= rctX || rctFarY <= rctY { error "rectangle is not for this display" }

r.X, r.Y          = rctX, rctY
r.Width, r.Height = rctFarX - rctX, rctFarY - rctY
Xd, Yd            = rctX - display.X, rctY - display.Y

switch display.Rotation {
case 0: out.X, out.Y = Xd, Yd
case 1: out.X, out.Y = Hd - Yd - r.Height, Xd;  out.Width, out.Height = r.Height, r.Width
case 2: out.X, out.Y = Wd - Xd - r.Width, Hd - Yd - r.Height
case 3: out.X, out.Y = Yd, Wd - Xd - r.Width;   out.Width, out.Height = r.Height, r.Width
}
out.ScreenID = display.ID
```

Touch events come back the other way through the inverse of the same function, and
swipe-direction ids 0..3 are remapped by rotation: [D]

| rotation | 0→ | 1→ | 2→ | 3→ |
|---|---|---|---|---|
| 0° | 0 | 1 | 2 | 3 |
| 90° | 3 | 2 | 0 | 1 |
| 180° | 1 | 0 | 3 | 2 |
| 270° | 2 | 3 | 1 | 0 |

### The panel ID is an opaque uint32

`DisplayType` (status tag 8, also called `DisplayIds`) is a **`uint32`, and the values are
not a dense enum**. The small values 0..17 coexist with large raw panel-controller
identifiers read off the e-ink module. [D]

| value | firmware-service name | driver |
|---|---|---|
| 0 | `DISPLAY_NONE` | `no display` |
| 1 | `DISPLAY_EINK_6_0` | `eink-generic` |
| 2 | `DISPLAY_EINK_9_7` | `eink-generic` |
| 3 | `DISPLAY_EINK_13_3` | `eink-generic` |
| 4 | `DISPLAY_PLASTIC_LOGIC_10_7` | `pl-generic` |
| 5 | `DISPLAY_EINK_30_0` | `eink-flip` |
| 6 | `DISPLAY_EINK_6_0_NEW` | `eink-generic` |
| 7 | `DISPLAY_EINK_9_7_C228` | `eink-generic` |
| 8 | `DISPLAY_EINK_6_0_C219` | `eink-generic` |
| 9 | `DISPLAY_EINK_9_7_HVOLT` | `eink-generic` |
| 10 | `DISPLAY_EINK_9_7_C242` | `eink-generic` |
| 11 | `DISPLAY_EINK_6_0_C246` | `eink-generic` |
| 12 | `DISPLAY_EINK_13_3_C222` | `eink-generic` |
| 13 | `DISPLAY_EINK_31_2_D039` | *(none)* |
| 14 | `DISPLAY_EINK_31_2_D041` | *(none)* |
| 15 | `DISPLAY_EINK_31_2_R064` | *(none)* |
| 16 | `DISPLAY_EINK_31_2_W001` | *(none)* |
| 17 | `DISPLAY_EINK_31_2_C247` | *(none)* |
| `0x60110154` | `DISPLAY_EINK_42_V2` | `eink-42-v2` * |
| `0x60110191` | `DISPLAY_EINK_42_V2` | `eink-42-v2` * |
| `0x7FFFFFFD` | `DISPLAY_UNKNOWN` | `eink-generic` |
| `0x80000000` | `DISPLAY_EINK_42` | `eink-42-flip` * |
| `0xA30500F3` | `DISPLAY_EINK_9_7_C243` | `eink-generic` |
| `0xAD0500F3` | `DISPLAY_EINK_13_3_C243` | `eink-generic` |
| anything else | `DISPLAY_UNKNOWN` | — |

\* `eink-42-v2` and `eink-42-flip` are names the vendor's own stringer returns but are
**not keys in the driver registry**, which has only five entries (below). Those two panel
ids would fail driver resolution in this build; treat them as unsupported rather than
guessing. [D]

Three observations that settle how to model this: [D][W]

- Values 13–17 — the 31.2" panels — return no legacy driver name at all, so the server has
  no legacy driver for them.
- `0x60110154` and `0x60110191` are **distinct** ids mapping to the **same** logical type,
  which is proof these are hardware identifiers rather than enum ordinals.
- **The reference device reports `0xC2050128`, which is in none of the cases.** Its
  `DisplayType` therefore resolves to `DISPLAY_UNKNOWN` — and it is nevertheless a
  perfectly valid key on the vendor's firmware service, which has over a thousand images
  for that panel. [W]

> **Treat `DisplayType` as an opaque `uint32` end-to-end.** Look it up in a table with a
> default row. Do not validate it against an enum, and do not normalise it to an ordinal —
> it is simultaneously the geometry key, the driver key and the firmware-service lookup
> key, and normalising it loses the ability to ask for firmware. `DISPLAY_UNKNOWN` does
> **not** mean the panel is unsupported.

### Where geometry actually comes from

Geometry comes from a server-side table keyed on `DisplayType`, **not** on
`HardwareNameID`: [D]

| `DisplayType` | panel W × H | display count |
|---|---|---|
| `0x7FFFFFFD` | 80 × 32 | 1 |
| 1 | 600 × 800 | 1 |
| 6 | 1024 × 758 | layout-dependent |
| 2, 7, 8, 9, 10, 11, `0xA30500F3` (the 9.7" family) | 1200 × 825 | 1 |
| 3, 12, `0xAD0500F3` (the 13.3" family) | 1600 × 1200 | 1 |
| 4 | 1280 × 960 | 1 |
| **5, 13, 14, 15, 16, 17 — and the default arm** | **1440 × 640** | **forced to 4** |

The reference panel id lands in the **default arm**, which is exactly what produces its
observed layout. [W] The code is explicit about it: a reported display count other than 4
triggers the log line `"We expect 4 virtual displays for one 32\" eink display!"` and is
forced to 4, and a device-reported 2880×640 surface is rewritten to 4 × 1440×640. [D]

A legacy fallback also exists, mapping raw **geometry** to a driver name: [D]

| width × height | driver |
|---|---|
| 80 × 32, 600 × 800, 1024 × 758, 1200 × 825, 1600 × 1200 | `eink-generic` |
| **1440 × 640** | **`eink-flip`** |
| 1280 × 960 | `pl-generic` |
| anything else | `unknown` |

### The driver abstraction is two booleans and a pixel filter

Five drivers, and that is the entire registry: [D]

| key | `Mirroring` | `Color` |
|---|---|---|
| `eink-generic` | false | false |
| **`eink-flip`** | **true** | false |
| `pl-generic` | false | false |
| `eink-32-inch-color-mask` | false | **true** |
| `eink-32-inch-color-mask-flip` | **true** | **true** |

Lookup falls back to `eink-generic` when a display's driver string is empty or
`"unknown"`. [D]

**`eink-flip` is literally `eink-generic` plus `Mirroring = true`.** It is a struct with a
single embedded pointer to the generic driver, overriding only four operations
(`image-encode`, `image-decode`, `rectangle-encode`, `rectangle-decode`) and delegating
touch and gesture decoding to the generic driver. The mirroring itself is a horizontal
flip. [D]

So **the whole per-device variability in the rendering path reduces to two booleans plus a
pixel filter.** `Mirroring` horizontally flips the raster; `Color` selects an RGBW→RGB
colour model for the colour panels. Bit packing, the alignment rule and the dithering
choice are shared by every driver. [D]

**Drivers carry no geometry of their own.**

## The interlaced fold (the 31.2" four-panel case)

This is specific to `HardwareNameID 8`, but it is the clearest worked example of how a
multi-panel sign is addressed, so it is documented in full.

The device reports **a single 2880 × 640 surface with `NumSupportedDisplays = 2`**. The
server models it as **four logical 1440 × 640 displays stacked vertically** (Y = 0, 640,
1280, 1920), i.e. a 1440 × 2560 canvas — and then folds them back into **two** wire
rectangles of 2880 × 640 before transmission.

### The mode selector

```go
func getInterlacingMode(status) int {
    if status["HardwareNameID"] == "8" {
        if version(Hardware) == version("1.0.0") { return 1 }
        return 2
    }
    return 0   // no interlacing
}
```

The reference device is hardware 1.1.0, so **mode 2**. [D] **Mode 1 (hardware revision
1.0.0) has never been observed**, so its band pairing is a **[GAP]** — guessing it would
put the image on the panel in the wrong order.

### The precondition

Interlacing only fires when there are **exactly 4 rectangles, each exactly 1440 × 640,
all with the same `Encoding`**. [D] Otherwise the image passes through unchanged. A set of
small dirty rectangles fails that precondition, which is why partial updates are
structurally impossible for this hardware.

### The interleavers

```c
// 4 bpp: output is twice as long; two bytes (= four pixels) at a time, alternating
for (i = 0, io = 0; io + 4 <= len(out); io += 4, i += 2) {
    out[io+0] = a[i]; out[io+1] = a[i+1];
    out[io+2] = b[i]; out[io+3] = b[i+1];
}

// 1 bpp: a nibble (= four pixels) at a time
for (i = 0, io = 0; io < len(out); io += 2, i++) {
    out[io+0] = (a[i] & 0xF0) | (b[i] >> 4);
    out[io+1] = (a[i] << 4)   | (b[i] & 0x0F);
}
```

The panel wants the two halves interleaved **at four-pixel granularity** — which is
exactly the 16-bit quantum the rectangle sizer enforces. [D] The post-fold rectangles get
`ScreenID` 0 and 1, `Width` doubled, and `NrPrimitives` set to 2. [D]

### The verified plane → display map

Both 921 600-byte payloads from the real push were decoded and compared against the
server's own 1440 × 2560 reconstruction of the same frame. All four bands match
**exactly, 100.0000% of pixels**: [W]

| wire rectangle | 2-byte group | logical display | canvas band |
|---|---|---|---|
| ScreenID 0 | A = groups 0, 2, 4, … | display 0 | y 0 … 639 |
| ScreenID 0 | B = groups 1, 3, 5, … | display 1 | y 640 … 1279 |
| ScreenID 1 | A = groups 0, 2, 4, … | **display 3** | y 1920 … 2559 |
| ScreenID 1 | B = groups 1, 3, 5, … | **display 2** | y 1280 … 1919 |

**The plane order is swapped on ScreenID 1.** That matches the `displayOrder` argument
being 0 for physical screen 0 and 1 for physical screen 1, and matches the server's own
preview reconstruction. [W][D]

### Exact receive-side decoder

Verified against the real push: [W]

```python
# payload: 921600 bytes, Encoding 4, W=2880, H=640
g = payload.reshape(-1, 4)                     # 4-byte groups
A = interleave(g[:, 0], g[:, 1])               # 460800 B -> one 1440x640 display
B = interleave(g[:, 2], g[:, 3])               # 460800 B -> the other

def unpack4(plane):                            # 460800 B -> 640x1440 8-bit grey
    lo = (plane & 0x0F) * 17                   # EVEN pixel  <- LOW nibble
    hi = (plane >> 4)   * 17                   # ODD  pixel  <- HIGH nibble
    px = interleave(lo, hi)                    # lo first
    return px.reshape(640, 1440)[:, ::-1]      # eink-flip: horizontal mirror

# ScreenID 0: A -> band 0, B -> band 1
# ScreenID 1: A -> band 3, B -> band 2
```

The horizontal mirror is `eink-flip`'s `Mirroring = true`: **the payload is stored mirrored
with respect to the session canvas**, so a decoder must flip it back and an encoder must
flip before packing. [W]

### Consequence

On this hardware the only shape the server ever sends is **two full-width 2880 × 640
rectangles at `ScreenID` 0 and 1, with `NrPrimitives = 2`**, roughly 1.84 MB per frame.
[W][D]

### A real wire rectangle set

Decoded from the captured 1.84 MB push, 385 block records reassembled. [W]

```
DataHeader   Type=5  ID=3544696833  Length=1843268
ImageHeader  Checksum=3741864387 (0xDF0851C3)  NrPrimitives=2  Options=0
             PayloadLength=1843248  Reserved=0
rect[0]      ImageType=1 ScreenID=0 X=0 Y=0 W=2880 H=640
             RectangleUpdateOptions=0x0102 Options=0x0000 Encoding=4 Reserved=0
             PayloadLength=921600
rect[1]      ImageType=1 ScreenID=1 X=0 Y=0 W=2880 H=640
             (identical header)  PayloadLength=921600
```

Every static prediction checks out: `PayloadLength = 1843268 - 20`; `ImageType = 1` =
Gray; `RectangleUpdateOptions = 0x0102` is the auto-default for `Encoding 4`, which proves
the field arrived as 0 and was filled in at marshal time; both `Options` fields are 0,
matching the shipped configuration; `921600 = 2880 * 640 / 2` confirms 4 bpp with no row
padding; and `NrPrimitives = 2` with `W = 2880` is the interlaced output. [W]

> `DataHeader.ID` (3544696833) is a packet sequence id, **not** the image checksum. The
> image state tag is `ImageHeader.Checksum`.

## Region and delta updates: the vendor server refuses them, the device does not

The captured traffic shows **only full-frame pushes**, roughly hourly, and **no deltas at
all**. That is not because delta push is missing — the whole machinery exists and is
reachable on other hardware. Three gates close it for `HardwareNameID 8` in the vendor
stack: [D]

1. The server's rectangle-support check returns **false unconditionally** for
   `HardwareNameID == 8`, *before* consulting the merge configuration, a global force
   flag, or the device's own `RectangleSupport` option. So neither forcing it globally nor
   setting the device option can turn it on for this hardware. The engine therefore takes
   the full-screen response path: every update is a full-screen 4-rectangle set, which
   interlacing then folds into 2.
2. Interlacing only fires on exactly four full-size rectangles (above), so a set of small
   dirty rectangles in **canvas** coordinates could not be interlaced for the panel anyway.
3. A separate cap forces a full update after **10** consecutive partial updates, even
   where partials work.

Region merging, change detection and the 16-bit rectangle sizer all still *run* inside the
server's per-display bookkeeping on this device — they shape the internal rectangle list
and hence which displays are marked dirty — but their output is overridden by the
full-screen requirement before the packet is built. [D]

> **All three are server policy. The device itself accepts partial rectangles.** Verified
> on the 31.2" sign on 2026-10-05 with our own listener, twice: first with hand-built
> packets (15 rectangles), then with the shipped `pyvisionect.imaging.partial` encoder
> (10 pushes, 8 of them partial, on both `ScreenID`s, including a 3-rectangle packet and
> a rectangle straddling a band seam). Every one was acked, drawn **only where
> addressed**, and echoed back as `DisplayStateCRC` equal to the state checksum we
> computed. `DisplayUpdateCount` advances once per partial and the firmware enforces no
> cap of its own. [W] See `OPEN-QUESTIONS.md` A10 for the full evidence — including the
> one thing that *is* refused, below: a partial that also asks for the inverse clearing
> waveform.

### How a partial has to be addressed on this hardware

Gate 2 above is real and is the whole trick: a rectangle in **canvas** coordinates cannot
survive the fold, because `interlacingHack` wants four full-size display rectangles. A
rectangle in **screen** coordinates never enters the fold at all — it is addressed
directly at one of the two physical `2880 x 640` channels, and the interleave is something
the *encoder* performs rather than something the rectangle must pass through:

```
screen x -> lane column  x / 2            (x and w multiples of 8, so each lane row
                                           is a whole number of 2-byte groups)
lane column c -> canvas column 1439 - c   (the eink-flip mirror)
screen y -> band-local y, unchanged
ScreenID 0 lanes = displays 0, 1          ScreenID 1 lanes = displays 3, 2
```

A sub-rectangle cut this way is byte-identical to the same window of the full-screen
`2880 x 640` payload. [W] Note the consequence: **one screen-space rectangle always
touches two canvas bands**, 640 rows apart. To repaint a single band, fill the partner
lane with the unchanged pixels from the state image — twice the bytes you strictly need,
still a rounding error against 1.84 MB, and verified on glass to leave the partner band
untouched. [W]

The firmware echoes the parsed header to the USB console as
`l: (ScreenID X Y W H), enc: 0x4` followed by `u: (...), wfn: 2`, which makes a partial
easy to confirm without a camera. [W]

### The alignment rule is already right in screen space

`W*H` must divide the 16-bit quantum (§"The alignment rule is on `W*H`"). On a screen
rectangle the width is **double** a lane's, and that is exactly what makes the existing
rule correct rather than an approximation: [D]

| encoding | screen rule | implies per lane | which is what `pack` needs |
|---|---|---|---|
| 4 bpp | `w*h % 4 == 0` | `lane_w*h % 2 == 0` | even byte count, no pad |
| 1 bpp | `w*h % 16 == 0` | `lane_w*h % 8 == 0` | whole bytes, no pad |

So `is_aligned` / `align_to_quantum` need no screen-space variant for the quantum itself.
What they *do* need on top is the pairing constraint: `x` and `w` must be multiples of
**8**, one lane group plus its partner's, or the rectangle starts half-way into a group
and the lanes come out swapped. At 1 bpp an odd height additionally forces `w % 16`,
which one more 8-pixel growth always supplies.

### A partial may not also ask for the clearing waveform — this one bites

`RectangleHeader.Options` bit `0x0002` **clear** means "inverse update" (§"`ImageHeader`
and `RectangleHeader`"), and the vendor ships `RectangleFlags = 0`, so the bit is clear by
default. Send a partial that way and the firmware parses it, echoes the header, and then
throws it out: [W]

```
l: (0 1168 160 512 128), enc: 0x4 pde: 0x0, te: 0x0
u: (0 1168 160 512 128), wfn: 2, dum: 1, inv: 1
Image download completed (0 0)
Skip image update: partial image not allowed
                                     -> NACK, ErrorCode 0x06000000, EpdUpd=0
```

The same rectangle with bit `0x0002` **set** logs `inv: 0`, acks, and draws in 1723 ms.
Which is reasonable — an inverse clearing refresh drives the whole panel, so "clear only
this rectangle" is not something the waveform can express.

So: **partial rectangles must carry the normal-update bit; full-screen ones should not.**
That is not a conflict, it is the design. The full-screen push is where the clearing
refresh belongs, and it is what the consecutive-partial budget is spent on.

### What a partial actually saves

Measured from the firmware's own `Profiling:` line, same sign, 2026-10-05: [W]

| push | screen rect | `Pv2Len` (wire) | raw bytes | `EpdUpd` | total |
|---|---|---:|---:|---:|---:|
| full screen (dense test grid) | 2 x `2880x640` | 144 976 | 1 843 200 | 5 298 ms | 6 287 ms |
| full screen (typical content) | 2 x `2880x640` | 83 729 | 1 843 200 | 2 916 ms | 3 618 ms |
| `1232x200` canvas block | `2464x200` | 21 856 | 246 400 | 1 693 ms | 1 860 ms |
| `256x128` canvas block | `512x128` | 1 590 | 32 768 | 1 723 ms | 1 807 ms |
| two bands, 3 rects, one packet | 3 rects | 3 355 | 36 800 | 1 819 ms | 1 911 ms |
| `160x96` canvas block | `320x96` | 402 | 15 360 | 1 719 ms | 1 798 ms |
| `200x100` canvas block | `400x100` | **342** | 20 000 | 1 690 ms | **1 770 ms** |

**The wire cost is the headline: ~245x less for a realistic small change** (342 B against
83 729 B), and the server also skips dithering, packing, interlacing and LZ4-ing 1.84 MB.

The panel time moves too, but read it carefully before designing around it. A partial runs
~1.7 s and a full push 2.9–5.3 s, and most of that gap is **not** area — it is the
clearing waveform. A partial is forbidden from requesting the inverse update (above), so
it never pays for one; a full push with the shipped `RectangleFlags = 0` always asks for
one. Whether it *gets* one is the device's call: in one session, five full pushes with
byte-identical `inv: 1` headers split three to two between `Force Inverse and full area
update` → 8x `UPD_FULL` (5.3 s) and no such line → 4x `UPD_FULL_AREA` (2.9 s), with no
interval or ordering that explains the split. [W] Treat partial updates as a **bandwidth
and CPU** optimisation; the refresh you save is the clearing refresh you still have to ask
for periodically anyway, for ghosting.

### The firmware clears nothing on its own — measured

Across eight accepted partial pushes and twelve rectangles, on both `ScreenID`s, the
console showed `wfn: 2, inv: 0` → exactly one `UPD_FULL_AREA` per rectangle, every time.
**No partial ever produced an `inv: 1`, a `Force Inverse` line or a `UPD_FULL` that the
server did not ask for.** [W] So nothing is clearing the panel between full pushes, and
the consecutive-partial budget is a requirement rather than a precaution. (A ~25-minute
run does not prove the firmware *never* self-manages, but it removes the hope that it
will.)

`wfn: 2` is the only waveform number ever observed on this panel, across every capture
to date. Nothing is known about what selects it, the panel reports a waveform file
`WF=31.2_C296` that has never been examined, and **the library exposes no
waveform-selection API** because there is no evidence one exists. [W]

### What the library does with all this

`pyvisionect.imaging.partial` — opt-in, off by default:

```python
from pyvisionect.imaging import DirtyTracker, PartialPolicy, Dithering

tracker = DirtyTracker(panel=panel, encoding=4, dithering=Dithering.BLUE_NOISE,
                       policy=PartialPolicy(max_consecutive_partials=10))
frame = tracker.update(img)        # first call: full screen
frame = tracker.update(img2)       # then screen-space partials
if frame.rectangles:
    conn.send_image(frame)
```

`encode_frame(..., partial=True)` and `encode_partial_frame(...)` are the stateless forms.
`encode_frame` with no `partial=` behaves exactly as it always has. The tracker falls back
to a full-screen push — and says why in `EncodedFrame.fallback_reason` — when there is no
previous state, when the encoding or dithering changed, when an inverse update is asked
for, when the dirty region would cost more than half a full push, when there are more
dirty regions than merging can reduce, when the panel has no screen-space addressing, and
when the consecutive-partial budget trips.

Rectangles outside `2880 x 640` are **refused, not clamped**: what the firmware does with
an out-of-bounds rectangle was deliberately never probed. [W]

A reimplementation that only ever pushes full frames remains correct, and that is still
what `encode_frame` does by default; the region logic matters for other Visionect
hardware, for matching the vendor's internal checksum bookkeeping exactly, and for the
partial path above.

Related constants, for completeness: [D]

| thing | value |
|---|---|
| max regions per display before merging is forced | **15** |
| region-merge defaults | 5% / 10% **of the display diagonal** |
| max partial updates before a forced full update | **10** (server policy; the firmware has none) |
| change-detection threshold | per-pixel absolute grey difference, default 0 (any difference counts) |

## The state checksum

```go
func calculateStateImageChecksum() uint32 {
    if stateImg == nil { checksum = 0; return 0 }
    pix := stateImg.Pix                        // the raw 8-bit byte slice
    checksum = xxhash.Checksum32S(pix, 0)      // XXHash32, seed 0
    if checksum == 0 { checksum = 1 }          // 0 is reserved for "unknown"
    return checksum
}
```

**XXHash32, seed 0, over the raw pixel bytes of the server's un-encoded 8-bit model of
what the panel shows** — not over the packed wire data. Zero is remapped to 1 so that 0
can mean "unknown". [D] This value is stamped into `ImageHeader.Checksum` and forwarded
unchanged to the device.

**The device treats it as an opaque tag.** It stores the `ImageHeader.Checksum` of the last
successfully applied image and reports it back as status tag 9, `DisplayStateCRC`. **The
device never computes a hash of its own framebuffer** — it just persists and echoes the
32-bit number. [D]

Confirmed end to end by the capture: the device's heartbeat carried tag 9 =
`0xDB963A1A`, identical to what the vendor API reported; the subsequent push carried
`ImageHeader.Checksum = 0xDF0851C3`; and after the push the device reported that new value
back with `DisplayUpdateCount` advancing. [W] Confirmed a second time, independently, by a
from-scratch implementation whose own computed checksum came back verbatim from the
device. [W]

### The sync decision

```go
fullScreen()                                        // mark displays dirty
syncDisplayRects()                                  // re-encode
if lastChecksum != 0 && lastChecksum != checksumLastPacket {
    checksumLastPacket = lastChecksum
    checksumLastPacketBypass = true
    return true                                     // -> send full screen
}
if checksum != checksumLastPacket { return true }
if checksumLastPacketBypass      { return true }
return isDirty()
```

**Reimplementation rule.** Compute a 32-bit tag over your own 8-bit state buffer, send it
in every image packet, and store and compare the value the device echoes back. If your
value and the device's differ, resend the full screen — a reimplementation that gets this
wrong either loops forever on full-screen pushes or never recovers from a device-side
framebuffer loss.

You are **free to use any stable 32-bit function of your own state buffer**, provided it
is deterministic across restarts. But to interoperate with a device that was last written
by the vendor server — or to share a device with it — you must reproduce XXHash32 (seed 0)
over the same 8-bit byte range, or the first status after takeover looks like a mismatch
and forces one extra full-screen redraw. **That single redundant redraw is the only
penalty; the device does not refuse a mismatched tag.** [D]

### Do not confuse the checksums

| value | algorithm | over what |
|---|---|---|
| `ProtocolHeader.Checksum` | **CRC-32/IEEE** | the frame body, or the header — [direction-dependent](framing.md#the-checksum-is-direction-dependent) |
| the encrypted-chunk `Checksum` | **CRC-32/IEEE** | the padded plaintext of that chunk |
| firmware block `Checksum` | **CRC-32/IEEE** | the decompressed plaintext of that block |
| **`ImageHeader.Checksum`** | **XXHash32, seed 0** | the server's 8-bit state image |

**Only the image state tag uses XXHash32.** [D]

### `DisplayStateCRC` versus `DisplayIds`

Two device-reported uint32s that are easy to confuse:

- **`DisplayStateCRC`** (tag 9) is the echo of the last applied `ImageHeader.Checksum`.
- **`DisplayIds`** (tag 8) is the **panel identity**, used by the server to decide when to
  rebuild its display list and sent verbatim to the firmware service as a lookup key.
  **[GAP]** the function behind its value was not recovered — it is produced by the device
  firmware, and the server only reads it and compares it against a stored copy.

`DisplayUpdateCount` (tag 99) is pure device telemetry: a lifetime count of panel
refreshes. Nothing in the server reads it; it is a wear metric. [D]

## Quick-reference constants

| thing | value |
|---|---|
| encodings | `1` = 1 bpp / 2 levels, `4` = 4 bpp / 16 levels. Nothing else. |
| image type | `1` = Gray |
| dithering | 0 default (invalid), 1 none, 2 bayer, 3 floyd-steinberg |
| **4 bpp nibble order** | **even pixel = LOW nibble** |
| 1 bpp bit order | MSB-first, hard threshold at `>= 0x80` |
| 4-bit grey ramp | `n * 17` |
| row padding | **none**; stride == width, packing is linear over `W*H` |
| rectangle size quantum | `W*H % (16/bpp) == 0` ⇒ `%4` at 4 bpp, `%16` at 1 bpp |
| state checksum | **XXHash32, seed 0**, over the 8-bit state image, with 0 → 1 |
| transport checksum | CRC-32/IEEE |
| image packet type | **5** |
| `RectangleUpdateOptions` auto-default | `0x0101` (1 bpp) / `0x0102` (4 bpp) |
| inverse-update signal | clear `RectangleHeader.Options & 0x0002` — clear = inverse, verified on hardware |
| anti-ghost timer | 120 s tick, fires when idle past a per-session timeout, old firmware only |
| max regions per display | 15 |
| max partial updates before a forced full | 10 |

## Performance, measured

From a real push to a live sign: [W]

| stage | time |
|---|---|
| encode a 1440 × 2560 canvas at 4 bpp with dithering, into 2 rectangles of 921 600 bytes | **0.05 s** |
| transfer and device ack of the 1.84 MB frame | **~7 s** (a TI CC3100 at 1–3 Mbps; LZ4 buys almost nothing on dithered data) |
| `DisplayStateCRC` confirmation | only on the **next heartbeat**, ~53 s later |

So the perceived "about a minute" is mostly *confirmation* latency, not draw time.

The panel draw is no longer a gap: the firmware's `Profiling:` line reports it as
`EpdUpd`, and watching the serial console across a push gives **~1.7 s for a partial, 2.9 s
for a full push that does not run the clearing waveform, and 5.3 s for one that does.** [W]
See "What a partial actually saves" above.

### Encode cost, full versus partial

Same machine, the real captured dashboard canvas (1440 × 2560), one `300 × 120` region
changed, best of 3: [W]

| dithering | full screen | partial, change-detected | partial, `rects=` given |
|---|---:|---:|---:|
| none | 13.9 ms | 9.4 ms | **1.4 ms** |
| blue-noise | 34.3 ms | 9.3 ms | **1.6 ms** |
| Floyd-Steinberg | 49.7 ms | 10.4 ms | **2.0 ms** |

Two things make the partial cheap. Ordered dithers are phased on the **canvas**
coordinate, so quantising the dirty slice with its canvas origin is byte-identical to
quantising the whole canvas and cutting the slice out — a partial therefore dithers
`300 × 120` pixels instead of 1440 × 2560. And packing and interlacing scale with the
rectangle, not the screen.

What is left is change detection, ~8 ms of the ~9 ms, because it runs over the whole
canvas however small the change. A caller that already knows what it redrew should pass
`rects=`; that is the 1.4 ms column.
