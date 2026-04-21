import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
import threading
import os
import re
import time

try:
    from deep_translator import GoogleTranslator
    TRANSLATOR_OK = True
except ImportError:
    TRANSLATOR_OK = False

# --- HOI4 localisation helpers ---

PLACEHOLDER_RE = re.compile(r'(\$[^$]+\$|\[[\w\.]+\]|§[A-Za-z!]|\\n|\\t|£\w+|@\w+!|@\w+\[|\])')
VALUE_RE = re.compile(r'^(\s*\S+:\d*\s*)"(.+)"(.*)$')
CYRILLIC_RE = re.compile(r'[а-яёА-ЯЁ]')
BATCH_SIZE = 40

def has_cyrillic(text):
    return bool(CYRILLIC_RE.search(text))

def protect_placeholders(text):
    tokens = []
    def replacer(m):
        tokens.append(m.group(0))
        return f'<<{len(tokens)-1}>>'
    protected = PLACEHOLDER_RE.sub(replacer, text)
    return protected, tokens

def restore_placeholders(text, tokens):
    def replacer(m):
        idx = int(m.group(1))
        return tokens[idx] if idx < len(tokens) else m.group(0)
    return re.sub(r'<<(\d+)>>', replacer, text)

def translate_batch(texts, translator):
    if not texts:
        return []
    protected_list, tokens_list = [], []
    for t in texts:
        p, tok = protect_placeholders(t)
        protected_list.append(p)
        tokens_list.append(tok)
    SEP = ' ||| '
    joined = SEP.join(protected_list)
    try:
        translated = translator.translate(joined)
        if not translated:
            return texts
        parts = translated.split(SEP)
        if len(parts) != len(texts):
            # fallback: translate one by one
            result = []
            for p, tok in zip(protected_list, tokens_list):
                try:
                    t = translator.translate(p)
                    result.append(restore_placeholders(t if t else p, tok))
                    time.sleep(0.05)
                except Exception:
                    result.append(restore_placeholders(p, tok))
            return result
        return [restore_placeholders(p, tok) for p, tok in zip(parts, tokens_list)]
    except Exception:
        return texts

def load_existing_translations(dst_path):
    existing = {}
    if not os.path.exists(dst_path):
        return existing
    try:
        with open(dst_path, 'r', encoding='utf-8-sig') as f:
            for line in f:
                m = VALUE_RE.match(line)
                if m:
                    key = m.group(1).strip()
                    value = m.group(2)
                    if has_cyrillic(value):
                        existing[key] = value
    except Exception:
        pass
    return existing

def process_file(src_path, dst_path, translator, log_cb, skip_translated_lines):
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)

    existing = {}
    if skip_translated_lines:
        existing = load_existing_translations(dst_path)
        if existing:
            log_cb(f"  Найдено уже переведённых строк: {len(existing)}")

    with open(src_path, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()

    # Первый проход — собираем индексы строк для перевода
    to_translate_idx = []   # индексы в lines
    to_translate_val = []   # значения для перевода
    meta = []               # (prefix, suffix) для каждой строки с VALUE

    out_lines = list(lines)

    skipped = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i == 0 and stripped.startswith('l_'):
            out_lines[i] = re.sub(r'^(\s*)l_english:', r'\1l_russian:', line)
            continue
        m = VALUE_RE.match(line)
        if m:
            key = m.group(1).strip()
            prefix, value, suffix = m.group(1), m.group(2), m.group(3)
            if not value.strip():
                continue
            if skip_translated_lines and key in existing:
                out_lines[i] = f'{prefix}"{existing[key]}"{suffix}\n'
                skipped += 1
            else:
                to_translate_idx.append(i)
                to_translate_val.append(value)
                meta.append((prefix, suffix))

    # Второй проход — батч-перевод
    total = len(to_translate_val)
    translated_vals = []
    for start in range(0, total, BATCH_SIZE):
        batch = to_translate_val[start:start + BATCH_SIZE]
        result = translate_batch(batch, translator)
        translated_vals.extend(result)

    # Собираем результат
    for i, (line_idx, translated, (prefix, suffix)) in enumerate(
            zip(to_translate_idx, translated_vals, meta)):
        out_lines[line_idx] = f'{prefix}"{translated}"{suffix}\n'

    with open(dst_path, 'w', encoding='utf-8-sig') as f:
        f.writelines(out_lines)

    if skipped:
        log_cb(f"  Пропущено (уже переведено): {skipped}")
    return total

def rename_dst(src_path, src_root, dst_root):
    rel = os.path.relpath(src_path, src_root)
    rel_renamed = rel.replace('_l_english.yml', '_l_russian.yml')
    rel_renamed = re.sub(r'[\\/]english[\\/]', lambda m: m.group(0).replace('english', 'russian'), rel_renamed)
    return os.path.join(dst_root, rel_renamed)

def collect_yml_files(root):
    result = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith('.yml') and fn.lower() != 'languages.yml':
                result.append(os.path.join(dirpath, fn))
    return result

# --- GUI ---

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HOI4 Localisation Translator")
        self.resizable(True, True)
        self.minsize(620, 520)
        self.configure(bg='#1a1a1a')

        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TFrame', background='#1a1a1a')
        style.configure('TLabel', background='#1a1a1a', foreground='#e0e0e0', font=('Segoe UI', 10))
        style.configure('TButton', background='#2d2d2d', foreground='#e0e0e0', font=('Segoe UI', 10), borderwidth=1)
        style.map('TButton', background=[('active', '#3d3d3d')])
        style.configure('TEntry', fieldbackground='#2d2d2d', foreground='#e0e0e0', insertcolor='#e0e0e0')
        style.configure('TCheckbutton', background='#1a1a1a', foreground='#e0e0e0', font=('Segoe UI', 10))
        style.configure('green.Horizontal.TProgressbar', troughcolor='#2d2d2d', background='#4caf50')

        pad = {'padx': 12, 'pady': 5}

        ttk.Label(self, text="HOI4 Localisation Translator", font=('Segoe UI', 14, 'bold')).pack(pady=(18, 4))
        ttk.Label(self, text="Копирует, переименовывает и переводит .yml файлы через Google Translate",
                  foreground='#888').pack(pady=(0, 14))

        src_frame = ttk.Frame(self)
        src_frame.pack(fill='x', **pad)
        ttk.Label(src_frame, text="Папка english (оригинал):").pack(anchor='w')
        row1 = ttk.Frame(src_frame)
        row1.pack(fill='x', pady=2)
        self.src_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.src_var).pack(side='left', fill='x', expand=True)
        ttk.Button(row1, text="Обзор", command=self.browse_src, width=8).pack(side='left', padx=(6, 0))

        dst_frame = ttk.Frame(self)
        dst_frame.pack(fill='x', **pad)
        ttk.Label(dst_frame, text="Папка russian (твой мод):").pack(anchor='w')
        row2 = ttk.Frame(dst_frame)
        row2.pack(fill='x', pady=2)
        self.dst_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.dst_var).pack(side='left', fill='x', expand=True)
        ttk.Button(row2, text="Обзор", command=self.browse_dst, width=8).pack(side='left', padx=(6, 0))

        # Batch size slider
        batch_frame = ttk.Frame(self)
        batch_frame.pack(fill='x', **pad)
        ttk.Label(batch_frame, text="Размер батча (строк за запрос):").pack(side='left')
        self.batch_var = tk.IntVar(value=40)
        self.batch_label = ttk.Label(batch_frame, text="40")
        self.batch_label.pack(side='right')
        slider = ttk.Scale(batch_frame, from_=10, to=80, variable=self.batch_var, orient='horizontal',
                           command=lambda v: self.batch_label.config(text=str(int(float(v)))))
        slider.pack(side='right', padx=(8, 8), fill='x', expand=True)

        opt_frame = ttk.Frame(self)
        opt_frame.pack(fill='x', **pad)
        self.skip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="Пропускать уже переведённые строки (с кириллицей)", variable=self.skip_var).pack(anchor='w')

        self.progress = ttk.Progressbar(self, style='green.Horizontal.TProgressbar', mode='determinate')
        self.progress.pack(fill='x', padx=12, pady=(10, 4))
        self.status_var = tk.StringVar(value="Готов к работе")
        ttk.Label(self, textvariable=self.status_var, foreground='#aaa').pack(anchor='w', padx=14)

        self.start_btn = ttk.Button(self, text="▶  Начать перевод", command=self.start)
        self.start_btn.pack(pady=10, ipadx=20, ipady=4)

        ttk.Label(self, text="Лог:").pack(anchor='w', padx=12)
        self.log = scrolledtext.ScrolledText(self, height=12, bg='#111', fg='#bfbfbf',
                                              font=('Consolas', 9), state='disabled',
                                              insertbackground='white', relief='flat')
        self.log.pack(fill='both', expand=True, padx=12, pady=(2, 12))

        if not TRANSLATOR_OK:
            self.log_line("ОШИБКА: deep-translator не установлен. Запусти: pip install deep-translator")

    def browse_src(self):
        d = filedialog.askdirectory(title="Выбери папку english оригинального мода")
        if d:
            self.src_var.set(d)

    def browse_dst(self):
        d = filedialog.askdirectory(title="Выбери папку russian твоего мода")
        if d:
            self.dst_var.set(d)

    def log_line(self, text):
        self.log.config(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.config(state='disabled')

    def start(self):
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        if not src or not dst:
            self.log_line("Укажи обе папки!")
            return
        if not os.path.isdir(src):
            self.log_line(f"Папка не найдена: {src}")
            return
        global BATCH_SIZE
        BATCH_SIZE = int(self.batch_var.get())
        self.start_btn.config(state='disabled')
        threading.Thread(target=self.run_translation, args=(src, dst), daemon=True).start()

    def run_translation(self, src, dst):
        skip = self.skip_var.get()
        translator = GoogleTranslator(source='en', target='ru')

        files = collect_yml_files(src)
        total = len(files)
        self.log_line(f"Найдено файлов: {total}, размер батча: {BATCH_SIZE}")
        self.progress['maximum'] = total
        self.progress['value'] = 0

        total_lines = 0
        for i, fp in enumerate(files):
            dst_path = rename_dst(fp, src, dst)
            rel = os.path.relpath(fp, src)
            self.status_var.set(f"[{i+1}/{total}] {os.path.basename(fp)}")
            self.log_line(f"[{i+1}/{total}] {rel}")
            try:
                n = process_file(fp, dst_path, translator, self.log_line, skip)
                total_lines += n
                self.log_line(f"  OK — переведено строк: {n}")
            except Exception as e:
                self.log_line(f"  ОШИБКА: {e}")
            self.progress['value'] = i + 1
            self.update_idletasks()

        self.status_var.set(f"Готово! Файлов: {total}, строк переведено: {total_lines}")
        self.log_line(f"\n=== ГОТОВО! Файлов: {total}, строк: {total_lines} ===")
        self.start_btn.config(state='normal')


if __name__ == '__main__':
    app = App()
    app.mainloop()
