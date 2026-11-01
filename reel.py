"""Turn a framed chart into a short Reel (1080x1920 MP4).

The frame (logo, date, title) stays still while the chart slowly zooms in
towards its right-hand side, where the latest price action is. The video has
a silent audio track because some players reject videos without one.
"""

import os
import shutil
import subprocess
import tempfile

FPS = 30


def _ffmpeg():
    try:
        import imageio_ffmpeg  # bundles a static ffmpeg, so no install step
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        if shutil.which("ffmpeg"):
            return "ffmpeg"
        raise RuntimeError("ffmpeg not found; pip install imageio-ffmpeg")


def render_reel(frame_path, chart_path, rect, out_path, seconds=10, zoom=0.2,
                anchor="right"):
    """Write the Reel to out_path.

    frame_path: the 1080x1920 framed chart (render_chart_frame, layout "reel").
    rect: where the chart sits in the frame, as returned by render_chart_frame.
    anchor: "right" keeps a chart's right edge (price scale, latest candles)
    in view; "center" zooms into the middle (text cards).
    """
    x, y, w, h = rect
    frames = int(seconds * FPS)
    zx = "iw-iw/zoom" if anchor == "right" else "(iw-iw/zoom)/2"
    # Scale up first so the slow zoom moves in sub-pixel steps (no jitter).
    chart = ("[1:v]scale={sw}:{sh},"
             "zoompan=z='1+{zoom}*on/{n}':d={n}:s={w}x{h}:fps={fps}"
             ":x='{zx}':y='(ih-ih/zoom)/2',setsar=1[z]").format(
                 sw=w * 4, sh=h * 4, zoom=zoom, n=frames, w=w, h=h, fps=FPS, zx=zx)
    # out_range=tv: the JPEG frame is full-range; video players expect TV range.
    video = ("[0:v][z]overlay={}:{}:shortest=1,scale=out_range=tv,"
             "format=yuv420p[v]").format(x, y)
    subprocess.run(
        [_ffmpeg(), "-y", "-loglevel", "error",
         "-loop", "1", "-framerate", str(FPS), "-t", str(seconds), "-i", frame_path,
         "-i", chart_path,
         "-f", "lavfi", "-t", str(seconds),
         "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
         "-filter_complex", chart + ";" + video,
         "-map", "[v]", "-map", "2:a",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", str(FPS),
         "-g", str(FPS * 2), "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
         "-movflags", "+faststart", "-t", str(seconds), out_path],
        check=True, capture_output=True, timeout=300)
    return out_path


def join_reels(parts, out_path):
    """Play several Reels made by render_reel one after another. They share
    size, frame rate and codecs, so the streams are copied, not re-encoded."""
    if len(parts) == 1:
        shutil.copyfile(parts[0], out_path)
        return out_path
    fd, listing = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.writelines("file '{}'\n".format(os.path.abspath(p)) for p in parts)
    try:
        subprocess.run(
            [_ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", listing, "-c", "copy", "-movflags", "+faststart", out_path],
            check=True, capture_output=True, timeout=300)
    finally:
        os.remove(listing)
    return out_path
