"""Modal preview window for a translated file."""
import os
import tkinter as tk
from tkinter import messagebox, ttk
from typing import List

from ..config import logger
from ..text import zip_equal
from ..validation import validate_preview_translations


class PreviewWindow(tk.Toplevel):
    """
    Модальное окно предпросмотра перевода.
    Показывает таблицу Оригинал | Перевод, каждую строку перевода можно отредактировать.
    on_accept(edited_list) — вызывается с финальным списком строк при нажатии «Сохранить».
    on_cancel() — вызывается при отмене.
    """

    # Two widgets per row: an unlimited preview froze the UI on large mods.
    MAX_PREVIEW_ROWS = 1000

    def __init__(self, parent, filename: str, originals: List[str], translations: List[str],
                 on_accept, on_cancel):
        super().__init__(parent)
        self.title(f"Предпросмотр — {os.path.basename(filename)}")
        self.configure(bg='#1a1a1a')
        self.resizable(True, True)
        self.minsize(900, 500)

        self._on_accept = on_accept
        self._on_cancel = on_cancel
        self._originals = list(originals)
        self._translations = list(translations)
        self._entries: List[tk.StringVar] = []

        # Делаем модальным
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        try:
            self._build(filename, originals, translations)

            # Центрируем относительно родителя
            self.update_idletasks()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(
                f"{max(w,900)}x{max(h,550)}+{px + (pw-max(w,900))//2}+{py + (ph-max(h,550))//2}"
            )
        except Exception:
            # A half-built modal window would keep the grab and freeze the UI.
            logger.exception("Preview window could not be built")
            try:
                self.destroy()
            except Exception:
                pass
            raise

    def _build(self, filename, originals, translations):
        BG = '#1a1a1a'

        # Заголовок
        hdr = tk.Frame(self, bg='#2d2d2d', pady=8)
        hdr.pack(fill='x')
        tk.Label(hdr, text=f"Предпросмотр: {os.path.basename(filename)}",
                 bg='#2d2d2d', fg='#4caf50', font=('Segoe UI', 11, 'bold')).pack(side='left', padx=15)
        shown = min(len(originals), self.MAX_PREVIEW_ROWS)
        count_text = (
            f"показаны первые {shown} из {len(originals)} строк"
            if shown < len(originals) else f"{len(originals)} строк"
        )
        tk.Label(hdr, text=count_text,
                 bg='#2d2d2d', fg='#aaa', font=('Segoe UI', 10)).pack(side='right', padx=15)

        # Заголовки колонок
        col_hdr = tk.Frame(self, bg='#252525')
        col_hdr.pack(fill='x', padx=10, pady=(8, 0))
        tk.Label(col_hdr, text="  Оригинал", bg='#252525', fg='#74c0fc',
                 font=('Segoe UI', 9, 'bold'), anchor='w').pack(side='left', fill='x', expand=True)
        tk.Label(col_hdr, text="  Перевод (можно редактировать)", bg='#252525', fg='#69db7c',
                 font=('Segoe UI', 9, 'bold'), anchor='w').pack(side='left', fill='x', expand=True)

        # Scrollable таблица
        container = tk.Frame(self, bg=BG)
        container.pack(fill='both', expand=True, padx=10, pady=4)

        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient='vertical', command=canvas.yview)
        self._scroll_frame = tk.Frame(canvas, bg=BG)

        self._scroll_frame.bind(
            '<Configure>',
            lambda _event: canvas.configure(scrollregion=canvas.bbox('all'))
        )
        canvas.create_window((0, 0), window=self._scroll_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        # Привязка колёсика мыши: Windows/macOS шлют <MouseWheel>, X11 — <Button-4/5>
        def _on_wheel(event):
            if getattr(event, "num", 0) == 4:
                canvas.yview_scroll(-1, 'units')
            elif getattr(event, "num", 0) == 5:
                canvas.yview_scroll(1, 'units')
            else:
                canvas.yview_scroll(-1 * (event.delta // 120), 'units')

        def _bind_wheel(_event=None):
            for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
                canvas.bind_all(sequence, _on_wheel)

        def _unbind_wheel(_event=None):
            for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
                canvas.unbind_all(sequence)

        canvas.bind('<Enter>', _bind_wheel)
        canvas.bind('<Leave>', _unbind_wheel)

        # Строки таблицы. На строку создаётся два виджета, поэтому на больших
        # файлах окно строилось минутами: редактируемую часть ограничиваем.
        for i, (orig, trans) in enumerate(
            zip_equal(
                originals[:self.MAX_PREVIEW_ROWS], translations[:self.MAX_PREVIEW_ROWS]
            )
        ):
            row_bg = BG if i % 2 == 0 else '#202020'
            row = tk.Frame(self._scroll_frame, bg=row_bg)
            row.pack(fill='x', pady=1)

            # Оригинал (только для чтения)
            orig_lbl = tk.Text(row, bg=row_bg, fg='#aaa', font=('Consolas', 9),
                               height=1, relief='flat', wrap='none', bd=0,
                               cursor='arrow', state='normal')
            orig_lbl.insert('1.0', orig[:120] + ('…' if len(orig) > 120 else ''))
            orig_lbl.config(state='disabled')
            orig_lbl.pack(side='left', fill='x', expand=True, padx=(4, 2), pady=2)

            # Перевод (редактируемый)
            var = tk.StringVar(value=trans)
            self._entries.append(var)
            entry = ttk.Entry(row, textvariable=var, font=('Consolas', 9))
            entry.pack(side='left', fill='x', expand=True, padx=(2, 4), pady=2)

        # Подсказка
        tk.Label(self, text="💡 Отредактируй любую строку перевода, затем нажми «Сохранить»",
                 bg=BG, fg='#555', font=('Segoe UI', 9)).pack(pady=(0, 4))

        # Кнопки
        btn_frame = tk.Frame(self, bg='#2d2d2d', pady=8)
        btn_frame.pack(fill='x')

        ttk.Button(btn_frame, text="✔ Сохранить и записать файл",
                   command=self._accept).pack(side='left', padx=15, ipadx=10, ipady=4)
        ttk.Button(btn_frame, text="✘ Отмена (не записывать)",
                   command=self._cancel).pack(side='left', ipadx=10, ipady=4)
        tk.Label(btn_frame, text=f"Файл: {os.path.basename(filename)}",
                 bg='#2d2d2d', fg='#555', font=('Segoe UI', 9)).pack(side='right', padx=15)

    def _accept(self):
        # Only the visible rows are editable; the rest keep the engine output.
        edited = [var.get() for var in self._entries]
        edited.extend(self._translations[len(edited):])
        issues = validate_preview_translations(self._originals, edited)
        if issues:
            shown_issues = issues[:15]
            details = "\n".join(shown_issues)
            if len(issues) > len(shown_issues):
                details += f"\n… и ещё {len(issues) - len(shown_issues)} строк(и)"
            messagebox.showerror(
                "Нарушены игровые плейсхолдеры",
                "Исправьте отмеченные строки перед сохранением:\n\n" + details,
                parent=self,
            )
            return
        self.destroy()
        self._on_accept(edited)

    def _cancel(self):
        self.destroy()
        self._on_cancel()
