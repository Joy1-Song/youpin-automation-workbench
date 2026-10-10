from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import youpin_execution_bridge as command_queue

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


DEFAULT_AVD = "YouPin_X64_API35"
DEFAULT_PACKAGE = "com.uu898.uuhavequality"
DEFAULT_ACTIVITY = "com.uu898.uuhavequality/.module.start.SplashActivity"
DEFAULT_APK = Path("state/apk/uuhavequality_5.45.6_candidate.apk")
DEFAULT_COMMAND_FILE = Path("state/emulator_commands.json")
DEFAULT_STATUS_FILE = Path("state/emulator_status.json")
DEFAULT_CAPTURE_DIR = Path("state/emulator")
SUBPROCESS_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
TEXT_FILL_TITLE = "\u586b\u5199\u8d2d\u5165\u4ef7"
TEXT_BUY_PRICE_LABEL = "\u8d2d\u5165\u4ef7\u683c"
TEXT_BUY_PRICE_SHORT = "\u8d2d\u5165\u4ef7"
TEXT_CONFIRM = "\u786e\u5b9a"
TEXT_CONFIRM_BUY = "\u786e\u8ba4\u6c42\u8d2d"
TEXT_PUBLISH_BUY = "\u53d1\u5e03\u6c42\u8d2d"
TEXT_BUY_CONFIRM = "\u6c42\u8d2d\u786e\u8ba4"
TEXT_BUYING = "\u6c42\u8d2d\u4e2d"
TEXT_INVENTORY = "\u5e93\u5b58"
TEXT_SELL = "\u51fa\u552e"
TEXT_PURCHASE = "\u6c42\u8d2d"
TEXT_MY = "\u6211\u7684"
TEXT_RISE_FALL = "\u6da8\u8dcc"
TEXT_MARKET_PRICE = "\u5e02\u573a\u4ef7"
TEXT_NO_BUY_PRICE = "\u65e0\u8d2d\u5165\u4ef7"
TEXT_NO_BUY_PRICE_COUNT = "\u4ef6\u65e0\u8d2d\u5165\u4ef7"
TEXT_STICKER = "\u5370\u82b1"
TEXT_STICKER_BOARD = "\u5370\u82b1\u677f"
TEXT_TOTAL = "\u603b\u8ba1"


class EmulatorError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def android_sdk_root() -> Path:
    for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        value = os.environ.get(key)
        if value and Path(value).exists():
            return Path(value)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Android" / "Sdk"
        if candidate.exists():
            return candidate
    raise EmulatorError("Android SDK not found. Install Android Studio SDK first.")


def adb_path() -> Path:
    path = android_sdk_root() / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    if not path.exists():
        raise EmulatorError(f"adb not found: {path}")
    return path


def emulator_path() -> Path:
    path = android_sdk_root() / "emulator" / ("emulator.exe" if os.name == "nt" else "emulator")
    if not path.exists():
        raise EmulatorError(f"emulator not found: {path}")
    return path


def avd_root() -> Path:
    value = os.environ.get("ANDROID_AVD_HOME")
    if value and Path(value).exists():
        return Path(value)
    return Path.home() / ".android" / "avd"


def avd_dir(avd: str = DEFAULT_AVD) -> Path:
    ini_path = avd_root() / f"{avd}.ini"
    if ini_path.exists():
        for line in ini_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("path="):
                candidate = Path(line.split("=", 1)[1].strip())
                if candidate.exists():
                    return candidate
    return avd_root() / f"{avd}.avd"


def disable_avd_snapshots(avd: str = DEFAULT_AVD) -> None:
    directory = avd_dir(avd)
    if not directory.exists():
        return

    snapshots = directory / "snapshots"
    if snapshots.exists():
        resolved = snapshots.resolve()
        allowed = directory.resolve()
        if allowed not in resolved.parents:
            raise EmulatorError(f"Refusing to delete unexpected snapshot path: {resolved}")
        shutil.rmtree(resolved, ignore_errors=True)
    snapshots.mkdir(parents=True, exist_ok=True)

    quickboot = directory / "quickbootChoice.ini"
    quickboot.write_text("saveOnExit = false\n", encoding="utf-8")


def run(args: list[str | Path], *, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
    str_args = [str(arg) for arg in args]
    proc = subprocess.run(
        str_args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=SUBPROCESS_CREATIONFLAGS,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise EmulatorError(f"Command failed: {' '.join(str_args)}\n{detail}")
    return proc


def reset_adb_server() -> None:
    path = adb_path()
    run([path, "kill-server"], timeout=20, check=False)
    run([path, "start-server"], timeout=20, check=False)


def adb(*args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = run([adb_path(), *args], timeout=timeout, check=False)
    detail = f"{proc.stdout}\n{proc.stderr}".lower()
    if proc.returncode != 0 and any(marker in detail for marker in ("error: closed", "device offline", "device unauthorized")):
        reset_adb_server()
        proc = run([adb_path(), *args], timeout=timeout, check=False)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise EmulatorError(f"Command failed: {adb_path()} {' '.join(args)}\n{detail}")
    return proc


def boot_completed() -> bool:
    return adb("shell", "getprop", "sys.boot_completed", check=False).stdout.strip() == "1"


def device_lines() -> list[str]:
    output = adb("devices", "-l").stdout.splitlines()
    return [line for line in output if line.strip() and not line.startswith("List of devices")]


def online_device() -> str | None:
    for line in device_lines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return None


def launch_emulator(avd: str = DEFAULT_AVD) -> None:
    if online_device():
        return
    reset_adb_server()
    if online_device():
        return
    disable_avd_snapshots(avd)
    log_dir = Path("logs/emulator")
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = open(log_dir / f"{avd}_stdout.log", "a", encoding="utf-8")
    stderr = open(log_dir / f"{avd}_stderr.log", "a", encoding="utf-8")
    subprocess.Popen(
        [
            str(emulator_path()),
            "-avd",
            avd,
            "-no-window",
            "-gpu",
            "swiftshader_indirect",
            "-no-snapshot-save",
            "-no-boot-anim",
            "-no-metrics",
        ],
        stdout=stdout,
        stderr=stderr,
        creationflags=SUBPROCESS_CREATIONFLAGS,
    )


def wait_for_boot(timeout_seconds: int = 360) -> None:
    adb("wait-for-device", timeout=timeout_seconds)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if boot_completed():
            return
        time.sleep(5)
    raise EmulatorError("Emulator did not finish booting.")


def ensure_ready(avd: str = DEFAULT_AVD, apk: Path | None = None) -> None:
    if not online_device():
        launch_emulator(avd)
    if not boot_completed():
        wait_for_boot()
    if apk and apk.exists() and not app_installed():
        install_apk(apk)


def install_apk(apk: Path) -> None:
    if not apk.exists():
        raise EmulatorError(f"APK not found: {apk}")
    adb("install", "-r", str(apk), timeout=180)


def start_app(activity: str = DEFAULT_ACTIVITY) -> None:
    adb("shell", "am", "start", "-n", activity)
    time.sleep(5)


def load_commands(path: Path = DEFAULT_COMMAND_FILE) -> dict[str, Any]:
    return command_queue.load_command_file(path)


def write_commands(payload: dict[str, Any], path: Path = DEFAULT_COMMAND_FILE) -> None:
    command_queue.write_command_file(payload, path)


def pending_commands(path: Path = DEFAULT_COMMAND_FILE, command_type: str | set[str] | None = None) -> list[dict[str, Any]]:
    payload = load_commands(path)
    commands = payload.get("commands", [])
    if not isinstance(commands, list):
        return []
    if command_type is None:
        allowed: set[str] | None = None
    elif isinstance(command_type, str):
        allowed = {command_type}
    else:
        allowed = set(command_type)
    result = []
    for cmd in commands:
        if not isinstance(cmd, dict) or command_queue.command_status(cmd) not in {
            command_queue.COMMAND_STATUS_PENDING,
            command_queue.COMMAND_STATUS_RETRYING,
        }:
            continue
        if allowed is not None and str(cmd.get("type", "")) not in allowed:
            continue
        result.append(cmd)
    return result


def mark_command(
    path: Path,
    command_id: str,
    status: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    payload = load_commands(path)
    commands = payload.get("commands", [])
    if not isinstance(commands, list):
        return
    for command in commands:
        if not isinstance(command, dict) or str(command.get("id", "")) != str(command_id):
            continue
        command_queue.set_command_status(command, status, result=result, error=error)
        break
    write_commands(payload, path)


def write_status(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    status["updated_at"] = now_iso()
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def app_installed(package_name: str = DEFAULT_PACKAGE) -> bool:
    result = adb("shell", "pm", "list", "packages", package_name, check=False).stdout
    return f"package:{package_name}" in result


def package_version(package_name: str = DEFAULT_PACKAGE) -> dict[str, str]:
    output = adb("shell", "dumpsys", "package", package_name, check=False).stdout
    version: dict[str, str] = {}
    for key in ("versionName", "versionCode"):
        match = re.search(rf"{key}=([^\s]+)", output)
        if match:
            version[key] = match.group(1)
    return version


def capture(capture_dir: Path = DEFAULT_CAPTURE_DIR) -> dict[str, str]:
    capture_dir.mkdir(parents=True, exist_ok=True)
    remote_xml = "/sdcard/youpin_window.xml"
    remote_png = "/sdcard/youpin_screen.png"
    local_xml = capture_dir / "window.xml"
    local_png = capture_dir / "screenshot.png"
    last_error = ""
    for attempt in range(4):
        dump = adb("shell", "uiautomator", "dump", remote_xml, check=False)
        if dump.returncode != 0:
            exists = adb("shell", "ls", remote_xml, check=False)
            if exists.returncode != 0:
                last_error = (dump.stderr or dump.stdout or "").strip()
                time.sleep(0.8)
                continue
        adb("pull", remote_xml, str(local_xml), check=False)
        if local_xml.exists() and local_xml.stat().st_size > 0:
            break
        time.sleep(0.8 + attempt * 0.4)
    else:
        raise EmulatorError(f"Command failed: {adb_path()} shell uiautomator dump {remote_xml}\n{last_error}")
    adb("shell", "screencap", "-p", remote_png)
    adb("pull", remote_png, str(local_png))
    return {"xml": str(local_xml), "screenshot": str(local_png)}


def parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom = bounds
    return (left + right) // 2, (top + bottom) // 2


def find_node(xml_path: Path, *, resource_id: str | None = None, text_contains: str | None = None) -> ET.Element | None:
    nodes = find_nodes(xml_path, resource_id=resource_id, text_contains=text_contains)
    return nodes[0] if nodes else None


def find_nodes(
    xml_path: Path,
    *,
    resource_id: str | None = None,
    text_contains: str | None = None,
    class_name: str | None = None,
) -> list[ET.Element]:
    root = ET.parse(xml_path).getroot()
    matches: list[ET.Element] = []
    for node in root.iter("node"):
        if resource_id and node.attrib.get("resource-id") == resource_id:
            matches.append(node)
            continue
        if text_contains and text_contains in node.attrib.get("text", ""):
            matches.append(node)
            continue
        if class_name and node.attrib.get("class") == class_name:
            matches.append(node)
    return matches


def tap_node(node: ET.Element) -> None:
    bounds = parse_bounds(node.attrib.get("bounds", ""))
    if not bounds:
        raise EmulatorError("Node has no valid bounds.")
    x, y = center(bounds)
    adb("shell", "input", "tap", str(x), str(y))


def visible_bounds(node: ET.Element) -> tuple[int, int, int, int] | None:
    bounds = parse_bounds(node.attrib.get("bounds", ""))
    if not bounds:
        return None
    left, top, right, bottom = bounds
    if right <= left or bottom <= top:
        return None
    if right <= 0 or bottom <= 0 or left >= 1080 or top >= 2160:
        return None
    return bounds


def tap_bounds(bounds: tuple[int, int, int, int]) -> None:
    x, y = center(bounds)
    adb("shell", "input", "tap", str(x), str(y))


def visible_text_nodes(xml_path: Path, text_contains: str) -> list[ET.Element]:
    return [
        node
        for node in find_nodes(xml_path, text_contains=text_contains)
        if visible_bounds(node) is not None
    ]


def find_node_text_any(xml_path: Path, texts: tuple[str, ...]) -> ET.Element | None:
    for text in texts:
        node = find_node(xml_path, text_contains=text)
        if node is not None:
            return node
    return None


def clear_focused_text(current_text: str = "") -> None:
    adb("shell", "input", "keyevent", "123", check=False)  # KEYCODE_MOVE_END
    for _ in range(max(len(current_text), 20) + 2):
        adb("shell", "input", "keyevent", "67", check=False)  # KEYCODE_DEL


def clear_edit_text_node(node: ET.Element) -> None:
    tap_node(node)
    time.sleep(0.25)
    bounds = parse_bounds(node.attrib.get("bounds", ""))
    if bounds and node.attrib.get("text"):
        _left, top, right, bottom = bounds
        clear_x = min(right + 36, 1020)
        clear_y = (top + bottom) // 2
        adb("shell", "input", "tap", str(clear_x), str(clear_y), check=False)
        time.sleep(0.25)
    clear_focused_text(node.attrib.get("text", ""))


def command_parts(command: dict[str, Any]) -> tuple[str, str, str]:
    name = str(command.get("commodity_name") or command.get("template_hash_name") or "")
    parts = [part.strip() for part in name.split("|") if part.strip()]
    if len(parts) >= 2:
        subject = parts[1]
        finish_match = re.search(r"\(([^)]*)\)", subject)
        finish = finish_match.group(1).strip() if finish_match else ""
        team = re.sub(r"\([^)]*\)", "", subject).strip()
        event = parts[2].strip() if len(parts) >= 3 else ""
        return team, finish, event
    return "", "", ""


def command_query(command: dict[str, Any]) -> str:
    team, finish, event = command_parts(command)
    if team:
        query_parts = [team]
        if finish:
            query_parts.append(finish)
        if event:
            query_parts.append(event)
        return " ".join(query_parts)
    name = str(command.get("commodity_name") or command.get("template_hash_name") or "")
    words = re.findall(r"[A-Za-z0-9]+", name)
    ignored = {"sticker", "holo", "foil", "paper", "glitter", "gold", "cologne", "2026"}
    useful = [word for word in words if word.lower() not in ignored]
    return " ".join(useful[:6]) or name


def command_item_name(command: dict[str, Any]) -> str:
    return str(command.get("commodity_name") or command.get("template_hash_name") or "").strip()


def adb_input_text(text: str) -> None:
    safe = re.sub(r"[^A-Za-z0-9 ._+-]", " ", text).strip()
    safe = re.sub(r"\s+", " ", safe).replace(" ", "%s")
    if safe:
        adb("shell", "input", "text", safe)


KEYEVENTS_BY_CHAR = {
    "0": "7",
    "1": "8",
    "2": "9",
    "3": "10",
    "4": "11",
    "5": "12",
    "6": "13",
    "7": "14",
    "8": "15",
    "9": "16",
    ".": "56",
}


def adb_input_number(text: str) -> None:
    for char in str(text):
        keycode = KEYEVENTS_BY_CHAR.get(char)
        if keycode is None:
            continue
        adb("shell", "input", "keyevent", keycode, check=False)
        time.sleep(0.12)


def decimal_value(text: str) -> float | None:
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def edit_text_nodes(xml_path: Path) -> list[ET.Element]:
    nodes = [
        node
        for node in find_nodes(xml_path, class_name="android.widget.EditText")
        if node.attrib.get("enabled") == "true"
    ]
    return sorted(
        nodes,
        key=lambda node: (
            (parse_bounds(node.attrib.get("bounds", "")) or (0, 0, 0, 0))[1],
            (parse_bounds(node.attrib.get("bounds", "")) or (0, 0, 0, 0))[0],
        ),
    )


def purchase_form_visible(xml_path: Path) -> bool:
    if find_node(xml_path, text_contains=TEXT_PUBLISH_BUY) is None:
        return False
    if len(edit_text_nodes(xml_path)) >= 2:
        return True
    # Some H5 form variants expose the page title, total and submit button but
    # omit both input elements from Android's accessibility tree.
    return (
        find_node(xml_path, text_contains=TEXT_TOTAL) is not None
        and find_node(xml_path, text_contains=TEXT_CONFIRM) is not None
    )


def wait_for_purchase_form(capture_dir: Path, timeout_seconds: int = 30) -> Path:
    deadline = time.time() + timeout_seconds
    last_xml = Path(capture(capture_dir)["xml"])
    while time.time() < deadline:
        if purchase_form_visible(last_xml):
            return last_xml
        time.sleep(1)
        last_xml = Path(capture(capture_dir)["xml"])
    raise EmulatorError("Purchase form did not appear.")


def dismiss_purchase_form_tips(capture_dir: Path) -> Path:
    xml_path = Path(capture(capture_dir)["xml"])
    for text in ("跳过", "知道了", "下一步"):
        node = find_node(xml_path, text_contains=text)
        if node is not None:
            tap_node(node)
            time.sleep(1.5)
            return Path(capture(capture_dir)["xml"])
    return xml_path


def settle_start_prompts(capture_dir: Path) -> Path:
    xml_path = Path(capture(capture_dir)["xml"])
    for _ in range(4):
        clicked = False
        for text in ("Got it", "同意并继续"):
            node = find_node(xml_path, text_contains=text)
            if node is not None:
                tap_node(node)
                clicked = True
                time.sleep(2)
                xml_path = Path(capture(capture_dir)["xml"])
                break
        if not clicked:
            return xml_path
    return xml_path


def find_search_entry(xml_path: Path) -> ET.Element | None:
    for resource_id in (
        "com.uu898.uuhavequality:id/steam_stock_search_et",
        "com.uu898.uuhavequality:id/et_search",
    ):
        node = find_node(xml_path, resource_id=resource_id)
        if node is not None:
            return node
    for text in ("搜索饰品", "请输入饰品关键词"):
        node = find_node(xml_path, text_contains=text)
        if node is not None:
            return node
    return None


def open_search_screen(capture_dir: Path) -> Path:
    xml_path = Path(capture(capture_dir)["xml"])
    for _ in range(6):
        node = find_search_entry(xml_path)
        if node is not None:
            tap_node(node)
            time.sleep(2)
            return Path(capture(capture_dir)["xml"])
        adb("shell", "input", "keyevent", "BACK", check=False)
        time.sleep(2)
        xml_path = Path(capture(capture_dir)["xml"])
    raise EmulatorError("Search entry not found in YouPin app.")


def is_publish_buy_page(xml_path: Path) -> bool:
    # The purchase tab always contains the "发布求购" label, even when the
    # selected page is the existing-order list. Existing-order search returns
    # no commodity results, so keep this check strict.
    order_list_markers = ("求购中", "暂停中", "待支付", "暂无求购记录")
    if find_node_text_any(xml_path, order_list_markers) is not None:
        return False
    return find_node(xml_path, text_contains=TEXT_PUBLISH_BUY) is not None and find_search_entry(xml_path) is not None


def open_publish_buy_page(capture_dir: Path) -> Path:
    start_app()
    settle_start_prompts(capture_dir)
    xml_path = Path(capture(capture_dir)["xml"])
    if is_publish_buy_page(xml_path):
        return xml_path

    for _ in range(6):
        if not has_bottom_navigation(xml_path):
            adb("shell", "input", "keyevent", "BACK", check=False)
            time.sleep(1.0)
            xml_path = Path(capture(capture_dir)["xml"])
            continue

        purchase_tabs = [
            node
            for node in visible_text_nodes(xml_path, TEXT_PURCHASE)
            if (visible_bounds(node) or (0, 0, 0, 0))[1] >= 1850
        ]
        if purchase_tabs:
            tap_node(purchase_tabs[-1])
        else:
            adb("shell", "input", "tap", "760", "2046", check=False)
        time.sleep(2)
        xml_path = Path(capture(capture_dir)["xml"])

        publish_tabs = [
            node
            for node in visible_text_nodes(xml_path, TEXT_PUBLISH_BUY)
            if (visible_bounds(node) or (0, 9999, 0, 9999))[1] < 260
        ]
        if publish_tabs:
            tap_node(publish_tabs[-1])
        else:
            adb("shell", "input", "tap", "540", "155", check=False)
        time.sleep(2)
        xml_path = Path(capture(capture_dir)["xml"])
        if is_publish_buy_page(xml_path):
            return xml_path

    raise EmulatorError("Publish-buy page not found in YouPin app.")


def input_search_query(query: str, capture_dir: Path) -> Path:
    xml_path = Path(capture(capture_dir)["xml"])
    focused_or_edit_nodes = [
        node for node in find_nodes(xml_path, class_name="android.widget.EditText")
        if node.attrib.get("enabled") == "true"
    ]
    if focused_or_edit_nodes:
        node = focused_or_edit_nodes[0]
        tap_node(node)
        clear_focused_text(node.attrib.get("text", ""))
    adb_input_text(query)
    adb("shell", "input", "keyevent", "66", check=False)
    time.sleep(4)
    return Path(capture(capture_dir)["xml"])


def tap_best_search_result(command: dict[str, Any], capture_dir: Path) -> dict[str, Any]:
    target = command_item_name(command)
    xml_path = Path(capture(capture_dir)["xml"])
    team, finish, event = command_parts(command)
    team_key = normalize_match_text(team)
    finish_key = normalize_match_text(finish)
    event_tokens = [token.lower() for token in re.findall(r"[A-Za-z0-9]+", event)]

    exact_nodes: list[ET.Element] = []
    for attempt in range(8):
        root = ET.parse(xml_path).getroot()
        for node in root.iter("node"):
            text = node.attrib.get("text", "")
            bounds = visible_bounds(node)
            if not text or bounds is None:
                continue
            normalized = normalize_match_text(text)
            if TEXT_STICKER_BOARD in text:
                continue
            if TEXT_STICKER not in text and "Sticker" not in text:
                continue
            if team_key:
                # Require the team to appear as the sticker subject, not as a substring
                # inside a player name such as BIG matching biguzera.
                if f"| {team_key} (" not in normalized and f"| {team_key} |" not in normalized:
                    continue
            if finish_key and finish_key not in normalized:
                continue
            if event_tokens and not all(token in normalized for token in event_tokens[:2]):
                continue
            exact_nodes.append(node)

        if not exact_nodes and target:
            exact_nodes = [node for node in find_nodes(xml_path, text_contains=target) if visible_bounds(node) is not None]
        if exact_nodes or attempt == 7:
            break

        adb("shell", "input", "swipe", "540", "1680", "540", "620", "450", check=False)
        time.sleep(1.5)
        xml_path = Path(capture(capture_dir)["xml"])

    if not exact_nodes:
        raise EmulatorError(f"Search result not found for create command: {target or command_query(command)}")

    selected_node = sorted(exact_nodes, key=lambda node: (visible_bounds(node) or (0, 0, 0, 0))[1])[0]
    bounds = visible_bounds(selected_node)
    if bounds is None:
        raise EmulatorError("Selected search result has no visible bounds.")
    left, top, right, _bottom = bounds
    tap_x = (left + right) // 2
    tap_y = max(360, top - 185)
    adb("shell", "input", "tap", str(tap_x), str(tap_y), check=False)
    time.sleep(4)
    paths = capture(capture_dir)
    return {
        "selected": True,
        "target": target,
        "query": command_query(command),
        "matched_text": selected_node.attrib.get("text", ""),
        "tap": {"x": tap_x, "y": tap_y},
        "capture": paths,
    }


def prepare_first_pending(command_file: Path, capture_dir: Path) -> dict[str, Any]:
    commands = pending_commands(command_file)
    if not commands:
        raise EmulatorError("No pending emulator commands.")
    command = commands[0]
    open_publish_buy_page(capture_dir)
    query = command_query(command)
    input_search_query(query, capture_dir)
    result = tap_best_search_result(command, capture_dir)
    return {"command": command, "query": query, **result}


def open_purchase_form(capture_dir: Path) -> dict[str, Any]:
    xml_path = Path(capture(capture_dir)["xml"])
    if find_node(xml_path, text_contains=TEXT_PUBLISH_BUY) is None:
        # The detail-page purchase button has unstable H5/native node metadata,
        # but a stable position on this emulator size.
        adb("shell", "input", "tap", "760", "652")
        time.sleep(6)
    xml_path = dismiss_purchase_form_tips(capture_dir)
    if find_node(xml_path, text_contains="发布求购") is None or len(edit_text_nodes(xml_path)) < 2:
        xml_path = wait_for_purchase_form(capture_dir)
    return {"capture": capture(capture_dir), "form_open": True}


def close_purchase_form_if_open(capture_dir: Path) -> Path:
    xml_path = Path(capture(capture_dir)["xml"])
    if find_node(xml_path, text_contains="发布求购") is not None:
        for _ in range(3):
            adb("shell", "input", "tap", "65", "125", check=False)
            time.sleep(2)
            xml_path = Path(capture(capture_dir)["xml"])
            if find_node(xml_path, text_contains="发布求购") is None:
                break
        return xml_path
    return xml_path


def fill_purchase_form(command: dict[str, Any], capture_dir: Path) -> dict[str, Any]:
    price_text = str(command.get("bid_price", "")).strip()
    qty_text = str(command.get("purchase_num", "")).strip()
    if not price_text or not qty_text:
        raise EmulatorError("Command is missing bid_price or purchase_num.")

    xml_path = wait_for_purchase_form(capture_dir)
    inputs = edit_text_nodes(xml_path)
    coordinate_fallback = len(inputs) < 2
    if coordinate_fallback:
        # The current 1080x2160 emulator layout keeps these two H5 rows stable
        # even when uiautomator does not expose their input nodes.
        for x, y, value in ((430, 675, price_text), (430, 830, qty_text)):
            adb("shell", "input", "tap", str(x), str(y), check=False)
            time.sleep(0.3)
            clear_focused_text()
            adb_input_number(value)
            time.sleep(0.5)
        adb("shell", "input", "keyevent", "BACK", check=False)
        time.sleep(0.8)
    else:
        for node, value in ((inputs[0], price_text), (inputs[1], qty_text)):
            clear_edit_text_node(node)
            time.sleep(0.25)
            adb_input_number(value)
            time.sleep(0.5)

    paths = capture(capture_dir)
    verify_xml = Path(paths["xml"])
    edits = edit_text_nodes(verify_xml)
    price_actual = decimal_value(edits[0].attrib.get("text", "")) if len(edits) >= 1 else None
    qty_actual = decimal_value(edits[1].attrib.get("text", "")) if len(edits) >= 2 else None
    expected_price = decimal_value(price_text)
    expected_qty = decimal_value(qty_text)

    total_text = ""
    for node in find_nodes(verify_xml, text_contains=TEXT_TOTAL):
        total_text = node.attrib.get("text", "")
        break
    expected_total = None
    total_actual = decimal_value(total_text)
    if expected_price is not None and expected_qty is not None:
        expected_total = round(expected_price * expected_qty, 2)
    price_ok = expected_price is not None and price_actual is not None and abs(price_actual - expected_price) < 0.01
    qty_ok = expected_qty is not None and qty_actual is not None and abs(qty_actual - expected_qty) < 0.01
    total_ok = expected_total is not None and total_actual is not None and abs(total_actual - expected_total) < 0.01
    # Some UU form variants keep the quantity value out of the accessibility XML
    # after input. In that case the computed total is the safer verification signal.
    verified = total_ok if coordinate_fallback else price_ok and (qty_ok or total_ok)
    return {
        "capture": paths,
        "bid_price": price_text,
        "purchase_num": qty_text,
        "price_actual": price_actual,
        "qty_actual": qty_actual,
        "expected_total": expected_total,
        "total_text": total_text,
        "input_mode": "coordinates" if coordinate_fallback else "accessibility",
        "verified": verified,
        "submit_ready": verified,
        "submitted": False,
    }


def fill_first_pending(command_file: Path, capture_dir: Path) -> dict[str, Any]:
    commands = pending_commands(command_file)
    if not commands:
        raise EmulatorError("No pending emulator commands.")
    command = commands[0]
    start_app()
    settle_start_prompts(capture_dir)
    close_purchase_form_if_open(capture_dir)
    xml_path = Path(capture(capture_dir)["xml"])
    if find_node(xml_path, text_contains="发布求购") is None:
        if find_node(xml_path, text_contains=command_item_name(command)) is None:
            prepare_first_pending(command_file, capture_dir)
        open_purchase_form(capture_dir)
    else:
        dismiss_purchase_form_tips(capture_dir)
    fill_result = fill_purchase_form(command, capture_dir)
    return {"command": command, **fill_result}


def visible_page_text(xml_path: Path) -> str:
    root = ET.parse(xml_path).getroot()
    return "\n".join(node.attrib.get("text", "") for node in root.iter("node") if node.attrib.get("text"))


def tap_purchase_form_submit(capture_dir: Path) -> dict[str, str]:
    paths = capture(capture_dir)
    xml_path = Path(paths["xml"])
    buttons = [
        node
        for node in visible_text_nodes(xml_path, TEXT_CONFIRM)
        if visible_bounds(node) is not None
    ]
    if buttons:
        # The Android numeric keyboard can stay open after filling quantity.
        # In that state the form's confirm button sits above the keyboard
        # instead of near the screen bottom, so do not constrain by Y position.
        buttons = sorted(
            buttons,
            key=lambda node: (
                (visible_bounds(node) or (0, 0, 0, 0))[1],
                (visible_bounds(node) or (0, 0, 0, 0))[0],
            ),
        )
        tap_node(buttons[-1])
    else:
        adb("shell", "input", "keyevent", "BACK", check=False)
        time.sleep(0.4)
        adb("shell", "input", "tap", "840", "1995", check=False)
    time.sleep(1.5)
    return capture(capture_dir)


def wait_for_buy_confirmation(capture_dir: Path, timeout_seconds: int = 15) -> dict[str, str]:
    deadline = time.time() + timeout_seconds
    paths = capture(capture_dir)
    while time.time() < deadline:
        xml_path = Path(paths["xml"])
        page = visible_page_text(xml_path)
        if TEXT_BUY_CONFIRM in page or TEXT_CONFIRM_BUY in page:
            return paths
        time.sleep(0.8)
        paths = capture(capture_dir)
    raise EmulatorError("Purchase confirmation dialog did not appear.")


def tap_buy_confirmation(capture_dir: Path) -> dict[str, str]:
    paths = wait_for_buy_confirmation(capture_dir)
    xml_path = Path(paths["xml"])
    button = find_node(xml_path, text_contains=TEXT_CONFIRM_BUY)
    if button is not None and visible_bounds(button) is not None:
        tap_node(button)
    else:
        adb("shell", "input", "tap", "540", "1305", check=False)
    time.sleep(4)
    return capture(capture_dir)


def wait_for_purchase_detail(command: dict[str, Any], capture_dir: Path, timeout_seconds: int = 25) -> dict[str, str]:
    team, _finish, _event = command_parts(command)
    team_key = normalize_match_text(team)
    deadline = time.time() + timeout_seconds
    paths = capture(capture_dir)
    while time.time() < deadline:
        xml_path = Path(paths["xml"])
        page = visible_page_text(xml_path)
        normalized = normalize_match_text(page)
        if TEXT_BUYING in page and (not team_key or team_key in normalized):
            return paths
        time.sleep(1)
        paths = capture(capture_dir)
    raise EmulatorError("Submitted order detail page did not appear.")


def create_purchase_order_command(command: dict[str, Any], capture_dir: Path, *, submit: bool = True) -> dict[str, Any]:
    query = command_query(command)
    open_publish_buy_page(capture_dir)
    input_search_query(query, capture_dir)
    selected = tap_best_search_result(command, capture_dir)
    wait_for_purchase_form(capture_dir)
    fill_result = fill_purchase_form(command, capture_dir)
    if not fill_result.get("verified"):
        raise EmulatorError(
            "Purchase form verification failed: "
            f"expected price={command.get('bid_price')} qty={command.get('purchase_num')} "
            f"actual price={fill_result.get('price_actual')} qty={fill_result.get('qty_actual')}"
        )
    result: dict[str, Any] = {
        "command": command,
        "query": query,
        "selected": selected,
        "fill": fill_result,
        "submitted": False,
    }
    if submit:
        result["submit_dialog"] = tap_purchase_form_submit(capture_dir)
        result["confirmation"] = tap_buy_confirmation(capture_dir)
        result["detail"] = wait_for_purchase_detail(command, capture_dir)
        result["submitted"] = True
    return result


def return_to_bottom_navigation(capture_dir: Path) -> Path:
    xml_path = Path(capture(capture_dir)["xml"])
    if is_buy_price_fill_page(xml_path) or has_bottom_navigation(xml_path):
        return xml_path
    for _ in range(6):
        adb("shell", "input", "keyevent", "BACK", check=False)
        time.sleep(1.2)
        xml_path = Path(capture(capture_dir)["xml"])
        if is_buy_price_fill_page(xml_path) or has_bottom_navigation(xml_path):
            return xml_path
        adb("shell", "input", "tap", "62", "126", check=False)
        time.sleep(1.2)
        xml_path = Path(capture(capture_dir)["xml"])
        if is_buy_price_fill_page(xml_path) or has_bottom_navigation(xml_path):
            return xml_path
    return xml_path

def is_buy_price_fill_page(xml_path: Path) -> bool:
    return (
        find_node(xml_path, text_contains=TEXT_FILL_TITLE) is not None
        and find_node(xml_path, text_contains=TEXT_BUY_PRICE_LABEL) is not None
    )


def has_bottom_navigation(xml_path: Path) -> bool:
    if find_node(xml_path, resource_id="com.uu898.uuhavequality:id/bottom_bar") is not None:
        return True
    for node in visible_text_nodes(xml_path, TEXT_INVENTORY):
        bounds = visible_bounds(node)
        if bounds and bounds[1] >= 1850:
            return True
    return False


def tap_missing_buy_price_entry(xml_path: Path) -> bool:
    buy_price_bounds = [
        bounds
        for node in visible_text_nodes(xml_path, TEXT_BUY_PRICE_SHORT)
        if (bounds := visible_bounds(node)) is not None and bounds[1] < 700
    ]
    buy_price_center_x = center(sorted(buy_price_bounds, key=lambda item: item[1])[-1])[0] if buy_price_bounds else None

    candidates: list[tuple[int, int, tuple[int, int, int, int]]] = []
    root = ET.parse(xml_path).getroot()
    for node in root.iter("node"):
        text = node.attrib.get("text", "")
        if TEXT_NO_BUY_PRICE not in text:
            continue
        bounds = visible_bounds(node)
        if bounds is None:
            continue
        left, top, right, bottom = bounds
        if top >= 760:
            continue
        x, y = center(bounds)
        distance = abs(x - buy_price_center_x) if buy_price_center_x is not None else 0
        candidates.append((distance, y, bounds))

    if not candidates:
        return False

    _distance, _y, bounds = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
    left, top, right, bottom = bounds
    tap_x = min(max(right + 50, (left + right) // 2), 1010)
    adb("shell", "input", "tap", str(tap_x), str((top + bottom) // 2), check=False)
    return True


def open_buy_price_fill_page(capture_dir: Path) -> Path:
    start_app()
    settle_start_prompts(capture_dir)
    xml_path = return_to_bottom_navigation(capture_dir)
    if is_buy_price_fill_page(xml_path):
        return xml_path

    for _ in range(6):
        inventory_nodes = [
            node
            for node in visible_text_nodes(xml_path, TEXT_INVENTORY)
            if (visible_bounds(node) or (0, 0, 0, 0))[1] >= 1850
        ]
        if inventory_nodes:
            tap_node(inventory_nodes[-1])
        else:
            adb("shell", "input", "tap", "324", "2046", check=False)
        time.sleep(2)
        xml_path = Path(capture(capture_dir)["xml"])
        if is_buy_price_fill_page(xml_path):
            return xml_path

        change_tabs = [
            node
            for node in visible_text_nodes(xml_path, TEXT_RISE_FALL)
            if (visible_bounds(node) or (0, 9999, 0, 9999))[1] < 260
        ]
        if change_tabs:
            tap_node(change_tabs[-1])
        else:
            adb("shell", "input", "tap", "520", "155", check=False)
        time.sleep(2)
        xml_path = Path(capture(capture_dir)["xml"])
        if is_buy_price_fill_page(xml_path):
            return xml_path

        if not tap_missing_buy_price_entry(xml_path):
            adb("shell", "input", "keyevent", "BACK", check=False)

        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(1)
            xml_path = Path(capture(capture_dir)["xml"])
            if is_buy_price_fill_page(xml_path):
                return xml_path
        if not has_bottom_navigation(xml_path):
            adb("shell", "input", "keyevent", "BACK", check=False)
            time.sleep(2)
            xml_path = Path(capture(capture_dir)["xml"])

    raise EmulatorError("Buy-price fill page not found.")


def normalize_match_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower())


def command_match_terms(command: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for key in ("commodity_name", "template_hash_name"):
        value = str(command.get(key) or "").strip()
        if not value:
            continue
        variants = {
            value,
            value.replace("Sticker |", f"{TEXT_STICKER} |"),
            value.replace("Sticker", TEXT_STICKER),
        }
        parts = [part.strip() for part in value.split("|") if part.strip()]
        if len(parts) >= 2:
            variants.add(" | ".join(parts[1:]))
            variants.add(parts[1])
        terms.extend(variant for variant in variants if variant)
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = normalize_match_text(term)
        if key and key not in seen:
            seen.add(key)
            deduped.append(term)
    return deduped


def card_matches_command(text: str, command: dict[str, Any]) -> bool:
    normalized = normalize_match_text(text)
    command_text = " ".join(str(command.get(key) or "") for key in ("commodity_name", "template_hash_name"))
    if (TEXT_STICKER in command_text or "Sticker" in command_text) and TEXT_STICKER not in text and "Sticker" not in text:
        return False
    for term in command_match_terms(command):
        term_normalized = normalize_match_text(term)
        if len(term_normalized) >= 4 and term_normalized in normalized:
            return True
    ignored = {"sticker", "holo", "foil", "paper", "glitter", "gold", "cologne", "2026"}
    tokens = [token.lower() for token in re.findall(r"[A-Za-z0-9]+", command_text) if token.lower() not in ignored]
    return bool(tokens and all(token in normalized for token in tokens[:4]))


def buy_price_card_nodes(xml_path: Path) -> list[ET.Element]:
    root = ET.parse(xml_path).getroot()
    cards: list[ET.Element] = []
    for node in root.iter("node"):
        text = node.attrib.get("text", "")
        if TEXT_BUY_PRICE_LABEL not in text:
            continue
        if node.attrib.get("class") != "android.view.View":
            continue
        bounds = visible_bounds(node)
        if bounds is None or bounds[3] - bounds[1] < 180:
            continue
        cards.append(node)
    return sorted(cards, key=lambda node: (visible_bounds(node) or (0, 0, 0, 0))[1])


def first_visible_edit_text(node: ET.Element) -> ET.Element | None:
    edits = []
    for child in node.iter("node"):
        if child.attrib.get("class") != "android.widget.EditText":
            continue
        bounds = visible_bounds(child)
        if bounds is None or bounds[3] - bounds[1] < 30:
            continue
        edits.append(child)
    if not edits:
        return None
    return sorted(edits, key=lambda child: (visible_bounds(child) or (0, 0, 0, 0))[1])[0]


def find_buy_price_card_and_input(
    command: dict[str, Any],
    capture_dir: Path,
    *,
    max_scrolls: int = 80,
    start_from_top: bool = True,
) -> tuple[ET.Element, ET.Element, dict[str, str]]:
    if start_from_top:
        for _ in range(8):
            adb("shell", "input", "swipe", "540", "650", "540", "1700", "260", check=False)
            time.sleep(0.2)

    seen_signatures: set[str] = set()
    last_paths = capture(capture_dir)
    for attempt in range(max_scrolls + 1):
        xml_path = Path(last_paths["xml"])
        cards = buy_price_card_nodes(xml_path)
        for card in cards:
            text = card.attrib.get("text", "")
            if not card_matches_command(text, command):
                continue
            edit = first_visible_edit_text(card)
            if edit is not None:
                return card, edit, last_paths

        signature = "|".join(card.attrib.get("text", "")[:140] for card in cards[:6])
        if signature:
            seen_signatures.add(signature)
        if attempt >= max_scrolls:
            break
        adb("shell", "input", "swipe", "540", "1760", "540", "560", "360", check=False)
        time.sleep(0.75)
        last_paths = capture(capture_dir)
        next_signature = "|".join(card.attrib.get("text", "")[:140] for card in buy_price_card_nodes(Path(last_paths["xml"]))[:6])
        if next_signature and next_signature in seen_signatures and len(seen_signatures) > 1:
            break

    target = command_item_name(command) or str(command.get("template_hash_name") or command.get("asset_id") or "")
    raise EmulatorError(f"Buy-price inventory item not found after scrolling: {target}")


def verify_buy_price_field(command: dict[str, Any], capture_dir: Path) -> tuple[bool, float | None, dict[str, str]]:
    paths = capture(capture_dir)
    try:
        _card, edit, _paths = find_buy_price_card_and_input(command, capture_dir, max_scrolls=0, start_from_top=False)
    except EmulatorError:
        return False, None, paths
    expected = decimal_value(str(command.get("buy_price", "")))
    actual = decimal_value(edit.attrib.get("text", ""))
    verified = expected is not None and actual is not None and abs(expected - actual) < 0.01
    return verified, actual, paths


def fill_buy_price_command(command: dict[str, Any], capture_dir: Path, *, submit: bool = True) -> dict[str, Any]:
    price_text = str(command.get("buy_price", "")).strip()
    if not price_text:
        raise EmulatorError("Command is missing buy_price.")

    open_buy_price_fill_page(capture_dir)
    _card, edit, paths = find_buy_price_card_and_input(command, capture_dir, max_scrolls=80)
    bounds = visible_bounds(edit)
    if bounds and bounds[3] > 1840:
        adb("shell", "input", "swipe", "540", "1710", "540", "1340", "260", check=False)
        time.sleep(0.8)
        _card, edit, paths = find_buy_price_card_and_input(command, capture_dir, max_scrolls=3, start_from_top=False)

    clear_edit_text_node(edit)
    time.sleep(0.25)
    adb_input_number(price_text)
    time.sleep(0.7)
    adb("shell", "input", "keyevent", "BACK", check=False)
    time.sleep(0.8)

    verified, actual_price, verify_paths = verify_buy_price_field(command, capture_dir)
    if not verified:
        raise EmulatorError(f"Buy-price field verification failed: expected={price_text}, actual={actual_price}")

    submitted = False
    submit_paths = verify_paths
    if submit:
        xml_path = Path(verify_paths["xml"])
        button = find_node(xml_path, text_contains=TEXT_CONFIRM)
        if button is not None and visible_bounds(button) is not None:
            tap_node(button)
        else:
            adb("shell", "input", "tap", "900", "1975", check=False)
        submitted = True
        time.sleep(4)
        submit_paths = capture(capture_dir)

    return {
        "command": command,
        "capture": submit_paths,
        "buy_price": price_text,
        "actual_price": actual_price,
        "verified": verified,
        "submitted": submitted,
    }


def edit_text_already_has_price(edit: ET.Element) -> bool:
    text = edit.attrib.get("text", "")
    return decimal_value(text) is not None


def fill_buy_price_edit(edit: ET.Element, price_text: str) -> None:
    clear_edit_text_node(edit)
    time.sleep(0.2)
    adb_input_number(price_text)
    time.sleep(0.45)
    adb("shell", "input", "keyevent", "BACK", check=False)
    time.sleep(0.45)


def tap_buy_price_confirm(capture_dir: Path) -> dict[str, str]:
    paths = capture(capture_dir)
    xml_path = Path(paths["xml"])
    button = find_node(xml_path, text_contains=TEXT_CONFIRM)
    if button is not None and visible_bounds(button) is not None:
        tap_node(button)
    else:
        adb("shell", "input", "tap", "900", "1975", check=False)
    time.sleep(5)
    return capture(capture_dir)


def process_buy_price_fill_batch(
    command_file: Path,
    capture_dir: Path,
    *,
    limit: int = 1,
    submit_buy_price_fill: bool = True,
) -> dict[str, Any]:
    commands = pending_commands(command_file, command_type="fill_inventory_buy_price")[: max(1, int(limit))]
    if not commands:
        return {"processed": [], "processed_count": 0, "pending_after": 0}

    open_buy_price_fill_page(capture_dir)
    for _ in range(8):
        adb("shell", "input", "swipe", "540", "650", "540", "1700", "260", check=False)
        time.sleep(0.18)

    remaining: dict[str, dict[str, Any]] = {str(command.get("id", "")): command for command in commands}
    processed: list[dict[str, Any]] = []
    filled: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    repeated_pages = 0
    last_paths = capture(capture_dir)

    for attempt in range(100):
        xml_path = Path(last_paths["xml"])
        cards = buy_price_card_nodes(xml_path)
        page_signature = "|".join(card.attrib.get("text", "")[:140] for card in cards[:6])
        page_filled = 0

        for card in cards:
            text = card.attrib.get("text", "")
            edit = first_visible_edit_text(card)
            if edit is None or edit_text_already_has_price(edit):
                continue

            matched_id = ""
            matched_command: dict[str, Any] | None = None
            for command_id, command in list(remaining.items()):
                if card_matches_command(text, command):
                    matched_id = command_id
                    matched_command = command
                    break
            if matched_command is None:
                continue

            price_text = str(matched_command.get("buy_price", "")).strip()
            if not price_text:
                continue
            mark_command(command_file, matched_id, command_queue.COMMAND_STATUS_RUNNING)
            fill_buy_price_edit(edit, price_text)
            filled.append(
                {
                    "id": matched_id,
                    "name": matched_command.get("commodity_name") or matched_command.get("template_hash_name"),
                    "buy_price": price_text,
                    "bounds": edit.attrib.get("bounds", ""),
                    "page": attempt,
                }
            )
            remaining.pop(matched_id, None)
            page_filled += 1
            if not remaining:
                break

        if not remaining:
            break

        if page_filled:
            repeated_pages = 0
        elif page_signature and page_signature in seen_signatures:
            repeated_pages += 1
        else:
            repeated_pages = 0
        if page_signature:
            seen_signatures.add(page_signature)
        if repeated_pages >= 3:
            break

        adb("shell", "input", "swipe", "540", "1760", "540", "560", "360", check=False)
        time.sleep(0.8)
        last_paths = capture(capture_dir)

    submit_paths = last_paths
    if filled and submit_buy_price_fill:
        submit_paths = tap_buy_price_confirm(capture_dir)

    status = command_queue.COMMAND_STATUS_DONE if submit_buy_price_fill else command_queue.COMMAND_STATUS_FILLED
    for item in filled:
        command_id = str(item["id"])
        result = {"batch_fill": item, "capture": submit_paths, "submitted": submit_buy_price_fill}
        mark_command(command_file, command_id, status, result=result)
        processed.append({"id": command_id, "type": "fill_inventory_buy_price", "status": status})

    return {
        "processed": processed,
        "processed_count": len(processed),
        "pending_after": len(pending_commands(command_file, command_type="fill_inventory_buy_price")),
        "filled": filled,
        "unmatched": list(remaining),
    }


def process_pending_commands(
    command_file: Path,
    capture_dir: Path,
    *,
    command_type: str | set[str] | None = None,
    limit: int = 1,
    submit_buy_price_fill: bool = True,
    submit_create_order: bool = True,
) -> dict[str, Any]:
    if command_type == "fill_inventory_buy_price" or (
        isinstance(command_type, set) and command_type == {"fill_inventory_buy_price"}
    ):
        return process_buy_price_fill_batch(
            command_file,
            capture_dir,
            limit=limit,
            submit_buy_price_fill=submit_buy_price_fill,
        )

    processed: list[dict[str, Any]] = []
    for _ in range(max(1, int(limit))):
        commands = pending_commands(command_file, command_type=command_type)
        if not commands:
            break
        command = commands[0]
        command_id = str(command.get("id", ""))
        try:
            mark_command(command_file, command_id, command_queue.COMMAND_STATUS_RUNNING)
            if str(command.get("type", "")) == "fill_inventory_buy_price":
                result = fill_buy_price_command(command, capture_dir, submit=submit_buy_price_fill)
                status = command_queue.COMMAND_STATUS_DONE if submit_buy_price_fill else command_queue.COMMAND_STATUS_FILLED
            elif str(command.get("type", "")) == "create_purchase_order":
                result = create_purchase_order_command(command, capture_dir, submit=submit_create_order)
                status = command_queue.COMMAND_STATUS_DONE if submit_create_order else command_queue.COMMAND_STATUS_FILLED
            else:
                raise EmulatorError(f"Unsupported emulator command type: {command.get('type')}")
            mark_command(command_file, command_id, status, result=result)
            processed.append({"id": command_id, "type": command.get("type"), "status": status})
        except Exception as exc:
            error = str(exc)
            mark_command(command_file, command_id, command_queue.COMMAND_STATUS_FAILED, error=error, result={"capture": capture(capture_dir)})
            processed.append({"id": command_id, "type": command.get("type"), "status": command_queue.COMMAND_STATUS_FAILED, "error": error})
    return {
        "processed": processed,
        "processed_count": len(processed),
        "pending_after": len(pending_commands(command_file, command_type=command_type)),
    }


def build_status(command_file: Path, capture_dir: Path) -> dict[str, Any]:
    device = online_device()
    pending_all = pending_commands(command_file)
    status: dict[str, Any] = {
        "device": device,
        "boot_completed": None,
        "app_installed": False,
        "pending_commands": len(pending_all),
        "pending_create_purchase_order_commands": len(
            [command for command in pending_all if command.get("type") == "create_purchase_order"]
        ),
        "pending_buy_price_fill_commands": len(
            [command for command in pending_all if command.get("type") == "fill_inventory_buy_price"]
        ),
        "capture_dir": str(capture_dir),
    }
    if device:
        status["boot_completed"] = adb("shell", "getprop", "sys.boot_completed", check=False).stdout.strip()
        status["android_version"] = adb("shell", "getprop", "ro.build.version.release", check=False).stdout.strip()
        status["abis"] = adb("shell", "getprop", "ro.product.cpu.abilist", check=False).stdout.strip()
        status["app_installed"] = app_installed()
        if status["app_installed"]:
            version = package_version()
            status["app_version_name"] = version.get("versionName")
            status["app_version_code"] = version.get("versionCode")
            if version.get("versionName") == "1.0.0":
                status["warning"] = "Installed YouPin APK is version 1.0.0; market endpoints may be obsolete."
            elif str(version.get("versionName") or "").startswith("5.") and "arm64-v8a" not in str(status.get("abis", "")):
                status["warning"] = "YouPin 5.x should run on an arm64-capable emulator; use YouPin_X64_API35."
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Android emulator bridge for YouPin command queue.")
    parser.add_argument("--avd", default=DEFAULT_AVD)
    parser.add_argument("--apk", type=Path, default=DEFAULT_APK)
    parser.add_argument("--command-file", type=Path, default=DEFAULT_COMMAND_FILE)
    parser.add_argument("--status-file", type=Path, default=DEFAULT_STATUS_FILE)
    parser.add_argument("--capture-dir", type=Path, default=DEFAULT_CAPTURE_DIR)
    parser.add_argument("--launch", action="store_true", help="Launch emulator if no device is online.")
    parser.add_argument("--install-apk", action="store_true", help="Install the official YouPin APK.")
    parser.add_argument("--start-app", action="store_true", help="Open the YouPin app.")
    parser.add_argument("--capture", action="store_true", help="Capture screenshot and UI XML.")
    parser.add_argument("--prepare-first", action="store_true", help="Search the first pending command; does not submit.")
    parser.add_argument("--fill-first", action="store_true", help="Open and fill the first pending bid form; does not submit.")
    parser.add_argument("--process-create-orders", action="store_true", help="Process queued create-purchase-order commands in the YouPin app.")
    parser.add_argument("--process-buy-price-fill", action="store_true", help="Process queued inventory buy-price fill commands.")
    parser.add_argument("--limit", type=int, default=1, help="Maximum queued commands to process.")
    parser.add_argument("--no-submit-create-order", action="store_true", help="Fill the create-order form but do not submit it.")
    parser.add_argument("--no-submit-buy-price-fill", action="store_true", help="Fill the cost field but do not tap Confirm.")
    parser.add_argument("--ensure-ready", action="store_true", help="Launch and prepare the emulator when needed.")
    parser.add_argument("--reset-adb", action="store_true", help="Restart the local ADB server.")
    parser.add_argument("--status", action="store_true", help="Write and print emulator status.")
    args = parser.parse_args()

    try:
        if args.reset_adb:
            reset_adb_server()
        needs_device = (
            args.ensure_ready
            or args.install_apk
            or args.start_app
            or args.capture
            or args.prepare_first
            or args.fill_first
            or args.process_create_orders
            or args.process_buy_price_fill
        )
        if needs_device:
            ensure_ready(args.avd, args.apk)
        if args.launch:
            launch_emulator(args.avd)
            wait_for_boot()
        if args.install_apk:
            install_apk(args.apk)
        if args.start_app:
            start_app()
        result: dict[str, Any] = {}
        if args.capture:
            result["capture"] = capture(args.capture_dir)
        if args.prepare_first:
            result["prepare_first"] = prepare_first_pending(args.command_file, args.capture_dir)
        if args.fill_first:
            result["fill_first"] = fill_first_pending(args.command_file, args.capture_dir)
        if args.process_create_orders:
            result["process_create_orders"] = process_pending_commands(
                args.command_file,
                args.capture_dir,
                command_type="create_purchase_order",
                limit=args.limit,
                submit_create_order=not args.no_submit_create_order,
            )
        if args.process_buy_price_fill:
            result["process_buy_price_fill"] = process_pending_commands(
                args.command_file,
                args.capture_dir,
                command_type="fill_inventory_buy_price",
                limit=args.limit,
                submit_buy_price_fill=not args.no_submit_buy_price_fill,
            )
        status = build_status(args.command_file, args.capture_dir)
        if result:
            status["last_result"] = result
        write_status(args.status_file, status)
        if args.status or result:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        error_status = {"error": str(exc), "pending_commands": len(pending_commands(args.command_file))}
        write_status(args.status_file, error_status)
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
