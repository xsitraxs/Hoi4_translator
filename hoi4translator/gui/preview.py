"""Modal preview window for a translated file."""
import os
import tkinter as tk
from tkinter import messagebox
from typing import List

import customtkinter as ctk

from ..config import logger
from ..validation import validate_preview_translations
from . import theme


class PreviewWindow(ctk.CTkToplevel):
    """
    Модальное окно предпросмотра перевода.
    Показывает список Оригинал | Перевод, каждую строку перевода можно отредактировать.
    on_accept(edited_list) — вызывается с финальным списком строк при нажатии «Сохранить».
    on_cancel() — вызывается при отмене.
    """

    # Two widgets per row: an unlimited preview froze the UI on large mods.
    MAX_PREVIEW_ROWS = 1000

    def __init__(self, parent, filename: str, originals: List[str], translations: List[str],
                 on_accept, on_cancel):
        super().__init__(parent)
        self._palette = theme.resolve()

        self.title(f"Предпросмотр — {os.path.basename(filename)}")
        self.configure(fg_color=theme.color('bg'))
        self.resizable(True, True)
        self.minsize(940, 560)

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
            width, height = max(self.winfo_width(), 940), max(self.winfo_height(), 580)
            pw, ph = parent.winfo_width(), parent.winfo_height()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            self.geometry(
                f"{width}x{height}+{px + max((pw - width) // 2, 0)}"
                f"+{py + max((ph - height) // 2, 0)}"
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

        # Заголовок
        hdr = ctk.CTkFrame(self, corner_radius=0, fg_color=theme.color('panel'), height=54)
        hdr.pack(fill='x')
        ctk.CTkLabel(
            hdr, text=f"Предпросмотр: {os.path.basename(filename)}",
            font=theme.font(15, "bold"), text_color=theme.color('accent'),
        ).pack(side='left', padx=18, pady=12)
        shown = min(len(originals), self.MAX_PREVIEW_ROWS)
        count_text = (
            f"показаны первые {shown} из {len(originals)} строк"
            if shown < len(originals) else f"{len(originals)} строк"
        )
        ctk.CTkLabel(
            hdr, text=count_text, font=theme.font(12), text_color=theme.color('fg_muted')
        ).pack(side='right', padx=18, pady=12)

        # Заголовки колонок
        columns = ctk.CTkFrame(self, fg_color='transparent')
        columns.pack(fill='x', padx=18, pady=(12, 4))
        ctk.CTkLabel(
            columns, text="Оригинал", font=theme.font(12, "bold"),
            text_color=theme.color('info'), anchor='w',
        ).pack(side='left', fill='x', expand=True, padx=(0, 6))
        ctk.CTkLabel(
            columns, text="Перевод (можно редактировать)",
            font=theme.font(12, "bold"), text_color=theme.color('success'), anchor='w',
        ).pack(side='left', fill='x', expand=True, padx=(6, 0))

        # Прокручиваемая таблица
        table = ctk.CTkScrollableFrame(
            self, corner_radius=10, label_text="", fg_color=theme.color('panel')
        )
        table.pack(fill='both', expand=True, padx=18, pady=4)

        # Строки таблицы. На строку создаётся два виджета, поэтому на больших
        # файлах окно строилось минутами: редактируемую часть ограничиваем.
        for number, (orig, trans) in enumerate(
            zip(
                originals[:self.MAX_PREVIEW_ROWS], translations[:self.MAX_PREVIEW_ROWS]
            )
        ):
            row = ctk.CTkFrame(table, fg_color='transparent')
            row.pack(fill='x', pady=2)

            ctk.CTkLabel(
                row, text=f"{number + 1}. {orig[:160]}"
                          f"{'…' if len(orig) > 160 else ''}",
                font=theme.font(12, mono=True), text_color=theme.color('fg_muted'),
                anchor='w', justify='left',
            ).pack(side='left', fill='x', expand=True, padx=(0, 6))

            var = tk.StringVar(value=trans)
            self._entries.append(var)
            ctk.CTkEntry(
                row, textvariable=var, font=theme.font(12, mono=True)
            ).pack(side='left', fill='x', expand=True, padx=(6, 0))

        # Подсказка
        ctk.CTkLabel(
            self,
            text="💡 Отредактируй любую строку перевода, затем нажми «Сохранить»",
            font=theme.font(12), text_color=theme.color('fg_subtle'),
        ).pack(pady=(6, 2))

        # Кнопки
        btn_frame = ctk.CTkFrame(self, corner_radius=0, fg_color=theme.color('panel'), height=60)
        btn_frame.pack(fill='x')
        ctk.CTkButton(
            btn_frame, text="✔ Сохранить и записать файл", height=34,
            fg_color=theme.color('accent'), hover_color=theme.color('accent_hover'),
            text_color=theme.color('on_accent'),
            font=theme.font(13, "bold"), command=self._accept,
        ).pack(side='left', padx=18, pady=13)
        ctk.CTkButton(
            btn_frame, text="✘ Отмена (не записывать)", height=34,
            fg_color=theme.color('panel_alt'), hover_color=theme.color('border'), font=theme.font(12),
            command=self._cancel,
        ).pack(side='left', pady=13)
        ctk.CTkLabel(
            btn_frame, text=f"Файл: {os.path.basename(filename)}",
            font=theme.font(12), text_color=theme.color('fg_subtle'),
        ).pack(side='right', padx=18, pady=13)

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
