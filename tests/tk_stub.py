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

    # Geometry queries must be numbers: the code centres windows with them.
    def winfo_width(self):
        return 1200

    def winfo_height(self):
        return 800

    def winfo_rootx(self):
        return 100

    def winfo_rooty(self):
        return 100

    def winfo_x(self):
        return 0

    def winfo_y(self):
        return 0

    def winfo_ismapped(self):
        return True

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

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


# ---------------------------------------------------------------------------
# CustomTkinter
# ---------------------------------------------------------------------------

# Documented constructor options per widget. Unknown options raise, so a typo
# like text_colorr= is caught here and not by the user on their screen.
CTK_OPTIONS = {
    "CTk": {"fg_color"},
    "CTkToplevel": {"fg_color"},
    "CTkFrame": {
        "width", "height", "corner_radius", "border_width", "bg_color",
        "fg_color", "border_color", "background_corner_colors",
        "overwrite_preferred_drawing_method",
    },
    "CTkLabel": {
        "width", "height", "corner_radius", "border_width", "bg_color",
        "fg_color", "border_color", "text_color", "text_color_disabled",
        "text", "font", "image", "compound", "anchor", "wraplength",
        "textvariable", "justify",
    },
    "CTkButton": {
        "width", "height", "corner_radius", "border_width", "border_spacing",
        "bg_color", "fg_color", "hover_color", "border_color", "text_color",
        "text_color_disabled", "background_corner_colors",
        "round_width_to_even_numbers", "round_height_to_even_numbers", "text",
        "font", "textvariable", "image", "state", "hover", "command",
        "compound", "anchor",
    },
    "CTkEntry": {
        "width", "height", "corner_radius", "border_width", "bg_color",
        "fg_color", "border_color", "text_color", "placeholder_text_color",
        "textvariable", "placeholder_text", "font", "state", "justify", "show",
    },
    "CTkComboBox": {
        "width", "height", "corner_radius", "border_width", "bg_color",
        "fg_color", "border_color", "button_color", "button_hover_color",
        "dropdown_fg_color", "dropdown_hover_color", "dropdown_text_color",
        "text_color", "text_color_disabled", "font", "dropdown_font", "values",
        "state", "hover", "variable", "command", "justify",
    },
    "CTkCheckBox": {
        "width", "height", "checkbox_width", "checkbox_height", "corner_radius",
        "border_width", "bg_color", "fg_color", "hover_color", "border_color",
        "checkmark_color", "text_color", "text_color_disabled", "text", "font",
        "textvariable", "state", "hover", "command", "onvalue", "offvalue",
        "variable",
    },
    "CTkSlider": {
        "width", "height", "corner_radius", "button_corner_radius",
        "border_width", "button_length", "bg_color", "fg_color",
        "border_color", "progress_color", "button_color", "button_hover_color",
        "from_", "to", "state", "number_of_steps", "scroll_step", "hover",
        "command", "variable", "orientation",
    },
    "CTkProgressBar": {
        "width", "height", "corner_radius", "border_width", "bg_color",
        "fg_color", "border_color", "progress_color", "variable",
        "orientation", "mode", "determinate_speed", "indeterminate_speed",
    },
    "CTkTextbox": {
        "width", "height", "corner_radius", "border_width", "border_spacing",
        "bg_color", "fg_color", "border_color", "text_color",
        "scrollbar_button_color", "scrollbar_button_hover_color", "font",
        "activate_scrollbars", "wrap", "state",
    },
    "CTkScrollableFrame": {
        "width", "height", "corner_radius", "border_width", "bg_color",
        "fg_color", "border_color", "scrollbar_fg_color",
        "scrollbar_button_color", "scrollbar_button_hover_color",
        "label_fg_color", "label_text_color", "label_text", "label_font",
        "label_anchor", "orientation",
    },
    "CTkScrollbar": {
        "width", "height", "corner_radius", "border_spacing",
        "minimum_pixel_length", "bg_color", "fg_color", "button_color",
        "button_hover_color", "hover", "command", "orientation",
    },
}

# Accepted by every widget on top of the class-specific options.
CTK_COMMON = {"cursor", "takefocus"}


def _ctk_name(obj) -> str:
    """The documented CTk class of a widget, even for subclasses."""
    for klass in type(obj).__mro__:
        if klass.__name__ in CTK_OPTIONS:
            return klass.__name__
    return type(obj).__name__


class CTkWidget(Widget):
    """A CTk widget that validates its options against the documentation."""

    def __init__(self, master=None, **kwargs):
        name = _ctk_name(self)
        unknown = set(kwargs) - CTK_OPTIONS.get(name, set()) - CTK_COMMON
        if unknown:
            raise TypeError(f"{name} получил неизвестные опции: {sorted(unknown)}")
        super().__init__(master, **kwargs)

    def configure(self, *args, **kwargs):
        require_redraw = kwargs.pop("require_redraw", None)
        name = _ctk_name(self)
        unknown = set(kwargs) - CTK_OPTIONS.get(name, set()) - CTK_COMMON - {"font"}
        if unknown:
            raise TypeError(f"{name}.configure: неизвестные опции {sorted(unknown)}")
        self.kwargs.update(kwargs)
        if require_redraw is not None:
            CALLS.append((f"{self._name}.configure", ("require_redraw",)))
        return self

    config = None  # CTk widgets have no .config() alias: catch it in tests

    def set(self, value):
        self.kwargs["value"] = value
        CALLS.append((f"{self._name}.set", (value,)))

    def tag_config(self, tag, **kwargs):
        if "font" in kwargs:
            raise AttributeError("'font' option forbidden (scaling)")
        CALLS.append((f"{self._name}.tag_config", (tag,)))

    def cget(self, key):
        return self.kwargs.get(key)


class CTkFont:
    def __init__(self, family=None, size=None, weight=None, slant="roman",
                 underline=False, overstrike=False):
        self.family = family
        self.size = size
        self.weight = weight
        self.slant = slant
        CALLS.append(("CTkFont", (family, size, weight)))

    def __repr__(self):
        return f"CTkFont({self.family!r}, {self.size!r}, {self.weight!r})"


def _install_customtkinter():
    ctk = types.ModuleType("customtkinter")
    ctk.CTkFont = CTkFont
    ctk.AppearanceModeTracker = None

    for name in CTK_OPTIONS:
        base = Tk if name == "CTk" else Toplevel if name == "CTkToplevel" else CTkWidget
        setattr(ctk, name, type(name, (base,), {}))

    ctk.CTkToplevel.__bases__ = (Toplevel, CTkWidget)

    state = {"appearance_mode": "dark", "theme": None, "scaling": 1.0}

    def set_appearance_mode(mode):
        state["appearance_mode"] = mode
        CALLS.append(("set_appearance_mode", (mode,)))

    def get_appearance_mode():
        return state["appearance_mode"]

    def set_default_color_theme(path):
        state["theme"] = path
        CALLS.append(("set_default_color_theme", (path,)))

    def set_widget_scaling(value):
        state["scaling"] = value

    def set_window_scaling(value):
        state["scaling"] = value

    def deactivate_automatic_dpi_awareness():
        CALLS.append(("deactivate_automatic_dpi_awareness", ()))

    ctk.set_appearance_mode = set_appearance_mode
    ctk.get_appearance_mode = get_appearance_mode
    ctk.set_default_color_theme = set_default_color_theme
    ctk.set_widget_scaling = set_widget_scaling
    ctk.set_window_scaling = set_window_scaling
    ctk.deactivate_automatic_dpi_awareness = deactivate_automatic_dpi_awareness
    ctk.state = state

    sys.modules["customtkinter"] = ctk
    return ctk


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
    _install_customtkinter()
    return tk
