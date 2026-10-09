#!/usr/bin/env python3
"""Add chapter-timed ElevenLabs narration without changing recorded video evidence."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


def probe_duration(path: Path) -> float:
    result = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path),
    ], text=True)
    return float(json.loads(result)["format"]["duration"])


def validate_plan(segments: list[dict], duration: float) -> None:
    previous = 0.0
    if not segments:
        raise ValueError("Narration plan is empty")
    for segment in segments:
        start, end = segment["start"], segment["end"]
        if (not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (start, end))
                or start < previous or end <= start or end > duration):
            raise ValueError("Narration windows must be ordered, nonoverlapping, and inside the video")
        if not isinstance(segment.get("text"), str) or not segment["text"].strip():
            raise ValueError("Each narration window requires text")
        previous = end


def speech_speed(audio_duration: float, window: float) -> float:
    speed = max(1.0, audio_duration / (window - 0.12))
    if speed > 1.18:
        raise ValueError("Narration exceeds its chapter window; shorten the script rather than rush or truncate it")
    return speed


def srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def caption_blocks(alignment: dict, offset: float, speed: float) -> list[tuple[float, float, str]]:
    text = "".join(alignment["characters"])
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    if not (len(text) == len(starts) == len(ends)):
        raise ValueError("Speech alignment is incomplete")
    words = list(re.finditer(r"\S+", text))
    blocks, group = [], []
    for word in words:
        if group and (word.end() - group[0].start() > 74 or len(group) >= 12):
            first, last = group[0], group[-1]
            blocks.append((offset + starts[first.start()] / speed,
                           offset + ends[last.end() - 1] / speed,
                           text[first.start():last.end()]))
            group = []
        group.append(word)
    if group:
        first, last = group[0], group[-1]
        blocks.append((offset + starts[first.start()] / speed,
                       offset + ends[last.end() - 1] / speed,
                       text[first.start():last.end()]))
    return blocks


def narrate(video: Path, plan: Path, output: Path) -> None:
    duration = probe_duration(video)
    segments = json.loads(plan.read_text())
    validate_plan(segments, duration)
    key, voice, model = (os.environ.get(name, "") for name in
                         ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL"))
    if not all((key, voice, model)):
        raise ValueError("Configure ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, and ELEVENLABS_MODEL")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", voice):
        raise ValueError("Invalid configured voice ID")
    folder = output.parent / "narration"
    folder.mkdir(parents=True, exist_ok=True)
    captions, timing, tracks = [], [], []
    for index, segment in enumerate(segments):
        body = {
            "text": segment["text"], "model_id": model,
            "voice_settings": {"stability": 0.55, "similarity_boost": 0.75,
                               "style": 0, "use_speaker_boost": True, "speed": 1},
            "previous_text": segments[index - 1]["text"] if index else "",
            "next_text": segments[index + 1]["text"] if index + 1 < len(segments) else "",
        }
        identity = hashlib.sha256(json.dumps({"voice": voice, **body}, sort_keys=True).encode()).hexdigest()
        audio_path = folder / f"{index + 1:02}.mp3"
        cache = folder / f"{index + 1:02}.json"
        cached = json.loads(cache.read_text()) if cache.exists() else {}
        if cached.get("identity") != identity or not audio_path.exists():
            request = Request(
                "https://api.elevenlabs.io/v1/text-to-speech/" + quote(voice, safe="")
                + "/with-timestamps?output_format=mp3_44100_128",
                data=json.dumps(body).encode(),
                headers={"xi-api-key": key, "Content-Type": "application/json"}, method="POST",
            )
            try:
                with urlopen(request, timeout=120) as response:
                    result = json.load(response)
            except HTTPError as error:
                detail = error.read().decode(errors="replace").replace(key, "[REDACTED]")
                raise RuntimeError(f"ElevenLabs HTTP {error.code}: {detail[:400]}") from None
            audio_path.write_bytes(base64.b64decode(result["audio_base64"], validate=True))
            cached = {"identity": identity, "alignment": result.get("normalized_alignment") or result["alignment"]}
            cache.write_text(json.dumps(cached, indent=2) + "\n")
        audio_duration = probe_duration(audio_path)
        speed = speech_speed(audio_duration, segment["end"] - segment["start"])
        tracks.append((audio_path, segment["start"], speed))
        captions.extend(caption_blocks(cached["alignment"], segment["start"], speed))
        timing.append({**segment, "audio_seconds": audio_duration, "playback_speed": round(speed, 5),
                       "spoken_end": round(segment["start"] + audio_duration / speed, 3)})
        print(f"Narration {index + 1}/{len(segments)}: {audio_duration:.2f}s, playback {speed:.3f}x", flush=True)
    subtitles = folder / "narration.srt"
    subtitles.write_text("\n\n".join(
        f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{text}"
        for i, (start, end, text) in enumerate(captions, 1)
    ) + "\n")
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for path, _, _ in tracks:
        command += ["-i", str(path)]
    filters = [f"anullsrc=r=48000:cl=mono:d={duration}[bed]"]
    for i, (_, start, speed) in enumerate(tracks):
        filters.append(f"[{i}:a]atempo={speed},aresample=48000,adelay={round(start * 1000)}:all=1[a{i}]")
    filters.append("[bed]" + "".join(f"[a{i}]" for i in range(len(tracks)))
                   + f"amix=inputs={len(tracks) + 1}:duration=longest:normalize=0,"
                   + f"atrim=duration={duration},loudnorm=I=-16:TP=-1.5:LRA=11[out]")
    master = folder / "narration.wav"
    subprocess.run(command + ["-filter_complex", ";".join(filters), "-map", "[out]",
                              "-ar", "48000", "-c:a", "pcm_s16le", str(master)], check=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-i", str(master),
        "-i", str(subtitles), "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "mov_text",
        "-metadata:s:a:0", "language=eng", "-metadata:s:s:0", "language=eng",
        "-metadata:s:s:0", "title=English narration", "-t", str(duration),
        "-movflags", "+faststart", str(output),
    ], check=True)
    (folder / "timing.json").write_text(json.dumps({"provider": "ElevenLabs", "model": model,
        "video_seconds": duration, "segments": timing}, indent=2) + "\n")
    (folder / "transcript.md").write_text("# Demo narration\n\n" + "\n\n".join(s["text"] for s in segments) + "\n")
    print(f"Narrated video saved: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    narrate(args.video.resolve(), args.plan.resolve(), args.output.resolve())
