import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
import threading
import os
import re
import time
import json

try:
    from deep_translator import GoogleTranslator, MyMemoryTranslator, DeepL
    TRANSLATOR_OK = True
except ImportError:
    TRANSLATOR_OK = False

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(
    r'(\$[^$]+\$|\[[\w\.\[\]]+\]|§[A-Za-z!]|\\n|\\t|£\w+|@\w+[\[!]?|\])'
)
VALUE_RE      = re.compile(r'^(\s*\S+:\d*\s*)"(.+)"(.*)$')
CYRILLIC_RE   = re.compile(r'[а-яёА-ЯЁ]')
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
        return f'<<PH{len(tokens)-1}>>'
    return PLACEHOLDER_RE.sub(replacer, text), tokens

def restore_placeholders(text, tokens):
    def replacer(m):
        try:
            idx = int(m.group(1))
            return tokens[idx] if idx < len(tokens) else m.group(0)
        except Exception:
            return m.group(0)
    return re.sub(r'<<PH(\d+)>>', replacer, text)

def translate_batch_logic(texts, translator, retries=3):
    if not texts:
        return []

    protected_list, tokens_list = [], []
    for t in texts:
        p, tok = protect_placeholders(t)
        protected_list.append(p)
        tokens_list.append(tok)

    # Для Google используем батч через сепаратор
    if isinstance(translator, GoogleTranslator):
        SEP = ' ||| '
        joined = SEP.join(protected_list)
        for attempt in range(retries):
            try:
                translated = translator.translate(joined)
                if not translated:
                    break
                parts = translated.split(SEP)
                if len(parts) == len(texts):
                    return [restore_placeholders(p, tok)
                            for p, tok in zip(parts, tokens_list)]
                break
            except Exception:
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))

    # Для DeepL, MyMemory или фоллбека — поштучно
    result = []
    for p, tok in zip(protected_list, tokens_list):
        translated_text = p
        for attempt in range(retries):
            try:
                t = translator.translate(p)
                translated_text = t if t else p
                break
            except Exception:
                if attempt < retries - 1:
                    time.sleep(1.0)
        result.append(restore_placeholders(translated_text, tok))
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
    existing = load_existing_translations(dst_path) if skip_translated else {}
    if existing:
        log_cb(f"  Найдено переведённых строк: {len(existing)}", 'info')

    with open(src_path, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()

    out_lines, to_translate_idx, to_translate_val, meta = list(lines), [], [], []
    skipped = 0

    for i, line in enumerate(lines):
        if i == 0 and line.strip().startswith('l_'):
            out_lines[i] = re.sub(r'^(\s*)l_[a-z]+:', r'\1l_russian:', line)
            continue
        m = VALUE_RE.match(line)
        if not m:
            continue
        key, prefix, value, suffix = m.group(1).strip(), m.group(1), m.group(2), m.group(3)
        if skip_translated and key in existing:
            out_lines[i] = f'{prefix}"{existing[key]}"{suffix}\n'
            skipped += 1
        else:
            to_translate_idx.append(i)
            to_translate_val.append(value)
            meta.append((prefix, suffix))

    total = len(to_translate_val)
    translated_vals = []
    for start in range(0, total, batch_size):
        if stop_event.is_set():
            return None
        batch = to_translate_val[start:start + batch_size]
        translated_vals.extend(translate_batch_logic(batch, translator))

    for line_idx, translated, (pfx, sfx) in zip(to_translate_idx, translated_vals, meta):
        out_lines[line_idx] = f'{pfx}"{translated}"{sfx}\n'

    with open(dst_path, 'w', encoding='utf-8-sig') as f:
        f.writelines(out_lines)

    if skipped:
        log_cb(f"  Пропущено (уже переведено): {skipped}", 'info')
    return total

def rename_dst(src_path, src_root, dst_root):
    rel = os.path.relpath(src_path, src_root)
    rel_renamed = re.sub(r'_l_[a-z]+\.yml$', '_l_russian.yml', rel)
    rel_renamed = re.sub(
        r'(?i)(^|[\\/])(english|german|french|spanish|polish)([\\/])',
        r'\1russian\3', rel_renamed
    )
    return os.path.join(dst_root, rel_renamed)

def collect_yml_files(root):
    res = []
    for d, _, fns in os.walk(root):
        for f in sorted(fns):
            if f.endswith('.yml') and f.lower() != 'languages.yml':
                res.append(os.path.join(d, f))
    return res

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
        self.title("HOI4 Localisation Translator v1.1.0")
        self.minsize(700, 650)
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

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        BG, BG2, BG3, FG = '#1a1a1a', '#2d2d2d', '#3d3d3d', '#ffffff'

        style.configure('TFrame',    background=BG)
        style.configure('TLabel',    background=BG, foreground=FG, font=('Segoe UI', 10))
        style.configure('TLabelframe',       background=BG, foreground=FG)
        style.configure('TLabelframe.Label', background=BG, foreground='#4caf50',
                        font=('Segoe UI', 10, 'bold'))
        style.configure('TButton',   background=BG2, foreground=FG, font=('Segoe UI', 10), borderwidth=1)
        style.map('TButton',
                  background=[('active', BG3), ('disabled', BG2)],
                  foreground=[('disabled', '#666')])
        style.configure('TEntry',    fieldbackground=BG2, foreground=FG, insertcolor=FG)
        style.configure('TCombobox', fieldbackground=BG2, background=BG2, foreground=FG, arrowcolor=FG)
        style.map('TCombobox',
                  fieldbackground=[('readonly', BG2), ('active', BG3)],
                  foreground=[('readonly', FG), ('active', FG)],
                  selectbackground=[('readonly', BG2)],
                  selectforeground=[('readonly', FG)])
        style.configure('green.Horizontal.TProgressbar', troughcolor=BG2, background='#4caf50')
        style.configure('TCheckbutton', background=BG, foreground=FG)
        style.map('TCheckbutton',
                  background=[('active', BG), ('pressed', BG)],
                  foreground=[('active', FG), ('pressed', FG)])
        style.configure('TScale', background=BG, troughcolor=BG2)

    def _build_ui(self):
        pad = {'padx': 15, 'pady': 5}
        ttk.Label(self, text="HOI4 Localisation Translator",
                  font=('Segoe UI', 16, 'bold'), foreground='#4caf50').pack(pady=10)

        # Папки
        f_folders = ttk.LabelFrame(self, text=" Папки ", padding=10)
        f_folders.pack(fill='x', **pad)

        self.src_var = tk.StringVar()
        ttk.Label(f_folders, text="Оригинал (english):").grid(row=0, column=0, sticky='w')
        ttk.Entry(f_folders, textvariable=self.src_var).grid(row=0, column=1, sticky='ew', padx=5)
        ttk.Button(f_folders, text="Обзор", command=self.browse_src, width=8).grid(row=0, column=2)

        self.dst_var = tk.StringVar()
        ttk.Label(f_folders, text="Перевод (russian):").grid(row=1, column=0, sticky='w', pady=5)
        ttk.Entry(f_folders, textvariable=self.dst_var).grid(row=1, column=1, sticky='ew', padx=5)
        ttk.Button(f_folders, text="Обзор", command=self.browse_dst, width=8).grid(row=1, column=2)
        f_folders.columnconfigure(1, weight=1)

        # Движок
        f_engine = ttk.LabelFrame(self, text=" Движок и API ", padding=10)
        f_engine.pack(fill='x', **pad)

        row1 = ttk.Frame(f_engine); row1.pack(fill='x')
        ttk.Label(row1, text="Движок:").pack(side='left')
        self.engine_var = tk.StringVar(value="Google")
        self.engine_cb = ttk.Combobox(row1, textvariable=self.engine_var,
                                      values=["Google", "DeepL", "MyMemory"],
                                      state="readonly", width=12)
        self.engine_cb.pack(side='left', padx=5)
        self.engine_cb.bind("<<ComboboxSelected>>", self._toggle_api_field)

        ttk.Label(row1, text="С языка:").pack(side='left', padx=(15, 0))
        self.lang_var = tk.StringVar(value="en")
        ttk.Combobox(row1, textvariable=self.lang_var,
                     values=["en", "de", "fr", "es", "pl"],
                     state="readonly", width=5).pack(side='left', padx=5)

        row2 = ttk.Frame(f_engine); row2.pack(fill='x', pady=(10, 0))
        self.api_lbl = ttk.Label(row2, text="DeepL API Key:", foreground='#555')
        self.api_lbl.pack(side='left')
        self.api_key_var = tk.StringVar()
        self.api_entry = ttk.Entry(row2, textvariable=self.api_key_var, show="*",
                                   width=40, state='disabled')
        self.api_entry.pack(side='left', padx=5)

        # Параметры
        f_params = ttk.Frame(self); f_params.pack(fill='x', **pad)
        ttk.Label(f_params, text="Батч:").pack(side='left')
        self.batch_var = tk.IntVar(value=20)
        self.blbl = ttk.Label(f_params, text="20", width=3)
        self.blbl.pack(side='right')
        ttk.Scale(f_params, from_=1, to=80, variable=self.batch_var,
                  command=lambda v: self.blbl.config(text=str(int(float(v))))).pack(
                  side='right', fill='x', expand=True, padx=10)

        # Опции
        of = ttk.Frame(self); of.pack(fill='x', **pad)
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="Пропускать переведенное", variable=self.skip_var).pack(side='left')
        self.open_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="Открыть папку по итогу", variable=self.open_var).pack(side='left', padx=20)

        # Прогресс
        self.progress = ttk.Progressbar(self, style='green.Horizontal.TProgressbar', mode='determinate')
        self.progress.pack(fill='x', padx=15, pady=(10, 2))

        f_status = ttk.Frame(self); f_status.pack(fill='x', padx=15)
        self.status_var = tk.StringVar(value="Готов")
        self.etr_var = tk.StringVar()
        ttk.Label(f_status, textvariable=self.status_var, foreground='#aaa').pack(side='left')
        ttk.Label(f_status, textvariable=self.etr_var, foreground='#666').pack(side='right')

        # Лог
        self.log_widget = scrolledtext.ScrolledText(
            self, height=10, bg='#111', fg='#ccc',
            font=('Consolas', 9), state='disabled'
        )
        self.log_widget.pack(fill='both', expand=True, padx=15, pady=(5, 5))
        self.log_widget.tag_config('error',   foreground='#ff6b6b')
        self.log_widget.tag_config('success', foreground='#69db7c')
        self.log_widget.tag_config('info',    foreground='#74c0fc')

        # Кнопки
        fb = ttk.Frame(self); fb.pack(fill='x', padx=15, pady=(0, 10))
        self.start_btn = ttk.Button(fb, text="НАЧАТЬ", command=self.start)
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 4), ipady=5)
        self.stop_btn = ttk.Button(fb, text="СТОП", state='disabled', command=self.stop)
        self.stop_btn.pack(side='left', fill='x', expand=True, padx=4, ipady=5)
        ttk.Button(fb, text="Очистить лог", command=self.clear_log).pack(
            side='left', fill='x', expand=True, padx=(4, 0), ipady=5)

    def _toggle_api_field(self, event=None):
        if self.engine_var.get() == "DeepL":
            self.api_entry.config(state='normal')
            self.api_lbl.config(foreground='#fff')
        else:
            self.api_entry.config(state='disabled')
            self.api_lbl.config(foreground='#555')

    def browse_src(self):
        d = filedialog.askdirectory(title="Выбери папку с оригинальной локализацией")
        if d:
            self.src_var.set(d)

    def browse_dst(self):
        d = filedialog.askdirectory(title="Выбери папку для перевода")
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
        self.status_var.set("Остановка...")
        self.stop_btn.config(state='disabled')

    def _load_settings(self):
        s = load_settings()
        if s.get('src'):    self.src_var.set(s['src'])
        if s.get('dst'):    self.dst_var.set(s['dst'])
        if s.get('api_key'): self.api_key_var.set(s['api_key'])
        if s.get('engine'): self.engine_var.set(s['engine'])
        if s.get('lang'):   self.lang_var.set(s['lang'])
        if s.get('batch'):
            self.batch_var.set(s['batch'])
            self.blbl.config(text=str(s['batch']))
        if s.get('skip') is not None:  self.skip_var.set(s['skip'])
        if s.get('open') is not None:  self.open_var.set(s['open'])
        self._toggle_api_field()

    def _on_close(self):
        save_settings({
            'src':     self.src_var.get(),
            'dst':     self.dst_var.get(),
            'api_key': self.api_key_var.get(),
            'engine':  self.engine_var.get(),
            'lang':    self.lang_var.get(),
            'batch':   int(self.batch_var.get()),
            'skip':    self.skip_var.get(),
            'open':    self.open_var.get(),
        })
        self.destroy()

    def start(self):
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        if not src or not dst:
            self.log_line("Укажи обе папки!", 'error')
            return
        if not os.path.isdir(src):
            self.log_line(f"Папка не найдена: {src}", 'error')
            return
        if self.engine_var.get() == "DeepL" and not self.api_key_var.get():
            self.log_line("Ошибка: нужен DeepL API Key!", 'error')
            return
        if not TRANSLATOR_OK:
            self.log_line("Установи: pip install deep-translator", 'error')
            return
        self._stop_event.clear()
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        save_settings({
            'src':     src, 'dst': dst,
            'api_key': self.api_key_var.get(),
            'engine':  self.engine_var.get(),
            'lang':    self.lang_var.get(),
            'batch':   int(self.batch_var.get()),
            'skip':    self.skip_var.get(),
            'open':    self.open_var.get(),
        })
        threading.Thread(target=self.run_translation, daemon=True).start()

    def run_translation(self):
        engine_name = self.engine_var.get()
        source_ln   = self.lang_var.get()
        key         = self.api_key_var.get()
        batch_size  = int(self.batch_var.get())
        src         = self.src_var.get()
        dst         = self.dst_var.get()

        try:
            if engine_name == "DeepL":
                translator = DeepL(api_key=key, source=source_ln, target="ru", use_pro=False)
            elif engine_name == "MyMemory":
                mm_map = {"en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "pl": "pl-PL"}
                translator = MyMemoryTranslator(source=mm_map.get(source_ln, "en-US"), target="ru-RU")
            else:
                translator = GoogleTranslator(source=source_ln, target="ru")

            files   = collect_yml_files(src)
            total   = len(files)
            self.progress['maximum'] = total
            self.progress['value']   = 0
            start_t = time.time()
            total_lines = 0
            errors  = 0

            self.log_line(f"Найдено файлов: {total} | движок: {engine_name} | батч: {batch_size}", 'info')

            for i, fp in enumerate(files):
                if self._stop_event.is_set():
                    self.log_line("[СТОП] Перевод остановлен.", 'error')
                    break

                rel = os.path.relpath(fp, src)
                self.status_var.set(f"Файл {i+1}/{total}")
                self.log_line(f"[{i+1}/{total}] {rel}")

                dst_path = rename_dst(fp, src, dst)
                try:
                    n = process_file(fp, dst_path, translator, self.log_line,
                                     self.skip_var.get(), batch_size, self._stop_event)
                    if n is None:
                        self.log_line("[СТОП] Перевод остановлен.", 'error')
                        break
                    total_lines += n
                    self.log_line(f"  OK — строк: {n}", 'success')
                except Exception as e:
                    self.log_line(f"  ОШИБКА: {e}", 'error')
                    errors += 1

                self.progress['value'] = i + 1
                elapsed = time.time() - start_t
                if i > 0:
                    etr = (elapsed / (i + 1)) * (total - (i + 1))
                    self.etr_var.set(f"Осталось: {int(etr//60)}м {int(etr%60)}с")
                self.update_idletasks()

            elapsed = int(time.time() - start_t)
            m_, s_  = divmod(elapsed, 60)
            summary = f"Готово! Файлов: {total} | строк: {total_lines} | {m_:02d}:{s_:02d}"
            if errors:
                summary += f" | ошибок: {errors}"
            self.status_var.set(summary)
            self.etr_var.set("")
            self.log_line(f"\n{'='*40}", 'info')
            self.log_line(f"  {summary}", 'success' if not errors else 'error')
            self.log_line(f"{'='*40}", 'info')

            if self.open_var.get() and not self._stop_event.is_set():
                try:
                    os.startfile(dst)
                except Exception:
                    pass

        except Exception as e:
            self.log_line(f"Критическая ошибка: {e}", 'error')
        finally:
            self.start_btn.config(state='normal')
            self.stop_btn.config(state='disabled')


if __name__ == '__main__':
    App().mainloop()
