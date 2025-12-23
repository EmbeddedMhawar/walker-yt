#!/usr/bin/env python3
import sys
import subprocess
import json
import os
import shutil
import tempfile
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor

# Configuration
VENV_PATH = os.path.expanduser("~/.local/share/walker-yt/venv")
DEMUCS_BIN = os.path.join(VENV_PATH, "bin", "demucs")
YT_DLP_BIN = os.path.join(os.path.expanduser("~/.local/bin"), "yt-dlp")
if not os.path.exists(YT_DLP_BIN):
    YT_DLP_BIN = "yt-dlp" # Fallback to path

CACHE_DIR = os.path.expanduser("~/.cache/walker-yt")
os.makedirs(CACHE_DIR, exist_ok=True)


def log(message):
    with open("/tmp/walker-yt.log", "a") as f:
        f.write(message + "\n")

def notify(title, body, icon=None, urgency="normal", progress=None, replace_id=None):
    log(f"NOTIFY: {title} - {body} (ID: {replace_id})")
    cmd = ["notify-send", "-u", urgency, title, body]
    if icon:
        cmd.extend(["-i", icon])
    if replace_id:
        cmd.extend(["-r", str(replace_id)])
    if progress is not None:
        cmd.extend(["-h", f"int:value:{progress}"])
        cmd.extend(["-h", f"string:x-canonical-private-synchronous:walker-yt"])
    
    if replace_id or progress is not None:
        cmd.append("-p")
        result = subprocess.run(cmd, capture_output=True, text=True)
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None
    else:
        subprocess.run(cmd)
        return None

def walker_dmenu(prompt, lines):
    """Run walker in dmenu mode."""
    log(f"WALKER: {prompt} ({len(lines)} lines)")
    cmd = ["walker", "-d", "-p", prompt]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out, _ = process.communicate(input="\n".join(lines))
    return out.strip()

def search_youtube(query):
    """Search YouTube and return list of dicts."""
    cmd = [
        YT_DLP_BIN,
        "ytsearch10:" + query,
        "--print", "%(title)s\t%(channel)s\t%(id)s\t%(thumbnail)s",
        "--no-playlist",
        "--no-warnings",
        "--ignore-config"
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        videos = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                videos.append({
                    "title": parts[0],
                    "channel": parts[1],
                    "id": parts[2],
                    "thumbnail": parts[3]
                })
        return videos
    except Exception as e:
        notify("Error", f"Search failed: {e}")
        return []

def download_thumbnail(url, video_id):
    path = os.path.join(CACHE_DIR, f"{video_id}.jpg")
    if not os.path.exists(path):
        subprocess.run(["curl", "-s", "-L", url, "-o", path], stdout=subprocess.DEVNULL)
    return path

def process_audio(video_id, mode):
    """Download and separate audio using high-quality htdemucs with optimized streaming."""
    
    # Cleanup existing processes
    try: subprocess.run(["pkill", "-f", "demucs"], stderr=subprocess.DEVNULL)
    except: pass

    work_dir = os.path.join(CACHE_DIR, "proc_" + video_id)
    os.makedirs(work_dir, exist_ok=True)
    
    # Paths
    audio_path = os.path.join(work_dir, "input.m4a")
    chunks_dir = os.path.join(work_dir, "chunks")
    out_chunks_dir = os.path.join(work_dir, "out_chunks")
    # Using a regular file instead of FIFO for stability
    playback_file = os.path.join(work_dir, "live_audio.pcm")
    
    if os.path.exists(playback_file): os.remove(playback_file)
    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(out_chunks_dir, exist_ok=True)

    nid = notify("Live Stream", "Step 1/3: Downloading & Splitting...", urgency="critical")

    # 1. Download Audio (Fast)
    if not os.path.exists(audio_path):
        subprocess.run([
            YT_DLP_BIN, "-f", "bestaudio[ext=m4a]/bestaudio",
            "-o", audio_path, "--no-playlist", video_id
        ], check=True)

    # 2. Split into 30s chunks
    for f in os.listdir(chunks_dir): os.remove(os.path.join(chunks_dir, f))
    subprocess.run([
        "ffmpeg", "-y", "-i", audio_path, 
        "-f", "segment", "-segment_time", "30", 
        "-c", "copy", os.path.join(chunks_dir, "chunk_%03d.m4a")
    ], check=True)

    sorted_chunks = sorted([f for f in os.listdir(chunks_dir) if f.endswith(".m4a")])
    nid = notify("Live Stream", f"Step 2/3: Separating (0/{len(sorted_chunks)})", urgency="critical", progress=0, replace_id=nid)

    def processing_worker():
        """Background thread to process chunks and append to the PCM file."""
        try:
            total = len(sorted_chunks)
            for i, chunk_file in enumerate(sorted_chunks):
                chunk_path = os.path.join(chunks_dir, chunk_file)
                
                # Optimized Demucs settings for Intel CPU
                cmd = [
                    "systemd-run", "--user", "--scope",
                    "-p", "MemoryMax=10G",
                    "-p", "CPUWeight=100",
                    DEMUCS_BIN, "-n", "htdemucs", "--two-stems=vocals",
                    "--segment", "7", "--shifts", "0", "--overlap", "0.1",
                    "-d", "cpu", "-j", "2",
                    "-o", out_chunks_dir, chunk_path
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                chunk_base = os.path.splitext(chunk_file)[0]
                stem_name = "vocals.wav" if mode == "vocals" else "no_vocals.wav"
                separated_wav = os.path.join(out_chunks_dir, "htdemucs", chunk_base, stem_name)
                
                if os.path.exists(separated_wav):
                    # Append to growing PCM file
                    with open(playback_file, "ab") as f_out:
                        subprocess.run([
                            "ffmpeg", "-y", "-i", separated_wav, 
                            "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", "-"
                        ], stdout=f_out, stderr=subprocess.DEVNULL)
                        f_out.flush()
                        os.fsync(f_out.fileno())
                
                percent = int(((i + 1) / total) * 100)
                notify("Live Stream", f"Separating: Chunk {i+1}/{total}", urgency="low", progress=percent, replace_id=nid)
        except Exception as e:
            log(f"Worker Error: {e}")

    # Start the worker
    worker_thread = threading.Thread(target=processing_worker, daemon=True)
    worker_thread.start()

    # 3. Wait for the FIRST chunk to have data
    start_time = time.time()
    while not os.path.exists(playback_file) or os.path.getsize(playback_file) < 500000: # Wait for ~3s of audio
        if not worker_thread.is_alive() and (not os.path.exists(playback_file) or os.path.getsize(playback_file) < 100000):
            raise Exception("Processing worker died.")
        if time.time() - start_time > 120:
            raise Exception("Timeout waiting for audio buffer.")
        time.sleep(1)

    notify("Live Stream", "Ready! Opening Player...", urgency="normal", progress=10, replace_id=nid)
    return playback_file, worker_thread


def get_video_qualities(video_id):
    """Fetch available resolutions and FPS for the video."""
    cmd = [
        YT_DLP_BIN,
        "--dump-json",
        "--no-playlist",
        "https://www.youtube.com/watch?v=" + video_id
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)
        formats = data.get('formats', [])
        quality_map = {}
        for f in formats:
            h = f.get('height')
            fps = f.get('fps')
            if h and f.get('vcodec') != 'none':
                if h not in quality_map:
                    quality_map[h] = set()
                if fps:
                    quality_map[h].add(int(fps))
        
        options = []
        for h in sorted(quality_map.keys(), reverse=True):
            f_list = sorted(list(quality_map[h]), reverse=True)
            if f_list:
                for fps in f_list:
                    if fps >= 50 or len(f_list) == 1:
                        options.append(f"{h}p{fps}")
                    elif fps == 30 and 60 not in quality_map[h]:
                         options.append(f"{h}p{fps}")
            else:
                options.append(f"{h}p")
        return options
    except Exception:
        return []

def select_quality(video_id):
    """Prompt user for video quality."""
    notify("Quality", "Fetching available resolutions & FPS...", urgency="low")
    options = get_video_qualities(video_id)
    if not options:
        options = ["1080p60", "1080p30", "720p60", "720p30", "480p", "360p"]
    
    selection = walker_dmenu("Select Video Quality", options)
    if not selection:
        return None
        
    match = re.match(r'(\d+)p(\d+)?', selection)
    if match:
        height = match.group(1)
        fps = match.group(2)
        if fps:
            return f"bestvideo[height<={height}][fps<={fps}]"
        return f"bestvideo[height<={height}]"
    return "bestvideo"

def get_subtitles(video_id):
    """Fetch available subtitles."""
    cmd = [
        YT_DLP_BIN,
        "--list-subs",
        "--quiet",
        "https://www.youtube.com/watch?v=" + video_id
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        lines = result.stdout.splitlines()
        subs = []
        start_parsing = False
        for line in lines:
            if "Language" in line and "Name" in line and "Formats" in line:
                start_parsing = True
                continue
            if start_parsing and line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    code = parts[0]
                    name = " ".join(parts[1:-1])
                    if not name: name = code
                    subs.append(f"{name} ({code})")
        return sorted(list(set(subs)))
    except Exception:
        return []

def select_subtitles(video_id):
    """Prompt user for subtitles."""
    notify("Subtitles", "Fetching subtitle list...", urgency="low")
    subs = get_subtitles(video_id)
    if not subs:
        notify("Subtitles", "No subtitles found.", urgency="normal")
        return None
    subs.insert(0, "🚫 None")
    selection = walker_dmenu("Select Subtitles", subs)
    if not selection or "None" in selection:
        return None
    match = re.search(r'\((.*?)\)$', selection)
    if match:
        return match.group(1)
    return None

def main():
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        proc = subprocess.Popen(["walker", "--dmenu", "--inputonly", "-p", "Search YouTube"], 
                                stdout=subprocess.PIPE, text=True)
        out, _ = proc.communicate()
        query = out.strip()
    
    if not query:
        return

    notify("Searching", f"Searching for: {query}...")
    videos = search_youtube(query)
    
    if not videos:
        notify("Walker YT", "No results found.")
        return

    display_lines = [f"{v['title']} ({v['channel']})" for v in videos]
    selected_str = walker_dmenu("Select Video", display_lines)
    if not selected_str:
        return
        
    try:
        index = display_lines.index(selected_str)
        selected_video = videos[index]
    except ValueError:
        return

    thumb_path = download_thumbnail(selected_video['thumbnail'], selected_video['id'])
    
    actions = [
        "🎬 Watch Video (Auto)",
        "⚙️ Watch Video (Select Quality & Subs)",
        "🎧 Listen Audio (MPV --no-video)",
        "🎤 Keep Vocals (Select Quality & Subs)",
        "🎵 Keep Music (Select Quality & Subs)"
    ]
    
    action_str = walker_dmenu(f"Action: {selected_video['title']}", actions)
    if not action_str:
        return

    url = f"https://www.youtube.com/watch?v={selected_video['id']}"
    
    mpv_cmd = [
        "mpv", 
        "--script-opts=ytdl_hook-ytdl_path=" + YT_DLP_BIN, 
        "--force-window",
        "--cache=yes",
        "--cache-pause-wait=5",
        "--demuxer-readahead-secs=20"
    ]
    
    video_format = "bestvideo+bestaudio/best"
    sub_args = []
    
    if "Select Quality" in action_str:
        quality_setting = select_quality(selected_video['id'])
        if not quality_setting:
            return
        
        if "Watch Video" in action_str:
            video_format = f"{quality_setting}+bestaudio/best"
        else:
            video_format = quality_setting
            
        sub_code = select_subtitles(selected_video['id'])
        if sub_code:
            sub_args = [f"--slang={sub_code}"]

    if "Watch Video" in action_str:
        notify("Playing", selected_video['title'], thumb_path)
        subprocess.Popen(mpv_cmd + [url, f"--ytdl-format={video_format}"] + sub_args)
        
    elif "Listen Audio" in action_str:
        notify("Playing Audio", selected_video['title'], thumb_path)
        subprocess.Popen(mpv_cmd + ["--no-video", url])
        
    elif "Keep Vocals" in action_str or "Keep Music" in action_str:
        mode = "vocals" if "Keep Vocals" in action_str else "music"
        try:
            audio_pcm, worker = process_audio(selected_video['id'], mode)
            notify("Playing " + mode, selected_video['title'], thumb_path)
            
            if "Select Quality" not in action_str:
                video_format = "bestvideo"
            else:
                if "bestvideo" not in video_format:
                    video_format = "bestvideo"
            
            # THE CRITICAL FIX: Force mpv to treat the file as a live stream
            live_args = [
                f"--audio-file={audio_pcm}",
                "--audio-demuxer=rawaudio",
                "--demuxer-rawaudio-rate=44100",
                "--demuxer-rawaudio-channels=2",
                "--demuxer-rawaudio-format=s16le",
                "--cache=yes",
                "--cache-secs=300", # Large buffer
                "--demuxer-readahead-secs=60",
                "--aid=1"
            ]
            
            final_cmd = mpv_cmd + [url, f"--ytdl-format={video_format}"] + live_args + sub_args
            player_proc = subprocess.Popen(final_cmd)
            
            # Keep script alive while player or worker is running
            while player_proc.poll() is None or worker.is_alive():
                time.sleep(1)
                
        except Exception as e:
            notify("Error", f"Processing failed: {e}")

if __name__ == "__main__":
    main()
