import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
import threading
import os
import re
import time
import json

try:
    from deep_translator import GoogleTranslator
    TRANSLATOR_OK = True
except ImportError:
    TRANSLATOR_OK = False

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(
    r'(\$[^$]+\$|\[[\w\.\[\]]+\]|§[A-Za-z!]|\\n|\\t|£\w+|@\w+[\[!]?|\])'
)
VALUE_RE     = re.compile(r'^(\s*\S+:\d*\s*)"(.+)"(.*)$')
CYRILLIC_RE  = re.compile(r'[а-яёА-ЯЁ]')
SETTINGS_FILE = os.path.join(os.path.expanduser('~'), '.hoi4_translator_settings.json')

# ---------------------------------------------------------------------------
# Логика перевода
# ---------------------------------------------------------------------------

def has_cyrillic(text):
    return bool(CYRILLIC_RE.search(text))

def protect_placeholders(text):
    tokens = []
    def replacer(m):
        tokens.append(m.group(0))
        return f'<<{len(tokens)-1}>>'
    return PLACEHOLDER_RE.sub(replacer, text), tokens

def restore_placeholders(text, tokens):
    def replacer(m):
        idx = int(m.group(1))
        return tokens[idx] if idx < len(tokens) else m.group(0)
    return re.sub(r'<<(\d+)>>', replacer, text)

def translate_batch(texts, translator, retries=3):
    if not texts:
        return []

    protected_list, tokens_list = [], []
    for t in texts:
        p, tok = protect_placeholders(t)
        protected_list.append(p)
        tokens_list.append(tok)

    SEP = ' ||| '
    joined = SEP.join(protected_list)

    # Попытки с паузой при ошибке
    for attempt in range(retries):
        try:
            translated = translator.translate(joined)
            if not translated:
                break
            parts = translated.split(SEP)
            if len(parts) == len(texts):
                return [restore_placeholders(p, tok)
                        for p, tok in zip(parts, tokens_list)]
            # Количество частей не совпало — фоллбек поштучно
            break
        except Exception:
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
            else:
                pass

    # Фоллбек: переводим по одной строке
    result = []
    for p, tok in zip(protected_list, tokens_list):
        for attempt in range(retries):
            try:
                t = translator.translate(p)
                result.append(restore_placeholders(t if t else p, tok))
                break
            except Exception:
                if attempt < retries - 1:
                    time.sleep(1.0)
                else:
                    result.append(restore_placeholders(p, tok))
    return result

def load_existing_translations(dst_path):
    existing = {}
    if not os.path.exists(dst_path):
        return existing
    try:
        with open(dst_path, 'r', encoding='utf-8-sig') as f:
            for line in f:
                m = VALUE_RE.match(line)
                if m and has_cyrillic(m.group(2)):
                    existing[m.group(1).strip()] = m.group(2)
    except Exception:
        pass
    return existing

def process_file(src_path, dst_path, translator, log_cb, skip_translated, batch_size, stop_event):
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)

    existing = {}
    if skip_translated:
        existing = load_existing_translations(dst_path)
        if existing:
            log_cb(f"  Найдено переведённых строк: {len(existing)}", 'info')

    with open(src_path, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()

    out_lines        = list(lines)
    to_translate_idx = []
    to_translate_val = []
    meta             = []
    skipped          = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if i == 0 and stripped.startswith('l_'):
            out_lines[i] = re.sub(r'^(\s*)l_english:', r'\1l_russian:', line)
            continue
        m = VALUE_RE.match(line)
        if not m:
            continue
        key    = m.group(1).strip()
        prefix = m.group(1)
        value  = m.group(2)
        suffix = m.group(3)

        if skip_translated and key in existing:
            out_lines[i] = f'{prefix}"{existing[key]}"{suffix}\n'
            skipped += 1
        else:
            to_translate_idx.append(i)
            to_translate_val.append(value)
            meta.append((prefix, suffix))

    # Батч-перевод
    total            = len(to_translate_val)
    translated_vals  = []
    for start in range(0, total, batch_size):
        if stop_event.is_set():
            return None  # сигнал отмены
        batch  = to_translate_val[start:start + batch_size]
        result = translate_batch(batch, translator)
        translated_vals.extend(result)

    for line_idx, translated, (prefix, suffix) in zip(to_translate_idx, translated_vals, meta):
        out_lines[line_idx] = f'{prefix}"{translated}"{suffix}\n'

    with open(dst_path, 'w', encoding='utf-8-sig') as f:
        f.writelines(out_lines)

    if skipped:
        log_cb(f"  Пропущено (уже переведено): {skipped}", 'info')
    return total

def rename_dst(src_path, src_root, dst_root):
    rel         = os.path.relpath(src_path, src_root)
    rel_renamed = rel.replace('_l_english.yml', '_l_russian.yml')
    # Заменяем папку english на russian только если она есть в пути
    rel_renamed = re.sub(
        r'(?i)(^|[\\/])english([\\/])',
        lambda m: m.group(1) + 'russian' + m.group(2),
        rel_renamed
    )
    return os.path.join(dst_root, rel_renamed)

def collect_yml_files(root):
    result = []
    for dirpath, _, filenames in os.walk(root):
        for fn in sorted(filenames):
            if fn.endswith('.yml') and fn.lower() != 'languages.yml':
                result.append(os.path.join(dirpath, fn))
    return result

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def load_settings():
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_settings(data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HOI4 Localisation Translator")
        self.resizable(True, True)
        self.minsize(640, 560)
        self.configure(bg='#1a1a1a')

        self._stop_event = threading.Event()

        self._setup_styles()
        self._build_ui()
        self._load_settings()

        if not TRANSLATOR_OK:
            self.log_line(
                "ОШИБКА: deep-translator не установлен. Запусти: pip install deep-translator",
                'error'
            )

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        BG   = '#1a1a1a'
        BG2  = '#2d2d2d'
        BG3  = '#3d3d3d'
        FG   = '#e0e0e0'

        style.configure('TFrame',       background=BG)
        style.configure('TLabel',       background=BG,  foreground=FG, font=('Segoe UI', 10))
        style.configure('TButton',      background=BG2, foreground=FG, font=('Segoe UI', 10), borderwidth=1)
        style.map('TButton',            background=[('active', BG3), ('disabled', BG2)],
                                        foreground=[('disabled', '#666')])
        style.configure('TEntry',       fieldbackground=BG2, foreground=FG, insertcolor=FG)
        style.configure('TCheckbutton', background=BG, foreground=FG, font=('Segoe UI', 10))
        style.map('TCheckbutton',
                  background=[('active', BG), ('pressed', BG)],
                  foreground=[('active', FG), ('pressed', FG)])
        style.configure('green.Horizontal.TProgressbar', troughcolor=BG2, background='#4caf50')
        style.configure('TScale',       background=BG, troughcolor=BG2)

    # ------------------------------------------------------------------
    def _build_ui(self):
        pad = {'padx': 12, 'pady': 4}

        ttk.Label(self, text="HOI4 Localisation Translator",
                  font=('Segoe UI', 14, 'bold')).pack(pady=(16, 2))
        ttk.Label(self, text="Переводит локализацию модов с английского на русский через Google Translate",
                  foreground='#888').pack(pady=(0, 12))

        # Папка english
        sf = ttk.Frame(self); sf.pack(fill='x', **pad)
        ttk.Label(sf, text="Папка english (оригинал):").pack(anchor='w')
        r1 = ttk.Frame(sf); r1.pack(fill='x', pady=2)
        self.src_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.src_var).pack(side='left', fill='x', expand=True)
        ttk.Button(r1, text="Обзор", command=self.browse_src, width=8).pack(side='left', padx=(6, 0))

        # Папка russian
        df = ttk.Frame(self); df.pack(fill='x', **pad)
        ttk.Label(df, text="Папка russian (твой мод):").pack(anchor='w')
        r2 = ttk.Frame(df); r2.pack(fill='x', pady=2)
        self.dst_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.dst_var).pack(side='left', fill='x', expand=True)
        ttk.Button(r2, text="Обзор", command=self.browse_dst, width=8).pack(side='left', padx=(6, 0))

        # Слайдер батча
        bf = ttk.Frame(self); bf.pack(fill='x', **pad)
        ttk.Label(bf, text="Размер батча:").pack(side='left')
        self.batch_var   = tk.IntVar(value=20)
        self.batch_label = ttk.Label(bf, text="20", width=3)
        self.batch_label.pack(side='right')
        ttk.Scale(bf, from_=5, to=80, variable=self.batch_var, orient='horizontal',
                  command=lambda v: self.batch_label.config(text=str(int(float(v))))).pack(
                  side='right', padx=(8, 8), fill='x', expand=True)

        # Галочки
        of = ttk.Frame(self); of.pack(fill='x', **pad)
        self.skip_var   = tk.BooleanVar(value=False)
        self.open_var   = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="Пропускать уже переведённые строки (с кириллицей)",
                        variable=self.skip_var).pack(anchor='w')
        ttk.Checkbutton(of, text="Открыть папку russian после завершения",
                        variable=self.open_var).pack(anchor='w')

        # Прогресс
        self.progress = ttk.Progressbar(self, style='green.Horizontal.TProgressbar', mode='determinate')
        self.progress.pack(fill='x', padx=12, pady=(10, 2))

        # Статус строка
        sf2 = ttk.Frame(self); sf2.pack(fill='x', padx=12)
        self.status_var  = tk.StringVar(value="Готов к работе")
        self.elapsed_var = tk.StringVar(value="")
        ttk.Label(sf2, textvariable=self.status_var,  foreground='#aaa').pack(side='left')
        ttk.Label(sf2, textvariable=self.elapsed_var, foreground='#666').pack(side='right')

        # Кнопки
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill='x', padx=12, pady=8)
        self.start_btn = ttk.Button(btn_frame, text="Начать перевод", command=self.start)
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 4), ipady=6)
        self.stop_btn  = ttk.Button(btn_frame, text="Стоп", command=self.stop, state='disabled')
        self.stop_btn.pack(side='left', fill='x', expand=True, padx=4, ipady=6)
        self.clear_btn = ttk.Button(btn_frame, text="Очистить логи", command=self.clear_log)
        self.clear_btn.pack(side='left', fill='x', expand=True, padx=(4, 0), ipady=6)

        # Лог
        ttk.Label(self, text="Лог:").pack(anchor='w', padx=12)
        self.log_widget = scrolledtext.ScrolledText(
            self, height=12, bg='#111', fg='#bfbfbf',
            font=('Consolas', 9), state='disabled',
            insertbackground='white', relief='flat'
        )
        self.log_widget.pack(fill='both', expand=True, padx=12, pady=(2, 12))
        self.log_widget.tag_config('error',   foreground='#ff6b6b')
        self.log_widget.tag_config('success', foreground='#69db7c')
        self.log_widget.tag_config('info',    foreground='#74c0fc')
        self.log_widget.tag_config('warn',    foreground='#ffd43b')

    # ------------------------------------------------------------------
    def browse_src(self):
        d = filedialog.askdirectory(title="Выбери папку english оригинального мода")
        if d:
            self.src_var.set(d)

    def browse_dst(self):
        d = filedialog.askdirectory(title="Выбери папку russian твоего мода")
        if d:
            self.dst_var.set(d)

    def log_line(self, text, tag=None):
        self.log_widget.config(state='normal')
        if tag:
            self.log_widget.insert('end', text + '\n', tag)
        else:
            self.log_widget.insert('end', text + '\n')
        self.log_widget.see('end')
        self.log_widget.config(state='disabled')

    def clear_log(self):
        self.log_widget.config(state='normal')
        self.log_widget.delete('1.0', 'end')
        self.log_widget.config(state='disabled')

    def stop(self):
        self._stop_event.set()
        self.status_var.set("Останавливается...")
        self.stop_btn.config(state='disabled')

    # ------------------------------------------------------------------
    def _load_settings(self):
        s = load_settings()
        if s.get('src'):  self.src_var.set(s['src'])
        if s.get('dst'):  self.dst_var.set(s['dst'])
        if s.get('batch'): self.batch_var.set(s['batch']); self.batch_label.config(text=str(s['batch']))
        if s.get('skip') is not None:  self.skip_var.set(s['skip'])
        if s.get('open') is not None:  self.open_var.set(s['open'])

    def _save_settings(self):
        save_settings({
            'src':   self.src_var.get(),
            'dst':   self.dst_var.get(),
            'batch': int(self.batch_var.get()),
            'skip':  self.skip_var.get(),
            'open':  self.open_var.get(),
        })

    def _on_close(self):
        self._save_settings()
        self.destroy()

    # ------------------------------------------------------------------
    def start(self):
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        if not src or not dst:
            self.log_line("Укажи обе папки!", 'error')
            return
        if not os.path.isdir(src):
            self.log_line(f"Папка не найдена: {src}", 'error')
            return
        if not TRANSLATOR_OK:
            self.log_line("Установи deep-translator: pip install deep-translator", 'error')
            return

        self._stop_event.clear()
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self._save_settings()
        threading.Thread(target=self.run_translation, args=(src, dst), daemon=True).start()

    # ------------------------------------------------------------------
    def run_translation(self, src, dst):
        skip       = self.skip_var.get()
        open_after = self.open_var.get()
        batch_size = int(self.batch_var.get())
        translator = GoogleTranslator(source='en', target='ru')

        files = collect_yml_files(src)
        total = len(files)
        self.log_line(f"Найдено файлов: {total} | батч: {batch_size}", 'info')
        self.progress['maximum'] = max(total, 1)
        self.progress['value']   = 0

        start_time  = time.time()
        total_lines = 0
        errors      = 0

        for i, fp in enumerate(files):
            if self._stop_event.is_set():
                self.log_line("[СТОП] Перевод остановлен пользователем.", 'warn')
                break

            dst_path = rename_dst(fp, src, dst)
            rel      = os.path.relpath(fp, src)
            self.status_var.set(f"[{i+1}/{total}] {os.path.basename(fp)}")
            self.log_line(f"[{i+1}/{total}] {rel}")

            try:
                n = process_file(fp, dst_path, translator, self.log_line,
                                 skip, batch_size, self._stop_event)
                if n is None:
                    self.log_line("[СТОП] Перевод остановлен пользователем.", 'warn')
                    break
                total_lines += n
                self.log_line(f"  ✓ переведено строк: {n}", 'success')
            except Exception as e:
                self.log_line(f"  ✗ ОШИБКА: {e}", 'error')
                errors += 1

            self.progress['value'] = i + 1

            # Обновляем таймер
            elapsed = int(time.time() - start_time)
            m_, s_  = divmod(elapsed, 60)
            self.elapsed_var.set(f"{m_:02d}:{s_:02d}")
            self.update_idletasks()

        elapsed = int(time.time() - start_time)
        m_, s_  = divmod(elapsed, 60)
        summary = f"Готово! Файлов: {total} | строк: {total_lines} | время: {m_:02d}:{s_:02d}"
        if errors:
            summary += f" | ошибок: {errors}"
        self.status_var.set(summary)
        self.log_line(f"\n{'='*50}", 'info')
        self.log_line(f"  {summary}", 'success' if not errors else 'warn')
        self.log_line(f"{'='*50}", 'info')

        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')

        if open_after and not self._stop_event.is_set():
            try:
                os.startfile(dst)
            except Exception:
                pass


if __name__ == '__main__':
    app = App()
    app.mainloop()
