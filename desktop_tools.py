import ctypes
import json
import os
import re
from ctypes import wintypes

from langchain_core.tools import tool
from pywinauto import Desktop

MAX_CONTROLS = 150
MAX_TYPE_CHARS = 2000

RISKY_CONTROL_NAMES = {
    "close", "exit", "quit", "send", "submit", "delete", "remove",
    "don't save", "dont save", "discard", "pay", "purchase", "buy",
    "place order", "sign out", "log out", "uninstall", "format",
}


def _find_window(title_contains: str):
    """Return exactly one visible window whose title contains the text."""
    desktop = Desktop(backend="uia")
    matches = desktop.windows(title_re=f".*{re.escape(title_contains)}.*")

    if not matches:
        raise ValueError(
            f"No window found with title containing: {title_contains!r}. "
            "Use list_desktop_windows to see available windows."
        )

    if len(matches) > 1:
        titles = [m.window_text() for m in matches][:10]
        raise ValueError(
            f"{len(matches)} windows match {title_contains!r}: {titles}. "
            "Use a more specific title fragment."
        )

    return matches[0]


def _escape_keys(text: str) -> str:
    """Escape characters pywinauto's type_keys treats as key codes."""
    return "".join("{" + c + "}" if c in "+^%~(){}" else c for c in text)


@tool
def list_desktop_windows() -> str:
    """List visible desktop windows and their titles."""
    user32 = ctypes.windll.user32

    # Explicit types so 64-bit handles are passed correctly.
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int
    ]
    user32.GetWindowTextW.restype = ctypes.c_int

    windows = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value.strip()
                if title:
                    windows.append({"title": title, "handle": int(hwnd)})
        return True

    callback_ref = EnumWindowsProc(callback)
    user32.EnumWindows(callback_ref, 0)

    return json.dumps(windows, ensure_ascii=False)


@tool
def inspect_window(title_contains: str) -> str:
    """Inspect named UI controls in a visible window matching its title."""
    window = _find_window(title_contains)
    controls = []
    truncated = False

    for control in window.descendants():
        try:
            info = control.element_info
            name = info.name or ""
            automation_id = info.automation_id or ""

            # Unnamed, id-less controls are layout noise and waste context.
            if not name and not automation_id:
                continue

            if len(controls) >= MAX_CONTROLS:
                truncated = True
                break

            controls.append({
                "type": info.control_type or "",
                "name": name,
                "automation_id": automation_id,
                "class_name": info.class_name or "",
                "enabled": str(control.is_enabled()),
                "visible": str(control.is_visible()),
            })
        except Exception:
            continue

    result = {"window": window.window_text(), "controls": controls}
    if truncated:
        result["note"] = f"Truncated to the first {MAX_CONTROLS} controls."

    return json.dumps(result, ensure_ascii=False)


@tool
def focus_window(title_contains: str) -> str:
    """Bring a window matching the given title fragment to the foreground."""
    window = _find_window(title_contains)

    try:
        if window.is_minimized():
            window.restore()
    except Exception:
        pass

    window.set_focus()
    return f"Focused window: {window.window_text()}"


@tool
def click_control(
    title_contains: str,
    control_name: str,
    control_type: str = "Button",
) -> str:
    """Click one uniquely identified, visible, enabled control."""
    if (
        control_name.strip().casefold() in RISKY_CONTROL_NAMES
        and os.getenv("ALLOW_RISKY_DESKTOP_ACTIONS") != "1"
    ):
        raise PermissionError(
            f"Clicking {control_name!r} is blocked because it may be a "
            "consequential action. Ask the user to authorize it "
            "(operator must set ALLOW_RISKY_DESKTOP_ACTIONS=1)."
        )

    window = _find_window(title_contains)

    controls = []
    for control in window.descendants():
        try:
            info = control.element_info
            if (
                info.name == control_name
                and info.control_type == control_type
                and control.is_visible()
                and control.is_enabled()
            ):
                controls.append(control)
        except Exception:
            continue

    if len(controls) != 1:
        raise ValueError(
            f"Expected exactly one matching control, found {len(controls)}. "
            "Run inspect_window to confirm the exact name and type."
        )

    controls[0].click_input()
    return f"Clicked {control_type}: {control_name}"


@tool
def type_text(title_contains: str, text: str, control_name: str) -> str:
    """Type text into one uniquely identified text-entry control."""
    if len(text) > MAX_TYPE_CHARS:
        raise ValueError(f"Text exceeds the {MAX_TYPE_CHARS}-character limit.")

    window = _find_window(title_contains)
    window.set_focus()

    controls = []
    candidates = []
    requested = control_name.strip().casefold()

    for control in window.descendants():
        try:
            info = control.element_info

            if (
                info.control_type in ("Edit", "Document")
                and control.is_visible()
                and control.is_enabled()
            ):
                name = (info.name or "").strip()
                class_name = (info.class_name or "").strip()
                automation_id = (info.automation_id or "").strip()

                candidates.append({
                    "name": name,
                    "type": info.control_type or "",
                    "class_name": class_name,
                    "automation_id": automation_id,
                })

                if requested in {
                    name.casefold(),
                    class_name.casefold(),
                    automation_id.casefold(),
                }:
                    controls.append(control)
        except Exception:
            continue

    if len(controls) != 1:
        raise ValueError(
            f"Expected exactly one matching text-entry control, "
            f"found {len(controls)}. Requested: {control_name!r}. "
            f"Available controls: {candidates[:10]}"
        )

    editor = controls[0]
    editor.click_input()
    editor.set_focus()

    editor.type_keys(
        _escape_keys(text),
        with_spaces=True,
        with_newlines=True,
        with_tabs=True,
        set_foreground=True,
        pause=0.01,
    )

    return (
        f"Typed {len(text)} characters into "
        f"'{editor.element_info.name or control_name}'. "
        "Call verify_desktop_text to confirm."
    )


@tool
def verify_desktop_text(title_contains: str, expected_text: str) -> str:
    """Check expected text using accessible control values and names."""
    if not expected_text.strip():
        raise ValueError("Expected text cannot be empty.")

    window = _find_window(title_contains)
    observed_text = []

    for control in window.descendants():
        try:
            if not control.is_visible():
                continue

            info = control.element_info

            if info.name:
                observed_text.append(info.name)

            try:
                value = control.get_value()
                if value:
                    observed_text.append(value)
            except Exception:
                pass

            try:
                text = control.window_text()
                if text:
                    observed_text.append(text)
            except Exception:
                pass
        except Exception:
            continue

    combined = "\n".join(observed_text)

    if expected_text.casefold() in combined.casefold():
        return f"VERIFIED: found expected text: {expected_text}"

    return (
        "NOT VERIFIED: the expected text was not exposed through "
        "the available UI Automation properties."
    )
