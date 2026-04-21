import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, Menu
import threading
import os
import re
import time
import json

try:
    from deep_translator import GoogleTranslator, MyMemoryTranslator
    TRANSLATOR_OK = True
except ImportError:
    TRANSLATOR_OK = False

# ---------------------------------------------------------------------------
# Константы и Регулярки
# ---------------------------------------------------------------------------

# Улучшенный regex для захвата плейсхолдеров, скриптов, цветов и переменных
PLACEHOLDER_RE = re.compile(
    r'(\$[^$]+\$|\[[^\]]+\]|§[A-Za-z!]|\\n|\\t|£\w+|@[a-zA-Z0-9_]+)'
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

    # Умное разбиение: если слишком длинная строка (лимит Google ~5000), бьем пополам
    if len(joined) > 4500 and len(texts) > 1:
        mid = len(texts) // 2
        return translate_batch(texts[:mid], translator, retries) + \
               translate_batch(texts[mid:], translator, retries)

    for attempt in range(retries):
        try:
            translated = translator.translate(joined)
            if not translated:
                break
            parts = translated.split(SEP)
            # Иногда переводчик съедает пробелы вокруг сепаратора
            if len(parts) != len(texts):
                parts = [p.strip() for p in translated.split('|||')]
            
            if len(parts) == len(texts):
                return [restore_placeholders(p, tok) for p, tok in zip(parts, tokens_list)]
            break
        except Exception:
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
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

def process_file(src_path, dst_path, translator, log_cb, skip_translated, batch_size, delay_sec, stop_event):
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
            # Заменяем язык в заголовке файла
            out_lines[i] = re.sub(r'^(\s*)l_[a-zA-Z]+:', r'\1l_russian:', line)
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

    total = len(to_translate_val)
    translated_vals = []
    
    for start in range(0, total, batch_size):
        if stop_event.is_set():
            return None
        batch = to_translate_val[start:start + batch_size]
        result = translate_batch(batch, translator)
        translated_vals.extend(result)
        
        # Пауза между батчами для защиты от бана
        if delay_sec > 0 and (start + batch_size) < total:
            time.sleep(delay_sec)

    for line_idx, translated, (prefix, suffix) in zip(to_translate_idx, translated_vals, meta):
        out_lines[line_idx] = f'{prefix}"{translated}"{suffix}\n'

    # Безопасное сохранение (сначала во временный файл)
    tmp_path = dst_path + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8-sig') as f:
        f.writelines(out_lines)
    os.replace(tmp_path, dst_path)

    if skipped:
        log_cb(f"  Пропущено (уже переведено): {skipped}", 'info')
    return total

def rename_dst(src_path, src_root, dst_root):
    rel = os.path.relpath(src_path, src_root)
    # Универсальная замена языка в названии файла и папках
    rel_renamed = re.sub(r'_l_[a-zA-Z]+\.yml$', '_l_russian.yml', rel)
    rel_renamed = re.sub(
        r'(?i)(^|[\\/])[a-zA-Z]+([\\/])',
        lambda m: m.group(1) + 'russian' + m.group(2) if 'russian' not in rel.lower() else m.group(0),
        rel_renamed, count=1
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
        self.title("HOI4 Localisation Translator Pro")
        self.resizable(True, True)
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
        BG, BG2, BG3, FG = '#1a1a1a', '#2d2d2d', '#3d3d3d', '#e0e0e0'

        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=FG, font=('Segoe UI', 10))
        style.configure('TButton', background=BG2, foreground=FG, font=('Segoe UI', 10), borderwidth=1)
        style.map('TButton', background=[('active', BG3), ('disabled', BG2)],
                             foreground=[('disabled', '#666')])
        style.configure('TEntry', fieldbackground=BG2, foreground=FG, insertcolor=FG)
        style.configure('TCheckbutton', background=BG, foreground=FG, font=('Segoe UI', 10))
        style.map('TCheckbutton', background=[('active', BG), ('pressed', BG)],
                                  foreground=[('active', FG), ('pressed', FG)])
        style.configure('green.Horizontal.TProgressbar', troughcolor=BG2, background='#4caf50')
        style.configure('TScale', background=BG, troughcolor=BG2)
        style.configure('TCombobox', fieldbackground=BG2, background=BG2, foreground=FG)

    def _build_ui(self):
        pad = {'padx': 12, 'pady': 4}

        ttk.Label(self, text="HOI4 Localisation Translator Pro", font=('Segoe UI', 14, 'bold')).pack(pady=(12, 2))
        
        # Настройки папок
        sf = ttk.Frame(self); sf.pack(fill='x', **pad)
        ttk.Label(sf, text="Оригинал (например, папка english):").pack(anchor='w')
        r1 = ttk.Frame(sf); r1.pack(fill='x', pady=2)
        self.src_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.src_var).pack(side='left', fill='x', expand=True)
        ttk.Button(r1, text="Обзор", command=self.browse_src, width=8).pack(side='left', padx=(6, 0))

        df = ttk.Frame(self); df.pack(fill='x', **pad)
        ttk.Label(df, text="Перевод (папка russian):").pack(anchor='w')
        r2 = ttk.Frame(df); r2.pack(fill='x', pady=2)
        self.dst_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.dst_var).pack(side='left', fill='x', expand=True)
        ttk.Button(r2, text="Обзор", command=self.browse_dst, width=8).pack(side='left', padx=(6, 0))

        # Настройки движка и языка
        eng_frame = ttk.Frame(self); eng_frame.pack(fill='x', **pad)
        ttk.Label(eng_frame, text="Движок:").pack(side='left')
        self.engine_var = tk.StringVar(value="Google")
        ttk.Combobox(eng_frame, textvariable=self.engine_var, values=["Google", "MyMemory"], 
                     state="readonly", width=10).pack(side='left', padx=(4, 16))
        
        ttk.Label(eng_frame, text="С языка:").pack(side='left')
        self.lang_var = tk.StringVar(value="en")
        ttk.Combobox(eng_frame, textvariable=self.lang_var, values=["en", "de", "fr", "es", "pl"], 
                     state="readonly", width=5).pack(side='left', padx=(4, 0))

        # Слайдеры (Батч и Задержка)
        slider_frame = ttk.Frame(self); slider_frame.pack(fill='x', **pad)
        
        # Батч
        bf = ttk.Frame(slider_frame); bf.pack(side='left', fill='x', expand=True, padx=(0, 6))
        ttk.Label(bf, text="Размер батча:").pack(side='left')
        self.batch_var = tk.IntVar(value=20)
        self.batch_label = ttk.Label(bf, text="20", width=3)
        self.batch_label.pack(side='right')
        ttk.Scale(bf, from_=5, to=80, variable=self.batch_var, orient='horizontal',
                  command=lambda v: self.batch_label.config(text=str(int(float(v))))).pack(side='right', fill='x', expand=True)

        # Задержка
        dfrm = ttk.Frame(slider_frame); dfrm.pack(side='left', fill='x', expand=True, padx=(6, 0))
        ttk.Label(dfrm, text="Задержка (сек):").pack(side='left')
        self.delay_var = tk.DoubleVar(value=0.5)
        self.delay_label = ttk.Label(dfrm, text="0.5", width=3)
        self.delay_label.pack(side='right')
        ttk.Scale(dfrm, from_=0, to=5, variable=self.delay_var, orient='horizontal',
                  command=lambda v: self.delay_label.config(text=f"{float(v):.1f}")).pack(side='right', fill='x', expand=True)

        # Галочки
        of = ttk.Frame(self); of.pack(fill='x', **pad)
        self.skip_var = tk.BooleanVar(value=False)
        self.open_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="Пропускать уже переведённые строки (с кириллицей)", variable=self.skip_var).pack(anchor='w')
        ttk.Checkbutton(of, text="Открыть папку после завершения", variable=self.open_var).pack(anchor='w')

        # Прогресс
        self.progress = ttk.Progressbar(self, style='green.Horizontal.TProgressbar', mode='determinate')
        self.progress.pack(fill='x', padx=12, pady=(10, 2))

        # Статус и ETR
        sf2 = ttk.Frame(self); sf2.pack(fill='x', padx=12)
        self.status_var = tk.StringVar(value="Готов к работе")
        self.elapsed_var = tk.StringVar(value="")
        ttk.Label(sf2, textvariable=self.status_var, foreground='#aaa').pack(side='left')
        ttk.Label(sf2, textvariable=self.elapsed_var, foreground='#666').pack(side='right')

        # Кнопки
        btn_frame = ttk.Frame(self); btn_frame.pack(fill='x', padx=12, pady=8)
        self.start_btn = ttk.Button(btn_frame, text="Начать перевод", command=self.start)
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 4), ipady=6)
        self.stop_btn  = ttk.Button(btn_frame, text="Стоп", command=self.stop, state='disabled')
        self.stop_btn.pack(side='left', fill='x', expand=True, padx=4, ipady=6)
        self.clear_btn = ttk.Button(btn_frame, text="Очистить логи", command=self.clear_log)
        self.clear_btn.pack(side='left', fill='x', expand=True, padx=(4, 0), ipady=6)

        # Лог и контекстное меню
        ttk.Label(self, text="Лог:").pack(anchor='w', padx=12)
        self.log_widget = scrolledtext.ScrolledText(self, height=12, bg='#111', fg='#bfbfbf',
                                                    font=('Consolas', 9), state='disabled', relief='flat')
        self.log_widget.pack(fill='both', expand=True, padx=12, pady=(2, 12))
        
        self.log_widget.tag_config('error', foreground='#ff6b6b')
        self.log_widget.tag_config('success', foreground='#69db7c')
        self.log_widget.tag_config('info', foreground='#74c0fc')
        self.log_widget.tag_config('warn', foreground='#ffd43b')
        
        self.context_menu = Menu(self, tearoff=0, bg='#2d2d2d', fg='#e0e0e0')
        self.context_menu.add_command(label="Копировать", command=self._copy_log)
        self.log_widget.bind("<Button-3>", self._show_context_menu)

    def _show_context_menu(self, event):
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def _copy_log(self):
        try:
            selected_text = self.log_widget.selection_get()
            self.clipboard_clear()
            self.clipboard_append(selected_text)
        except tk.TclError:
            pass # Нет выделенного текста

    def browse_src(self):
        d = filedialog.askdirectory(title="Выбери оригинальную папку (english)")
        if d: self.src_var.set(d)

    def browse_dst(self):
        d = filedialog.askdirectory(title="Выбери папку перевода (russian)")
        if d: self.dst_var.set(d)

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

    def _load_settings(self):
        s = load_settings()
        if s.get('src'): self.src_var.set(s['src'])
        if s.get('dst'): self.dst_var.set(s['dst'])
        if s.get('batch'): self.batch_var.set(s['batch']); self.batch_label.config(text=str(s['batch']))
        if s.get('delay') is not None: self.delay_var.set(s['delay']); self.delay_label.config(text=f"{s['delay']:.1f}")
        if s.get('engine'): self.engine_var.set(s['engine'])
        if s.get('lang'): self.lang_var.set(s['lang'])
        if s.get('skip') is not None: self.skip_var.set(s['skip'])
        if s.get('open') is not None: self.open_var.set(s['open'])

    def _save_settings(self):
        save_settings({
            'src': self.src_var.get(),
            'dst': self.dst_var.get(),
            'batch': int(self.batch_var.get()),
            'delay': float(self.delay_var.get()),
            'engine': self.engine_var.get(),
            'lang': self.lang_var.get(),
            'skip': self.skip_var.get(),
            'open': self.open_var.get(),
        })

    def _on_close(self):
        self._save_settings()
        self.destroy()

    def update_etr(self, start_time, current_index, total_count):
        elapsed = time.time() - start_time
        m_el, s_el = divmod(int(elapsed), 60)
        h_el, m_el = divmod(m_el, 60)
        el_str = f"{h_el:02d}:{m_el:02d}:{s_el:02d}" if h_el > 0 else f"{m_el:02d}:{s_el:02d}"

        if current_index > 0 and current_index < total_count:
            time_per_file = elapsed / current_index
            remaining_files = total_count - current_index
            remaining_seconds = int(time_per_file * remaining_files)
            
            m_rem, s_rem = divmod(remaining_seconds, 60)
            h_rem, m_rem = divmod(m_rem, 60)
            rem_str = f"{h_rem:02d}:{m_rem:02d}:{s_rem:02d}" if h_rem > 0 else f"{m_rem:02d}:{s_rem:02d}"
            
            self.elapsed_var.set(f"Прошло: {el_str} | Осталось: ~{rem_str}")
        else:
            self.elapsed_var.set(f"Время: {el_str}")
        self.update_idletasks()

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

    def run_translation(self, src, dst):
        skip       = self.skip_var.get()
        open_after = self.open_var.get()
        batch_size = int(self.batch_var.get())
        delay_sec  = float(self.delay_var.get())
        source_ln  = self.lang_var.get()
        engine     = self.engine_var.get()

        if engine == "DeepL":
            translator = DeepL(api_key=key, source=source_ln, target="ru", use_pro=False)
        elif engine == "MyMemory":
            # MyMemory требует полные региональные коды
            mm_src_map = {"en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "pl": "pl-PL"}
            mm_src = mm_src_map.get(source_ln, "en-US")
            translator = MyMemoryTranslator(source=mm_src, target="ru-RU")
        else:
            translator = GoogleTranslator(source=source_ln, target="ru")

        files = collect_yml_files(src)
        total = len(files)
        self.log_line(f"Старт: {engine} ({source_ln}->ru) | Файлов: {total} | Батч: {batch_size} | Пауза: {delay_sec}с", 'info')
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
                n = process_file(fp, dst_path, translator, self.log_line, skip, batch_size, delay_sec, self._stop_event)
                if n is None:
                    self.log_line("[СТОП] Перевод остановлен пользователем.", 'warn')
                    break
                total_lines += n
                self.log_line(f"  ✓ строк: {n}", 'success')
            except Exception as e:
                self.log_line(f"  ✗ ОШИБКА: {e}", 'error')
                errors += 1

            self.progress['value'] = i + 1
            self.update_etr(start_time, i + 1, total)

        # Финальный отчет
        self.update_etr(start_time, total, total)
        summary = f"Готово! Файлов: {total} | Строк: {total_lines}"
        if errors: summary += f" | Ошибок: {errors}"
        
        self.status_var.set(summary)
        self.log_line(f"\n{'='*50}", 'info')
        self.log_line(f"  {summary}", 'success' if not errors else 'warn')
        self.log_line(f"{'='*50}", 'info')

        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')

        if open_after and not self._stop_event.is_set():
            try: os.startfile(dst)
            except Exception: pass

if __name__ == '__main__':
    app = App()
    app.mainloop()
