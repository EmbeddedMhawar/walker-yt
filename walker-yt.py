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
import signal

# Configuration
VENV_PATH = os.path.expanduser("~/.local/share/walker-yt/venv")
DEMUCS_BIN = os.path.join(VENV_PATH, "bin", "demucs")
YT_DLP_BIN = os.path.join(os.path.expanduser("~/.local/bin"), "yt-dlp")
if not os.path.exists(YT_DLP_BIN):
    YT_DLP_BIN = "yt-dlp"

CACHE_DIR = os.path.expanduser("~/.cache/walker-yt")
os.makedirs(CACHE_DIR, exist_ok=True)


def log(message):
    with open("/tmp/walker-yt.log", "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {message}\n")

def cleanup_handler(sig, frame):
    log(f"Received signal {sig}. Cleaning up...")
    try: subprocess.run(["pkill", "-f", "demucs"], stderr=subprocess.DEVNULL)
    except: pass
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup_handler)
signal.signal(signal.SIGTERM, cleanup_handler)

def notify(title, body, icon=None, urgency="normal", progress=None, replace_id=None):
    log(f"NOTIFY: {title} - {body}")
    cmd = ["notify-send", "-u", urgency, title, body]
    if icon: cmd.extend(["-i", icon])
    if replace_id: cmd.extend(["-r", str(replace_id)])
    if progress is not None:
        cmd.extend(["-h", f"int:value:{progress}"])
        cmd.extend(["-h", f"string:x-canonical-private-synchronous:walker-yt"])
    
    if replace_id or progress is not None:
        cmd.append("-p")
        result = subprocess.run(cmd, capture_output=True, text=True)
        try: return int(result.stdout.strip())
        except: return None
    else:
        subprocess.run(cmd)
        return None

def walker_dmenu(prompt, lines):
    cmd = ["walker", "-d", "-p", prompt]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out, _ = process.communicate(input="\n".join(lines))
    return out.strip()

def search_youtube(query):
    cmd = [YT_DLP_BIN, "ytsearch10:" + query, "--print", "%(title)s\t%(channel)s\t%(id)s\t%(thumbnail)s", "--no-playlist"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        videos = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                videos.append({"title": parts[0], "channel": parts[1], "id": parts[2], "thumbnail": parts[3]})
        return videos
    except: return []

def download_thumbnail(url, video_id):
    path = os.path.join(CACHE_DIR, f"{video_id}.jpg")
    if not os.path.exists(path):
        subprocess.run(["curl", "-s", "-L", url, "-o", path], stdout=subprocess.DEVNULL)
    return path

def get_video_qualities(video_id):
    cmd = [YT_DLP_BIN, "--dump-json", "--no-playlist", "https://www.youtube.com/watch?v=" + video_id]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)
        formats = data.get('formats', [])
        quality_map = {}
        for f in formats:
            h = f.get('height')
            fps = f.get('fps')
            if h and f.get('vcodec') != 'none':
                if h not in quality_map: quality_map[h] = set()
                if fps: quality_map[h].add(int(fps))
        options = []
        for h in sorted(quality_map.keys(), reverse=True):
            f_list = sorted(list(quality_map[h]), reverse=True)
            if f_list:
                for fps in f_list:
                    if fps >= 50 or len(f_list) == 1: options.append(f"{h}p{fps}")
                    elif fps == 30 and 60 not in quality_map[h]: options.append(f"{h}p{fps}")
            else: options.append(f"{h}p")
        return options
    except: return []

def select_quality(video_id):
    notify("Quality", "Fetching available resolutions & FPS...", urgency="low")
    options = get_video_qualities(video_id)
    if not options: options = ["1080p60", "1080p30", "720p60", "720p30", "480p", "360p"]
    selection = walker_dmenu("Select Video Quality", options)
    if not selection: return None
    match = re.match(r'(\d+)p(\d+)?', selection)
    if match:
        height, fps = match.group(1), match.group(2)
        return f"bestvideo[height<={height}][fps<={fps}]" if fps else f"bestvideo[height<={height}]"
    return "bestvideo"

def get_subtitles(video_id):
    cmd = [YT_DLP_BIN, "--list-subs", "--quiet", "https://www.youtube.com/watch?v=" + video_id]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        lines = result.stdout.splitlines()
        subs, start_parsing = [], False
        for line in lines:
            if "Language" in line and "Name" in line and "Formats" in line: start_parsing = True; continue
            if start_parsing and line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    code, name = parts[0], " ".join(parts[1:-1])
                    if not name: name = code
                    subs.append(f"{name} ({code})")
        return sorted(list(set(subs)))
    except: return []

def select_subtitles(video_id):
    notify("Subtitles", "Fetching subtitle list...", urgency="low")
    subs = get_subtitles(video_id)
    if not subs: notify("Subtitles", "No subtitles found.", urgency="normal"); return None
    subs.insert(0, "🚫 None")
    selection = walker_dmenu("Select Subtitles", subs)
    if not selection or "None" in selection: return None
    match = re.search(r'\((.*?)\)$', selection)
    return match.group(1) if match else None

def process_audio(video_id, mode):
    """Download and separate audio using high-quality htdemucs with optimized streaming."""
    my_pid = os.getpid()
    try:
        out = subprocess.check_output(["pgrep", "-f", "walker-yt"], text=True)
        for pid_str in out.splitlines():
            pid = int(pid_str)
            if pid != my_pid: os.kill(pid, signal.SIGTERM)
        subprocess.run(["pkill", "-f", "demucs"], stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", "mpv.*--title=walker-yt"], stderr=subprocess.DEVNULL)
    except: pass

    work_dir = os.path.join(CACHE_DIR, "proc_" + video_id)
    os.makedirs(work_dir, exist_ok=True)
    audio_path, chunks_dir, out_chunks_dir = os.path.join(work_dir, "input.m4a"), os.path.join(work_dir, "chunks"), os.path.join(work_dir, "out_chunks")
    playback_file = os.path.join(work_dir, "live_audio.pcm")
    
    if os.path.exists(playback_file): os.remove(playback_file)
    os.makedirs(chunks_dir, exist_ok=True); os.makedirs(out_chunks_dir, exist_ok=True)

    nid = notify("Live AI Stream", "Step 1/3: Downloading & Splitting...", urgency="critical")
    if not os.path.exists(audio_path):
        subprocess.run([YT_DLP_BIN, "-f", "bestaudio[ext=m4a]/bestaudio", "-o", audio_path, "--no-playlist", video_id], check=True)

    subprocess.run(["ffmpeg", "-y", "-i", audio_path, "-f", "segment", "-segment_time", "30", "-c", "copy", os.path.join(chunks_dir, "chunk_%03d.m4a")], check=True)
    sorted_chunks = sorted([f for f in os.listdir(chunks_dir) if f.endswith(".m4a")])
    nid = notify("Live AI Stream", f"Step 2/3: Separating (0/{len(sorted_chunks)})", urgency="critical", progress=0, replace_id=nid)

    def processing_worker():
        log("WORKER: Started")
        try:
            total = len(sorted_chunks)
            for i, chunk_file in enumerate(sorted_chunks):
                chunk_path = os.path.join(chunks_dir, chunk_file)
                # Maximized CPU usage: 8 threads and 400% quota
                cmd = ["systemd-run", "--user", "--scope", "-p", "MemoryMax=10G", "-p", "CPUQuota=400%",
                       "-E", "OMP_NUM_THREADS=8", "-E", "MKL_NUM_THREADS=8",
                       "-E", "OPENBLAS_NUM_THREADS=8", "-E", "VECLIB_MAXIMUM_THREADS=8",
                       DEMUCS_BIN, "-n", "htdemucs", "--two-stems=vocals", "--segment", "7", "--shifts", "0", "--overlap", "0.1", "-d", "cpu", "-j", "1", "-o", out_chunks_dir, chunk_path]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                chunk_base = os.path.splitext(chunk_file)[0]
                separated_wav = os.path.join(out_chunks_dir, "htdemucs", chunk_base, "vocals.wav" if mode == "vocals" else "no_vocals.wav")
                
                if os.path.exists(separated_wav):
                    pcm_data = subprocess.check_output(["ffmpeg", "-y", "-i", separated_wav, "-f", "s16le", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", "-"], stderr=subprocess.DEVNULL)
                    with open(playback_file, "ab") as f_out: f_out.write(pcm_data); f_out.flush(); os.fsync(f_out.fileno())
                    log(f"WORKER: Chunk {i+1} appended. Size: {os.path.getsize(playback_file)}")
                
                percent = int(((i + 1) / total) * 100)
                notify("Live AI Stream", f"Separating: Chunk {i+1}/{total}", urgency="low", progress=percent, replace_id=nid)
            log("WORKER: Finished")
        except Exception as e: log(f"WORKER ERROR: {e}")

    worker_thread = threading.Thread(target=processing_worker, daemon=True)
    worker_thread.start()

    start_time = time.time()
    while True:
        if os.path.exists(playback_file) and os.path.getsize(playback_file) >= 500000: break
        if not worker_thread.is_alive(): raise Exception("Worker died")
        if time.time() - start_time > 120: raise Exception("Timeout")
        time.sleep(1)

    notify("Live AI Stream", "Ready! Opening Player...", urgency="normal", progress=10, replace_id=nid)
    return playback_file, worker_thread

def main():
    if len(sys.argv) > 1: query = " ".join(sys.argv[1:])
    else:
        proc = subprocess.Popen(["walker", "--dmenu", "--inputonly", "-p", "Search YouTube"], stdout=subprocess.PIPE, text=True)
        query = proc.communicate()[0].strip()
    
    if not query: return
    notify("Searching", f"Searching for: {query}...")
    videos = search_youtube(query)
    if not videos: return

    display_lines = [f"{v['title']} ({v['channel']})" for v in videos]
    selected_str = walker_dmenu("Select Video", display_lines)
    if not selected_str: return
    selected_video = videos[display_lines.index(selected_str)]

    thumb_path = download_thumbnail(selected_video['thumbnail'], selected_video['id'])
    actions = ["🎬 Watch Video (Auto)", "⚙️ Watch Video (Select Quality & Subs)", "🎧 Listen Audio (MPV --no-video)", "🎤 Keep Vocals (Select Quality & Subs)", "🎵 Keep Music (Select Quality & Subs)"]
    action_str = walker_dmenu(f"Action: {selected_video['title']}", actions)
    if not action_str: return

    url = f"https://www.youtube.com/watch?v={selected_video['id']}"
    mpv_cmd = ["mpv", "--title=walker-yt", "--script-opts=ytdl_hook-ytdl_path=" + YT_DLP_BIN, "--force-window", "--cache=yes", "--cache-pause-wait=5", "--demuxer-readahead-secs=20"]
    
    video_format, sub_args = "bestvideo+bestaudio/best", []
    if "Select Quality" in action_str:
        quality_setting = select_quality(selected_video['id'])
        if not quality_setting: return
        video_format = f"{quality_setting}+bestaudio/best" if "Watch Video" in action_str else quality_setting
        sub_code = select_subtitles(selected_video['id'])
        if sub_code: sub_args = [f"--slang={sub_code}"]

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
            tail_proc = subprocess.Popen(["tail", "-f", "-c", "+0", audio_pcm], stdout=subprocess.PIPE)
            live_args = ["--audio-file=fd://0", "--audio-demuxer=rawaudio", "--demuxer-rawaudio-rate=44100", "--demuxer-rawaudio-channels=2", "--demuxer-rawaudio-format=s16le", "--cache=yes", "--cache-secs=3600", "--aid=1"]
            
            if "Select Quality" not in action_str: video_format = "bestvideo"
            elif "bestvideo" not in video_format: video_format = "bestvideo"
            
            player_proc = subprocess.Popen(mpv_cmd + [url, f"--ytdl-format={video_format}"] + live_args + sub_args, stdin=tail_proc.stdout)
            if tail_proc.stdout: tail_proc.stdout.close()
            
            while player_proc.poll() is None: time.sleep(1)
            tail_proc.terminate(); subprocess.run(["pkill", "-f", "demucs"], stderr=subprocess.DEVNULL)
        except Exception as e: notify("Error", f"Failed: {e}")

if __name__ == "__main__": main()
