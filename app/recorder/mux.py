"""Финализация записи: сборка итоговых файлов из сегментов через FFmpeg.

Вход: segments.json (сырые PCM-аудиосегменты и MKV-видеосегменты с меткой t0)
и интервалы мьюта микрофона. Все дорожки выравниваются по общей нулевой точке
(минимальный t0) фильтром adelay/tpad.

Результат в папке созвона:
  mic.ogg / speakers.ogg  — раздельные mono-дорожки (Opus, удобно для диаризации);
  mix.ogg                 — общий голосовой микс (вход для распознавания речи);
  call.mp4                — видео окна + микс (или call.m4a, если видео нет).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.config import Config
from app.recorder.encode import scale_max_height, scaled_dims, video_encode_args

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
COPY_MODE_MAX_LEAD_S = 0.05


class MuxError(RuntimeError):
    pass


def _ffprobe_path(cfg: Config) -> str:
    p = Path(cfg.ffmpeg_path)
    if p.name.lower().startswith("ffmpeg"):
        cand = p.with_name(p.name.lower().replace("ffmpeg", "ffprobe", 1))
        if p.is_absolute():
            return str(cand)
        return cand.name  # "ffmpeg" из PATH -> "ffprobe" из PATH
    return "ffprobe"


async def _run_ffmpeg(cfg: Config, args: list[str], log_path: Path) -> None:
    cmd = [cfg.ffmpeg_path, "-hide_banner", "-y", *args]
    with open(log_path, "ab") as lf:
        lf.write(("\n$ " + " ".join(cmd) + "\n").encode("utf-8", "replace"))
        lf.flush()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=lf, stderr=lf, creationflags=CREATE_NO_WINDOW)
        rc = await proc.wait()
    if rc != 0:
        tail = ""
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            pass
        raise MuxError(f"ffmpeg завершился с кодом {rc}. Хвост лога:\n{tail}")


async def _probe_duration(cfg: Config, path: Path) -> float:
    try:
        proc = await asyncio.create_subprocess_exec(
            _ffprobe_path(cfg), "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nk=1:nw=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)
        out, _ = await proc.communicate()
        return max(0.0, float(out.decode("ascii", "ignore").strip() or 0))
    except (OSError, ValueError):
        return 0.0


def _mute_expr(mute_intervals: list, t_base: float) -> str | None:
    parts = []
    for s, e in mute_intervals:
        s_rel, e_rel = max(0.0, s - t_base), max(0.0, e - t_base)
        if e_rel - s_rel > 0.2:
            parts.append(f"between(t,{s_rel:.3f},{e_rel:.3f})")
    return "+".join(parts) if parts else None


async def _build_audio_track(cfg: Config, call_dir: Path, segs: list[dict],
                             out_name: str, log_path: Path,
                             t_base: float, mute_expr: str | None = None,
                             denoise: bool = False,
                             duck_with: Path | None = None) -> Path | None:
    """PCM-сегменты одного источника -> mono Opus с расстановкой по таймлайну.

    denoise   — шумоподавление (highpass + afftdn), для микрофона;
    duck_with — приглушать дорожку, пока «говорит» указанный файл
                (sidechain-компрессия; убирает эхо динамиков в микрофоне).
    """
    segs = [s for s in segs if (call_dir / s["path"]).exists()
            and (call_dir / s["path"]).stat().st_size > 0]
    if not segs:
        return None
    args: list[str] = ["-loglevel", "warning"]
    for s in segs:
        args += ["-f", "s16le", "-ar", str(s["rate"]), "-ac", str(s["channels"]),
                 "-i", str(call_dir / s["path"])]
    duck_idx: int | None = None
    if duck_with is not None and duck_with.exists():
        duck_idx = len(segs)
        args += ["-i", str(duck_with)]
    chains = []
    labels = []
    for i, s in enumerate(segs):
        ms = max(0, round((s["t0"] - t_base) * 1000))
        chain = f"[{i}:a]aformat=sample_rates=48000:channel_layouts=mono"
        if ms > 0:
            chain += f",adelay={ms}:all=1"
        lbl = f"a{i}"
        chains.append(chain + f"[{lbl}]")
        labels.append(lbl)
    if len(labels) > 1:
        chains.append("".join(f"[{l}]" for l in labels) +
                      f"amix=inputs={len(labels)}:duration=longest:normalize=0[t]")
        out_lbl = "t"
    else:
        out_lbl = labels[0]
    if denoise:
        chains.append(f"[{out_lbl}]highpass=f=90,afftdn=nr=12:nf=-32[dn]")
        out_lbl = "dn"
    if duck_idx is not None:
        chains.append(
            f"[{out_lbl}][{duck_idx}:a]sidechaincompress="
            f"threshold=0.02:ratio=8:attack=5:release=350[dk]")
        out_lbl = "dk"
    if mute_expr:
        chains.append(f"[{out_lbl}]volume=enable='{mute_expr}':volume=0[tm]")
        out_lbl = "tm"
    out_path = call_dir / out_name
    args += ["-filter_complex", ";".join(chains), "-map", f"[{out_lbl}]",
             "-c:a", "libopus", "-b:a", "48k", "-vbr", "on", "-application", "voip",
             str(out_path)]
    await _run_ffmpeg(cfg, args, log_path)
    return out_path


async def finalize_call(cfg: Config, call_dir: Path, segdata: dict,
                        mute_intervals: list) -> dict:
    """Собирает итоговые файлы; возвращает поля путей для журнала записи."""
    audio_segs = segdata.get("audio", [])
    video_segs = [v for v in segdata.get("video", [])
                  if (call_dir / v["path"]).exists()
                  and (call_dir / v["path"]).stat().st_size > 0]
    mic_segs = [s for s in audio_segs if s.get("kind") == "mic"]
    spk_segs = [s for s in audio_segs if s.get("kind") == "speakers"]

    all_t0 = ([s["t0"] for s in mic_segs] + [s["t0"] for s in spk_segs] +
              [v["t0"] for v in video_segs])
    if not all_t0:
        log.warning("Нет медиасегментов в %s — собирать нечего", call_dir)
        return {}
    t_base = min(all_t0)
    log_path = call_dir / "ffmpeg_mux.log"
    result: dict = {}

    # Сначала динамики: их дорожка нужна микрофону как sidechain для эхо-дака.
    spk_path = await _build_audio_track(
        cfg, call_dir, spk_segs, "speakers.ogg", log_path, t_base)
    mic_path = await _build_audio_track(
        cfg, call_dir, mic_segs, "mic.ogg", log_path, t_base,
        mute_expr=_mute_expr(mute_intervals, t_base),
        denoise=cfg.mic_denoise,
        duck_with=spk_path if cfg.echo_duck else None)
    if mic_path:
        result["mic_path"] = str(mic_path)
    if spk_path:
        result["speakers_path"] = str(spk_path)

    tracks = [p for p in (mic_path, spk_path) if p]

    # Единый компактный микс mix.ogg — вход для сервера распознавания речи.
    if tracks:
        mix_path = call_dir / "mix.ogg"
        args = ["-loglevel", "warning"]
        for t in tracks:
            args += ["-i", str(t)]
        if len(tracks) == 2:
            fc = "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[m]"
        else:
            fc = "[0:a]anull[m]"
        args += ["-filter_complex", fc, "-map", "[m]", "-ac", "1",
                 "-c:a", "libopus", "-b:a", "48k", "-vbr", "on",
                 "-application", "voip", str(mix_path)]
        await _run_ffmpeg(cfg, args, log_path)

    # Основной артефакт: mp4 с видео либо m4a без него.
    if video_segs:
        out = call_dir / "call.mp4"
        await _mux_video(cfg, call_dir, video_segs, tracks, t_base, out, log_path)
        result["video_path"] = str(out)
        result["media_path"] = str(out)
    elif tracks:
        out = call_dir / "call.m4a"
        args = ["-loglevel", "warning"]
        for t in tracks:
            args += ["-i", str(t)]
        if len(tracks) == 2:
            args += ["-filter_complex",
                     "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[o]",
                     "-map", "[o]"]
        else:
            args += ["-map", "0:a"]
        args += ["-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)]
        await _run_ffmpeg(cfg, args, log_path)
        result["media_path"] = str(out)

    _cleanup_raw(cfg, call_dir, audio_segs, video_segs)

    # Экономия места (опции из настроек): раздельные дорожки и лог сборки нужны
    # уже собранному mix.ogg/call.mp4 — удаляем только в самом конце.
    if cfg.delete_stems:
        for name in ("mic.ogg", "speakers.ogg"):
            try:
                (call_dir / name).unlink(missing_ok=True)
            except OSError:
                pass
        result["mic_path"] = None
        result["speakers_path"] = None
    if cfg.delete_mux_log:
        try:
            (call_dir / "ffmpeg_mux.log").unlink(missing_ok=True)
        except OSError:
            pass
    return result


async def _mux_video(cfg: Config, call_dir: Path, video_segs: list[dict],
                     tracks: list[Path], t_base: float, out: Path,
                     log_path: Path) -> None:
    segs = sorted(video_segs, key=lambda v: v["t0"])
    durs = []
    for v in segs:
        durs.append(await _probe_duration(cfg, call_dir / v["path"]))
    # Абсолютная расстановка сегментов на таймлайне через ведущие паузы.
    leads = []
    pos = 0.0
    for v, d in zip(segs, durs):
        off = max(0.0, v["t0"] - t_base)
        start = max(off, pos)
        leads.append(start - pos)
        pos = start + d

    n_aud = len(tracks)
    audio_maps: list[str] = []
    fc_audio = ""
    if n_aud == 2:
        fc_audio = (f"[{len(segs)}:a][{len(segs) + 1}:a]"
                    f"amix=inputs=2:duration=longest:normalize=0[am]")
        audio_maps = ["-map", "[am]"]
    elif n_aud == 1:
        audio_maps = ["-map", f"{len(segs)}:a"]

    args: list[str] = ["-loglevel", "warning"]
    for v in segs:
        args += ["-i", str(call_dir / v["path"])]
    for t in tracks:
        args += ["-i", str(t)]

    # Масштабирование обязывает пересжать: поток «как есть» скопировать нельзя.
    max_h = scale_max_height(cfg)
    need_scale = max_h > 0 and any(int(v["height"]) > max_h for v in segs)
    copy_ok = (len(segs) == 1 and leads[0] <= COPY_MODE_MAX_LEAD_S
               and not need_scale)
    if copy_ok:
        fc = fc_audio
        args += (["-filter_complex", fc] if fc else [])
        args += ["-map", "0:v", "-c:v", "copy", *audio_maps]
    else:
        w = max(int(v["width"]) // 2 * 2 for v in segs)
        h = max(int(v["height"]) // 2 * 2 for v in segs)
        # Общий кадр сжимаем целиком: сегменты и так вписываются в него
        # фильтром scale+pad ниже, отдельно масштабировать каждый не нужно.
        w, h = scaled_dims(w, h, max_h)
        if need_scale:
            log.info("Масштабирование итогового видео до %dx%d", w, h)
        fps = int(segs[0].get("fps") or cfg.video_fps)
        chains = []
        for i, (v, lead) in enumerate(zip(segs, leads)):
            chain = (f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}")
            if lead > 0.01:
                chain += f",tpad=start_duration={lead:.3f}"
            chains.append(chain + f"[v{i}]")
        chains.append("".join(f"[v{i}]" for i in range(len(segs))) +
                      f"concat=n={len(segs)}:v=1:a=0[vc]")
        if fc_audio:
            chains.append(fc_audio)
        args += ["-filter_complex", ";".join(chains), "-map", "[vc]",
                 *video_encode_args(cfg, fps), *audio_maps]
    if audio_maps:
        args += ["-c:a", "aac", "-b:a", "96k"]
    args += ["-movflags", "+faststart", str(out)]
    await _run_ffmpeg(cfg, args, log_path)


def _cleanup_raw(cfg: Config, call_dir: Path, audio_segs: list[dict],
                 video_segs: list[dict]) -> None:
    if cfg.keep_raw:
        return
    for s in audio_segs + video_segs:
        try:
            (call_dir / s["path"]).unlink(missing_ok=True)
        except OSError:
            pass
    for name in ("segments.json", "ffmpeg_video.log"):
        try:
            (call_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
