#!/usr/bin/env python3
import sys
import subprocess
import json
import os
import shutil
import tempfile
import re
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
    """Download and separate audio."""
    # mode: 'vocals' (keep vocals) or 'music' (keep music/no_vocals)
    
    # Initial notification
    nid = notify("Processing", "Step 1/2: Downloading audio...", urgency="critical", progress=0)
    
    work_dir = os.path.join(CACHE_DIR, "proc_" + video_id)
    os.makedirs(work_dir, exist_ok=True)
    
    # 1. Download Audio
    audio_path = os.path.join(work_dir, "input.m4a")
    if not os.path.exists(audio_path):
        # yt-dlp doesn't give easy progress we can parse reliably without complex logic,
        # so just pulse/unknown progress for download.
        subprocess.run([
            YT_DLP_BIN,
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "-o", audio_path,
            "--no-playlist",
            video_id
        ], check=True)
    
    # Update notification for separation start
    nid = notify("Processing", "Step 2/2: Separating stems...", urgency="critical", progress=0, replace_id=nid)

    # 2. Run Demucs
    # Output structure: <out>/htdemucs/input/vocals.wav
    cmd = [
        DEMUCS_BIN,
        "-n", "htdemucs",
        "--two-stems=vocals", # Separates into 'vocals' and 'no_vocals'
        "-o", work_dir,
        audio_path
    ]
    
    try:
        # Run process and read stderr for tqdm progress
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        # Buffer to accumulate characters to find percentage
        buf = ""
        last_percent = -1
        while True:
            # Read character by character to handle \r
            # We explicitly check for stderr existence to satisfy linters, though PIPE guarantees it.
            if process.stderr:
                char = process.stderr.read(1)
            else:
                char = ""
                
            if not char and process.poll() is not None:
                break
            if char:
                buf += char
                if char in ('\r', '\n'):
                    # Analyze the line/segment
                    # Tqdm output example: " 15%|###   | ..."
                    if "%" in buf:
                        match = re.search(r'(\d+)%', buf)
                        if match:
                            percent = int(match.group(1))
                            # Update notification if percentage changed (to avoid spam but keep it smooth)
                            if percent != last_percent:
                                nid = notify("Processing", f"Separating: {percent}%", urgency="critical", progress=percent, replace_id=nid)
                                last_percent = percent
                    buf = ""
        
        # Check return code
        if process.returncode != 0:
            if process.stderr:
                 err = process.stderr.read()
            else:
                 err = "Unknown error"
            raise subprocess.CalledProcessError(process.returncode, cmd, stderr=err)
            
    except subprocess.CalledProcessError as e:
        notify("Error", f"Demucs failed:\n{e.stderr}", urgency="critical", replace_id=nid)
        raise e
    
    # Final success update
    notify("Processing", "Done! Opening player...", urgency="normal", progress=100, replace_id=nid)

    # 3. Find output
    # Demucs output is usually inside the model name folder, then filename (without ext)
    # input filename is 'input'
    base_out = os.path.join(work_dir, "htdemucs", "input")
    
    if mode == "vocals":
        return os.path.join(base_out, "vocals.wav")
    else:
        return os.path.join(base_out, "no_vocals.wav")

def select_quality():
    """Prompt user for video quality."""
    options = [
        "🌟 Max (4K/8K)",
        "🖥️ 1080p",
        "💻 720p",
        "📱 480p",
        "📉 360p"
    ]
    selection = walker_dmenu("Select Video Quality", options)
    
    if not selection:
        return None
        
    if "Max" in selection:
        return "bestvideo"
    elif "1080p" in selection:
        return "bestvideo[height<=1080]"
    elif "720p" in selection:
        return "bestvideo[height<=720]"
    elif "480p" in selection:
        return "bestvideo[height<=480]"
    elif "360p" in selection:
        return "bestvideo[height<=360]"
    return "bestvideo" # Default

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
    
    mpv_cmd = ["mpv", "--script-opts=ytdl_hook-ytdl_path=" + YT_DLP_BIN, "--force-window"]
    
    # Logic Handling
    video_format = "bestvideo+bestaudio/best" # Default normal watch
    sub_args = []
    
    if "Select Quality" in action_str:
        # 1. Quality
        quality_setting = select_quality()
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
                
            subprocess.Popen(mpv_cmd + [url, f"--ytdl-format={video_format}", f"--audio-file={audio_file}"] + sub_args)
        except Exception as e:
            notify("Error", f"Processing failed: {e}")

if __name__ == "__main__":
    main()
