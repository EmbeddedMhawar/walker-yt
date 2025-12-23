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
        # Standard hint for progress bar (0-100)
        cmd.extend(["-h", f"int:value:{progress}"])
        # Synchronous hint to ensure replacement works on servers ignoring -r
        cmd.extend(["-h", f"string:x-canonical-private-synchronous:walker-yt"])
    
    if replace_id or progress is not None:
        # Capture stdout to get the ID if we are starting a new replaceable notification
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
    # If using images in dmenu becomes possible/known, add flags here.
    # Currently sending just text.
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
    """Download and separate audio using a high-speed 'Split & Pipe' architecture."""
    
    # Cleanup existing processes
    try: subprocess.run(["pkill", "-f", "demucs"], stderr=subprocess.DEVNULL)
    except: pass

    work_dir = os.path.join(CACHE_DIR, "proc_" + video_id)
    os.makedirs(work_dir, exist_ok=True)
    
    # Paths
    audio_path = os.path.join(work_dir, "input.m4a")
    chunks_dir = os.path.join(work_dir, "chunks")
    out_chunks_dir = os.path.join(work_dir, "out_chunks")
    # We use a raw PCM file for the "growing" audio stream
    playback_file = os.path.join(work_dir, "live_audio.raw")
    
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

    # 2. Split into 30s chunks (Nearly Instant)
    # Clear old chunks
    for f in os.listdir(chunks_dir): os.remove(os.path.join(chunks_dir, f))
    subprocess.run([
        "ffmpeg", "-y", "-i", audio_path, 
        "-f", "segment", "-segment_time", "30", 
        "-c", "copy", os.path.join(chunks_dir, "chunk_%03d.m4a")
    ], check=True)

    sorted_chunks = sorted([f for f in os.listdir(chunks_dir) if f.endswith(".m4a")])
    
    nid = notify("Live Stream", f"Step 2/3: Separating (0/{len(sorted_chunks)})", urgency="critical", progress=0, replace_id=nid)

    def processing_worker():
        """Background thread to process chunks one by one."""
        total = len(sorted_chunks)
        for i, chunk_file in enumerate(sorted_chunks):
            chunk_path = os.path.join(chunks_dir, chunk_file)
            
            # Run Demucs on this tiny 30s chunk
            cmd = [
                DEMUCS_BIN, "-n", "mdx_extra", "--two-stems=vocals",
                "-d", "cpu", "-j", "4",
                "-o", out_chunks_dir, chunk_path
            ]
            
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Find the output
            chunk_base = os.path.splitext(chunk_file)[0]
            stem_name = "vocals.wav" if mode == "vocals" else "no_vocals.wav"
            separated_wav = os.path.join(out_chunks_dir, "mdx_extra", chunk_base, stem_name)
            
            if os.path.exists(separated_wav):
                # Convert to raw PCM and append to the playback file
                with open(playback_file, "ab") as f_out:
                    subprocess.run([
                        "ffmpeg", "-y", "-i", separated_wav, 
                        "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", "-"
                    ], stdout=f_out, stderr=subprocess.DEVNULL)

            
            # Update progress
            percent = int(((i + 1) / total) * 100)
            notify("Live Stream", f"Separating: Chunk {i+1}/{total}", urgency="low", progress=percent, replace_id=nid)

    # Start the worker in background
    worker_thread = threading.Thread(target=processing_worker, daemon=True)
    worker_thread.start()

    # 3. Wait for the FIRST chunk to be ready before returning to main
    start_time = time.time()
    while not os.path.exists(playback_file) or os.path.getsize(playback_file) < 100000:
        if not worker_thread.is_alive() and (not os.path.exists(playback_file) or os.path.getsize(playback_file) < 100000):
            raise Exception("Processing worker died prematurely.")
        if time.time() - start_time > 120:
            raise Exception("Timeout waiting for first live chunk.")
        time.sleep(1)

    notify("Live Stream", "Ready! Opening Player...", urgency="normal", progress=10, replace_id=nid)
    return playback_file


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
        
        # Store (height, fps) pairs
        quality_map = {}
        for f in formats:
            h = f.get('height')
            fps = f.get('fps')
            if h and f.get('vcodec') != 'none':
                if h not in quality_map:
                    quality_map[h] = set()
                if fps:
                    quality_map[h].add(int(fps))
        
        # Build display strings
        options = []
        for h in sorted(quality_map.keys(), reverse=True):
            f_list = sorted(list(quality_map[h]), reverse=True)
            if f_list:
                for fps in f_list:
                    # Only show FPS if it's high (>= 50) or if it's the only one
                    if fps >= 50 or len(f_list) == 1:
                        options.append(f"{h}p{fps}")
                    elif fps == 30 and 60 not in quality_map[h]:
                         options.append(f"{h}p{fps}")
            else:
                options.append(f"{h}p")
        
        # Clean up duplicates like 1080p60 and 1080p30 if they exist
        return options
    except Exception:
        return []

def select_quality(video_id):
    """Prompt user for video quality."""
    notify("Quality", "Fetching available resolutions & FPS...", urgency="low")
    options = get_video_qualities(video_id)
    
    if not options:
        # Fallback
        options = ["1080p60", "1080p30", "720p60", "720p30", "480p", "360p"]
    
    selection = walker_dmenu("Select Video Quality", options)
    
    if not selection:
        return None
        
    # Extract height and fps from "1080p60"
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
        "--quiet", # Don't print progress
        "https://www.youtube.com/watch?v=" + video_id
    ]
    
    # yt-dlp --list-subs output is human readable, but we can parse it.
    # Alternatively we can use --dump-json but that fetches EVERYTHING.
    # Let's parse the human readable output.
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        lines = result.stdout.splitlines()
        subs = []
        
        # Parse output looking for 'Language Name Formats' table
        start_parsing = False
        for line in lines:
            if "Available automatic captions" in line:
                # We generally prefer manual captions if available, but auto is better than nothing
                pass 
            if "Language" in line and "Name" in line and "Formats" in line:
                start_parsing = True
                continue
            
            if start_parsing and line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    code = parts[0]
                    # Name can be multi-word. Last part is formats.
                    # This is a rough parse. 
                    # A better way is usually to just offer common languages or prompt user to type code.
                    # But let's try to grab the code and a readable name.
                    name = " ".join(parts[1:-1])
                    if not name: name = code
                    subs.append(f"{name} ({code})")
        
        # Remove duplicates
        return sorted(list(set(subs)))
        
    except Exception:
        return []

def select_subtitles(video_id):
    """Prompt user for subtitles."""
    # We can either fetch the list (slow) or offer common presets.
    # User asked to "select subtitles to add them later".
    # Fetching list takes 1-2s.
    
    notify("Subtitles", "Fetching subtitle list...", urgency="low")
    subs = get_subtitles(video_id)
    
    if not subs:
        notify("Subtitles", "No subtitles found.", urgency="normal")
        return None
        
    # Add an "None" option
    subs.insert(0, "🚫 None")
    
    selection = walker_dmenu("Select Subtitles", subs)
    
    if not selection or "None" in selection:
        return None
        
    # Extract code from "Name (code)"
    match = re.search(r'\((.*?)\)$', selection)
    if match:
        return match.group(1)
    return None

def main():
    # 1. Get Query
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
    
    # 2. Search
    videos = search_youtube(query)
    
    if not videos:
        notify("Walker YT", "No results found.")
        return

    # 3. Prepare Menu
    display_lines = [f"{v['title']} ({v['channel']})" for v in videos]
    
    selected_str = walker_dmenu("Select Video", display_lines)
    
    if not selected_str:
        return
        
    # Find selected video
    selected_video = None
    try:
        index = display_lines.index(selected_str)
        selected_video = videos[index]
    except ValueError:
        return

    # Download thumbnail in background
    thumb_path = download_thumbnail(selected_video['thumbnail'], selected_video['id'])
    
    # 4. Action Menu
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
        "--cache-pause-wait=5", # Wait for 5s of buffer before resuming if it hits end
        "--demuxer-readahead-secs=20" # Buffer 20s ahead
    ]
    
    # Logic Handling
    video_format = "bestvideo+bestaudio/best" # Default normal watch
    sub_args = []
    
    if "Select Quality" in action_str:
        # 1. Quality
        quality_setting = select_quality(selected_video['id'])
        if not quality_setting:
            return # Cancelled
        
        if "Watch Video" in action_str:
            # Normal watch with quality cap
            video_format = f"{quality_setting}+bestaudio/best"
        else:
            # Keep Vocals/Music (video only stream for visual)
            video_format = quality_setting
            
        # 2. Subtitles
        # User asked to select them "to add them later when desired".
        # This implies we pass them to MPV but maybe disabled by default?
        # MPV loads subs if we pass --sub-file or --ytdl-raw-options=sub-lang=...
        # We'll ask the user which language they want available.
        sub_code = select_subtitles(selected_video['id'])
        if sub_code:
            # Tell MPV to pull this subtitle track from YouTube
            # --ytdl-raw-options=sub-lang=en,write-sub=,write-auto-sub=
            # Actually mpv handles this cleaner with --slang
            sub_args = [f"--slang={sub_code}"]
            # We can also force it to show immediately with --sid=1 or keep hidden
            # Default mpv behavior is usually to show if slang matches system, else hidden?
            # We will just make it available.

    if "Watch Video" in action_str:
        notify("Playing", selected_video['title'], thumb_path)
        subprocess.Popen(mpv_cmd + [url, f"--ytdl-format={video_format}"] + sub_args)
        
    elif "Listen Audio" in action_str:
        notify("Playing Audio", selected_video['title'], thumb_path)
        subprocess.Popen(mpv_cmd + ["--no-video", url])
        
    elif "Keep Vocals" in action_str or "Keep Music" in action_str:
        mode = "vocals" if "Keep Vocals" in action_str else "music"
        try:
            audio_file = process_audio(selected_video['id'], mode)
            notify("Playing " + mode, selected_video['title'], thumb_path)
            
            # If we didn't select quality (rare case if I changed menu), default to bestvideo
            if "Select Quality" not in action_str:
                video_format = "bestvideo"
            
            live_args = []
            if audio_file.endswith(".raw"):
                # Tell MPV how to interpret the raw PCM stream
                live_args = [
                    "--demuxer-rawaudio-rate=44100",
                    "--demuxer-rawaudio-channels=2",
                    "--demuxer-rawaudio-format=s16le",
                    "--cache=yes",
                    "--cache-pause-wait=5",
                    "--demuxer-readahead-secs=60"
                ]
                
            subprocess.Popen(mpv_cmd + [url, f"--ytdl-format={video_format}", f"--audio-file={audio_file}"] + sub_args + live_args)
        except Exception as e:
            notify("Error", f"Processing failed: {e}")

if __name__ == "__main__":
    main()
