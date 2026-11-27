import tkinter as tk
import winreg
from tkinter import ttk

import customtkinter as ctk


APP_NAME = "Cloudian Support Translate"


THEME_LABELS = {"system": "시스템 설정", "dark": "다크 모드", "light": "라이트 모드"}


BODY_FONT = ("Malgun Gothic", 12)


CHOICE_HOVER = ("#dbeafe", "#355170")


THEME_COLORS = {
    "light": {
        "background": "#f1f2f4",
        "card": "#ffffff",
        "text": "#21252d",
        "muted": "#565f6e",
        "status": "#edf0f2",
        "connected": "#236548",
        "pending": "#515a6a",
        "button": "#e9ecf0",
        "button_border": "#d7dbe1",
        "button_text": "#323a47",
        "hover": "#e8eaef",
        "primary": "#285a9a",
        "primary_hover": "#214b81",
        "divider": "#dce0e5",
        "input_border": "#c8cdd5",
        "table_border": "#e4e6ea",
        "table_header": "#f0f2f4",
        "table_header_text": "#495363",
        "table_selected": "#d9e9fc",
        "table_selected_text": "#163d70",
        "table_hover": "#e8eaef",
        "input": "#ffffff",
        "table_alternate": "#fafafb",
    },
    "dark": {
        "background": "#15171c",
        "card": "#20232a",
        "text": "#eef0f4",
        "muted": "#aab1bf",
        "status": "#292e37",
        "connected": "#a6d6bd",
        "pending": "#c4cfdf",
        "button": "#2a2e37",
        "button_border": "#3b424f",
        "button_text": "#dce1ea",
        "hover": "#333a46",
        "primary": "#3264ab",
        "primary_hover": "#3d70bd",
        "divider": "#353b46",
        "input_border": "#424a58",
        "table_border": "#353b46",
        "table_header": "#282c35",
        "table_header_text": "#c8cfdb",
        "table_selected": "#2a4568",
        "table_selected_text": "#eef5ff",
        "table_hover": "#303641",
        "input": "#191c22",
        "table_alternate": "#242830",
    },
}


def theme_color(role):
    return THEME_COLORS["light"][role], THEME_COLORS["dark"][role]


def windows_appearance(*, taskbar=False):
    setting = "SystemUsesLightTheme" if taskbar else "AppsUseLightTheme"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return "light" if winreg.QueryValueEx(key, setting)[0] else "dark"
    except OSError:
        return "light"


class FieldComboBox(ctk.CTkComboBox):
    def __init__(self, parent, *, open_choices, readonly=False, **kwargs):
        self.open_choices = open_choices
        self.enabled_state = "readonly" if readonly else "normal"
        super().__init__(parent, state=self.enabled_state, **kwargs)
        self.bind("<Button-1>", self.show_choices)
        self.bind("<Alt-Down>", self.show_choices)
        self.bind("<F4>", self.show_choices)
        for tag in ("right_parts", "dropdown_arrow"):
            self._canvas.tag_unbind(tag, "<Button-1>")
        self._canvas.bind("<Button-1>", self.show_choices)
        self.update_choice_cursor()

    def configure(self, require_redraw=False, **kwargs):
        super().configure(require_redraw=require_redraw, **kwargs)
        if "state" in kwargs or "values" in kwargs:
            self.update_choice_cursor()

    def _on_leave(self, event=0):
        super()._on_leave(event)
        self.update_choice_cursor()

    def update_choice_cursor(self):
        state = self.cget("state")
        clickable = state != "disabled" and bool(self.cget("values"))
        cursor = "hand2" if clickable else "arrow"
        self._canvas.configure(cursor=cursor)
        self._entry.configure(cursor="xterm" if not clickable and state == "normal" else cursor)

    def show_choices(self, _event=None):
        if self.cget("state") == "disabled":
            return "break"
        values = self.cget("values")
        if values:
            self.open_choices(self, values, self.get(), self.set, restore_focus=self.focus_set)
            return "break"


class _ThemeWidgets:
    def theme_choice(self):
        return next((key for key, label in THEME_LABELS.items() if label == self.appearance.get()), "system")

    def update_appearance(self, _choice=None):
        theme = self.theme_choice()
        if theme == "system":
            theme = windows_appearance()
        if getattr(self, "active_theme", None) == theme:
            return
        self.active_theme = theme
        colors = THEME_COLORS[theme]
        ctk.set_appearance_mode(theme)
        style = ttk.Style(self.root)
        style.configure("TSpinbox", fieldbackground=colors["input"], foreground=colors["text"],
                        insertcolor=colors["text"], arrowcolor=colors["text"], bordercolor=colors["input_border"],
                        background=colors["button"], lightcolor=colors["input"], darkcolor=colors["input"],
                        font=("Malgun Gothic", 10), padding=(8, 8))
        style.map("TSpinbox", bordercolor=[("focus", colors["primary"])])
        style.configure("Cell.TEntry", fieldbackground=colors["input"], foreground=colors["text"],
                        insertcolor=colors["text"], bordercolor=colors["primary"])
        row_height = max(27, self.row_font.metrics("linespace") + 8)
        style.configure("Ledger.Treeview", font=self.row_font, rowheight=row_height, background=colors["card"],
                        fieldbackground=colors["card"], foreground=colors["text"], borderwidth=0, relief="flat",
                        bordercolor=colors["table_border"], lightcolor=colors["table_border"], darkcolor=colors["table_border"])
        style.configure("Ledger.Treeview.Heading", font=self.heading_font, padding=(8, 6), relief="flat",
                        background=colors["table_header"], foreground=colors["table_header_text"], bordercolor=colors["table_border"],
                        lightcolor=colors["table_header"], darkcolor=colors["table_header"])
        style.map("Ledger.Treeview", background=[("selected", colors["table_selected"])],
                  foreground=[("selected", colors["table_selected_text"])])
        style.map("Ledger.Treeview.Heading", background=[("active", colors["table_hover"])])
        if hasattr(self, "viewport"):
            self.viewport.configure(background=colors["background"])
            self.table.tag_configure("alternate", background=colors["table_alternate"])
        windows = [self.root]
        while windows:
            for child in windows.pop().winfo_children():
                if isinstance(child, tk.Listbox):
                    child.configure(background=colors["input"], foreground=colors["text"],
                                    selectbackground=colors["table_selected"], selectforeground=colors["table_selected_text"],
                                    highlightbackground=colors["divider"])
                windows.append(child)

    def watch_appearance(self):
        if not self.closing:
            self.update_appearance()
            tray_theme = windows_appearance(taskbar=True)
            if tray_theme != self.tray_theme:
                self.tray.icon = self.logos[tray_theme]
                self.tray_theme = tray_theme
            self.root.after(2_000, self.watch_appearance)

    def frame(self, parent, *, surface=None, corner_radius=0, **kwargs):
        return ctk.CTkFrame(parent, fg_color=theme_color(surface) if surface else "transparent",
                            corner_radius=corner_radius, **kwargs)

    def divider(self, parent):
        return self.frame(parent, surface="divider", height=1, corner_radius=0)

    def label(self, parent, *, muted=False, heading=False, title=False, **kwargs):
        font = ("Malgun Gothic", 18 if title else 14, "bold") if heading or title else BODY_FONT
        kwargs.setdefault("text_color", theme_color("muted" if muted else "text"))
        return ctk.CTkLabel(parent, font=font, anchor="w", justify="left", height=22, **kwargs)

    def button(self, parent, *, primary=False, width=None, **kwargs):
        return ctk.CTkButton(
            parent, width=width if width is not None else (108 if primary else 52), height=32, corner_radius=8,
            font=("Malgun Gothic", 12, "bold") if primary else BODY_FONT,
            fg_color=theme_color("primary" if primary else "button"),
            hover_color=theme_color("primary_hover" if primary else "hover"),
            border_width=0 if primary else 1, border_color=theme_color("button_border"),
            text_color="white" if primary else theme_color("button_text"),
            text_color_disabled=("#dbe3ec", "#c4cfdf") if primary else theme_color("muted"), **kwargs,
        )

    def entry(self, parent, **kwargs):
        return ctk.CTkEntry(parent, height=32, corner_radius=7, border_width=1, font=BODY_FONT,
                            fg_color=theme_color("input"), border_color=theme_color("input_border"),
                            text_color=theme_color("text"), **kwargs)

    def dropdown_colors(self):
        return {
            "font": BODY_FONT,
            "dropdown_font": BODY_FONT,
            "text_color": theme_color("text"),
            "button_color": theme_color("button"),
            "button_hover_color": theme_color("hover"),
            "dropdown_fg_color": theme_color("card"),
            "dropdown_hover_color": theme_color("table_selected"),
            "dropdown_text_color": theme_color("text"),
        }

    def menu(self, parent, *, values, **kwargs):
        return ctk.CTkOptionMenu(parent, values=list(values), height=32, corner_radius=7,
                                 dynamic_resizing=False, fg_color=theme_color("button"),
                                 **self.dropdown_colors(), **kwargs)

    def combo(self, parent, variable, values, *, readonly=False):
        return FieldComboBox(parent, open_choices=self.open_choice_menu, readonly=readonly, variable=variable,
                             values=values, height=32, corner_radius=7, border_width=1,
                             fg_color=theme_color("input"), border_color=theme_color("input_border"),
                             **self.dropdown_colors())

    def scrollbar(self, parent, **kwargs):
        return ctk.CTkScrollbar(parent, fg_color="transparent", button_color=theme_color("input_border"),
                                 button_hover_color=theme_color("muted"), **kwargs)

    def switch(self, parent, **kwargs):
        return ctk.CTkSwitch(parent, width=0, font=BODY_FONT, text_color=theme_color("text"),
                              fg_color=theme_color("input_border"), progress_color=theme_color("primary"),
                              button_color=("#ffffff", "#e7eaf0"), **kwargs)

    def open_theme_menu(self, button):
        def apply_theme(label):
            self.appearance.set(label)
            self.update_appearance()

        self.open_choice_menu(button, list(THEME_LABELS.values()), self.appearance.get(), apply_theme,
                              mark_current=True)

    def open_choice_menu(self, button, labels, value, on_select, *, mark_current=False, restore_focus=None):
        colors = THEME_COLORS[self.active_theme]
        hover_color = CHOICE_HOVER[self.active_theme == "dark"]
        popup = tk.Toplevel(self.root)
        popup.withdraw()
        popup.transient(self.root)
        popup.overrideredirect(True)
        scale = ctk.ScalingTracker.get_widget_scaling(button)
        choices = tk.Listbox(popup, height=min(8, len(labels)), font=(BODY_FONT[0], -round(BODY_FONT[1] * scale)),
                             background=colors["card"], foreground=colors["text"],
                             selectbackground=hover_color, selectforeground=colors["table_selected_text"],
                             highlightbackground=colors["input_border"], highlightcolor=colors["input_border"],
                             highlightthickness=1, borderwidth=0, selectborderwidth=0, activestyle="none",
                             exportselection=False)
        if len(labels) > 8:
            scrollbar = self.scrollbar(popup, height=0, command=choices.yview)
            scrollbar.pack(side="right", fill="y")
            choices.configure(yscrollcommand=scrollbar.set)
        choices.pack(side="left", fill="both", expand=True)
        current = labels.index(value) if value in labels else 0
        choices.insert("end", *(
            ((" ✓  " if index == current else "      ") if mark_current else "  ") + label
            for index, label in enumerate(labels)
        ))
        choices.itemconfigure(current, background=colors["table_alternate"])
        choices.activate(current)
        if not mark_current:
            choices.selection_set(current)
            choices.see(current)
        hovered = None

        def highlight(index):
            nonlocal hovered
            if hovered == index:
                return
            if hovered is not None:
                background = colors["table_alternate"] if hovered == current else colors["card"]
                choices.itemconfigure(hovered, background=background)
            hovered = index
            if index is not None:
                choices.selection_clear(0, "end")
                choices.itemconfigure(index, background=hover_color)

        def close():
            popup.destroy()
            if restore_focus:
                restore_focus()
            return "break"

        def select(index):
            close()
            on_select(labels[index])
            return "break"

        def dismiss_outside(event):
            if not (popup.winfo_rootx() <= event.x_root < popup.winfo_rootx() + popup.winfo_width()
                    and popup.winfo_rooty() <= event.y_root < popup.winfo_rooty() + popup.winfo_height()):
                popup.destroy()
                return "break"

        def click_choice(event):
            if 0 <= event.x < choices.winfo_width() and 0 <= event.y < choices.winfo_height():
                return select(choices.nearest(event.y))

        choices.bind("<Motion>", lambda event: highlight(choices.nearest(event.y)))
        choices.bind("<Leave>", lambda _: highlight(None))
        choices.bind("<ButtonRelease-1>", click_choice)
        choices.bind("<Return>", lambda _: select(choices.index("active")))
        choices.bind("<FocusOut>", lambda _: popup.destroy())
        popup.bind("<ButtonPress-1>", dismiss_outside)
        popup.bind("<Escape>", lambda _: close())
        popup.update_idletasks()
        x = button.winfo_rootx()
        gap = round(3 * scale)
        height = choices.winfo_reqheight()
        y = button.winfo_rooty() + button.winfo_height() + gap
        above = button.winfo_rooty() - height - gap
        if y + height > self.root.winfo_rooty() + self.root.winfo_height() and above >= self.root.winfo_rooty():
            y = above
        popup.geometry(f"{button.winfo_width()}x{height}+{x}+{y}")
        popup.deiconify()
        popup.grab_set()
        choices.focus_force()

    def card(self, parent, title, row, column, *, colspan=1):
        shell = self.frame(parent, surface="card", corner_radius=12,
                           border_width=1, border_color=theme_color("divider"))
        shell.grid(row=row, column=column, columnspan=colspan, sticky="new",
                   padx=(16, 6) if row and not column else (6, 16) if column else 16,
                   pady=(8, 9) if not row else (0, 8))
        panel = self.frame(shell)
        panel.pack(fill="both", expand=True, padx=14, pady=9)
        panel.columnconfigure(0, weight=0 if row else 1)
        panel.columnconfigure(1, weight=1 if row else 0)
        self.label(panel, text=title, heading=True).grid(row=0, column=0, columnspan=2, sticky="w")
        return panel, shell

    def field(self, parent, caption, widget, row, *, span=1, pady=4):
        self.label(parent, text=caption).grid(row=row, column=0, sticky="w", pady=pady, padx=(0, 12))
        widget.grid(row=row, column=1, columnspan=span, sticky="ew", pady=pady)

    def scrollable_list(self, panel, widget):
        scrollbar = self.scrollbar(panel, command=widget.yview)
        scrollbar.pack(side="right", fill="y", padx=(4, 0))
        widget.configure(yscrollcommand=scrollbar.set)
        widget.pack(side="left", fill="both", expand=True)
