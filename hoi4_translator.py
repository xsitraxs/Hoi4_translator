import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
import threading
import os
import re
import shutil
import time

try:
    from deep_translator import GoogleTranslator
    TRANSLATOR_OK = True
except ImportError:
    TRANSLATOR_OK = False

# --- HOI4 localisation helpers ---

PLACEHOLDER_RE = re.compile(r'(\$[^$]+\$|\[[\w\.]+\]|§[A-Za-z!]|\\n|\\t|£\w+|@\w+!|@\w+\[|\])')

def split_placeholders(text):
    parts = PLACEHOLDER_RE.split(text)
    return parts

def translate_text(text, translator):
    if not text.strip():
        return text
    parts = split_placeholders(text)
    translated_parts = []
    for part in parts:
        if PLACEHOLDER_RE.fullmatch(part):
            translated_parts.append(part)
        elif part.strip():
            try:
                t = translator.translate(part)
                translated_parts.append(t if t else part)
                time.sleep(0.05)
            except Exception:
                translated_parts.append(part)
        else:
            translated_parts.append(part)
    return ''.join(translated_parts)

VALUE_RE = re.compile(r'^(\s*\S+:\d*\s*)"(.*)"(.*)$')

def process_line(line, translator):
    m = VALUE_RE.match(line)
    if not m:
        return line
    prefix, value, suffix = m.group(1), m.group(2), m.group(3)
    if not value.strip():
        return line
    translated = translate_text(value, translator)
    return f'{prefix}"{translated}"{suffix}\n'

def process_file(src_path, dst_path, translator, log_cb, skip_existing):
    if skip_existing and os.path.exists(dst_path):
        log_cb(f"  SKIP (exists): {os.path.basename(dst_path)}")
        return 0

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)

    with open(src_path, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()

    out_lines = []
    count = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i == 0 and stripped.startswith('l_'):
            out_lines.append(re.sub(r'^(\s*)l_english:', r'\1l_russian:', line))
            continue
        if VALUE_RE.match(line):
            out_lines.append(process_line(line, translator))
            count += 1
        else:
            out_lines.append(line)

    with open(dst_path, 'w', encoding='utf-8-sig') as f:
        f.writelines(out_lines)

    return count

def rename_dst(src_path, src_root, dst_root):
    rel = os.path.relpath(src_path, src_root)
    rel_renamed = rel.replace('_l_english.yml', '_l_russian.yml')
    rel_renamed = re.sub(r'[\\/]english[\\/]', lambda m: m.group(0).replace('english', 'russian'), rel_renamed)
    return os.path.join(dst_root, rel_renamed)

def collect_yml_files(root):
    result = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith('.yml'):
                result.append(os.path.join(dirpath, fn))
    return result

# --- GUI ---

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HOI4 Localisation Translator")
        self.resizable(True, True)
        self.minsize(620, 500)
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

        # Source
        src_frame = ttk.Frame(self)
        src_frame.pack(fill='x', **pad)
        ttk.Label(src_frame, text="Папка english (оригинал):").pack(anchor='w')
        row1 = ttk.Frame(src_frame)
        row1.pack(fill='x', pady=2)
        self.src_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.src_var).pack(side='left', fill='x', expand=True)
        ttk.Button(row1, text="Обзор", command=self.browse_src, width=8).pack(side='left', padx=(6, 0))

        # Dest
        dst_frame = ttk.Frame(self)
        dst_frame.pack(fill='x', **pad)
        ttk.Label(dst_frame, text="Папка russian (твой мод):").pack(anchor='w')
        row2 = ttk.Frame(dst_frame)
        row2.pack(fill='x', pady=2)
        self.dst_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.dst_var).pack(side='left', fill='x', expand=True)
        ttk.Button(row2, text="Обзор", command=self.browse_dst, width=8).pack(side='left', padx=(6, 0))

        # Options
        opt_frame = ttk.Frame(self)
        opt_frame.pack(fill='x', **pad)
        self.skip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="Пропускать уже переведённые файлы", variable=self.skip_var).pack(anchor='w')

        # Progress
        self.progress = ttk.Progressbar(self, style='green.Horizontal.TProgressbar', mode='determinate')
        self.progress.pack(fill='x', padx=12, pady=(10, 4))
        self.status_var = tk.StringVar(value="Готов к работе")
        ttk.Label(self, textvariable=self.status_var, foreground='#aaa').pack(anchor='w', padx=14)

        # Start button
        self.start_btn = ttk.Button(self, text="▶  Начать перевод", command=self.start)
        self.start_btn.pack(pady=10, ipadx=20, ipady=4)

        # Log
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
        self.start_btn.config(state='disabled')
        threading.Thread(target=self.run_translation, args=(src, dst), daemon=True).start()

    def run_translation(self, src, dst):
        skip = self.skip_var.get()
        translator = GoogleTranslator(source='en', target='ru')

        files = collect_yml_files(src)
        total = len(files)
        self.log_line(f"Найдено файлов: {total}")
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
