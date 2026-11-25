"""Optional offline smoke tests with genuine whisper.cpp inference and synthetic speech.

Set WHISPER_CLI to whisper-cli and WHISPER_MODEL to a multilingual ggml model.
Set WHISPER_VAD_MODEL to the Silero GGML file to exercise VAD and its noise regression.
FFmpeg's flite filter supplies the spoken reference. No network calls are made.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "transcribe.py"
PHRASES = (
    "At the start of the recording the garden gate is open",
    "In the middle of the recording the kitchen light is blue",
    "At the end of the recording the silver train is leaving",
)


def speech_fixture(path, duration, phrases):
    filters = []
    tracks = []
    for idx, (start_ms, phrase) in enumerate(phrases):
        track = f"p{idx}"
        filters.append(
            f"flite=text='{phrase}':voice=slt,aresample=16000,"
            f"adelay={start_ms},apad=whole_dur={duration}[{track}]"
        )
        tracks.append(f"[{track}]")
    filters.append("".join(tracks) + f"amix=inputs={len(tracks)}:normalize=0[out]")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-filter_complex", ";".join(filters),
         "-map", "[out]", "-ar", "16000", "-ac", "1", "-c:a", "flac", str(path)],
        check=True, timeout=240,
    )


@unittest.skipUnless(os.getenv("WHISPER_CLI") and os.getenv("WHISPER_MODEL"),
                     "set WHISPER_CLI and WHISPER_MODEL to enable real inference")
class ReferenceSpeech(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("ffmpeg"):
            raise unittest.SkipTest("requires FFmpeg with flite")
        filters = subprocess.check_output(["ffmpeg", "-hide_banner", "-filters"],
                                          stderr=subprocess.STDOUT, text=True)
        if not re.search(r"\bflite\s+", filters):
            raise unittest.SkipTest("FFmpeg lacks the flite speech synthesizer")

    def transcribe(self, duration, phrases, chunk=240, overlap=5):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "speech.flac"
            out = Path(tmp) / "speech.txt"
            speech_fixture(source, duration, phrases)
            return self.run_recognizer(source, out, chunk, overlap)

    def run_recognizer(self, source, out, chunk, overlap, detect_sibling=False):
        model = Path(os.environ["WHISPER_MODEL"]).resolve()
        vad_arg = []
        vad = os.getenv("WHISPER_VAD_MODEL")
        if vad:
            vad = Path(vad).resolve()
            if detect_sibling:
                alias = out.parent / model.name
                alias.symlink_to(model)
                model = alias
                (out.parent / "ggml-silero-v5.1.2.bin").symlink_to(vad)
            else:
                vad_arg = ["--vad-model", str(vad)]
        command = [
            sys.executable, str(RUNNER), str(source),
            "--cli", os.environ["WHISPER_CLI"], "--model", str(model),
            "--language", "en", "--threads", "2", "--chunk-seconds", str(chunk),
            "--overlap-seconds", str(overlap), "--output", str(out), *vad_arg,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr)
        if vad:
            manifest = json.loads((out.parent / (out.name + ".work") / "identity.json").read_text())
            self.assertEqual(manifest["vad"], hashlib.sha256(vad.read_bytes()).hexdigest())
        return " ".join(re.findall(r"[a-z]+", out.read_text(encoding="utf8").lower()))

    @unittest.skipUnless(os.getenv("WHISPER_VAD_MODEL"), "set WHISPER_VAD_MODEL for noise regression")
    def test_pink_noise_with_sibling_vad(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "noise.wav"
            out = Path(tmp) / "noise.txt"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            "anoisesrc=color=pink:amplitude=0.2:seed=1234:duration=8",
                            "-ar", "16000", "-ac", "1", str(source)], check=True, timeout=60)
            text = self.run_recognizer(source, out, chunk=240, overlap=5, detect_sibling=True)
            self.assertEqual(text, "")
            self.assertEqual(out.read_text(encoding="utf8").strip(), "")

    def test_markers_in_65_minutes(self):
        phrases = list(zip((2000, 1922000, 3900000), PHRASES))
        text = self.transcribe(3910, phrases)
        positions = []
        for marker in ("garden gate is open", "kitchen light is blue", "silver train is leaving"):
            self.assertEqual(text.count(marker), 1, text)
            positions.append(text.index(marker))
        self.assertEqual(positions, sorted(positions), text)

    def test_spoken_clause_crosses_chunk_boundary(self):
        phrase = PHRASES[0]
        text = self.transcribe(34, [(14400, phrase)], chunk=16, overlap=5)
        self.assertEqual(text, phrase.lower(), text)


if __name__ == "__main__":
    unittest.main()
