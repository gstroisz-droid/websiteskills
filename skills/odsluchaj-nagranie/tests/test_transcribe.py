"""End-to-end CLI mechanics with real FFmpeg and a deterministic stand-in recognizer.

Run using: python3 -m unittest discover -s tests -v
The generated tones encode source seconds. These tests measure routing, overlap
and resume behavior, not speech-recognition accuracy. No network is required.
"""
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "transcribe.py"
SAMPLE_RATE = 16000
FAKE_ENGINE = '''#!/usr/bin/env python3
import json
import os
import struct
import sys
import wave
from pathlib import Path

args = sys.argv[1:]
if "-f" not in args:
    print("whisper-cli integration fixture")
    sys.exit(0)
wav_path = args[args.index("-f") + 1]
prefix = args[args.index("-of") + 1]
with wave.open(wav_path, "rb") as wav:
    rate = wav.getframerate()
    assert rate == 16000 and wav.getnchannels() == 1 and wav.getsampwidth() == 2
    length = wav.getnframes()
    labels = []
    for frame in range(0, length - rate // 2 + 1, rate):
        wav.setpos(frame)
        samples = struct.unpack("<64h", wav.readframes(64))
        level = max(abs(sample) for sample in samples)
        if level < 4096:
            continue
        second = (level - 4096) // 4
        start_ms = frame * 1000 // rate
        labels.append({"offsets": {"from": start_ms, "to": start_ms + 500},
                       "text": f" znacznik{second:04d}."})
log = Path(os.environ["ENGINE_LOG"])
count = len(log.read_text().splitlines()) if log.exists() else 0
with log.open("a") as stream:
    print(f"{count + 1} {length}", file=stream)
if count + 1 == int(os.environ.get("ENGINE_FAIL_CALL", "0")):
    sys.exit("simulated recognition failure")
Path(prefix + ".json").write_text(json.dumps({"transcription": labels}), encoding="utf8")
'''


def tagged_recording(path, seconds):
    """Create compressible PCM, embedding each source second in tone amplitude."""
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        for second in range(seconds):
            level = 4096 + 4 * second
            period = struct.pack("<hh", level, -level)
            wav.writeframesraw(period * (SAMPLE_RATE // 2))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
class TranscribeCLI(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.source = self.base / "nagranie z przerwą.wav"
        self.output = self.base / "pełny tekst.txt"
        self.model = self.base / "ggml-test.bin"
        self.model.write_bytes(b"fixture model")
        self.cli = self.base / "whisper-cli"
        self.cli.write_text(FAKE_ENGINE, encoding="utf8")
        self.cli.chmod(0o755)
        self.log = self.base / "calls"

    def invoke(self, chunk=30, overlap=4, **extra):
        return subprocess.run(
            [sys.executable, str(RUNNER), str(self.source), "--model", str(self.model),
             "--cli", str(self.cli), "--output", str(self.output), "--language", "pl",
             "--chunk-seconds", str(chunk), "--overlap-seconds", str(overlap)],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "ENGINE_LOG": str(self.log), **extra},
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_transcript(self, seconds):
        words = re.findall(r"znacznik(\d+)", self.output.read_text(encoding="utf8"))
        self.assertEqual([int(word) for word in words], list(range(seconds)))

    def test_new_short_audio(self):
        tagged_recording(self.source, 8)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_transcript(8)
        self.assertEqual(len(self.calls()), 1)

    def test_hour_plus_and_partial_last_chunk(self):
        seconds, chunk, overlap = 3661, 300, 5
        tagged_recording(self.source, seconds)
        result = self.invoke(chunk, overlap)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_transcript(seconds)
        lengths = [int(line.split()[1]) for line in self.calls()]
        self.assertGreater(len(lengths), 12)
        self.assertLessEqual(max(lengths), (chunk + 2 * overlap + 1) * SAMPLE_RATE)

    def test_failed_chunk_resumes_without_repeating_success(self):
        seconds = 75
        tagged_recording(self.source, seconds)
        failed = self.invoke(ENGINE_FAIL_CALL="2")
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(self.calls()), 2)
        recovered = self.invoke()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assert_transcript(seconds)
        self.assertEqual(len(self.calls()), 4, "expected 3 successful chunks and 1 failed attempt")

    def test_digital_silence_produces_no_recognized_words(self):
        with wave.open(str(self.source), "wb") as wav:
            wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * (65 * SAMPLE_RATE))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(encoding="utf8").strip(), "")
        self.assertEqual(self.calls(), [])

    def test_repeat_is_idempotent_and_change_is_detected(self):
        tagged_recording(self.source, 8)
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.invoke()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.model.write_bytes(b"changed model")
        changed = self.invoke()
        self.assertNotEqual(changed.returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assert_transcript(8)

    def test_preexisting_output_is_preserved(self):
        tagged_recording(self.source, 8)
        self.output.write_text("existing", encoding="utf8")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(encoding="utf8"), "existing")

    def test_invalid_input_does_not_create_transcript(self):
        self.source.write_bytes(b"not an audio file")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
