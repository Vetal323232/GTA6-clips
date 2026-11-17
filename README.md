# GTA6 clips

Automatically cut highlight clips out of long GTA 6 gameplay recordings.

The tool listens to the video's audio track. Explosions, gunfire, crashes and
other loud moments stand out from normal gameplay, so every loudness peak is
treated as a highlight. Each highlight is cut into its own clip with a few
seconds of padding before and after, using ffmpeg.

## Requirements

- Python 3.9 or newer
- [ffmpeg](https://ffmpeg.org/download.html) (both `ffmpeg` and `ffprobe` on your PATH)
  - Windows: `winget install ffmpeg`
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`

No Python packages are needed.

## Install

```bash
git clone https://github.com/Vetal323232/GTA6-clips.git
cd GTA6-clips
pip install -e .
```

You can also run it without installing: `python -m gta6clips VIDEO`.

## Usage

```bash
gta6clips my_session.mp4
```

Clips are written to `./clips/`, named like `my_session_clip001_754s.mp4`
(the number is the clip's start time in the original video).

Preview what would be cut, without writing anything:

```bash
gta6clips my_session.mp4 --dry-run
```

### Options

| Option | Default | What it does |
| --- | --- | --- |
| `-o, --out DIR` | `clips` | Output folder |
| `-b, --before SEC` | `5` | Seconds kept before each highlight |
| `-a, --after SEC` | `3` | Seconds kept after each highlight |
| `-s, --sensitivity N` | `2.0` | How much louder than normal a moment must be (in standard deviations). Lower finds more clips, higher finds fewer |
| `--min-db DB` | off | Use a fixed loudness threshold in dBFS instead, e.g. `-12` |
| `-n, --top N` | all | Only keep the N loudest clips |
| `--merge-gap SEC` | `3` | Loud moments closer than this become one clip |
| `--max-len SEC` | `60` | Longest allowed clip |
| `--window SEC` | `0.5` | Size of the loudness analysis window |
| `--copy` | off | Stream-copy instead of re-encoding. Much faster, but cuts snap to keyframes so they can be a few seconds off |
| `--dry-run` | off | Only list the detected highlights |

### Examples

```bash
# Ten best moments, with a longer lead-in
gta6clips session.mkv -n 10 --before 8

# Busy video with lots of noise: be stricter
gta6clips session.mp4 -s 3

# Quiet video: be more generous
gta6clips session.mp4 -s 1.5
```

## How it works

1. ffmpeg decodes the audio to mono 8 kHz PCM.
2. The audio is split into 0.5 s windows and each window's loudness (RMS, dBFS) is measured.
3. Windows louder than `median + sensitivity × std-dev` are marked as highlights; nearby ones are grouped.
4. Each group is padded, clamped to the video length, overlapping clips are merged, and ffmpeg cuts them
   (re-encoded to H.264/AAC MP4 by default for frame-accurate cuts).

Tip: game audio works best. If your recording has loud voice chat or music mixed in,
those will be picked up as highlights too.

## Development

```bash
python -m unittest discover -s tests -v
```

The tests generate a synthetic 60 s video with two loud bursts and check that
exactly those two moments are cut. To make that sample yourself:

```bash
python tests/make_sample.py sample.mp4
gta6clips sample.mp4
```
