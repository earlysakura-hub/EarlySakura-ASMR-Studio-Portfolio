"""Small GUI preferences, independent of works and subtitles."""
import json
import os
from pathlib import Path
import uuid

DEFAULT_PLOT_FONT_SIZE = 14
MIN_PLOT_FONT_SIZE = 10
MAX_PLOT_FONT_SIZE = 24
DEFAULT_LIBRARY_COLUMN_WIDTHS = {"title": 245, "rj": 115, "state": 75, "favorite": 45}
MIN_LIBRARY_COLUMN_WIDTH = 40
MAX_LIBRARY_COLUMN_WIDTH = 10000


class PreferencesError(ValueError):
    pass


class GuiPreferences:
    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError) as error:
            raise PreferencesError(f"阅读配置无法读取，原文件保留：{error}") from error
        if not isinstance(data, dict):
            raise PreferencesError("阅读配置应为 JSON 对象，原文件保留。")
        return data

    def plot_font_size(self):
        try:
            value = self._read().get("plot_font_size", DEFAULT_PLOT_FONT_SIZE)
        except PreferencesError:
            return DEFAULT_PLOT_FONT_SIZE
        return value if type(value) is int and MIN_PLOT_FONT_SIZE <= value <= MAX_PLOT_FONT_SIZE else DEFAULT_PLOT_FONT_SIZE

    def save_plot_font_size(self, size):
        if type(size) is not int or not MIN_PLOT_FONT_SIZE <= size <= MAX_PLOT_FONT_SIZE:
            raise PreferencesError("剧情字号必须为 10–24 pt 的整数。")
        # Read afresh to preserve other preferences, including fields unknown here.
        data = self._read()
        data["plot_font_size"] = size
        self._write(data)

    def library_column_widths(self):
        try:
            saved = self._read().get("library_column_widths", {})
        except PreferencesError:
            saved = {}
        if not isinstance(saved, dict): saved = {}
        return {key: saved[key] if self._valid_width(saved.get(key)) else default
                for key, default in DEFAULT_LIBRARY_COLUMN_WIDTHS.items()}

    @staticmethod
    def _valid_width(value):
        return type(value) is int and MIN_LIBRARY_COLUMN_WIDTH <= value <= MAX_LIBRARY_COLUMN_WIDTH

    def save_library_column_widths(self, widths):
        if (not isinstance(widths, dict) or widths.keys() != DEFAULT_LIBRARY_COLUMN_WIDTHS.keys()
                or not all(self._valid_width(value) for value in widths.values())):
            raise PreferencesError("媒体库列宽必须包含四列，每列为 40–10000 像素的整数。")
        data = self._read()  # Preserve the font size and any other settings.
        if data.get("library_column_widths") == widths: return
        data["library_column_widths"] = dict(widths)
        self._write(data)

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()  # Only this preference write's temporary file.
