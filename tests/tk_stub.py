"""A recording stand-in for tkinter.

The domain modules are imported on headless builders where Python was built
without Tk, and the GUI itself cannot be rendered in CI at all. This stub lets
the tests import ``hoi4translator.gui`` and execute its layout code: unknown
widget methods are recorded instead of raising, so a typo or a missing widget
in ``_build_ui`` still fails the test.
"""
import sys
import types

CALLS = []


class _Var:
    def __init__(self, master=None, value=None):
        self._value = value if value is not None else ("" if not isinstance(value, bool) else False)

    def set(self, value):
        self._value = value

    def get(self):
        return self._value


class _VarBool(_Var):
    def __init__(self, master=None, value=False):
        self._value = bool(value)


class _VarInt(_Var):
    def __init__(self, master=None, value=0):
        self._value = int(value)


class _VarDouble(_VarInt):
    pass


class Widget:
    def __init__(self, master=None, **kwargs):
        self.master = master
        self.kwargs = kwargs
        self._name = type(self).__name__
        CALLS.append((self._name, kwargs.get("text", kwargs.get("textvariable", ""))))

    # geometry managers and common widget API
    def pack(self, **kw):
        return self

    def grid(self, **kw):
        return self

    def place(self, **kw):
        return self

    def config(self, *args, **kw):
        # tkinter: w.configure(option=value) and Style.configure(name, opt=…)
        self.kwargs.update(kw)
        return self

    configure = config

    def cget(self, key):
        return self.kwargs.get(key)

    def bind(self, *a, **kw):
        return ""

    def columnconfigure(self, *a, **kw):
        pass

    def rowconfigure(self, *a, **kw):
        pass

    def winfo_children(self):
        return []

    def winfo_exists(self):
        return True

    def __getattr__(self, item):
        # Private names are real attributes: getattr(obj, name, default) must
        # be able to report "not there yet" instead of returning a callable.
        if item.startswith("_") or item.islower() and item.endswith("_widget"):
            raise AttributeError(item)
        # Everything else (insert, delete, see, tag_config, focus, lift, …)
        # is recorded as a no-op so layout code can be exercised.
        def _noop(*args, **kwargs):
            CALLS.append((f"{self._name}.{item}", args[:1]))

        return _noop


class Tk(Widget):
    def __init__(self, *a, **kw):
        super().__init__(None, **kw)
        self._after = []

    def mainloop(self, *a, **kw):
        return None

    def after(self, delay, callback=None, *args):
        self._after.append((delay, callback))
        return "after#1"

    def after_cancel(self, handle):
        pass

    def protocol(self, *a, **kw):
        pass

    def resizable(self, *a, **kw):
        pass

    def geometry(self, *a, **kw):
        pass

    def destroy(self):
        pass

    def update_idletasks(self):
        pass

    def quit(self):
        pass


class Toplevel(Tk):
    def transient(self, *a, **kw):
        pass

    def grab_set(self):
        pass

    def wait_window(self, *a, **kw):
        pass


def _widget_factory(name):
    return type(name, (Widget,), {})


def install():
    tk = types.ModuleType("tkinter")
    tk.Tk = Tk
    tk.Toplevel = Toplevel
    tk.StringVar = _Var
    tk.BooleanVar = _VarBool
    tk.IntVar = _VarInt
    tk.DoubleVar = _VarDouble
    for const in ("END", "NORMAL", "DISABLED", "LEFT", "RIGHT", "TOP", "BOTTOM",
                  "BOTH", "X", "Y", "W", "E", "N", "S", "CENTER", "WORD", "CHAR",
                  "HORIZONTAL", "VERTICAL", "SEL", "INSERT", "ANCHOR"):
        setattr(tk, const, const)
    for cls in ("Frame", "LabelFrame", "Label", "Button", "Entry", "Text", "Canvas",
                "Scrollbar", "Listbox", "Checkbutton", "Radiobutton", "Scale",
                "Spinbox", "Menu", "Menubutton", "Message", "PanedWindow"):
        setattr(tk, cls, _widget_factory(cls))

    ttk = types.ModuleType("tkinter.ttk")
    for cls in ("Frame", "LabelFrame", "Label", "Button", "Entry", "Checkbutton",
                "Combobox", "Progressbar", "Scale", "Separator", "Notebook",
                "Treeview", "Radiobutton", "Spinbox", "Sizegrip", "PanedWindow",
                "Scrollbar"):
        setattr(ttk, cls, _widget_factory(cls))

    class Style(Widget):
        pass

    ttk.Style = Style
    tk.ttk = ttk

    for mod in ("filedialog", "messagebox", "scrolledtext", "font", "colorchooser",
                "simpledialog", "commondialog"):
        module = types.ModuleType(f"tkinter.{mod}")
        if mod == "scrolledtext":
            module.ScrolledText = _widget_factory("ScrolledText")
        for func in ("askopenfilename", "askdirectory", "asksaveasfilename",
                     "showinfo", "showerror", "showwarning", "askyesno", "askokcancel"):
            setattr(module, func, lambda *a, **kw: None)
        setattr(tk, mod, module)

    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    for mod in ("filedialog", "messagebox", "scrolledtext", "font", "colorchooser",
                "simpledialog", "commondialog"):
        sys.modules[f"tkinter.{mod}"] = getattr(tk, mod)
    return tk
