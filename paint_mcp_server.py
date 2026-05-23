"""
Paint MCP server — config, drawing backends, and FastMCP tools.

Run server (stdio):
    python paint_mcp_server.py

Show backend / canvas settings:
    python paint_mcp_server.py --info

Tools (call in order):
  1. open_paint
  2. draw_rectangle
  3. add_text_in_paint
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from mcp.server.fastmcp import FastMCP

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DRAW_BACKEND: str = os.getenv("DRAW_BACKEND", "auto")
MONITOR_INDEX: int = int(os.getenv("PAINT_MONITOR_INDEX", "1"))

CANVAS_WIDTH: int = int(os.getenv("CANVAS_WIDTH", "900"))
CANVAS_HEIGHT: int = int(os.getenv("CANVAS_HEIGHT", "600"))
CANVAS_BACKGROUND: str = os.getenv("CANVAS_BACKGROUND", "white")
RECT_OUTLINE_COLOR: str = os.getenv("RECT_OUTLINE_COLOR", "black")
RECT_OUTLINE_WIDTH: int = int(os.getenv("RECT_OUTLINE_WIDTH", "3"))
TEXT_COLOR: str = os.getenv("TEXT_COLOR", "black")
FONT_SIZE: int = int(os.getenv("FONT_SIZE", "22"))
FONT_PATH: str = os.getenv("FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
WSL_OPEN_IN_PAINT: bool = os.getenv("WSL_OPEN_IN_PAINT", "true").lower() in (
    "1",
    "true",
    "yes",
)
LINUX_OPEN_VIEWER: bool = os.getenv("LINUX_OPEN_VIEWER", "true").lower() in (
    "1",
    "true",
    "yes",
)

RECT_X1: int = int(os.getenv("PAINT_RECT_X1", "100"))
RECT_Y1: int = int(os.getenv("PAINT_RECT_Y1", "80"))
RECT_X2: int = int(os.getenv("PAINT_RECT_X2", "780"))
RECT_Y2: int = int(os.getenv("PAINT_RECT_Y2", "380"))
TEXT_X: int = int(os.getenv("PAINT_TEXT_X", "120"))
TEXT_Y: int = int(os.getenv("PAINT_TEXT_Y", "180"))

PAINT_WINDOW_X: int = int(os.getenv("PAINT_WINDOW_X", "40"))
PAINT_WINDOW_Y: int = int(os.getenv("PAINT_WINDOW_Y", "40"))
PAINT_WINDOW_WIDTH: int = int(os.getenv("PAINT_WINDOW_WIDTH", "1280"))
PAINT_WINDOW_HEIGHT: int = int(os.getenv("PAINT_WINDOW_HEIGHT", "820"))
PAINT_LAUNCH_DELAY: float = float(os.getenv("PAINT_LAUNCH_DELAY", "2.0"))
PAINT_AUTO_CLOSE: bool = os.getenv("PAINT_AUTO_CLOSE", "true").lower() in (
    "1",
    "true",
    "yes",
)
PAINT_CLOSE_DELAY: float = float(os.getenv("PAINT_CLOSE_DELAY", "6"))
RECT_TOOL_X: int = int(os.getenv("PAINT_RECT_TOOL_X", "180"))
RECT_TOOL_Y: int = int(os.getenv("PAINT_RECT_TOOL_Y", "145"))
TEXT_TOOL_X: int = int(os.getenv("PAINT_TEXT_TOOL_X", "250"))
TEXT_TOOL_Y: int = int(os.getenv("PAINT_TEXT_TOOL_Y", "145"))


@dataclass(frozen=True)
class MonitorInfo:
    index: int
    left: int
    top: int
    width: int
    height: int

    @property
    def offset_x(self) -> int:
        return self.left

    @property
    def offset_y(self) -> int:
        return self.top


def _require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError(
            "Monitor detection requires Windows Python. "
            "On WSL2 use DRAW_BACKEND=wsl (default auto-detect)."
        )


def list_monitors() -> list[MonitorInfo]:
    _require_windows()
    import win32api

    monitors: list[MonitorInfo] = []
    for idx, (_handle, _device, rect) in enumerate(win32api.EnumDisplayMonitors()):
        left, top, right, bottom = rect
        monitors.append(
            MonitorInfo(
                index=idx,
                left=left,
                top=top,
                width=right - left,
                height=bottom - top,
            )
        )
    return monitors


def get_monitor(index: int | None = None) -> MonitorInfo:
    monitors = list_monitors()
    idx = MONITOR_INDEX if index is None else index
    if idx < 0 or idx >= len(monitors):
        raise IndexError(
            f"Monitor index {idx} out of range. Detected {len(monitors)} monitor(s)."
        )
    return monitors[idx]


def to_screen(x: int, y: int, monitor: MonitorInfo | None = None) -> tuple[int, int]:
    mon = monitor or get_monitor()
    return mon.offset_x + x, mon.offset_y + y


def to_screen_with_monitor(x: int, y: int, monitor: MonitorInfo) -> tuple[int, int]:
    return monitor.offset_x + x, monitor.offset_y + y


def schedule_close_paint() -> None:
    """Close MS Paint after PAINT_CLOSE_DELAY seconds — detached so it survives MCP exit."""
    if not PAINT_AUTO_CLOSE:
        return

    delay = max(1, int(PAINT_CLOSE_DELAY))
    kill_cmd = f"timeout /t {delay} /nobreak >nul && taskkill /IM mspaint.exe /F"

    try:
        if is_wsl():
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "/B", "cmd.exe", "/c", kill_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif os.name == "nt":
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "/B", "cmd.exe", "/c", kill_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        logger.info("Scheduled Paint auto-close in {}s (detached)", delay)
    except Exception as exc:
        logger.warning("Auto-close Paint failed: {}", exc)


# ---------------------------------------------------------------------------
# Drawing backends
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent
OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
CANVAS_FILE = OUTPUT_DIR / "canvas.png"


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", encoding="utf-8", errors="ignore") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def resolve_backend() -> str:
    explicit = os.getenv("DRAW_BACKEND", "auto").lower()
    if explicit in {"windows", "wsl", "linux"}:
        return explicit
    if os.name == "nt":
        return "windows"
    if is_wsl():
        return "wsl"
    return "linux"


class DrawBackend(ABC):
    name: str

    @abstractmethod
    def open_paint(self) -> str: ...

    @abstractmethod
    def draw_rectangle(
        self,
        x1: int | None = None,
        y1: int | None = None,
        x2: int | None = None,
        y2: int | None = None,
    ) -> str: ...

    @abstractmethod
    def add_text_in_paint(
        self,
        text: str,
        x: int | None = None,
        y: int | None = None,
    ) -> str: ...


class PillowCanvasMixin:
    def __init__(self) -> None:
        self._ready = False
        self._image = None
        self._draw = None

    def _ensure_pillow(self):
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401

    def _new_canvas(self):
        from PIL import Image, ImageDraw

        self._image = Image.new(
            "RGB",
            (CANVAS_WIDTH, CANVAS_HEIGHT),
            CANVAS_BACKGROUND,
        )
        self._draw = ImageDraw.Draw(self._image)
        self._ready = True

    def _save_canvas(self) -> Path:
        if self._image is None:
            raise RuntimeError("Canvas not initialized. Call open_paint first.")
        self._image.save(CANVAS_FILE)
        return CANVAS_FILE

    def _draw_rect(self, x1: int, y1: int, x2: int, y2: int) -> None:
        if self._draw is None:
            raise RuntimeError("Canvas not initialized. Call open_paint first.")
        self._draw.rectangle(
            [x1, y1, x2, y2],
            outline=RECT_OUTLINE_COLOR,
            width=RECT_OUTLINE_WIDTH,
        )

    def _draw_text(self, text: str, x: int, y: int) -> None:
        from PIL import ImageFont

        if self._draw is None:
            raise RuntimeError("Canvas not initialized. Call open_paint first.")
        font = ImageFont.load_default()
        try:
            font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
        except OSError:
            pass
        self._draw.multiline_text((x, y), text, fill=TEXT_COLOR, font=font)


class WslPaintBackend(PillowCanvasMixin, DrawBackend):
    name = "wsl"

    def _wsl_to_windows_path(self, path: Path) -> str:
        return subprocess.check_output(
            ["wslpath", "-w", str(path.resolve())],
            text=True,
        ).strip()

    def _open_in_windows_paint(self, path: Path) -> None:
        if not WSL_OPEN_IN_PAINT:
            return
        win_path = self._wsl_to_windows_path(path)
        logger.info("Opening in Windows Paint: {}", win_path)
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", "mspaint.exe", win_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(PAINT_LAUNCH_DELAY)

    def open_paint(self) -> str:
        self._ensure_pillow()
        self._new_canvas()
        path = self._save_canvas()
        # Canvas only — open Windows Paint once after add_text_in_paint (final step).
        msg = (
            f"WSL2 backend: canvas {CANVAS_WIDTH}x{CANVAS_HEIGHT} "
            f"ready at {path}."
        )
        logger.success(msg)
        return msg

    def draw_rectangle(
        self,
        x1: int | None = None,
        y1: int | None = None,
        x2: int | None = None,
        y2: int | None = None,
    ) -> str:
        if not self._ready:
            self.open_paint()
        rx1 = RECT_X1 if x1 is None else x1
        ry1 = RECT_Y1 if y1 is None else y1
        rx2 = RECT_X2 if x2 is None else x2
        ry2 = RECT_Y2 if y2 is None else y2
        self._draw_rect(rx1, ry1, rx2, ry2)
        path = self._save_canvas()
        msg = f"Drew rectangle on canvas ({rx1},{ry1}) -> ({rx2},{ry2}). Saved {path}."
        logger.success(msg)
        return msg

    def add_text_in_paint(
        self,
        text: str,
        x: int | None = None,
        y: int | None = None,
    ) -> str:
        if not text.strip():
            raise ValueError("text must not be empty")
        if not self._ready:
            self.open_paint()
        tx = TEXT_X if x is None else x
        ty = TEXT_Y if y is None else y
        self._draw_text(text, tx, ty)
        path = self._save_canvas()
        if WSL_OPEN_IN_PAINT:
            self._open_in_windows_paint(path)
            schedule_close_paint()
        msg = f"Added text at ({tx},{ty}): {text!r}. Final image: {path}"
        logger.success(msg)
        return msg


class LinuxCanvasBackend(PillowCanvasMixin, DrawBackend):
    name = "linux"

    def open_paint(self) -> str:
        self._ensure_pillow()
        self._new_canvas()
        path = self._save_canvas()
        msg = f"Linux backend: canvas ready at {path}"
        logger.success(msg)
        return msg

    def draw_rectangle(
        self,
        x1: int | None = None,
        y1: int | None = None,
        x2: int | None = None,
        y2: int | None = None,
    ) -> str:
        if not self._ready:
            self.open_paint()
        rx1 = RECT_X1 if x1 is None else x1
        ry1 = RECT_Y1 if y1 is None else y1
        rx2 = RECT_X2 if x2 is None else x2
        ry2 = RECT_Y2 if y2 is None else y2
        self._draw_rect(rx1, ry1, rx2, ry2)
        path = self._save_canvas()
        return f"Drew rectangle ({rx1},{ry1}) -> ({rx2},{ry2}) on {path}."

    def add_text_in_paint(
        self,
        text: str,
        x: int | None = None,
        y: int | None = None,
    ) -> str:
        if not text.strip():
            raise ValueError("text must not be empty")
        if not self._ready:
            self.open_paint()
        tx = TEXT_X if x is None else x
        ty = TEXT_Y if y is None else y
        self._draw_text(text, tx, ty)
        path = self._save_canvas()
        if LINUX_OPEN_VIEWER and shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", str(path)])
        return f"Added text {text!r} on {path}."


class WindowsPaintBackend(DrawBackend):
    name = "windows"

    def __init__(self) -> None:
        self._ready = False

    def _require_windows_modules(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows backend requires Windows Python (os.name=nt).")
        import pyautogui  # noqa: F401
        import win32con  # noqa: F401
        import win32gui  # noqa: F401
        from pywinauto import Application  # noqa: F401

    def _focus_paint_window(self) -> None:
        import win32con
        import win32gui

        handles: list[int] = []

        def callback(hwnd, lst):
            title = win32gui.GetWindowText(hwnd)
            if title and "Paint" in title and win32gui.IsWindowVisible(hwnd):
                lst.append(hwnd)
            return True

        win32gui.EnumWindows(callback, handles)
        if not handles:
            raise RuntimeError("Paint window not found. Call open_paint first.")
        hwnd = handles[-1]
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            logger.warning("SetForegroundWindow failed; continuing.")
        time.sleep(0.4)

    def open_paint(self) -> str:
        self._require_windows_modules()
        from pywinauto import Application

        monitor = get_monitor()
        subprocess.Popen(["mspaint.exe"])
        time.sleep(PAINT_LAUNCH_DELAY)
        app = Application(backend="uia").connect(title_re=".*Paint.*", timeout=15)
        window = app.top_window()
        window.set_focus()
        window.move_window(
            x=monitor.offset_x + PAINT_WINDOW_X,
            y=monitor.offset_y + PAINT_WINDOW_Y,
            width=PAINT_WINDOW_WIDTH,
            height=PAINT_WINDOW_HEIGHT,
            repaint=True,
        )
        time.sleep(0.8)
        self._focus_paint_window()
        self._ready = True
        msg = (
            f"Paint opened on monitor {monitor.index} "
            f"({monitor.width}x{monitor.height}) and focused."
        )
        logger.success(msg)
        return msg

    def draw_rectangle(
        self,
        x1: int | None = None,
        y1: int | None = None,
        x2: int | None = None,
        y2: int | None = None,
    ) -> str:
        self._require_windows_modules()
        import pyautogui

        if not self._ready:
            logger.warning("draw_rectangle before open_paint; focusing Paint anyway.")
        self._focus_paint_window()
        monitor = get_monitor()
        rx1 = RECT_X1 if x1 is None else x1
        ry1 = RECT_Y1 if y1 is None else y1
        rx2 = RECT_X2 if x2 is None else x2
        ry2 = RECT_Y2 if y2 is None else y2
        tool_x, tool_y = to_screen(RECT_TOOL_X, RECT_TOOL_Y, monitor)
        start_x, start_y = to_screen(rx1, ry1, monitor)
        end_x, end_y = to_screen(rx2, ry2, monitor)
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
        pyautogui.click(tool_x, tool_y)
        time.sleep(0.4)
        pyautogui.moveTo(start_x, start_y, duration=0.2)
        pyautogui.mouseDown(button="left")
        pyautogui.moveTo(end_x, end_y, duration=0.35)
        pyautogui.mouseUp(button="left")
        msg = f"Drew rectangle from ({start_x},{start_y}) to ({end_x},{end_y})."
        logger.success(msg)
        return msg

    def add_text_in_paint(
        self,
        text: str,
        x: int | None = None,
        y: int | None = None,
    ) -> str:
        self._require_windows_modules()
        import pyautogui

        if not text.strip():
            raise ValueError("text must not be empty")
        self._focus_paint_window()
        monitor = get_monitor()
        tx = TEXT_X if x is None else x
        ty = TEXT_Y if y is None else y
        tool_x, tool_y = to_screen(TEXT_TOOL_X, TEXT_TOOL_Y, monitor)
        click_x, click_y = to_screen(tx, ty, monitor)
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.05
        pyautogui.click(tool_x, tool_y)
        time.sleep(0.4)
        pyautogui.click(click_x, click_y)
        time.sleep(0.3)
        pyautogui.write(text, interval=0.04)
        schedule_close_paint()
        msg = f"Added text at ({click_x},{click_y}): {text!r}"
        logger.success(msg)
        return msg


_backend: DrawBackend | None = None


def get_backend() -> DrawBackend:
    global _backend
    if _backend is None:
        name = resolve_backend()
        if name == "windows":
            _backend = WindowsPaintBackend()
        elif name == "wsl":
            _backend = WslPaintBackend()
        else:
            _backend = LinuxCanvasBackend()
        logger.info(
            "Draw backend={} platform={} python={}",
            _backend.name,
            platform.platform(),
            sys.version.split()[0],
        )
    return _backend


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)
logger.remove()
logger.add(sys.stderr, level="INFO")
logger.add(LOG_DIR / "paint_mcp_server.log", rotation="1 MB", retention=10, enqueue=False)

mcp = FastMCP("PaintAutomationServer")


@mcp.tool()
def open_paint() -> str:
    """Launch the drawing app (Paint on WSL/Windows) and prepare the canvas."""
    return get_backend().open_paint()


@mcp.tool()
def draw_rectangle(
    x1: int | None = None,
    y1: int | None = None,
    x2: int | None = None,
    y2: int | None = None,
) -> str:
    """Draw a rectangle on the canvas. On WSL2 uses Pillow; on Windows clicks in Paint."""
    return get_backend().draw_rectangle(x1, y1, x2, y2)


@mcp.tool()
def add_text_in_paint(
    text: str,
    x: int | None = None,
    y: int | None = None,
) -> str:
    """Write text inside the rectangle. Pass the user's question as `text`."""
    return get_backend().add_text_in_paint(text, x, y)


def _print_config_info() -> None:
    backend = resolve_backend()
    print(f"Detected backend: {backend}")
    print(f"WSL: {is_wsl()} | os.name: {os.name}")
    print(f"Canvas: {CANVAS_WIDTH}x{CANVAS_HEIGHT}")
    print(f"Rectangle: ({RECT_X1},{RECT_Y1}) -> ({RECT_X2},{RECT_Y2})")
    print(f"Text anchor: ({TEXT_X},{TEXT_Y})")
    if backend == "windows":
        try:
            for mon in list_monitors():
                mark = " <-- active" if mon.index == MONITOR_INDEX else ""
                print(
                    f"Monitor {mon.index}: origin=({mon.left},{mon.top}) "
                    f"size={mon.width}x{mon.height}{mark}"
                )
        except RuntimeError as exc:
            print(exc)


if __name__ == "__main__":
    if "--info" in sys.argv:
        _print_config_info()
    else:
        logger.info(
            "Starting Paint MCP server | backend={} | monitor_index={}",
            resolve_backend(),
            MONITOR_INDEX,
        )
        mcp.run()
