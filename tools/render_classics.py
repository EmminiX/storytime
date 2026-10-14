"""Pre-render the Classics audio once on a Mac, so the server never needs
a runtime voice for them (MAY-706, option A).

Run from the project folder with the Kokoro environment:

    ~/.venvs/kokoro/bin/python tools/render_classics.py

It synthesises every classic with the Kokoro voice (bf_emma by default,
the reader fixed in classics.py), joins the paragraphs with the same pauses
the server uses (1.2 s between parts, 2.5 s after the last), encodes one
MP3 per tale and records the paragraph start times in a manifest. The
server serves these files from classics_audio/ and never calls a paid or
runtime voice for a classic that has them.

Dependencies (on the Mac only, not needed by the server): kokoro-onnx,
numpy, and ffmpeg on PATH. Model files: kokoro-v1.0.onnx and
voices-v1.0.bin in ~/.cache/kokoro.
"""

import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import classics  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
OUTPUT = BASE / 'classics_audio'
MODEL = Path.home() / '.cache' / 'kokoro' / 'kokoro-v1.0.onnx'
VOICES = Path.home() / '.cache' / 'kokoro' / 'voices-v1.0.bin'
RATE = 24000
PAUSE = 1.2
LAST_PAUSE = 2.5


def audio_chunks(result):
    """kokoro-onnx returns one result, or a list of chunks for long text."""
    parts = result if isinstance(result, list) else [result]
    for part in parts:
        if hasattr(part, 'audio'):
            yield part.audio
        else:
            yield part[0]


def render(voice):
    from kokoro_onnx import Kokoro
    import numpy as np

    kokoro = Kokoro(str(MODEL), str(VOICES))
    OUTPUT.mkdir(exist_ok=True)
    manifest = {}
    for tale in classics.CLASSICS:
        folder = OUTPUT / tale['id']
        folder.mkdir(exist_ok=True)
        starts, position = [], 0.0
        with tempfile.TemporaryDirectory() as work:
            clips = []
            for number, paragraph in enumerate(tale['paragraphs'], 1):
                result = kokoro.create(paragraph, voice=voice, speed=1.0, lang='en-gb')
                samples = np.concatenate([np.asarray(chunk) for chunk in audio_chunks(result)])
                clip = Path(work) / f'p{number:02d}.wav'
                with wave.open(str(clip), 'wb') as audio:
                    audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
                    audio.writeframes((samples * 32767).astype('<i2').tobytes())
                clips.append(clip)
                starts.append(round(position, 3))
                position += len(samples) / RATE + (LAST_PAUSE if number == len(tale['paragraphs']) else PAUSE)
            joined = Path(work) / 'story.wav'
            with wave.open(str(joined), 'wb') as audio:
                audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
                for number, clip in enumerate(clips, 1):
                    with wave.open(str(clip), 'rb') as part:
                        while frames := part.readframes(24000):
                            audio.writeframesraw(frames)
                        audio.writeframesraw(b'\0' * round(RATE * (LAST_PAUSE if number == len(clips) else PAUSE)) * 2)
            mp3 = folder / 'story.mp3'
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(joined),
                            '-codec:a', 'libmp3lame', '-b:a', '48k', '-ar', str(RATE), '-ac', '1', str(mp3)], check=True)
        manifest[tale['id']] = {'starts': starts, 'seconds': round(position, 3)}
        print(f"{tale['title']}: {position / 60:.1f} min, {mp3.stat().st_size / 1e6:.1f} MB", flush=True)
    (OUTPUT / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print(f"manifest for {len(manifest)} tales written to {OUTPUT / 'manifest.json'}")


if __name__ == '__main__':
    render(sys.argv[1] if len(sys.argv) > 1 else 'bf_emma')
