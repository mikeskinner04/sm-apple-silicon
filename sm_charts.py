"""Charts for the sm_diag report, rendered to self-contained SVG and PNG.

Each function returns a string the HTML report can drop straight in: SVG markup,
or a data: URI for an <img>. Nothing here touches the device.

    zero_map_svg   where in the whole capture the samples were exact zeros, with
                   the library's sample-loss and sync flags marked
    iq_trace_svg   I and Q against time for the first few hundred samples
    spectrogram    a waterfall of the kept samples, as a PNG data URI
"""

import base64
import struct
import zlib

import numpy as np

WIDTH = 960


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_complex(raw, short):
    """Kept sample bytes as complex64: 16-bit pairs or 32-bit float pairs."""
    if short:
        v = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    else:
        v = np.frombuffer(raw, dtype="<f4")
    v = v[: len(v) // 2 * 2]
    return (v[0::2] + 1j * v[1::2]).astype(np.complex64)


# ---- zero map ---------------------------------------------------------------

def zero_map_svg(zmap, total_samples):
    """zmap: {"segment": samples per segment, "segments": count,
    "zero_runs": [[first segment, length], ...], "loss_at": [sample, ...],
    "sync_at": [sample, ...]}.
    """
    if not zmap or not zmap.get("segments"):
        return ""
    nseg, seg = zmap["segments"], zmap["segment"]
    height, bar = 34, 18
    parts = [f'<svg viewBox="0 0 {WIDTH} {height}" width="100%" role="img" '
             f'aria-label="zero map" xmlns="http://www.w3.org/2000/svg">',
             f'<rect x="0" y="0" width="{WIDTH}" height="{bar}" fill="var(--ok)" '
             f'opacity="0.35"/>']
    for start, length in zmap["zero_runs"]:
        x = start / nseg * WIDTH
        w = max(1.0, length / nseg * WIDTH)
        parts.append(f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="{bar}" '
                     f'fill="var(--bad)"><title>segments {start}-{start + length - 1} '
                     f'all zero ({length * seg} samples)</title></rect>')
    span = max(1, total_samples)
    for key, colour, label in (("loss_at", "var(--warn)", "sample-loss flag"),
                               ("sync_at", "#7b4bd6", "sync error")):
        for s in zmap.get(key, [])[:500]:
            x = s / span * WIDTH
            parts.append(f'<rect x="{x:.1f}" y="{bar}" width="2" height="8" '
                         f'fill="{colour}"><title>{label} at sample {s}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


# ---- I/Q trace --------------------------------------------------------------

def iq_trace_svg(iq, sample_rate, count=512):
    if iq is None or not len(iq):
        return ""
    iq = iq[:count]
    n = len(iq)
    peak = float(np.max(np.abs(np.concatenate([iq.real, iq.imag])))) or 1.0
    height, pad = 180, 4
    mid = height / 2

    def line(values, colour):
        xs = np.linspace(0, WIDTH, n)
        ys = mid - values / peak * (mid - pad)
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        return (f'<polyline points="{pts}" fill="none" stroke="{colour}" '
                f'stroke-width="1"/>')

    dur_us = n / sample_rate * 1e6 if sample_rate else 0
    return (f'<svg viewBox="0 0 {WIDTH} {height + 16}" width="100%" role="img" '
            f'aria-label="I/Q trace" xmlns="http://www.w3.org/2000/svg">'
            f'<line x1="0" x2="{WIDTH}" y1="{mid}" y2="{mid}" stroke="var(--line)"/>'
            f'{line(iq.real, "#2a6fdb")}{line(iq.imag, "#d9822b")}'
            f'<text x="0" y="{height + 13}" font-size="11" fill="var(--dim)">'
            f'I (blue), Q (orange): first {n} samples, {dur_us:.2f} µs; '
            f'full scale ±{_esc(f"{peak:.4g}")}</text></svg>')


# ---- spectrogram ------------------------------------------------------------

# A perceptually ordered ramp, dark to light.
_STOPS = np.array([[0.0, 13, 8, 135], [0.25, 126, 3, 168], [0.5, 204, 71, 120],
                   [0.75, 248, 149, 64], [1.0, 240, 249, 33]])


def _colour(x):
    x = np.clip(x, 0, 1)
    return np.stack([np.interp(x, _STOPS[:, 0], _STOPS[:, c]) for c in (1, 2, 3)],
                    axis=-1).astype(np.uint8)


def _png(rgb):
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(kind, data):
        body = kind + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def spectrogram(iq, sample_rate, center, nfft=256, max_rows=256, dynamic_db=80.0):
    """Waterfall of the kept samples. Returns {"png": data URI, "caption": text}.

    Rows run top to bottom in time; columns run from centre minus half the
    sample rate on the left to centre plus half on the right. Exact zeros come
    out as the floor colour, so a zero stretch shows as a dark band.
    """
    if iq is None or len(iq) < nfft:
        return {}
    rows = min(max_rows, len(iq) // nfft)
    frames = iq[: rows * nfft].reshape(rows, nfft) * np.hanning(nfft).astype(np.float32)
    power = np.abs(np.fft.fftshift(np.fft.fft(frames, axis=1), axes=1)) ** 2
    db = 10 * np.log10(power + 1e-30)
    top = float(db.max())
    img = _colour((db - (top - dynamic_db)) / dynamic_db)
    uri = "data:image/png;base64," + base64.b64encode(_png(img)).decode()
    span_ms = rows * nfft / sample_rate * 1e3 if sample_rate else 0
    lo = (center - sample_rate / 2) / 1e6
    hi = (center + sample_rate / 2) / 1e6
    return {"png": uri,
            "caption": f"{lo:.3f} to {hi:.3f} MHz across, {span_ms:.3f} ms down "
                       f"({rows} rows of {nfft}), {dynamic_db:.0f} dB range"}


# ---- sweep trace ------------------------------------------------------------

def sweep_svg(start_hz, bin_hz, values):
    """The max-hold trace of one sweep, in dBm."""
    v = np.asarray(values, dtype=np.float64)
    good = np.isfinite(v) & (np.abs(v) < 1e30)
    if not good.any():
        return ""
    v = np.where(good, v, np.nan)
    if len(v) > WIDTH:                     # max per pixel, so peaks survive
        edges = np.linspace(0, len(v), WIDTH + 1).astype(int)
        v = np.array([np.nanmax(v[a:b]) if b > a else np.nan
                      for a, b in zip(edges[:-1], edges[1:])])
    lo, hi = float(np.nanmin(v)), float(np.nanmax(v))
    hi = hi if hi > lo else lo + 1
    height, pad = 180, 4
    xs = np.linspace(0, WIDTH, len(v))
    ys = pad + (hi - v) / (hi - lo) * (height - 2 * pad)
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys) if np.isfinite(y))
    f0 = start_hz / 1e6
    f1 = (start_hz + bin_hz * (len(values) - 1)) / 1e6
    return (f'<svg viewBox="0 0 {WIDTH} {height + 16}" width="100%" role="img" '
            f'aria-label="sweep trace" xmlns="http://www.w3.org/2000/svg">'
            f'<polyline points="{pts}" fill="none" stroke="#2a6fdb" stroke-width="1"/>'
            f'<text x="0" y="{height + 13}" font-size="11" fill="var(--dim)">'
            f'{f0:.3f} to {f1:.3f} MHz, {lo:.1f} to {hi:.1f} dBm</text></svg>')
