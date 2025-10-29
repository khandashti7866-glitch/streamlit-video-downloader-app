# app.py
import streamlit as st
import requests
import os
import tempfile
import shutil
import math
from pathlib import Path
from urllib.parse import urlparse
import threading

# Optional dependencies; we import lazily and show helpful errors if missing
def import_yt_dlp():
    try:
        import yt_dlp as ytdlp
        return ytdlp
    except Exception as e:
        raise ImportError(
            "yt-dlp is required for YouTube downloads. Install it with `pip install yt-dlp`."
        )

def import_m3u8():
    try:
        import m3u8
        return m3u8
    except Exception:
        raise ImportError(
            "m3u8 is required for HLS (.m3u8) downloads. Install it with `pip install m3u8`."
        )

st.set_page_config(page_title="Android Video Downloader", layout="centered", initial_sidebar_state="auto")

st.title("📥 Video Downloader — Android-friendly")
st.markdown(
    """
This Streamlit app downloads videos from **direct URLs**, **HLS (.m3u8) streams**, or **YouTube (via yt-dlp)**.
**Only use for content you own or have permission to download.**
"""
)

st.sidebar.header("Settings")
save_folder = st.sidebar.text_input("Save folder (server-side)", value="downloads")
os.makedirs(save_folder, exist_ok=True)
st.sidebar.markdown("Files will be saved server-side and offered for download to your device.")

mode = st.radio("Choose download mode", ("Direct video URL", "HLS (.m3u8)", "YouTube (yt-dlp)"))

url = st.text_input("Enter video URL (http/https)", "")

filename_input = st.text_input("Suggested filename (optional, include extension like .mp4)", "")
max_retries = st.number_input("Retry attempts for segment download (HLS)", min_value=0, max_value=10, value=3)

download_button = st.button("Start download")

progress_bar = None
status_text = st.empty()
log_area = st.empty()


def safe_filename_from_url(u):
    parsed = urlparse(u)
    name = Path(parsed.path).name
    if not name:
        name = "downloaded_video"
    return name

def stream_download_direct(url, out_path, chunk_size=1024 * 256):
    # Stream download with progress
    with requests.get(url, stream=True, timeout=15) as r:
        r.raise_for_status()
        total = r.headers.get("content-length")
        if total is None:
            total = 0
        else:
            total = int(total)
        downloaded = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        progress_bar.progress(min(1.0, downloaded / total))
                        status_text.text(f"Downloading: {downloaded:,} / {total:,} bytes")
                    else:
                        # estimate progress
                        progress_bar.progress(min(0.99, downloaded / (1024 * 1024 * 50)))  # arbitrary
                        status_text.text(f"Downloading: {downloaded:,} bytes")
    progress_bar.progress(1.0)
    status_text.text("Download complete.")


def download_hls_playlist(m3u8_url, output_file, retries=3):
    m3u8 = import_m3u8()
    playlist = m3u8.load(m3u8_url)
    if playlist.is_variant:
        # pick the highest bandwidth variant
        streams = sorted(playlist.playlists, key=lambda p: p.stream_info.bandwidth or 0, reverse=True)
        if not streams:
            raise RuntimeError("No playable streams found in variant playlist.")
        chosen = streams[0].absolute_uri
        playlist = m3u8.load(chosen)

    segments = playlist.segments
    if not segments:
        raise RuntimeError("No segments found in playlist.")
    tmpdir = tempfile.mkdtemp()
    try:
        total_segments = len(segments)
        progress_bar.progress(0.0)
        status_text.text(f"Found {total_segments} segments. Downloading...")
        segment_files = []
        session = requests.Session()
        for idx, seg in enumerate(segments, start=1):
            seg_url = seg.absolute_uri
            seg_name = os.path.join(tmpdir, f"seg_{idx:05d}.ts")
            attempt = 0
            while attempt <= retries:
                attempt += 1
                try:
                    with session.get(seg_url, stream=True, timeout=15) as r:
                        r.raise_for_status()
                        with open(seg_name, "wb") as fh:
                            for chunk in r.iter_content(chunk_size=1024 * 256):
                                if chunk:
                                    fh.write(chunk)
                    break
                except Exception as e:
                    if attempt > retries:
                        raise RuntimeError(f"Failed to download segment {idx}: {e}")
            segment_files.append(seg_name)
            progress_bar.progress(idx / total_segments)
            status_text.text(f"Downloaded segment {idx}/{total_segments}")
        # concatenate .ts files
        status_text.text("Merging segments...")
        with open(output_file, "wb") as outfh:
            for segf in segment_files:
                with open(segf, "rb") as s:
                    shutil.copyfileobj(s, outfh)
        progress_bar.progress(1.0)
        status_text.text("HLS download and merge complete.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def download_youtube(url, out_path):
    ytdlp = import_yt_dlp()
    ydl_opts = {
        "outtmpl": out_path,
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "no_warnings": True,
        # Do not attempt postprocessing that requires ffmpeg if not available.
        # If ffmpeg is available, yt-dlp will use it automatically for merging.
        "quiet": True,
        "progress_hooks": [],
    }
    # Use a wrapper to capture progress if needed
    class MyLogger(object):
        def debug(self, msg):
            pass
        def warning(self, msg):
            log_area.text(f"yt-dlp warning: {msg}")
        def error(self, msg):
            log_area.text(f"yt-dlp error: {msg}")

    def ydl_hook(d):
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                progress_bar.progress(min(1.0, downloaded / total))
                status_text.text(f"Downloading: {int(downloaded):,} / {int(total):,} bytes")
            else:
                status_text.text(f"Downloading: {int(downloaded):,} bytes")
        elif d.get("status") == "finished":
            progress_bar.progress(1.0)
            status_text.text("Download finished, finalizing...")
    ydl_opts["logger"] = MyLogger()
    ydl_opts["progress_hooks"] = [ydl_hook]

    # yt-dlp expects a template filename; ensure extension present
    # If out_path ends with a directory, append template
    if os.path.isdir(out_path):
        out_path = os.path.join(out_path, "%(title)s.%(ext)s")
    with ytdlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    status_text.text("YouTube download finished.")


def run_download():
    # Determine output path
    if filename_input.strip():
        out_name = filename_input.strip()
    else:
        out_name = safe_filename_from_url(url)
    out_path = os.path.join(save_folder, out_name)
    # make sure extension exists for direct and hls
    if mode in ("Direct video URL", "HLS (.m3u8)") and "." not in Path(out_path).name:
        out_path = out_path + ".mp4"

    # Ensure progress bar exists
    global progress_bar
    progress_bar = st.progress(0.0)

    try:
        if mode == "Direct video URL":
            status_text.text("Starting direct download...")
            stream_download_direct(url, out_path)
        elif mode == "HLS (.m3u8)":
            status_text.text("Starting HLS download...")
            download_hls_playlist(url, out_path, retries=max_retries)
        elif mode == "YouTube (yt-dlp)":
            status_text.text("Starting YouTube download...")
            # yt-dlp may write to template; pass a tmp filename pattern in save_folder
            tmp_template = os.path.join(save_folder, "%(title)s.%(ext)s")
            download_youtube(url, tmp_template)
            # find the created file (yt-dlp created it inside save_folder)
            # pick most recent file in save_folder
            files = sorted(
                (Path(save_folder).glob("*")),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
            if not files:
                raise RuntimeError("yt-dlp reported success but no file was found.")
            out_path = str(files[0])
        else:
            raise RuntimeError("Unknown mode.")

        # Show download link
        status_text.text("Preparing download link...")
        with open(out_path, "rb") as f:
            data = f.read()
        download_name = Path(out_path).name
        st.success("Ready — tap the button below to download to your device.")
        st.download_button("Save to device 📲", data, file_name=download_name, mime="application/octet-stream")
        status_text.text(f"Saved on server as: {out_path}")
        log_area.text("Operation completed successfully.")
    except Exception as e:
        st.error(f"Download failed: {e}")
        log_area.text(f"Error details: {e}")
        progress_bar.progress(0.0)


if download_button:
    if not url.strip():
        st.warning("Enter a valid URL before starting.")
    else:
        # Start download in the app (synchronous; Streamlit will show progress)
        run_download()

st.markdown("---")
st.markdown(
    """
**Notes & troubleshooting**
- For YouTube downloads install `yt-dlp` in the environment (`pip install yt-dlp`).  
- HLS (.m3u8) is supported by fetching and concatenating TS segments — merging works for most streams but very complex streams may need ffmpeg.  
- This app runs server-side; to use on Android open the app URL in your Android browser or use a WebView wrapper.  
- If you plan to run this on an Android device directly (Termux / Pydroid), ensure the Python environment has the required packages.
"""
)
