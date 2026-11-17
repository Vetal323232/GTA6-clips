"""Generate a 60 s sample video: quiet background with loud bursts at 15 s and 42 s."""

import subprocess
import sys


def make_sample(path: str, duration: int = 60, bursts=(15, 42)) -> None:
    loud = "+".join(f"between(t,{b},{b + 1})" for b in bursts)
    audio = (
        f"anoisesrc=d={duration}:c=pink:a=0.02:r=44100[bg];"
        f"anoisesrc=d={duration}:c=white:a=0.9:r=44100,volume='if({loud},1,0)':eval=frame[boom];"
        "[bg][boom]amix=inputs=2:normalize=0[a]"
    )
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=d={duration}:s=640x360:r=30",
        "-filter_complex", audio, "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", path,
    ], check=True)


if __name__ == "__main__":
    make_sample(sys.argv[1] if len(sys.argv) > 1 else "sample.mp4")
