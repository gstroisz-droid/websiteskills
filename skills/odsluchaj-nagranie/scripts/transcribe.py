#!/usr/bin/env python3
"""Offline transcription with FFmpeg and whisper.cpp. Requires Python 3.9+ on POSIX."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import wave

RATE = 16_000
FRAME = 2  # One signed 16-bit little-endian PCM sample.


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


@contextlib.contextmanager
def atomic_text(path):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        name = stream.name
        try:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)


def executable(name):
    path = shutil.which(name)
    if not path:
        raise ValueError(f"Missing executable: {name}")
    return str(Path(path).resolve())


def decode_windows(source, ffmpeg, step, overlap, work):
    """Stream once, keeping at most one core and its two context regions in RAM."""
    args = [ffmpeg, "-nostdin", "-v", "error", "-xerror",
            "-protocol_whitelist", "file,pipe", "-i", str(source),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(RATE),
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    with tempfile.TemporaryFile(dir=work) as errors, subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=errors
    ) as proc:
        try:
            start = offset = 0
            data = b""
            while True:
                target = (start + step + overlap - offset) * FRAME
                data += proc.stdout.read(target - len(data))
                eof = len(data) < target
                if eof and proc.wait() != 0:
                    errors.seek(max(0, errors.tell() - 4000))
                    raise ValueError(f"FFmpeg: {errors.read().decode(errors='replace').strip()}")
                if len(data) % FRAME:
                    raise ValueError("Incomplete PCM frame from FFmpeg")
                end = min(start + step, offset + len(data) // FRAME)
                if end <= start:
                    break
                yield data, offset, start, end
                if eof and end == offset + len(data) // FRAME:
                    break
                start += step
                next_offset = max(0, start - overlap)
                data = data[(next_offset - offset) * FRAME:]
                offset = next_offset
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()


def recognize(pcm, origin, start, end, args, directory):
    # Trim only digital zeros. Retain up to 0.25 s context and complete sample frames.
    left = len(pcm) - len(pcm.lstrip(b"\0"))
    if left == len(pcm):
        return ""
    right = len(pcm.rstrip(b"\0"))
    first = max(0, left // FRAME - RATE // 4)
    last = min(len(pcm) // FRAME, (right + FRAME - 1) // FRAME + RATE // 4)
    wav_path = directory / "audio.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setparams((1, FRAME, RATE, 0, "NONE", "not compressed"))
        wav.writeframes(pcm[first * FRAME:last * FRAME])
    prefix = directory / "recognized"
    command = [args.cli, "-m", str(args.model), "-f", str(wav_path), "-l", args.language,
               "-t", str(args.threads), "-ml", "1", "-sow", "-oj", "-of", str(prefix)]
    if args.vad_model is not None:
        command.extend(["--vad", "-vm", str(args.vad_model)])
    # Whisper emits JSON with millisecond offsets. Request short word-aligned segments
    # so the midpoint ownership rule can discard duplicates in overlapping context.
    with tempfile.TemporaryFile(dir=directory) as errors:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=errors)
        if result.returncode:
            errors.seek(max(0, errors.tell() - 4000))
            raise ValueError(f"whisper-cli exited {result.returncode}: "
                             f"{errors.read().decode(errors='replace').strip()}")
    segments = json.loads(prefix.with_suffix(".json").read_text(encoding="utf-8"))["transcription"]
    if not isinstance(segments, list):
        raise ValueError("Invalid transcription in whisper-cli JSON")
    words = []
    for segment in segments:
        text = segment["text"]
        a, b = segment["offsets"]["from"], segment["offsets"]["to"]
        if not isinstance(text, str) or not all(isinstance(t, (int, float)) and math.isfinite(t)
                                                for t in (a, b)) or not 0 <= a <= b:
            raise ValueError("Invalid whisper-cli text or segment offsets")
        center = origin + first + (a + b) * RATE / 2000
        if start <= center < end and text.strip():
            words.append(text.strip())
    return " ".join(words)


def run(args):
    args.cli = executable(args.cli)
    ffmpeg = executable("ffmpeg")
    step = round(args.chunk_seconds * RATE)
    overlap = round(args.overlap_seconds * RATE)
    if not 1 <= args.chunk_seconds <= 1800 or not 0 <= overlap <= step // 2:
        raise ValueError("Choose a chunk duration of 1–1800 seconds and overlap of 0–half a chunk")
    if args.threads < 1:
        raise ValueError("Thread count must be positive")
    source, model, output = (path.resolve() for path in (args.input, args.model, args.output))
    if not all(path.is_file() and path.stat().st_size for path in (source, model)):
        raise ValueError("Provide a readable, nonempty input and whisper.cpp model")
    if args.vad_model is None:
        # Prefer the requested model directory, including a symlink-based runtime.
        for directory in (args.model.absolute().parent, model.parent):
            sibling = directory / "ggml-silero-v5.1.2.bin"
            if sibling.is_file():
                args.vad_model = sibling
                break
    if args.vad_model is not None and not args.vad_model.is_file():
        raise ValueError(f"Missing VAD model: {args.vad_model}")
    if output in (source, model) or (args.vad_model and output == args.vad_model.resolve()):
        raise ValueError("Output must differ from the input and model paths")
    work = output.with_name(output.name + ".work")
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (work / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"Another transcription owns {work}") from None
        signature = dict(schema=1, input=digest(source), model=digest(model), cli=digest(Path(args.cli)),
                         ffmpeg=digest(Path(ffmpeg)), vad=digest(args.vad_model) if args.vad_model else None,
                         language=args.language, step=step,
                         overlap=overlap, threads=args.threads)
        manifest = work / "identity.json"
        if manifest.exists():
            if json.loads(manifest.read_text(encoding="utf-8")) != signature:
                raise ValueError("Input, engine or settings changed. Choose a new output path.")
        else:
            if output.exists() or any(work.glob("chunk-*.txt")):
                raise ValueError("Output or checkpoints already exist. Choose a new output path.")
            with atomic_text(manifest) as file:
                json.dump(signature, file)
        count = 0
        with contextlib.closing(decode_windows(source, ffmpeg, step, overlap, work)) as windows:
            for index, (pcm, origin, start, end) in enumerate(windows):
                count += 1
                chunk = work / f"chunk-{index:08d}.txt"
                if chunk.exists():
                    continue
                print(f"{start/RATE:.2f}–{end/RATE:.2f} s", file=sys.stderr, flush=True)
                with tempfile.TemporaryDirectory(dir=work) as folder:
                    text = recognize(pcm, origin, start, end, args, Path(folder))
                with atomic_text(chunk) as file:
                    file.write(text)
        if not count:
            raise ValueError("The input contains no decoded audio frames")
        with atomic_text(output) as result:
            for index in range(count):
                text = (work / f"chunk-{index:08d}.txt").read_text(encoding="utf-8")
                if text:
                    print(text, file=result)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--vad-model", type=Path, help="Silero GGML model (uses ggml-silero-v5.1.2.bin beside the ASR model if present)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cli", default="whisper-cli")
    parser.add_argument("--language", default="auto")
    parser.add_argument("--threads", type=int, default=min(os.cpu_count() or 1, 4))
    parser.add_argument("--chunk-seconds", type=float, default=240)
    parser.add_argument("--overlap-seconds", type=float, default=5)
    try:
        return run(parser.parse_args())
    except (ValueError, OSError, KeyError, TypeError, OverflowError, EOFError, wave.Error) as error:
        print(f"Transcription failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
