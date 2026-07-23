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
import sqlite3
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from app import autostart, summarize, transcribe
from app.config import APP_TITLE, appdata_dir, load_config, set_config_value

DELETABLE_STATUSES = {"done", "log_only", "error"}


def _monitor_count() -> int:
    try:
        import win32api

        return max(1, int(win32api.GetSystemMetrics(80)))  # SM_CMONITORS
    except Exception:
        return 1


def _load_calls() -> list[sqlite3.Row]:
    db = appdata_dir() / "calls.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(db, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT id, room, started_at, duration_sec, status, dir, "
                "transcript_path, summary_path FROM calls "
                "ORDER BY id DESC LIMIT 300"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _delete_call_db(call_id: int) -> bool:
    """Удаляет запись созвона и связанные строки из журнала.

    Отдельный процесс окна настроек пишет в ту же calls.db, что и основное
    приложение (WAL допускает конкурентную запись); короткий таймаут — на случай,
    если основное приложение как раз держит короткий write-lock.
    """
    db = appdata_dir() / "calls.db"
    conn = sqlite3.connect(db, timeout=5)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM calls WHERE id=?", (call_id,)).fetchone()
        if row is None or str(row[0]).lower() not in DELETABLE_STATUSES:
            conn.rollback()
            return False
        conn.execute("DELETE FROM events WHERE call_id=?", (call_id,))
        conn.execute("DELETE FROM participants WHERE call_id=?", (call_id,))
        conn.execute("DELETE FROM calls WHERE id=?", (call_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    var_av1 = tk.BooleanVar(value=(cfg.video_encoder == "libsvtav1"))
    ttk.Checkbutton(
        lf_v, text="Сжимать в AV1 (меньше размер, выше нагрузка на CPU)",
        variable=var_av1).grid(row=4, column=0, columnspan=3, sticky="w",
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
        # Кодек трогаем только если галку AV1 переключили — чтобы не затирать
        # вручную выставленный аппаратный кодер.
        want_av1 = bool(var_av1.get())
        if want_av1 != (cfg.video_encoder == "libsvtav1"):
            set_config_value("video", "encoder",
                             "libsvtav1" if want_av1 else "libx264")
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

    rows_cache: dict[int, sqlite3.Row] = {}

    def refresh() -> None:
        tree.delete(*tree.get_children())
        rows_cache.clear()
        for r in _load_calls():
            rows_cache[int(r["id"])] = r
            start = (r["started_at"] or "")[:16].replace("T", " ")
            dur = divmod(int(r["duration_sec"] or 0), 60)
            has_tr = bool(r["transcript_path"]
                          and Path(r["transcript_path"]).exists())
            has_sum = bool(r["summary_path"]
                           and Path(r["summary_path"]).exists())
            tree.insert("", "end", iid=str(r["id"]),
                        values=(start, r["room"] or "",
                                f"{dur[0]}:{dur[1]:02d}",
                                r["status"] or "",
                                "есть" if has_tr else "",
                                "есть" if has_sum else ""))

    def selected() -> sqlite3.Row | None:
        sel = tree.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Сначала выберите запись в списке")
            return None
        return rows_cache.get(int(sel[0]))

    def open_folder(_event=None) -> None:
        r = selected()
        if r is None:
            return
        if r["dir"] and Path(r["dir"]).exists():
            os.startfile(r["dir"])  # noqa: S606
        else:
            messagebox.showwarning(
                APP_TITLE, "У этой записи нет папки с файлами\n"
                           "(журнал без записи или файлы удалены).")

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
                                   "У этой записи нет папки с файлами.")
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
        transcribe.enqueue(int(r["id"]), "stt")
        _queued_info("Транскрибация добавлена в очередь.")

    def send_summary() -> None:
        r = selected()
        if r is None:
            return
        d = _check_dir(r)
        if d is None:
            return
        if (d / "transcript.txt").exists():
            transcribe.enqueue(int(r["id"]), "summary")
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
            transcribe.enqueue(int(r["id"]), "stt_summary")
            _queued_info("Транскрибация с последующим резюме добавлена "
                         "в очередь.")

    def delete_call() -> None:
        r = selected()
        if r is None:
            return
        status = (r["status"] or "").lower()
        if status not in DELETABLE_STATUSES:
            messagebox.showwarning(
                APP_TITLE, "Эта запись ещё идёт, собирается или обрабатывается — "
                           "дождитесь завершения, затем удаляйте.")
            return
        d = r["dir"]
        has_folder = bool(d) and Path(d).exists()
        when = (r["started_at"] or "")[:16].replace("T", " ")
        detail = (f"Папка будет удалена безвозвратно:\n{d}" if has_folder
                  else "Папки с файлами нет — удалится только запись в журнале.")
        if not messagebox.askyesno(
                APP_TITLE,
                f"Удалить запись «{r['room'] or ''}» от {when}?\n\n{detail}\n\n"
                "Отменить будет нельзя.",
                icon="warning", default="no"):
            return
        # Сначала журнал: если строку убрать не удалось, папку не трогаем —
        # иначе запись «зависает» в списке и её больше нельзя удалить.
        try:
            deleted = _delete_call_db(int(r["id"]))
        except sqlite3.Error as e:
            messagebox.showerror(APP_TITLE,
                                 f"Не удалось удалить запись из журнала:\n{e}")
            return
        if not deleted:
            messagebox.showwarning(
                APP_TITLE,
                "Статус записи изменился: она сейчас обрабатывается или уже "
                "удалена. Обновите список.")
            refresh()
            return
        if has_folder:
            import shutil

            try:
                shutil.rmtree(d)
            except OSError as e:
                messagebox.showwarning(
                    APP_TITLE,
                    f"Запись убрана из журнала, но папку удалить не удалось:\n{e}\n\n"
                    f"Удалите вручную:\n{d}")
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
    ttk.Label(frm, text=f"Журнал: {appdata_dir() / 'calls.db'}",
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
    tab_summary = ttk.Frame(nb, padding=12)
    tab_records = ttk.Frame(nb, padding=12)
    nb.add(tab_settings, text="Настройки")
    nb.add(tab_summary, text="Резюме")
    nb.add(tab_records, text="Список записей")
    nb.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

    collect_settings = _build_settings_tab(tab_settings, cfg)
    collect_summary = _build_summary_tab(tab_summary, cfg)
    _build_records_tab(tab_records, cfg)

    btns = ttk.Frame(root, padding=(8, 0, 8, 8))
    btns.grid(row=1, column=0, sticky="e")

    def save() -> None:
        if collect_settings() and collect_summary():
            root.destroy()

    ttk.Button(btns, text="Сохранить", command=save).grid(
        row=0, column=0, padx=(0, 6))
    ttk.Button(btns, text="Отмена", command=root.destroy).grid(row=0, column=1)

    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}"
                  f"+{(root.winfo_screenheight() - h) // 2}")
    root.mainloop()
