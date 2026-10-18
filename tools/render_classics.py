"""Pre-render the Classics audio once on a Mac, so the server never needs
a runtime or paid voice for them (MAY-706, option A; readers MAY-1276).

Run from the project folder:

    .venv/bin/python tools/render_classics.py                      # every tale without an MP3 yet
    .venv/bin/python tools/render_classics.py classic-stone-soup   # these tales only, rendered again
    ~/.venvs/kokoro/bin/python tools/render_classics.py --kokoro   # Emma and George with local Kokoro

Each tale is read by the reader slot named in classics.py. Emma and George
use the Mistral voices in config.json, one request per sentence like the
server; Nana and Grandad use ElevenLabs. Keys come from the environment or
.env (MISTRAL_API_KEY, ELEVENLABS_API_KEY). With --kokoro, Emma and George
use the local Kokoro voices instead (kokoro-onnx, numpy, model files
kokoro-v1.0.onnx and voices-v1.0.bin in ~/.cache/kokoro); the nine first
classics were made that way.

The paragraphs are joined with the server's pauses (1.2 s between parts,
2.5 s after the last), encoded as one MP3 per tale, and their start times
are merged into classics_audio/manifest.json. ffmpeg must be on PATH.
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


def kokoro_reader():
    from kokoro_onnx import Kokoro
    import numpy as np

    kokoro = Kokoro(str(MODEL), str(VOICES))

    def speak(text, voice, clip, previous):
        result = kokoro.create(text, voice=voice, speed=1.0, lang='en-gb')
        samples = np.concatenate([np.asarray(chunk) for chunk in audio_chunks(result)])
        with wave.open(str(clip), 'wb') as audio:
            audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
            audio.writeframes((samples * 32767).astype('<i2').tobytes())
    return speak


def provider_reader(kokoro):
    def speak(text, voice, clip, previous):
        if kokoro and voice in ('bf_emma', 'bm_george'):
            return kokoro(text, voice, clip, previous)
        import server  # only for the paid readers: the Kokoro environment lacks the server's packages
        if voice in server.TWIN:
            server.eleven_speech(text, voice, clip, previous)
        else:
            server.mistral_speech(text, voice, clip)
    return speak


def render_tale(tale, speak):
    folder = OUTPUT / tale['id']
    folder.mkdir(parents=True, exist_ok=True)
    voice = tale.get('reader', 'bf_emma')
    starts, position, clips = [], 0.0, []
    with tempfile.TemporaryDirectory() as work:
        for number, paragraph in enumerate(tale['paragraphs'], 1):
            clip = Path(work) / f'p{number:02d}.wav'
            speak(paragraph, voice, clip, tale['paragraphs'][number - 2] if number > 1 else '')
            with wave.open(str(clip), 'rb') as audio:
                seconds = audio.getnframes() / audio.getframerate()
            clips.append(clip)
            starts.append(round(position, 3))
            position += seconds + (LAST_PAUSE if number == len(tale['paragraphs']) else PAUSE)
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
    print(f"{tale['title']} ({voice}): {position / 60:.1f} min, {mp3.stat().st_size / 1e6:.1f} MB", flush=True)
    return {'starts': starts, 'seconds': round(position, 3)}


def render(only, kokoro):
    manifest_path = OUTPUT / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    tales = [tale for tale in classics.CLASSICS
             if (tale['id'] in only if only else not (OUTPUT / tale['id'] / 'story.mp3').is_file())]
    if only - {tale['id'] for tale in tales}:
        raise SystemExit(f'Unknown tale ids: {sorted(only - {tale["id"] for tale in tales})}')
    speak = provider_reader(kokoro_reader() if kokoro else None)
    for tale in tales:
        manifest[tale['id']] = render_tale(tale, speak)
        manifest_path.write_text(json.dumps(manifest, indent=2))  # after each tale, so a failure keeps the finished ones
    print(f'{len(tales)} tales rendered, manifest holds {len(manifest)}')


if __name__ == '__main__':
    arguments = sys.argv[1:]
    render({a for a in arguments if a != '--kokoro'}, '--kokoro' in arguments)
