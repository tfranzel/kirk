import math
import re

from blessed import Terminal
from blessed.color import dist_cie2000, rgb_to_lab
from blessed.colorspace import X11_COLORNAMES_TO_RGB, RGBColor


def build_color_map() -> list[tuple[str, RGBColor]]:
    selection: list[tuple[str, RGBColor]] = []

    for name, rgb in X11_COLORNAMES_TO_RGB.items():
        skip = False
        luma, _, _ = rgb_to_lab(*rgb)

        if luma < 30:
            continue
        # skip if any already added color is too close
        for _, selected_rgb in selection:
            if math.sqrt(dist_cie2000(rgb, selected_rgb)) < 13:
                skip = True
        if not skip:
            selection.append((name, rgb))

    return selection


COLOR_LIST = build_color_map()


def name_to_rgb(name: str) -> RGBColor:
    return COLOR_LIST[sum(ord(c) for c in name) % len(COLOR_LIST)][1]


MIRC_FORMAT_MAPPING = {
    "\x02": "bold",
    "\x1d": "italic",
    "\x1e": "strikethrough",
    "\x1f": "underline",
    "\x0f": "normal",
}

MIRC_COLOR_MAPPING = {
    0: "white",
    1: "black",
    2: "navy",
    3: "green",
    4: "red",
    5: "maroon",
    6: "purple",
    7: "orange",
    8: "yellow",
    9: "lightgreen",
    10: "teal",
    11: "cyan",
    12: "royalblue",
    13: "magenta",
    14: "gray",
    15: "lightgray",
}

IRC_COLOR_RE = re.compile(r"\x03((?P<color_fg>\d{1,2})(,(?P<color_bg>\d{1,2}))?)?")
IRC_FORMAT_RE = re.compile(r"(?P<format>[\x02\x1d\x1e\x1f])(?P<text>.*?)(?P=format)")


def irc_to_ansi(text: str, term: Terminal) -> str:
    """
    Maps encountered MIRC color/formatting sequences (recycled ASCII control section)
    to ANSI sequences that can be displayed in terminal.

    https://modern.ircdocs.horse/formatting
    """
    for match in IRC_COLOR_RE.finditer(text):
        try:
            fg = MIRC_COLOR_MAPPING[int(match.group("color_fg"))]
        except (TypeError, KeyError):
            fg = None
        try:
            bg = MIRC_COLOR_MAPPING[int(match.group("color_bg"))]
        except (TypeError, KeyError):
            bg = None
        if fg and bg:
            color_fmt = getattr(term, f"{fg}_on_{bg}", term.normal)
        elif fg:
            color_fmt = getattr(term, fg, term.normal)
        else:
            color_fmt = term.normal
        text = text.replace(match.group(0), color_fmt, count=1)

    for match in IRC_FORMAT_RE.finditer(text):
        try:
            fmt_name = MIRC_FORMAT_MAPPING[match.group("format")]
        except (TypeError, KeyError):
            fmt_name = ""
        if fmt := getattr(term, fmt_name):
            text = text.replace(match.group(0), fmt(match.group("text")), count=1)

    return text + term.normal
