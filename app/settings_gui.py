"""Окно настроек (вкладки «Настройки», «Резюме», «Список записей»).

Запускается ОТДЕЛЬНЫМ процессом:
  JitsiStreamLogger.exe --settings   (из exe)
  pythonw -m app.main --settings     (из исходников)

Отдельный процесс — принципиально: tkinter обязан жить в главном потоке
своего процесса; вызов из потока меню трея вешает и диалог, и приложение.
Основное приложение подхватывает правки config.toml и очередь транскрипции
автоматически (watchdog следит за файлами в APPDATA).
"""
from __future__ import annotations

import os
import shutil
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from app import autostart, records, summarize, transcribe
from app.config import APP_TITLE, appdata_dir, load_config, set_config_value
from app.recorder.encode import VIDEO_QUALITY_KEYS, VIDEO_QUALITY_PRESETS

DELETABLE_STATUSES = records.DELETABLE_STATUSES


def _monitor_count() -> int:
    try:
        import win32api

        return max(1, int(win32api.GetSystemMetrics(80)))  # SM_CMONITORS
    except Exception:
        return 1


# --------------------------------------------------------------- вкладка 1

def _build_settings_tab(frm: ttk.Frame, cfg):
    """Возвращает collect() — сохранение значений вкладки в config.toml."""
    monitors = _monitor_count()

    lf_dir = ttk.LabelFrame(frm, text="Записи", padding=8)
    lf_dir.grid(row=0, column=0, sticky="we", pady=(0, 8))
    var_dir = tk.StringVar(value=str(cfg.records_dir))
    ttk.Label(lf_dir, text="Папка:").grid(row=0, column=0, sticky="w")
    ttk.Entry(lf_dir, textvariable=var_dir, width=52).grid(
        row=0, column=1, sticky="we", padx=6)

    def browse() -> None:
        chosen = filedialog.askdirectory(initialdir=var_dir.get(),
                                         title="Папка для записей созвонов")
        if chosen:
            var_dir.set(chosen)

    ttk.Button(lf_dir, text="Обзор…", command=browse).grid(row=0, column=2)

    lf_v = ttk.LabelFrame(frm, text="Видео", padding=8)
    lf_v.grid(row=1, column=0, sticky="we", pady=(0, 8))
    var_mode = tk.StringVar(
        value="off" if not cfg.video_enabled else cfg.video_mode)
    ttk.Radiobutton(lf_v, text="Окно созвона (пауза, если вкладка неактивна)",
                    variable=var_mode, value="window").grid(
        row=0, column=0, columnspan=3, sticky="w")
    ttk.Radiobutton(lf_v, text="Экран, где курсор (универсально)",
                    variable=var_mode, value="cursor").grid(
        row=1, column=0, columnspan=3, sticky="w")
    ttk.Radiobutton(lf_v, text="Монитор №", variable=var_mode,
                    value="monitor").grid(row=2, column=0, sticky="w")
    var_mon = tk.IntVar(value=min(max(1, cfg.video_monitor), monitors))
    ttk.Spinbox(lf_v, from_=1, to=monitors, textvariable=var_mon,
                width=4).grid(row=2, column=1, sticky="w")
    ttk.Label(lf_v, text=f"(найдено мониторов: {monitors})").grid(
        row=2, column=2, sticky="w", padx=6)
    ttk.Radiobutton(lf_v, text="Видео отключено (только звук и журнал)",
                    variable=var_mode, value="off").grid(
        row=3, column=0, columnspan=3, sticky="w")
    ttk.Label(lf_v, text="Кодек и качество — на вкладке «Качество видео».",
              foreground="#777").grid(row=4, column=0, columnspan=3, sticky="w",
                                      pady=(4, 0))

    lf_a = ttk.LabelFrame(frm, text="Звук (обработка при сборке)", padding=8)
    lf_a.grid(row=2, column=0, sticky="we", pady=(0, 8))
    var_denoise = tk.BooleanVar(value=cfg.mic_denoise)
    var_duck = tk.BooleanVar(value=cfg.echo_duck)
    var_mute = tk.BooleanVar(value=cfg.respect_mic_mute)
    ttk.Checkbutton(lf_a, text="Шумоподавление микрофона",
                    variable=var_denoise).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(lf_a, text="Приглушать эхо динамиков в микрофоне",
                    variable=var_duck).grid(row=1, column=0, sticky="w")
    ttk.Checkbutton(lf_a, text="Вырезать интервалы мьюта из дорожки микрофона",
                    variable=var_mute).grid(row=2, column=0, sticky="w")

    lf_f = ttk.LabelFrame(frm, text="Хранение файлов", padding=8)
    lf_f.grid(row=3, column=0, sticky="we", pady=(0, 8))
    var_del_stems = tk.BooleanVar(value=cfg.delete_stems)
    var_del_muxlog = tk.BooleanVar(value=cfg.delete_mux_log)
    ttk.Checkbutton(
        lf_f, text="Удалять mic.ogg и speakers.ogg после создания mix.ogg",
        variable=var_del_stems).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(lf_f, text="Удалять ffmpeg_mux.log после сборки",
                    variable=var_del_muxlog).grid(row=1, column=0, sticky="w")

    lf_t = ttk.LabelFrame(frm, text="Транскрипция (сервер STT)", padding=8)
    lf_t.grid(row=4, column=0, sticky="we", pady=(0, 8))
    var_tr = tk.BooleanVar(value=cfg.tr_enabled)
    ttk.Checkbutton(lf_t,
                    text="Отправлять запись на распознавание после созвона",
                    variable=var_tr).grid(row=0, column=0, columnspan=2,
                                          sticky="w")
    ttk.Label(lf_t, text="Адрес сервера:").grid(row=1, column=0, sticky="w")
    var_url = tk.StringVar(value=cfg.tr_url)
    ttk.Entry(lf_t, textvariable=var_url, width=44).grid(
        row=1, column=1, sticky="we", padx=6)

    lf_m = ttk.LabelFrame(frm, text="Разное", padding=8)
    lf_m.grid(row=5, column=0, sticky="we")
    try:
        autostart_now = autostart.is_installed()
    except Exception:
        autostart_now = False
    var_auto = tk.BooleanVar(value=autostart_now)
    ttk.Checkbutton(lf_m, text="Автозапуск при входе в Windows",
                    variable=var_auto).grid(row=0, column=0, sticky="w")
    ttk.Button(lf_m, text="Открыть лог",
               command=lambda: os.startfile(  # noqa: S606
                   appdata_dir() / "logs" / "app.log")).grid(
        row=0, column=1, sticky="e", padx=12)

    def collect() -> bool:
        try:
            new_dir = Path(var_dir.get()).expanduser()
            new_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Папка записей недоступна:\n{e}")
            return False
        set_config_value("general", "records_dir", str(new_dir))
        mode = var_mode.get()
        set_config_value("video", "enabled", mode != "off")
        if mode != "off":
            set_config_value("video", "mode", mode)
        set_config_value("video", "monitor_index", int(var_mon.get()))
        set_config_value("audio", "mic_denoise", bool(var_denoise.get()))
        set_config_value("audio", "echo_duck", bool(var_duck.get()))
        set_config_value("audio", "respect_mic_mute", bool(var_mute.get()))
        set_config_value("finalize", "delete_stems", bool(var_del_stems.get()))
        set_config_value("finalize", "delete_mux_log", bool(var_del_muxlog.get()))
        set_config_value("transcribe", "enabled", bool(var_tr.get()))
        set_config_value("transcribe", "url",
                         var_url.get().strip().rstrip("/"))
        try:
            if bool(var_auto.get()) != autostart.is_installed():
                if var_auto.get():
                    autostart.install()
                else:
                    autostart.remove()
        except Exception as e:
            messagebox.showwarning(
                APP_TITLE,
                f"Настройки сохранены, но автозапуск не изменён:\n{e}")
        return True

    return collect


# --------------------------------------------------------- вкладка «качество»

def _build_quality_tab(frm: ttk.Frame, cfg):
    """Радио-выбор пресета кодека; применяется к итоговому видео (call.mp4)."""
    # Текущий пресет: из config, иначе выводим из ранее выбранного кодека
    # (совместимость со старыми config.toml, где был только encoder).
    current = getattr(cfg, "video_quality_preset", "") or ""
    if current not in VIDEO_QUALITY_KEYS:
        current = "av1_crf50" if cfg.video_encoder == "libsvtav1" else "h264_crf28"
    var_q = tk.StringVar(value=current)

    ttk.Label(
        frm,
        text="Кодек и параметры сжатия итогового видео созвона (call.mp4).",
    ).grid(row=0, column=0, sticky="w", pady=(0, 8))

    lf = ttk.LabelFrame(frm, text="Кодек и параметры", padding=8)
    lf.grid(row=1, column=0, sticky="we")
    for i, p in enumerate(VIDEO_QUALITY_PRESETS):
        ttk.Radiobutton(lf, text=p["label"], value=p["key"],
                        variable=var_q).grid(row=i * 2, column=0, sticky="w",
                                             pady=(6 if i else 0, 0))
        ttk.Label(lf, text=p["hint"], foreground="#777").grid(
            row=i * 2 + 1, column=0, sticky="w", padx=(24, 0))

    ttk.Label(
        frm, foreground="#777", justify="left",
        text=("CRF больше — меньше размер и ниже качество. H.265, AV1 и VP9\n"
              "дают меньший файл, чем H.264, но сильнее нагружают процессор\n"
              "и хуже совместимы со старыми плеерами."),
    ).grid(row=2, column=0, sticky="w", pady=(10, 0))

    def collect() -> bool:
        set_config_value("video", "quality_preset", var_q.get())
        return True

    return collect


# --------------------------------------------------------------- вкладка 2

def _build_summary_tab(frm: ttk.Frame, cfg):
    """Возвращает collect() — сохранение настроек резюме и промпта."""
    var_on = tk.BooleanVar(value=cfg.sum_enabled)
    ttk.Checkbutton(
        frm, text="Составлять резюме по транскрипту (LLM, /v1/chat/completions)",
        variable=var_on).grid(row=0, column=0, columnspan=3, sticky="w")

    grid_opts = {"sticky": "w", "pady": (6, 0)}
    ttk.Label(frm, text="Адрес LLM:").grid(row=1, column=0, **grid_opts)
    var_url = tk.StringVar(value=cfg.sum_url)
    ttk.Entry(frm, textvariable=var_url, width=44).grid(
        row=1, column=1, columnspan=2, sticky="we", padx=6, pady=(6, 0))
    ttk.Label(frm, text="Модель:").grid(row=2, column=0, **grid_opts)
    var_model = tk.StringVar(value=cfg.sum_model)
    ttk.Entry(frm, textvariable=var_model, width=44).grid(
        row=2, column=1, columnspan=2, sticky="we", padx=6, pady=(6, 0))
    ttk.Label(frm, text="Температура:").grid(row=3, column=0, **grid_opts)
    var_temp = tk.DoubleVar(value=cfg.sum_temperature)
    ttk.Spinbox(frm, from_=0.0, to=1.0, increment=0.05, textvariable=var_temp,
                width=6).grid(row=3, column=1, sticky="w", padx=6, pady=(6, 0))

    ttk.Label(frm, text="Системный промпт:").grid(row=4, column=0,
                                                  columnspan=2, sticky="w",
                                                  pady=(10, 2))
    txt = tk.Text(frm, width=76, height=13, wrap="word", undo=True)
    txt.insert("1.0", summarize.load_system_prompt())
    scroll = ttk.Scrollbar(frm, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=scroll.set)
    txt.grid(row=5, column=0, columnspan=2, sticky="nsew")
    scroll.grid(row=5, column=2, sticky="ns")
    frm.rowconfigure(5, weight=1)
    frm.columnconfigure(1, weight=1)

    def reset_prompt() -> None:
        if messagebox.askyesno(APP_TITLE, "Заменить текст промпта стандартным?"):
            txt.delete("1.0", "end")
            txt.insert("1.0", summarize.DEFAULT_SYSTEM_PROMPT)

    ttk.Button(frm, text="Вернуть стандартный промпт",
               command=reset_prompt).grid(row=6, column=0, sticky="w",
                                          pady=(6, 0))
    ttk.Label(frm, text=f"Файл промпта: {summarize.prompt_path()}",
              foreground="#777").grid(row=7, column=0, columnspan=2,
                                      sticky="w", pady=(6, 0))

    def collect() -> bool:
        prompt = txt.get("1.0", "end").strip()
        if not prompt:
            messagebox.showerror(APP_TITLE, "Системный промпт пуст.")
            return False
        try:
            summarize.save_system_prompt(prompt)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Не удалось сохранить промпт:\n{e}")
            return False
        set_config_value("summary", "enabled", bool(var_on.get()))
        set_config_value("summary", "url", var_url.get().strip().rstrip("/"))
        set_config_value("summary", "model", var_model.get().strip())
        try:
            temp = max(0.0, min(1.0, float(var_temp.get())))
        except (tk.TclError, ValueError):
            temp = 0.2
        set_config_value("summary", "temperature", temp)
        return True

    return collect


# --------------------------------------------------------------- вкладка 3

def _build_records_tab(frm: ttk.Frame, cfg) -> None:
    cols = ("start", "room", "dur", "status", "tr", "sum")
    tree = ttk.Treeview(frm, columns=cols, show="headings", height=15,
                        selectmode="browse")
    for cid, title, width, anchor in (
            ("start", "Начало", 120, "w"),
            ("room", "Комната", 160, "w"),
            ("dur", "Длит.", 55, "center"),
            ("status", "Статус", 85, "center"),
            ("tr", "Текст", 55, "center"),
            ("sum", "Резюме", 60, "center")):
        tree.heading(cid, text=title)
        tree.column(cid, width=width, anchor=anchor)
    scroll = ttk.Scrollbar(frm, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")
    frm.rowconfigure(0, weight=1)
    frm.columnconfigure(0, weight=1)

    rows_cache: dict[str, dict] = {}

    def refresh() -> None:
        tree.delete(*tree.get_children())
        rows_cache.clear()
        for r in records.scan_records(cfg.records_dir):
            d = Path(r["dir"])
            rows_cache[r["dir"]] = r
            start = (r["started_at"] or "")[:16].replace("T", " ")
            dur = divmod(int(r["duration_sec"] or 0), 60)
            has_tr = (d / "transcript.txt").exists()
            has_sum = (d / "summary.txt").exists()
            tree.insert("", "end", iid=r["dir"],
                        values=(start, r["room"] or "",
                                f"{dur[0]}:{dur[1]:02d}",
                                r["status"] or "",
                                "есть" if has_tr else "",
                                "есть" if has_sum else ""))

    def selected() -> dict | None:
        sel = tree.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Сначала выберите запись в списке")
            return None
        return rows_cache.get(sel[0])

    def open_folder(_event=None) -> None:
        r = selected()
        if r is None:
            return
        if r["dir"] and Path(r["dir"]).exists():
            os.startfile(r["dir"])  # noqa: S606
        else:
            messagebox.showwarning(
                APP_TITLE, "Папка этой записи не найдена\n"
                           "(возможно, удалена).")

    def _queued_info(what: str) -> None:
        messagebox.showinfo(
            APP_TITLE,
            f"{what}\n\n"
            "Если приложение запущено, задача начнётся в течение нескольких "
            "секунд (голубая точка на иконке в трее); иначе — после его "
            "запуска. Результат появится в папке записи.")

    def _check_dir(r) -> Path | None:
        if not r["dir"] or not Path(r["dir"]).exists():
            messagebox.showwarning(APP_TITLE,
                                   "Папка этой записи не найдена.")
            return None
        return Path(r["dir"])

    def send_stt() -> None:
        r = selected()
        if r is None:
            return
        d = _check_dir(r)
        if d is None:
            return
        audio = transcribe.pick_audio(cfg, d)
        if not audio.exists():
            messagebox.showwarning(
                APP_TITLE, "В папке записи нет аудио для распознавания\n"
                           f"({audio.name}).")
            return
        transcribe.enqueue(r["dir"], "stt")
        _queued_info("Транскрибация добавлена в очередь.")

    def send_summary() -> None:
        r = selected()
        if r is None:
            return
        d = _check_dir(r)
        if d is None:
            return
        if (d / "transcript.txt").exists():
            transcribe.enqueue(r["dir"], "summary")
            _queued_info("Резюме по транскрипту добавлено в очередь.")
            return
        audio = transcribe.pick_audio(cfg, d)
        if not audio.exists():
            messagebox.showwarning(
                APP_TITLE, "У записи нет ни транскрипта, ни аудио — "
                           "резюме сделать не из чего.")
            return
        if messagebox.askyesno(
                APP_TITLE,
                "Транскрипта у этой записи ещё нет.\n\n"
                "Сначала распознать речь, а затем автоматически "
                "составить резюме?"):
            transcribe.enqueue(r["dir"], "stt_summary")
            _queued_info("Транскрибация с последующим резюме добавлена "
                         "в очередь.")

    def delete_call() -> None:
        r = selected()
        if r is None:
            return
        if not r.get("owned"):
            messagebox.showwarning(
                APP_TITLE,
                "У записи нет подтверждённого маркера Jitsi Stream Logger. "
                "Удаление заблокировано.")
            return
        status = (r["status"] or "").lower()
        if status not in DELETABLE_STATUSES or not r.get("ended_at"):
            messagebox.showwarning(
                APP_TITLE, "Эта запись ещё идёт, собирается или обрабатывается — "
                           "дождитесь завершения, затем удаляйте.")
            return
        d = r["dir"]
        if not d or not Path(d).exists():
            refresh()
            return
        when = (r["started_at"] or "")[:16].replace("T", " ")
        if not messagebox.askyesno(
                APP_TITLE,
                f"Удалить запись «{r['room'] or ''}» от {when}?\n\n"
                f"Папка будет удалена безвозвратно:\n{d}\n\n"
                "Отменить будет нельзя.",
                icon="warning", default="no"):
            return
        call_dir = Path(d)
        if not records.try_acquire(call_dir):
            messagebox.showwarning(
                APP_TITLE,
                "Запись прямо сейчас обрабатывается. Дождитесь завершения "
                "транскрипции или резюме.")
            refresh()
            return
        try:
            # Статус перечитываем уже под межпроцессной блокировкой: значение
            # из rows_cache могло устареть, пока пользователь читал диалог.
            meta = records.read_meta(call_dir)
            current = (meta.get("call") if meta else None) or {}
            current_status = str(current.get("status") or "").lower()
            if (not records.is_owned_meta(meta)
                    or current_status not in DELETABLE_STATUSES
                    or not current.get("ended_at")):
                messagebox.showwarning(
                    APP_TITLE,
                    "Статус записи изменился или её журнал повреждён. "
                    "Удаление отменено.")
                return
            try:
                shutil.rmtree(call_dir)
            except OSError as e:
                messagebox.showwarning(
                    APP_TITLE,
                    f"Не удалось удалить папку записи:\n{e}\n\n"
                    f"Удалите вручную:\n{d}")
        finally:
            records.release(call_dir)
        refresh()

    tree.bind("<Double-1>", open_folder)

    btns = ttk.Frame(frm)
    btns.grid(row=1, column=0, columnspan=2, sticky="we", pady=(8, 0))
    ttk.Button(btns, text="Открыть папку", command=open_folder).grid(
        row=0, column=0, padx=(0, 6))
    ttk.Button(btns, text="На транскрибацию", command=send_stt).grid(
        row=0, column=1, padx=(0, 6))
    ttk.Button(btns, text="Резюме", command=send_summary).grid(
        row=0, column=2, padx=(0, 6))
    ttk.Button(btns, text="Удалить", command=delete_call).grid(
        row=0, column=3, padx=(0, 6))
    ttk.Button(btns, text="Обновить", command=refresh).grid(row=0, column=4)
    ttk.Label(frm, text=f"Папка записей: {cfg.records_dir}",
              foreground="#777").grid(row=2, column=0, columnspan=2,
                                      sticky="w", pady=(6, 0))
    refresh()


# ------------------------------------------------------------------- окно

def run_settings() -> None:
    cfg = load_config()
    root = tk.Tk()
    root.title(f"{APP_TITLE} — настройки")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    nb = ttk.Notebook(root)
    tab_settings = ttk.Frame(nb, padding=12)
    tab_quality = ttk.Frame(nb, padding=12)
    tab_summary = ttk.Frame(nb, padding=12)
    tab_records = ttk.Frame(nb, padding=12)
    nb.add(tab_settings, text="Настройки")
    nb.add(tab_quality, text="Качество видео")
    nb.add(tab_summary, text="Резюме")
    nb.add(tab_records, text="Список записей")
    nb.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

    collect_settings = _build_settings_tab(tab_settings, cfg)
    collect_quality = _build_quality_tab(tab_quality, cfg)
    collect_summary = _build_summary_tab(tab_summary, cfg)
    _build_records_tab(tab_records, cfg)

    btns = ttk.Frame(root, padding=(8, 0, 8, 8))
    btns.grid(row=1, column=0, sticky="e")

    def save() -> None:
        if collect_settings() and collect_quality() and collect_summary():
            root.destroy()

    ttk.Button(btns, text="Сохранить", command=save).grid(
        row=0, column=0, padx=(0, 6))
    ttk.Button(btns, text="Отмена", command=root.destroy).grid(row=0, column=1)

    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}"
                  f"+{(root.winfo_screenheight() - h) // 2}")
    root.mainloop()
