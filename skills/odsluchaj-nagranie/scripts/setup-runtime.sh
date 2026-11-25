#!/usr/bin/env bash
# Install verified Whisper resources once. This script never reads recordings.
set -euo pipefail
version=1.7.6
prefix=''
profile=base
model_name=ggml-base.bin
archive=''
model=''
offline=false
vad_name=ggml-silero-v5.1.2.bin
vad_member=whisper.cpp-$version/models/for-tests-silero-v5.1.2-ggml.bin
vad_sha=29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf
source_sha=166140e9a6d8a36f787a2bd77f8f44dd64874f12dd8359ff7c1f4f9acb86202e
model_sha=60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe
source_url="https://github.com/ggml-org/whisper.cpp/archive/refs/tags/v$version.tar.gz"
model_repo='https://huggingface.co/ggerganov/whisper.cpp/resolve/fb5ed9369c26eaf08392504d8446da431e0fffb9'

usage() {
    cat <<'USAGE'
Usage: bash setup-runtime.sh [--prefix DIR] [--quality] [--offline] [--source-archive PATH] [--model-file PATH]
Requires Python 3, FFmpeg, and, on first setup, CMake, a C/C++ compiler and a build tool.
First setup fetches whisper.cpp v1.7.6 and multilingual Whisper base (or the
larger large-v3-turbo-q5_0 with --quality in its own cache) unless local
files are provided. --offline forbids downloads. The last stdout line is the
absolute runtime directory containing bin/whisper-cli and the speech and voice-activity models under models/.
An older cache missing the voice-activity model is upgraded using the source
archive only. For an offline upgrade, pass --source-archive.
USAGE
}
die() { printf '%s\n' "$*" >&2; exit 1; }
while (($#)); do
    case "$1" in
        --prefix|--source-archive|--model-file)
            [[ $# -ge 2 && -n $2 ]] || die "Missing value for $1"
            case "$1" in
                --prefix) prefix=$2 ;;
                --source-archive) archive=$2 ;;
                --model-file) model=$2 ;;
            esac
            shift 2 ;;
        --quality)
            profile=quality
            model_name=ggml-large-v3-turbo-q5_0.bin
            model_sha=394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2
            shift ;;
        --offline) offline=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done
command -v python3 >/dev/null || die 'Python 3 is required.'
command -v ffmpeg >/dev/null || die 'FFmpeg is required.'
prefix=${prefix:-"${XDG_CACHE_HOME:-$HOME/.cache}/odsluchaj-nagranie/whisper-$version-$profile"}
prefix=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$prefix")
checksum() {
    python3 - "$1" "$2" <<'PY'
import hashlib
import sys
digest = hashlib.sha256()
with open(sys.argv[1], 'rb') as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(block)
if digest.hexdigest() != sys.argv[2]:
    sys.exit("SHA256 mismatch: " + sys.argv[1])
PY
}
ready_engine() {
    [[ -x "$prefix/bin/whisper-cli" && -f "$prefix/models/$model_name" ]] &&
        checksum "$prefix/models/$model_name" "$model_sha" &&
        "$prefix/bin/whisper-cli" -h >/dev/null 2>&1
}
mkdir -p -- "$(dirname "$prefix")"
stage=$(mktemp -d "$(dirname "$prefix")/.whisper-install.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

obtain() {
    local existing=$1 url=$2 target=$3 expected=$4
    if [[ -n "$existing" ]]; then
        [[ -f "$existing" ]] || die "Missing input: $existing"
        cp -- "$existing" "$target"
    else
        [[ "$offline" == false ]] || die "Offline setup requires a local copy of ${url##*/}"
        curl --fail --show-error --location --retry 2 --connect-timeout 15 \
            --proto '=https' --proto-redir '=https' --output "$target" "$url"
    fi
    checksum "$target" "$expected"
}

unpack_vad() {
    tar -xzOf "$stage/source.tar.gz" "$vad_member" > "$1"
    checksum "$1" "$vad_sha"
}

if [[ -e "$prefix" ]]; then
    ready_engine || die "Incomplete or unusable runtime at $prefix. Choose a new --prefix."
    if ! [[ -f "$prefix/models/$vad_name" ]] || ! checksum "$prefix/models/$vad_name" "$vad_sha"; then
        obtain "$archive" "$source_url" "$stage/source.tar.gz" "$source_sha"
        unpack_vad "$stage/$vad_name"
        python3 -c 'import os,sys; os.replace(sys.argv[1],sys.argv[2])' \
            "$stage/$vad_name" "$prefix/models/$vad_name"
    fi
    printf '%s\n' "$prefix"
    exit 0
fi

command -v cmake >/dev/null || die 'CMake is required on PATH for the first build.'
mkdir -p "$stage/src" "$stage/runtime/bin" "$stage/runtime/models"
obtain "$archive" "$source_url" "$stage/source.tar.gz" "$source_sha"
unpack_vad "$stage/runtime/models/$vad_name"
obtain "$model" "$model_repo/$model_name" "$stage/runtime/models/$model_name" "$model_sha"
tar -xzf "$stage/source.tar.gz" --strip-components=1 -C "$stage/src"
cmake -S "$stage/src" -B "$stage/build" -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=ON -DGGML_OPENMP=OFF \
    -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=ON >&2
cmake --build "$stage/build" --config Release --target whisper-cli --parallel 2 >&2
cp "$stage/build/bin/whisper-cli" "$stage/runtime/bin/"
"$stage/runtime/bin/whisper-cli" -h >/dev/null 2>&1 || die 'New whisper-cli failed to start.'
# rename() on a nonempty installation owned by a parallel setup fails without replacing it.
if ! python3 -c 'import os,sys; os.rename(sys.argv[1], sys.argv[2])' "$stage/runtime" "$prefix"; then
    ready_engine && checksum "$prefix/models/$vad_name" "$vad_sha" || die "Unable to finalize runtime in $prefix"
fi
printf '%s\n' "$prefix"
