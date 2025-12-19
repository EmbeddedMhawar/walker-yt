# Walker YouTube (walker-yt)

A powerful, interactive YouTube launcher for Linux (Hyprland/Wayland) using `walker` as the UI, `mpv` for playback, and AI-powered stem separation.

## 🚀 Features

- **🔍 Smart Search**: Instant YouTube search with title and channel information.
- **🎬 Visual Playback**: Stream video directly in `mpv`.
- **🎧 Audio Only**: Efficient background listening mode.
- **🎤 Music Remover (AI)**:
  - **Keep Vocals**: Isolate the singing from any video.
  - **Keep Music**: Remove vocals to create an instrumental/karaoke track.
  - **Real-time Progress**: Live notification bars showing separation percentage.
- **⚙️ Quality Selection**: Choose your preferred resolution (4K, 1080p, 720p, etc.) to save bandwidth.
- **📜 Subtitles**: Select and load any available YouTube subtitle track.
- **🔗 Synced Stems**: Watch the original video while listening to the AI-separated audio perfectly synced.

## 🛠️ Requirements

- **[Walker](https://github.com/abenz1267/walker)**: The application launcher/UI.
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)**: YouTube engine.
- **[mpv](https://mpv.io/)**: Media player.
- **[Demucs](https://github.com/facebookresearch/demucs)**: AI stem separation.
- **FFmpeg**: For audio/video processing.
- **notify-send**: For progress updates.

## 📦 Installation

1. **Clone the script**:
   ```bash
   mkdir -p ~/System/bashScripts/walker-yt
   # Save the python script to walker-yt.py
   ```

2. **Setup AI Environment**:
   ```bash
   python -m venv ~/.local/share/walker-yt/venv
   ~/.local/share/walker-yt/venv/bin/pip install demucs yt-dlp soundfile
   ```

3. **Add Keybind (Hyprland)**:
   Add this to your `bindings.conf` or `hyprland.conf`:
   ```bash
   bind = SUPER, Y, exec, ~/System/bashScripts/walker-yt/walker-yt.py
   ```

## 📂 Cache
Downloaded audio and processed stems are stored in `~/.cache/walker-yt/`.

## 🤝 Credits
- Powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [Demucs](https://github.com/facebookresearch/demucs).
- Designed for the [Walker](https://github.com/abenz1267/walker) launcher.
