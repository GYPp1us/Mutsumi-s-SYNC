from prompt_toolkit.styles import Style

SOLARIZED_DARK = {
    "bg": "#002b36",
    "fg": "#839496",
    "accent": "#2aa198",
    "warning": "#b58900",
    "error": "#dc322f",
    "success": "#859900",
    "status-bg": "#073642",
    "border": "#586e75",
}

STYLE = Style.from_dict({
    "output": f"bg:{SOLARIZED_DARK['bg']} fg:{SOLARIZED_DARK['fg']}",
    "status-bar": f"bg:{SOLARIZED_DARK['status-bg']} fg:{SOLARIZED_DARK['fg']}",
    "status-online": f"fg:{SOLARIZED_DARK['success']}",
    "status-offline": f"fg:{SOLARIZED_DARK['error']}",
    "prompt": f"fg:{SOLARIZED_DARK['accent']}",
    "input": f"fg:{SOLARIZED_DARK['fg']} bg:{SOLARIZED_DARK['bg']}",
    "completion-menu": f"bg:{SOLARIZED_DARK['status-bg']} fg:{SOLARIZED_DARK['fg']}",
    "completion-menu.selected": f"bg:{SOLARIZED_DARK['accent']} fg:{SOLARIZED_DARK['bg']}",
})