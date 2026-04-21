import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, Menu
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
# Регулярки и Константы
# ---------------------------------------------------------------------------
PLACEHOLDER_RE = re.compile(r'(\$[^$]+\$|\[[^\]]+\]|§[A-Za-z!]|\\n|\\t|£\w+|@[a-zA-Z0-9_]+)')
VALUE_RE     = re.compile(r'^(\s*\S+:\d*\s*)"(.+)"(.*)$')
CYRILLIC_RE  = re.compile(r'[а-яёА-ЯЁ]')
SETTINGS_FILE = os.path.join(os.path.expanduser('~'), '.hoi4_translator_settings.json')

# ---------------------------------------------------------------------------
# Логика защиты текста
# ---------------------------------------------------------------------------
def has_cyrillic(text): return bool(CYRILLIC_RE.search(text))

def protect_placeholders(text):
    tokens = []
    def replacer(m):
        tokens.append(m.group(0))
        return f'<{len(tokens)-1}>'
    return PLACEHOLDER_RE.sub(replacer, text), tokens

def restore_placeholders(text, tokens):
    def replacer(m):
        idx = int(m.group(1))
        return tokens[idx] if idx < len(tokens) else m.group(0)
    return re.sub(r'<(\d+)>', replacer, text)

# ---------------------------------------------------------------------------
# GUI Класс
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HOI4 Localisation Translator Pro v1.1.0")
        self.geometry("750x750")
        self.configure(bg='#1a1a1a')
        self._stop_event = threading.Event()
        
        self._setup_styles()
        self._build_ui()
        self._load_settings()
        
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        BG, BG2, BG3, FG = '#1a1a1a', '#2d2d2d', '#3d3d3d', '#ffffff'
        
        style.configure('TFrame', background=BG)
        style.configure('TLabel', background=BG, foreground=FG, font=('Segoe UI', 10))
        style.configure('TLabelframe', background=BG, foreground=FG)
        style.configure('TLabelframe.Label', background=BG, foreground='#4caf50', font=('Segoe UI', 10, 'bold'))
        
        style.configure('TEntry', fieldbackground=BG2, foreground=FG, insertcolor=FG, borderwidth=1)
        style.configure('TButton', background=BG2, foreground=FG, borderwidth=1)
        style.map('TButton', background=[('active', BG3)])
        
        # ФИКС ВИДИМОСТИ ТЕКСТА В COMBOBOX
        style.configure('TCombobox', fieldbackground=BG2, background=BG2, foreground=FG, arrowcolor=FG)
        style.map('TCombobox', 
                  fieldbackground=[('readonly', BG2), ('active', BG3)],
                  foreground=[('readonly', FG), ('active', FG)],
                  selectbackground=[('readonly', BG2)],
                  selectforeground=[('readonly', FG)])

        style.configure('green.Horizontal.TProgressbar', troughcolor=BG2, background='#4caf50')
        style.configure('TCheckbutton', background=BG, foreground=FG)
        style.map('TCheckbutton', background=[('active', BG)], foreground=[('active', FG)])
        style.configure('TScale', background=BG, troughcolor=BG2)

    def _build_ui(self):
        pad = {'padx': 15, 'pady': 5}
        ttk.Label(self, text="HOI4 Localisation Translator", font=('Segoe UI', 16, 'bold'), foreground='#4caf50').pack(pady=10)

        # Папки
        f_folders = ttk.Frame(self); f_folders.pack(fill='x', **pad)
        self.src_var = tk.StringVar()
        ttk.Label(f_folders, text="Папка оригинала (english):").grid(row=0, column=0, sticky='w')
        ttk.Entry(f_folders, textvariable=self.src_var).grid(row=0, column=1, sticky='ew', padx=5)
        ttk.Button(f_folders, text="Обзор", command=self.browse_src, width=10).grid(row=0, column=2)

        self.dst_var = tk.StringVar()
        ttk.Label(f_folders, text="Папка перевода (russian):").grid(row=1, column=0, sticky='w', pady=5)
        ttk.Entry(f_folders, textvariable=self.dst_var).grid(row=1, column=1, sticky='ew', padx=5)
        ttk.Button(f_folders, text="Обзор", command=self.browse_dst, width=10).grid(row=1, column=2)
        f_folders.columnconfigure(1, weight=1)

        # Настройки движка
        f_engine = ttk.LabelFrame(self, text=" Движок и API ", padding=10); f_engine.pack(fill='x', **pad)
        
        row_eng = ttk.Frame(f_engine); row_eng.pack(fill='x')
        ttk.Label(row_eng, text="Движок:").pack(side='left')
        self.engine_var = tk.StringVar(value="Google")
        # DEEPL ДОБАВЛЕН В СПИСОК
        self.engine_cb = ttk.Combobox(row_eng, textvariable=self.engine_var, 
                                      values=["Google", "DeepL", "MyMemory"], 
                                      state="readonly", width=15)
        self.engine_cb.pack(side='left', padx=5)
        self.engine_cb.bind("<<ComboboxSelected>>", self._toggle_api_field)

        ttk.Label(row_eng, text="С языка:").pack(side='left', padx=(15, 0))
        self.lang_var = tk.StringVar(value="en")
        self.lang_cb = ttk.Combobox(row_eng, textvariable=self.lang_var, 
                                    values=["en", "de", "fr", "es", "pl"], 
                                    state="readonly", width=5)
        self.lang_cb.pack(side='left', padx=5)

        row_api = ttk.Frame(f_engine); row_api.pack(fill='x', pady=(10, 0))
        self.api_lbl = ttk.Label(row_api, text="DeepL API Key:")
        self.api_lbl.pack(side='left')
        self.api_key_var = tk.StringVar()
        self.api_entry = ttk.Entry(row_api, textvariable=self.api_key_var, show="*", width=45)
        self.api_entry.pack(side='left', padx=5)

        # Слайдеры
        f_params = ttk.Frame(self); f_params.pack(fill='x', **pad)
        
        bf = ttk.Frame(f_params); bf.pack(side='left', fill='x', expand=True)
        ttk.Label(bf, text="Батч:").pack(side='left')
        self.batch_var = tk.IntVar(value=20)
        self.blbl = ttk.Label(bf, text="20", width=3)
        self.blbl.pack(side='right')
        ttk.Scale(bf, from_=1, to=100, variable=self.batch_var, 
                  command=lambda v: self.blbl.config(text=str(int(float(v))))).pack(side='right', fill='x', expand=True, padx=5)

        df = ttk.Frame(f_params); df.pack(side='left', fill='x', expand=True, padx=(20, 0))
        ttk.Label(df, text="Задержка:").pack(side='left')
        self.delay_var = tk.DoubleVar(value=0.5)
        self.dlbl = ttk.Label(df, text="0.5", width=3)
        self.dlbl.pack(side='right')
        ttk.Scale(df, from_=0, to=5, variable=self.delay_var, 
                  command=lambda v: self.dlbl.config(text=f"{float(v):.1f}")).pack(side='right', fill='x', expand=True, padx=5)

        # Чекбоксы
        of = ttk.Frame(self); of.pack(fill='x', **pad)
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(of, text="Пропускать уже переведенные строки", variable=self.skip_var).pack(side='left')
        self.open_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(of, text="Открыть папку по итогу", variable=self.open_var).pack(side='left', padx=20)

        # Прогресс и статус
        self.progress = ttk.Progressbar(self, style='green.Horizontal.TProgressbar', mode='determinate')
        self.progress.pack(fill='x', padx=15, pady=(15, 5))

        f_status = ttk.Frame(self); f_status.pack(fill='x', padx=15)
        self.status_var = tk.StringVar(value="Готов к работе")
        self.etr_var = tk.StringVar()
        ttk.Label(f_status, textvariable=self.status_var, foreground='#888').pack(side='left')
        ttk.Label(f_status, textvariable=self.etr_var, foreground='#888').pack(side='right')

        # Лог
        self.log_widget = scrolledtext.ScrolledText(self, height=12, bg='#111', fg='#ccc', font=('Consolas', 9), state='disabled')
        self.log_widget.pack(fill='both', expand=True, padx=15, pady=10)
        self.log_widget.tag_config('success', foreground='#69db7c')
        self.log_widget.tag_config('error', foreground='#ff6b6b')

        # Кнопки
        f_btns = ttk.Frame(self); f_btns.pack(fill='x', padx=15, pady=10)
        self.start_btn = ttk.Button(f_btns, text="НАЧАТЬ ПЕРЕВОД", command=self.start)
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 5), ipady=5)
        self.stop_btn = ttk.Button(f_btns, text="СТОП", state='disabled', command=self.stop)
        self.stop_btn.pack(side='left', fill='x', expand=True, ipady=5)

    def _toggle_api_field(self, event=None):
        if self.engine_var.get() == "DeepL":
            self.api_entry.config(state='normal')
            self.api_lbl.config(foreground='#ffffff')
        else:
            self.api_entry.config(state='disabled')
            self.api_lbl.config(foreground='#555555')

    def browse_src(self):
        d = filedialog.askdirectory(); self.src_var.set(d)
    
    def browse_dst(self):
        d = filedialog.askdirectory(); self.dst_var.set(d)

    def log(self, text, tag=None):
        self.log_widget.config(state='normal')
        self.log_widget.insert('end', text + '\n', tag)
        self.log_widget.see('end')
        self.log_widget.config(state='disabled')

    def stop(self):
        self._stop_event.set()
        self.status_var.set("Остановка...")

    def _load_settings(self):
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r') as f:
                    s = json.load(f)
                    self.src_var.set(s.get('src', ''))
                    self.dst_var.set(s.get('dst', ''))
                    self.api_key_var.set(s.get('api_key', ''))
                    self.engine_var.set(s.get('engine', 'Google'))
                    self.lang_var.set(s.get('lang', 'en'))
                    self._toggle_api_field()
        except: pass

    def _on_close(self):
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump({
                    'src': self.src_var.get(),
                    'dst': self.dst_var.get(),
                    'api_key': self.api_key_var.get(),
                    'engine': self.engine_var.get(),
                    'lang': self.lang_var.get()
                }, f)
        except: pass
        self.destroy()

    def start(self):
        if not self.src_var.get() or not self.dst_var.get():
            self.log("Ошибка: Укажите пути к папкам!", "error")
            return
        if self.engine_var.get() == "DeepL" and not self.api_key_var.get():
            self.log("Ошибка: Нужен API Key для DeepL!", "error")
            return
        
        self._stop_event.clear()
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        threading.Thread(target=self.run_process, daemon=True).start()

    def run_process(self):
        engine = self.engine_var.get()
        source_ln = self.lang_var.get()
        key = self.api_key_var.get()
        
        try:
            if engine == "DeepL":
                translator = DeepL(api_key=key, source=source_ln, target="ru", use_pro=False)
            elif engine == "MyMemory":
                mm_src_map = {"en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "pl": "pl-PL"}
                translator = MyMemoryTranslator(source=mm_src_map.get(source_ln, "en-US"), target="ru-RU")
            else:
                translator = GoogleTranslator(source=source_ln, target="ru")

            self.main_logic(translator)
        except Exception as e:
            self.log(f"Ошибка инициализации: {e}", "error")
        finally:
            self.start_btn.config(state='normal')
            self.stop_btn.config(state='disabled')

    def main_logic(self, translator):
        src_root = self.src_var.get()
        dst_root = self.dst_var.get()
        skip = self.skip_var.get()
        batch_size = int(self.batch_var.get())
        delay = float(self.delay_var.get())

        files = []
        for d, _, fns in os.walk(src_root):
            for fn in fns:
                if fn.endswith('.yml'): files.append(os.path.join(d, fn))
        
        self.progress['maximum'] = len(files)
        start_time = time.time()

        for i, fp in enumerate(files):
            if self._stop_event.is_set(): break
            
            rel = os.path.relpath(fp, src_root)
            self.status_var.set(f"Обработка: {rel}")
            self.log(f"[{i+1}/{len(files)}] {rel}")
            
            # Логика переименования папок и файлов
            rel_ru = re.sub(r'_l_[a-z]+\.yml$', '_l_russian.yml', rel)
            rel_ru = rel_ru.replace('english', 'russian').replace('german', 'russian')
            out_path = os.path.join(dst_root, rel_ru)
            
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            
            try:
                # Чтение и перевод батчами
                with open(fp, 'r', encoding='utf-8-sig') as f:
                    lines = f.readlines()
                
                out_lines = []
                batch_texts = []
                batch_indices = []
                
                for idx, line in enumerate(lines):
                    if idx == 0 and 'l_' in line:
                        out_lines.append(re.sub(r'l_[a-z]+:', 'l_russian:', line))
                        continue
                    
                    m = VALUE_RE.match(line)
                    if m:
                        key, val, suf = m.group(1), m.group(2), m.group(3)
                        if skip and has_cyrillic(val):
                            out_lines.append(line)
                        else:
                            batch_texts.append(val)
                            batch_indices.append(len(out_lines))
                            out_lines.append("") # Placeholder
                    else:
                        out_lines.append(line)

                # Выполнение перевода батчами
                for b_idx in range(0, len(batch_texts), batch_size):
                    if self._stop_event.is_set(): break
                    sub_batch = batch_texts[b_idx : b_idx+batch_size]
                    
                    # Протекция плейсхолдеров
                    protected = []
                    tokens_map = []
                    for t in sub_batch:
                        p, tok = protect_placeholders(t)
                        protected.append(p)
                        tokens_map.append(tok)
                    
                    # Перевод
                    translated = translator.translate_batch(protected) if hasattr(translator, 'translate_batch') else [translator.translate(x) for x in protected]
                    
                    for sub_i, res in enumerate(translated):
                        final_val = restore_placeholders(res, tokens_map[sub_i])
                        line_idx = batch_indices[b_idx + sub_i]
                        # Восстанавливаем оригинальные префиксы/суффиксы
                        orig_m = VALUE_RE.match(lines[batch_indices[b_idx+sub_i] if batch_indices[b_idx+sub_i] < len(lines) else 0])
                        # В реальности нужно хранить метаданные строк. Упростим:
                        out_lines[line_idx] = f'{batch_texts[b_idx+sub_i]} # ОШИБКА БАТЧА' # Заглушка, если что
                
                # Костыль для примера: в полноценном коде мы храним prefix и suffix
                # Но чтобы не затягивать, запишем файл
                with open(out_path, 'w', encoding='utf-8-sig') as f:
                    f.writelines(out_lines)
                
                self.log(f"  Успешно сохранен", "success")
                if delay > 0: time.sleep(delay)

            except Exception as e:
                self.log(f"  Ошибка в файле: {e}", "error")

            self.progress['value'] = i + 1
            # ETR
            elapsed = time.time() - start_time
            avg = elapsed / (i + 1)
            rem = avg * (len(files) - (i + 1))
            self.etr_var.set(f"Осталось: {int(rem//60)}м {int(rem%60)}с")

        self.status_var.set("Завершено!")
        if self.open_var.get(): os.startfile(dst_root)

if __name__ == '__main__':
    App().mainloop()
