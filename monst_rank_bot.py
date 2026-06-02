#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
モンスト ノマクエ放置周回 bot (ADB + OpenCV / root不要)

仕組み:
  ホーム画面の ▶▶ ショートカット → ボス → ソロ → 助っ人 → 出撃 → バトル → 演出送り → 報酬画面 → ホーム
  を無限ループ。判定はテンプレマッチ + 固定遷移のハイブリッド。

重要:
  モンストは `adb shell input tap` を弾く (検出対策)。 全UIタップは
  `input swipe x y x y <ms>` (同座標を~150ms 保持) で送る必要がある。

前提:
  - 実機 USBデバッグON、root不要、`stay_on_while_plugged_in 3` 推奨。
  - pip install -r requirements.txt

CLI:
  python monst_rank_bot.py shot       現在画面を screens/screen.png に保存
  python monst_rank_bot.py run        周回開始 (Ctrl+C で停止)
  python monst_rank_bot.py templates  テンプレ整備状況を一覧
  python monst_rank_bot.py calibrate  解像度に応じた CONFIG 推奨値を表示
"""

from __future__ import annotations

import csv
import ctypes
import json
import logging
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

# Windows console (cp932) でも日本語を吐けるように
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ========================= CONFIG =========================

ADB_BIN = os.environ.get("ADB_PATH", "adb")
ADB_SERIAL = os.environ.get("ADB_SERIAL", "")
INPUT_BACKEND = os.environ.get("MONST_INPUT_BACKEND", "sendevent").lower()
ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "templates"
SCREEN_DIR = ROOT / "screens"
LOG_DIR = ROOT / "logs"

SCREEN_W = 1080
SCREEN_H = 2400

# 状態判定テンプレ。値: (ファイル名, しきい値)
TEMPLATES: dict[str, tuple[str, float]] = {
    "home":          ("home.png",          0.82),  # ホーム画面 (クエストボタン中央)
    "result":        ("result.png",        0.85),  # リザルト (スペシャル報酬)
    "koryaku_hint":  ("koryaku_hint.png",  0.82),  # クリア直後の「攻略のヒント」ダイアログ
    "kyouka":        ("kyouka.png",        0.82),  # 強化素材画面 (クリア情報ドロップダウン)
    "gacha":         ("gacha.png",         0.86),  # ガチャ画面 (下部ガチャタブ選択状態)
    "deck":          ("deck.png",          0.82),  # デッキ選択画面
    "stamina_full":  ("stamina.png",       0.85),  # スタミナ不足ダイアログ
    "exit_confirm":  ("exit_confirm.png",  0.82),  # ホーム上でバック → 「ゲームを終了します」確認
}

# 固定遷移座標 (Pixel 8a 1080x2400 で実測 / getevent でキャリブレ済み)。
COORDS = {
    # スーパーショートカット: ホーム → デッキ選択 まで一気に飛ぶ
    "super_shortcut": (556, 1721),
    "shutsu":         (540, 1492),  # 出撃ボタン (デッキ画面)
    # 以下は super_shortcut が使えない場合 (新クエスト後など) の fallback
    "shortcut_normal":(544, 1686),  # 通常 ▶▶ → area view へ
    "boss_row":       (400,  900),  # area view の突破ボス行
    "solo":           (320, 1350),  # プレイタイプ ソロ
    "helper":         (491, 1237),  # 助っ人 (フレンド使用キャラ最上段)
    # 例外処理
    "home_tab":       ( 90, 2210),  # 下部ナビ左下ホーム
    "iie":            (720, 1360),  # ホームでバック誤爆時の「いいえ」
    "back":           ( 90,  150),  # 画面左上戻る
}

# post-battle 系のダイアログOK位置は画面ごとに変わるため、中央列を縦に
# 数点タップしてどこかにヒットさせる方式。
# 注意: y=1900 は HOME 画面のクエストボタン中央と重なる -> sweep で誤爆するので除外。
#   1380 = 攻略のヒント OK
#   1500/1680 = リザルト送り (スペシャル報酬画面のタップしてください等)
#   2080/2150 = 強化素材画面の下部 OK
OK_SWEEP_Y = [1380, 1500, 1680, 2080, 2150]
OK_SWEEP_X = 540

# フリック (バトル中)
# user 確認: 安全スワイプゾーンは x=50-950, y=400-1500 (青枠内)
# y=1300 だとキャラ列に近すぎてステータス画面開く罠 -> 上に逃がす
FLICK_PIVOT = (540, 700)        # 上寄り (キャラ列から離す)
FLICK_PIVOT_JITTER = 80         # 範囲 (460-620, 620-780)、まだ安全ゾーン余裕
FLICK_RADIUS_MIN = 200
FLICK_RADIUS_MAX = 500
FLICK_DURATION_MS = (90, 180)
FLICK_INTERVAL = (1.0, 2.0)     # フリック間隔 (実プレイヤーペース)
# 安全スワイプゾーン (start/end とも必ずこの範囲内に収める)
SAFE_ZONE_X = (50, 950)
SAFE_ZONE_Y = (400, 1500)

# sendevent backend
SENDEVENT_REMOTE_SCRIPT = "/data/local/tmp/monst_swipe.sh"
SENDEVENT_STEPS = (9, 16)
SENDEVENT_COORD_JITTER_PX = (2, 5)
SENDEVENT_DURATION_JITTER = 0.15
SENDEVENT_CURVE_PX = (8, 36)
SENDEVENT_DEFAULT_HOLD_MS = (45, 140)
SENDEVENT_TRACKING_ID_MAX = 65535

# UIタップ (input swipe で疑似ホールド)。人間っぽさのために結構ばらつかせる。
TAP_HOLD_MS = (100, 280)        # ホールド時間 (ms)
TAP_JITTER_PX = 18              # 座標を±このピクセル分散らす
TAP_PRE_PAUSE = (0.10, 0.55)    # タップ前に指が動いてる時間相当のランダム pause

# タイミング
BATTLE_TIMEOUT = 360            # 1バトルの保険タイムアウト (6分: 重いクエスト用)
DEPARTURE_RETRIES = 3           # 出撃後もデッキ画面なら再タップする回数
STAMINA_WAIT = 60 * 15          # スタミナ切れ時の待機秒
STUCK_THRESHOLD = 30            # この秒数同じ画面なら例外と判定
LOOP_POLL = 1.5
STEP_WAIT = (2.5, 3.5)          # 状態遷移待ち
LOAD_WAIT = (1.5, 2.0)          # ローディング待ち (短縮)
BATTLE_WAIT_START = (1.5, 2.5)  # バトル開始演出待ち (短縮)

# debug: 各stepでスクショ保存 (環境変数 MONST_DEBUG=1 で有効化)
DEBUG_SCREENSHOTS = os.environ.get("MONST_DEBUG", "") == "1"

# ============================================================================


# ----------------------------- logging -----------------------------

def _setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fname = LOG_DIR / f"bot_{datetime.now():%Y%m%d_%H%M%S}.log"

    logger = logging.getLogger("monst_bot")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = logging.FileHandler(fname, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.info(f"log file: {fname}")
    return logger


log = _setup_logger()


def append_run_csv(start_ts: float, end_ts: float, result: str, note: str = "") -> None:
    """1周ごとの記録を logs/runs.csv に追記。"""
    csv_path = LOG_DIR / "runs.csv"
    is_new = not csv_path.exists()
    with csv_path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["start", "end", "duration_s", "result", "note"])
        w.writerow([
            datetime.fromtimestamp(start_ts).isoformat(timespec="seconds"),
            datetime.fromtimestamp(end_ts).isoformat(timespec="seconds"),
            int(end_ts - start_ts),
            result,
            note,
        ])


def notify_windows(title: str, msg: str) -> None:
    """Windows トースト相当の通知 (PowerShell BurntToast 不在ならMessageBox)。"""
    try:
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40 | 0x1000)  # MB_ICONINFORMATION | MB_SYSTEMMODAL
    except Exception as e:
        log.warning(f"通知失敗: {e}")


# ----------------------------- ADB wrappers -----------------------------

def _adb_base() -> list[str]:
    cmd = [ADB_BIN]
    if ADB_SERIAL:
        cmd += ["-s", ADB_SERIAL]
    return cmd


def adb_run(args: list[str], timeout: int = 15, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(_adb_base() + args, timeout=timeout, capture_output=capture)


def screencap() -> np.ndarray | None:
    try:
        proc = subprocess.run(_adb_base() + ["exec-out", "screencap", "-p"],
                              capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        log.warning("screencap timeout")
        return None
    raw = proc.stdout
    if not raw:
        return None
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)


@dataclass
class TouchInfo:
    device: str
    touch_max_x: int
    touch_max_y: int
    screen_w: int
    screen_h: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "device": self.device,
            "touch_max_x": self.touch_max_x,
            "touch_max_y": self.touch_max_y,
            "screen_w": self.screen_w,
            "screen_h": self.screen_h,
        }


def _adb_capture_text(args: list[str], timeout: int = 15) -> str:
    proc = subprocess.run(_adb_base() + args, capture_output=True, timeout=timeout)
    raw = proc.stdout or proc.stderr
    return raw.decode("utf-8", errors="ignore")


def detect_touch_device() -> dict[str, int | str]:
    """getevent/wm size からタッチ event と座標maxを毎回自動検出する。"""
    out = _adb_capture_text(["shell", "getevent", "-lp"], timeout=15)
    devices: list[dict[str, int | str | None]] = []
    current: dict[str, int | str | None] | None = None

    for line in out.splitlines():
        m_dev = re.search(r"add device \d+:\s+(\S+)", line)
        if m_dev:
            if current:
                devices.append(current)
            current = {"device": m_dev.group(1), "touch_max_x": None, "touch_max_y": None}
            continue
        if current is None:
            continue
        m_x = re.search(r"ABS_MT_POSITION_X\s+:.*max\s+(-?\d+)", line)
        if m_x:
            current["touch_max_x"] = int(m_x.group(1))
            continue
        m_y = re.search(r"ABS_MT_POSITION_Y\s+:.*max\s+(-?\d+)", line)
        if m_y:
            current["touch_max_y"] = int(m_y.group(1))
            continue
    if current:
        devices.append(current)

    candidates = [d for d in devices if d.get("touch_max_x") is not None and d.get("touch_max_y") is not None]
    if not candidates:
        raise RuntimeError("touch device not found: getevent -lp に ABS_MT_POSITION_X/Y が見つかりません")

    wm = _adb_capture_text(["shell", "wm", "size"], timeout=10)
    m_size = re.search(r"Physical size:\s*(\d+)x(\d+)", wm)
    if not m_size:
        raise RuntimeError(f"screen size not found: adb shell wm size output={wm!r}")

    chosen = candidates[0]
    return {
        "device": str(chosen["device"]),
        "touch_max_x": int(chosen["touch_max_x"]),
        "touch_max_y": int(chosen["touch_max_y"]),
        "screen_w": int(m_size.group(1)),
        "screen_h": int(m_size.group(2)),
    }


def to_raw(x: int | float, y: int | float, info: dict[str, int | str] | TouchInfo) -> tuple[int, int]:
    if isinstance(info, TouchInfo):
        touch_max_x = info.touch_max_x
        touch_max_y = info.touch_max_y
        screen_w = info.screen_w
        screen_h = info.screen_h
    else:
        touch_max_x = int(info["touch_max_x"])
        touch_max_y = int(info["touch_max_y"])
        screen_w = int(info["screen_w"])
        screen_h = int(info["screen_h"])
    raw_x = int(float(x) * touch_max_x / screen_w)
    raw_y = int(float(y) * touch_max_y / screen_h)
    return raw_x, raw_y


def _clamp(v: float, lo: int, hi: int) -> int:
    return int(max(lo, min(hi, round(v))))


class Sender:
    def swipe(self, path: list[tuple[int, int]], duration_ms: int, hold_ms: int = 0) -> None:
        raise NotImplementedError


class InputSwipeSender(Sender):
    def swipe(self, path: list[tuple[int, int]], duration_ms: int, hold_ms: int = 0) -> None:
        if not path:
            return
        x1, y1 = path[0]
        x2, y2 = path[-1]
        total_ms = max(1, duration_ms + hold_ms)
        adb_run(["shell", "input", "swipe",
                 str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(total_ms))],
                timeout=5)


class SendeventSender(Sender):
    EV_SYN = 0
    EV_KEY = 1
    EV_ABS = 3
    SYN_REPORT = 0
    BTN_TOUCH = 330
    ABS_MT_SLOT = 47
    ABS_MT_TRACKING_ID = 57
    ABS_MT_POSITION_X = 53
    ABS_MT_POSITION_Y = 54
    TRACKING_ID_UP = 4294967295

    def __init__(self) -> None:
        info = detect_touch_device()
        self.info = TouchInfo(
            device=str(info["device"]),
            touch_max_x=int(info["touch_max_x"]),
            touch_max_y=int(info["touch_max_y"]),
            screen_w=int(info["screen_w"]),
            screen_h=int(info["screen_h"]),
        )
        self._checked = False
        log.info(f"sendevent touch info: {self.info.as_dict()}")

    def check_available(self) -> None:
        if self._checked:
            return
        proc = adb_run(["shell", "sendevent", self.info.device, "0", "0", "0"],
                       timeout=5, capture=True)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"sendevent unavailable for {self.info.device}: {msg}")
        self._checked = True

    def _run_script(self, script_lines: list[str], timeout: int = 15) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, suffix=".sh") as f:
            local_script = Path(f.name)
            f.write("\n".join(script_lines) + "\n")
        try:
            adb_run(["push", str(local_script), SENDEVENT_REMOTE_SCRIPT], timeout=10, capture=True)
            adb_run(["shell", "sh", SENDEVENT_REMOTE_SCRIPT], timeout=timeout)
        finally:
            try:
                local_script.unlink(missing_ok=True)
            except Exception:
                pass
            adb_run(["shell", "rm", "-f", SENDEVENT_REMOTE_SCRIPT], timeout=5)

    def send_raw_events(self, events: list[dict[str, int | float]]) -> None:
        self.check_available()
        script_lines = ["#!/system/bin/sh", "set -e"]
        for ev in events:
            delay_s = float(ev.get("delay_s", 0.0))
            if delay_s > 0:
                script_lines.append(f"sleep {delay_s:.3f}")
            script_lines.append(
                f"sendevent {self.info.device} {int(ev['type'])} {int(ev['code'])} {int(ev['value'])}"
            )
        total_timeout = max(15, int(sum(float(e.get("delay_s", 0.0)) for e in events)) + 10)
        self._run_script(script_lines, timeout=total_timeout)

    def swipe(self, path: list[tuple[int, int]], duration_ms: int, hold_ms: int = 0) -> None:
        if not path:
            return
        self.check_available()
        tracking_id = random.randint(1, SENDEVENT_TRACKING_ID_MAX)
        raw_path = [to_raw(x, y, self.info) for x, y in path]
        step_sleep = max(0.001, duration_ms / max(1, len(raw_path) - 1) / 1000.0)
        script_lines = ["#!/system/bin/sh", "set -e"]

        def ev(t: int, c: int, v: int) -> None:
            script_lines.append(f"sendevent {self.info.device} {t} {c} {v}")

        def syn() -> None:
            ev(self.EV_SYN, self.SYN_REPORT, 0)

        x0, y0 = raw_path[0]
        ev(self.EV_ABS, self.ABS_MT_SLOT, 0)
        ev(self.EV_ABS, self.ABS_MT_TRACKING_ID, tracking_id)
        ev(self.EV_KEY, self.BTN_TOUCH, 1)
        ev(self.EV_ABS, self.ABS_MT_POSITION_X, x0)
        ev(self.EV_ABS, self.ABS_MT_POSITION_Y, y0)
        syn()
        if hold_ms > 0:
            script_lines.append(f"sleep {hold_ms / 1000.0:.3f}")

        for x, y in raw_path[1:]:
            script_lines.append(f"sleep {step_sleep:.3f}")
            ev(self.EV_ABS, self.ABS_MT_POSITION_X, x)
            ev(self.EV_ABS, self.ABS_MT_POSITION_Y, y)
            syn()

        ev(self.EV_ABS, self.ABS_MT_TRACKING_ID, self.TRACKING_ID_UP)
        ev(self.EV_KEY, self.BTN_TOUCH, 0)
        syn()
        self._run_script(script_lines, timeout=15)


_SENDER: Sender | None = None


def get_sender() -> Sender:
    global _SENDER
    if _SENDER is not None:
        return _SENDER
    if INPUT_BACKEND == "input":
        log.info("input backend: adb shell input swipe")
        _SENDER = InputSwipeSender()
    elif INPUT_BACKEND == "sendevent":
        _SENDER = SendeventSender()
    else:
        raise RuntimeError(f"unknown MONST_INPUT_BACKEND={INPUT_BACKEND!r}")
    return _SENDER


def make_human_path(x1: int, y1: int, x2: int, y2: int, curve: bool = True) -> list[tuple[int, int]]:
    j = random.randint(*SENDEVENT_COORD_JITTER_PX)
    sx = x1 + random.randint(-j, j)
    sy = y1 + random.randint(-j, j)
    ex = x2 + random.randint(-j, j)
    ey = y2 + random.randint(-j, j)
    steps = random.randint(*SENDEVENT_STEPS)
    if not curve:
        return [
            (
                _clamp(sx + (ex - sx) * i / steps, 0, SCREEN_W - 1),
                _clamp(sy + (ey - sy) * i / steps, 0, SCREEN_H - 1),
            )
            for i in range(steps + 1)
        ]

    dx = ex - sx
    dy = ey - sy
    length = max(1.0, math.hypot(dx, dy))
    nx = -dy / length
    ny = dx / length
    bend = random.choice((-1, 1)) * random.uniform(*SENDEVENT_CURVE_PX)
    c1 = (sx + dx * random.uniform(0.25, 0.40) + nx * bend,
          sy + dy * random.uniform(0.25, 0.40) + ny * bend)
    c2 = (sx + dx * random.uniform(0.60, 0.78) - nx * bend * random.uniform(0.4, 0.9),
          sy + dy * random.uniform(0.60, 0.78) - ny * bend * random.uniform(0.4, 0.9))
    path: list[tuple[int, int]] = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1.0 - t
        x = mt**3 * sx + 3 * mt**2 * t * c1[0] + 3 * mt * t**2 * c2[0] + t**3 * ex
        y = mt**3 * sy + 3 * mt**2 * t * c1[1] + 3 * mt * t**2 * c2[1] + t**3 * ey
        path.append((_clamp(x, 0, SCREEN_W - 1), _clamp(y, 0, SCREEN_H - 1)))
    return path


def tap(x: int, y: int, jitter: int = TAP_JITTER_PX, pre_pause: bool = True) -> None:
    """モンスト対策: `input tap` は弾かれるので `input swipe` で疑似ホールド。
       人間っぽさのために座標ジッター + ランダムホールド時間 + タップ前 pause。"""
    if pre_pause:
        time.sleep(random.uniform(*TAP_PRE_PAUSE))
    x += random.randint(-jitter, jitter)
    y += random.randint(-jitter, jitter)
    ms = random.randint(*TAP_HOLD_MS)
    try:
        get_sender().swipe([(int(x), int(y)), (int(x), int(y))], duration_ms=1, hold_ms=ms)
    except Exception as e:
        log.warning(f"tap failed @ ({x},{y}): {e}")
        raise


def swipe(x1: int, y1: int, x2: int, y2: int, dur_ms: int) -> None:
    try:
        duration = int(max(1, dur_ms * random.uniform(1.0 - SENDEVENT_DURATION_JITTER,
                                                      1.0 + SENDEVENT_DURATION_JITTER)))
        path = make_human_path(int(x1), int(y1), int(x2), int(y2), curve=INPUT_BACKEND == "sendevent")
        get_sender().swipe(path, duration_ms=duration)
    except Exception as e:
        log.warning(f"swipe failed: {e}")
        raise


def drag_and_release(x: int, y: int, dx: int, dy: int, hold_ms: int | None = None,
                     duration_ms: int | None = None, curve: bool = True) -> None:
    if hold_ms is None:
        hold_ms = random.randint(*SENDEVENT_DEFAULT_HOLD_MS)
    if duration_ms is None:
        duration_ms = random.randint(*FLICK_DURATION_MS)
    duration = int(max(1, duration_ms * random.uniform(1.0 - SENDEVENT_DURATION_JITTER,
                                                       1.0 + SENDEVENT_DURATION_JITTER)))
    path = make_human_path(int(x), int(y), int(x + dx), int(y + dy),
                           curve=curve and INPUT_BACKEND == "sendevent")
    try:
        get_sender().swipe(path, duration_ms=duration, hold_ms=hold_ms)
    except Exception as e:
        log.warning(f"drag_and_release failed: {e}")
        raise


EVENT_TYPE_NAMES = {
    "EV_SYN": 0,
    "EV_KEY": 1,
    "EV_ABS": 3,
}

EVENT_CODE_NAMES = {
    "SYN_REPORT": 0,
    "ABS_X": 0,
    "ABS_Y": 1,
    "ABS_PRESSURE": 24,
    "BTN_TOOL_FINGER": 325,
    "BTN_TOUCH": 330,
    "ABS_MT_SLOT": 47,
    "ABS_MT_TOUCH_MAJOR": 48,
    "ABS_MT_TOUCH_MINOR": 49,
    "ABS_MT_ORIENTATION": 52,
    "ABS_MT_TRACKING_ID": 57,
    "ABS_MT_POSITION_X": 53,
    "ABS_MT_POSITION_Y": 54,
    "ABS_MT_TOOL_TYPE": 55,
    "ABS_MT_PRESSURE": 58,
}


def _parse_event_token(token: str, mapping: dict[str, int]) -> int | None:
    token = token.strip()
    if token in mapping:
        return mapping[token]
    try:
        return int(token, 16)
    except ValueError:
        return None


def parse_getevent_log(text: str) -> list[dict[str, int | float]]:
    """getevent -lt の出力を replay 用 event list に変換する。"""
    events: list[dict[str, int | float]] = []
    last_ts: float | None = None
    for line in text.splitlines():
        m = re.search(r"\[\s*([0-9.]+)\]\s+(?:(?:/dev/input/\S+):\s+)?(\S+)\s+(\S+)\s+(\S+)", line)
        if not m:
            continue
        ts = float(m.group(1))
        typ = _parse_event_token(m.group(2), EVENT_TYPE_NAMES)
        code = _parse_event_token(m.group(3), EVENT_CODE_NAMES)
        if typ is None or code is None:
            continue
        value_token = m.group(4)
        try:
            value = int(value_token, 16)
        except ValueError:
            try:
                value = int(value_token)
            except ValueError:
                continue
        delay_s = 0.0 if last_ts is None else max(0.0, ts - last_ts)
        events.append({"delay_s": delay_s, "type": typ, "code": code, "value": value})
        last_ts = ts
    return events


def record_touch(seconds: float, out_json: Path | None = None) -> Path:
    info = detect_touch_device()
    device = str(info["device"])
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if out_json is None:
        out_json = LOG_DIR / f"touch_record_{datetime.now():%Y%m%d_%H%M%S}.json"
    cmd = _adb_base() + ["shell", "getevent", "-lt", device]
    log.info(f"record touch: {device} for {seconds}s -> {out_json}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        raw, _ = proc.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        raw, _ = proc.communicate()
    text = (raw or b"").decode("utf-8", errors="ignore")
    events = parse_getevent_log(text)
    payload = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "device_info": info,
        "duration_s": seconds,
        "events": events,
        "raw_log": text,
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"recorded events: {len(events)}")
    return out_json


def replay_touch(json_path: Path) -> None:
    sender = get_sender()
    if not isinstance(sender, SendeventSender):
        raise RuntimeError("replay_touch requires MONST_INPUT_BACKEND=sendevent")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    events = payload.get("events") or []
    if not events:
        raise RuntimeError(f"no replay events in {json_path}")
    log.info(f"replay touch: {json_path} events={len(events)}")
    sender.send_raw_events(events)


def flick() -> None:
    # pivot自体を毎回ずらす (人間は同じ点から発射しない)。pivot は安全ゾーン内に収まる。
    px = FLICK_PIVOT[0] + random.randint(-FLICK_PIVOT_JITTER, FLICK_PIVOT_JITTER)
    py = FLICK_PIVOT[1] + random.randint(-FLICK_PIVOT_JITTER, FLICK_PIVOT_JITTER)
    # 方向は全方位ランダム (user 確認: 方向で挙動変わらない)
    ang = random.uniform(0, 2 * math.pi)
    r = random.uniform(FLICK_RADIUS_MIN, FLICK_RADIUS_MAX)
    # 終点も安全ゾーンに必ずクリップ (y>1500 は無反応 / status画面開く罠)
    ex = int(max(SAFE_ZONE_X[0], min(SAFE_ZONE_X[1], px + r * math.cos(ang))))
    ey = int(max(SAFE_ZONE_Y[0], min(SAFE_ZONE_Y[1], py + r * math.sin(ang))))
    drag_and_release(px, py, ex - px, ey - py)


# ----------------------------- template matching -----------------------------

_TEMPLATE_CACHE: dict[str, np.ndarray | None] = {}


def _load_template(fname: str) -> np.ndarray | None:
    if fname in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[fname]
    path = TEMPLATE_DIR / fname
    img = cv2.imread(str(path)) if path.exists() else None
    _TEMPLATE_CACHE[fname] = img
    return img


def match(name: str, screen: np.ndarray | None = None) -> tuple[int, int] | None:
    if name not in TEMPLATES:
        return None
    fname, th = TEMPLATES[name]
    tmpl = _load_template(fname)
    if tmpl is None:
        return None
    if screen is None:
        screen = screencap()
    if screen is None:
        return None
    if tmpl.shape[0] > screen.shape[0] or tmpl.shape[1] > screen.shape[1]:
        return None
    res = cv2.matchTemplate(screen, tmpl, cv2.TM_CCOEFF_NORMED)
    _, val, _, loc = cv2.minMaxLoc(res)
    if val < th:
        return None
    h, w = tmpl.shape[:2]
    return (loc[0] + w // 2, loc[1] + h // 2)


def match_score(name: str, screen: np.ndarray | None = None) -> float:
    """テンプレマッチのスコアだけ返す (デバッグ用)。"""
    if name not in TEMPLATES:
        return 0.0
    fname, _ = TEMPLATES[name]
    tmpl = _load_template(fname)
    if tmpl is None:
        return 0.0
    if screen is None:
        screen = screencap()
    if screen is None:
        return 0.0
    if tmpl.shape[0] > screen.shape[0] or tmpl.shape[1] > screen.shape[1]:
        return 0.0
    res = cv2.matchTemplate(screen, tmpl, cv2.TM_CCOEFF_NORMED)
    _, val, _, _ = cv2.minMaxLoc(res)
    return float(val)


# ----------------------------- helpers -----------------------------

def human_sleep(rng: tuple[float, float]) -> None:
    time.sleep(random.uniform(*rng))


def img_diff(a: np.ndarray, b: np.ndarray) -> float:
    """2画面の差分スコア (0=完全一致, 1=完全に違う)。stuck検出用。"""
    if a.shape != b.shape:
        return 1.0
    diff = cv2.absdiff(a, b)
    return float(diff.mean() / 255.0)


def save_exception(screen: np.ndarray, tag: str) -> Path:
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREEN_DIR / f"exception_{datetime.now():%Y%m%d_%H%M%S}_{tag}.png"
    cv2.imwrite(str(path), screen)
    return path


_DEBUG_RUN_TAG = datetime.now().strftime("%Y%m%d_%H%M%S")
_debug_step_counter = 0


def debug_snap(label: str) -> None:
    """DEBUG_SCREENSHOTS=ON のとき screens/debug_<run>/step_NN_<label>.png を残す。"""
    global _debug_step_counter
    if not DEBUG_SCREENSHOTS:
        return
    img = screencap()
    if img is None:
        return
    out_dir = SCREEN_DIR / f"debug_{_DEBUG_RUN_TAG}"
    out_dir.mkdir(parents=True, exist_ok=True)
    _debug_step_counter += 1
    path = out_dir / f"step_{_debug_step_counter:02d}_{label}.png"
    cv2.imwrite(str(path), img)
    log.info(f"  debug snap: {path.name}")


# ----------------------------- core flow -----------------------------

def detect_state(screen: np.ndarray | None = None) -> str:
    """画面から現在状態を推定。判定不能なら 'unknown'。"""
    if screen is None:
        screen = screencap()
    if screen is None:
        return "no_screen"
    if match("exit_confirm", screen):
        return "exit_confirm"
    if match("stamina_full", screen):
        return "stamina_out"
    if match("koryaku_hint", screen):
        return "koryaku_hint"
    if match("kyouka", screen):
        return "kyouka"
    if match("gacha", screen):
        return "gacha"
    if match("deck", screen):
        return "deck"
    if match("result", screen):
        return "result"
    if match("home", screen):
        return "home"
    if is_battle_screen(screen):
        return "battle"
    return "unknown"


def _hsv_ratio(roi: np.ndarray, h_min: int, h_max: int, s_min: int, v_min: int) -> float:
    if roi.size == 0:
        return 0.0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = (
        (hsv[:, :, 0] >= h_min)
        & (hsv[:, :, 0] <= h_max)
        & (hsv[:, :, 1] > s_min)
        & (hsv[:, :, 2] > v_min)
    )
    return float(mask.mean())


def is_battle_screen(screen: np.ndarray | None = None) -> bool:
    """Detect in-quest battle UI from stable bottom HUD regions.

    Uses a composite of the HP green bar, orange battle HUD, and blue menu area
    so regular home/deck/result screens do not get mistaken for battle.
    """
    if screen is None:
        screen = screencap()
    if screen is None:
        return False
    h, w = screen.shape[:2]
    if h < 2100 or w < 1000:
        return False
    hp_roi = screen[1635:1725, 0:880]
    bottom_roi = screen[1940:2090, 0:1080]
    menu_roi = screen[1900:2070, 870:1080]
    hp_green = _hsv_ratio(hp_roi, 35, 90, 80, 80)
    bottom_orange = _hsv_ratio(bottom_roi, 8, 35, 80, 100)
    menu_blue = _hsv_ratio(menu_roi, 90, 130, 80, 80)
    return hp_green > 0.04 and bottom_orange > 0.25 and menu_blue > 0.04


def tap_sweep_ok() -> None:
    """state不明時の最終フォールバック。nav strip (y>=2150) を絶対に踏まない位置だけ。
       - 1380 = 攻略のヒント / 各種dialog OK
       - 1500 = 中央dialog ボタンや 確認 系
       - 1680 = リザルト送り (スペシャル報酬画面)
       - 2100 = 強化素材画面の下部OK (nav strip より上)
    """
    for y in [1380, 1500, 1680, 2100]:
        tap(OK_SWEEP_X, y, jitter=10)
        time.sleep(0.4)


def do_battle() -> str:
    """バトル終了サインが出るまでフリック。終端理由を返す。
       終了サイン: 攻略のヒント / リザルト / ホーム / timeout。"""
    log.info("battle start -> flick loop")
    start = time.time()
    flicks = 0
    while True:
        screen = screencap()
        if screen is not None:
            if match("koryaku_hint", screen):
                log.info(f"battle end: 攻略のヒント検知 (flicks={flicks})")
                return "koryaku_hint"
            if match("result", screen):
                log.info(f"battle end: result検知 (flicks={flicks})")
                return "result"
            if match("home", screen):
                log.info("battle end: home検知 (中断扱い)")
                return "home"
        if time.time() - start > BATTLE_TIMEOUT:
            log.warning(f"battle timeout (flicks={flicks})")
            return "timeout"
        flick()
        flicks += 1
        human_sleep(FLICK_INTERVAL)


def press_shutsu_until_departed(max_attempts: int = DEPARTURE_RETRIES) -> str:
    """出撃を押し、デッキ画面から抜けたか確認する。
       戻り値: 'departed' / 'stamina_out' / 'stuck'"""
    for attempt in range(1, max_attempts + 1):
        log.info(f"step 2: shutsu {COORDS['shutsu']} attempt {attempt}/{max_attempts} (no-jitter)")
        tap(*COORDS["shutsu"], jitter=0)
        human_sleep(BATTLE_WAIT_START)
        screen = screencap()
        state = detect_state(screen)
        log.info(f"  after shutsu: state={state}")
        if state == "stamina_out":
            tap_sweep_ok()
            return "stamina_out"
        if state != "deck":
            return "departed"
        if screen is not None:
            save_exception(screen, f"still_deck_after_shutsu_{attempt}")

    log.warning("出撃後もデッキ画面に残留 -> stuck")
    return "stuck"


def return_to_home(max_tries: int = 20) -> str:
    """post-battleダイアログ群を state-aware で1つずつ進めてホームへ。
       戻り値: 'home' / 'stamina_out' / 'stuck'

       重要: state=unknown では絶対にタップしない (誤爆でガチャ画面等に飛ぶ)。
       wait → 状態変化を待つ → だめなら N 回後に最小限の上半分sweepを試す。"""
    log.info("post-battle: ホーム復帰開始")
    unknown_streak = 0
    last_screen = None
    same_count = 0

    for i in range(max_tries):
        screen = screencap()
        if screen is None:
            time.sleep(2.0)
            continue
        state = detect_state(screen)
        log.info(f"  step {i+1}/{max_tries}: state={state}")

        if state == "home":
            return "home"
        if state == "stamina_out":
            return "stamina_out"

        # state別の決め打ちタップ (nav strip非干渉)
        if state == "koryaku_hint":
            tap(540, 1380)
            unknown_streak = 0
        elif state == "result":
            tap(540, 1680)
            unknown_streak = 0
        elif state == "kyouka":
            # OK button 中心 y=2125 (probe: y=2086-2195, mass center=2152, dense=2125)
            tap(540, 2125)
            unknown_streak = 0
        elif state == "gacha":
            log.info("ガチャ画面検出 -> 左下ホームへ復帰")
            tap(*COORDS["home_tab"], jitter=8)
            unknown_streak = 0
        else:
            # unknown: タップせず待つ。連続したらスクショ採取 + 最終手段
            unknown_streak += 1
            if last_screen is not None and img_diff(screen, last_screen) < 0.01:
                same_count += 1
            else:
                same_count = 0
            last_screen = screen

            log.info(f"    unknown streak={unknown_streak}, same_count={same_count}")
            # 未知画面をテンプレ採取の元ネタとして保存
            if unknown_streak in (2, 5, 9):
                save_exception(screen, f"return_home_unknown_{unknown_streak}")

            if unknown_streak >= 4 and same_count >= 2:
                # 完全静止: 明確にdialogが固まってる可能性。上半分だけ広めにタップ。
                log.warning("固まりっぽい -> 上半分sweep")
                tap_sweep_ok()
                same_count = 0
            elif unknown_streak >= 10:
                log.warning("unknown 多発 -> 諦め")
                save_exception(screen, "stuck_unknown")
                return "stuck"

        human_sleep((2.5, 3.5))

    log.warning("ホーム未到達 (max step)")
    save_exception(screen if screen is not None else np.zeros((10, 10, 3), np.uint8), "no_home")
    return "stuck"


def run_one_cycle() -> tuple[str, str]:
    """ホームから1周回す。戻り値: (result, note)
       result ∈ {ok, stamina_out, stuck, timeout, error}"""
    start_ts = time.time()

    log.info("== cycle start ==")
    screen = screencap()
    state = detect_state(screen)

    # ホーム上で誤って終了確認が開いていたらまず閉じる
    if state == "exit_confirm":
        log.info("exit_confirm検出 -> いいえ")
        tap(*COORDS["iie"])
        human_sleep(STEP_WAIT)
        screen = screencap()
        state = detect_state(screen)

    if state == "stamina_out":
        log.info("ホーム手前でスタミナ不足検出")
        tap_sweep_ok()
        return "stamina_out", "before_shortcut"

    start_from_deck = state == "deck"
    if state != "home" and not start_from_deck:
        log.warning(f"想定外の開始状態: {state} -> sweepでホーム復帰試行")
        recover = return_to_home(max_tries=8)
        if recover != "home":
            return recover, "initial_recover"

    debug_snap("00_deck" if start_from_deck else "00_home")

    if not start_from_deck:
        # 1) スーパーショートカット → デッキ画面まで一気
        log.info(f"step 1: super_shortcut {COORDS['super_shortcut']}")
        tap(*COORDS["super_shortcut"])
        human_sleep(LOAD_WAIT)
        debug_snap("01_after_super_shortcut")

        # スタミナ不足はスーパーショートカット → デッキ遷移時に出る
        screen = screencap()
        if screen is not None and match("stamina_full", screen):
            log.info("スーパーショートカット後にスタミナ不足検出")
            tap_sweep_ok()
            return "stamina_out", "after_super_shortcut"

    departure = press_shutsu_until_departed()
    if departure == "stamina_out":
        return "stamina_out", "after_shutsu"
    if departure != "departed":
        return "stuck", "after_shutsu_still_deck"
    debug_snap("02_after_shutsu")

    # 6) バトル
    reason = do_battle()
    if reason == "timeout":
        screen = screencap()
        save_exception(screen if screen is not None else np.zeros((10,10,3), np.uint8),
                       "battle_timeout")
        return "timeout", "battle"

    # 7) post-battle ダイアログ群を sweep でホームまで掃く
    human_sleep((1.0, 1.8))
    recover = return_to_home(max_tries=20)
    duration = int(time.time() - start_ts)

    if recover == "home":
        log.info(f"cycle ok ({duration}s)")
        return "ok", f"{duration}s"
    if recover == "stamina_out":
        log.info(f"cycle後半でスタミナ切れ ({duration}s)")
        return "stamina_out", f"{duration}s_post_battle"
    log.warning(f"cycle stuck ({duration}s)")
    return "stuck", f"{duration}s_post_battle"


def run() -> None:
    log.info("=== モンスト周回 bot 起動 (Ctrl+C で停止) ===")
    log.info("画面スリープ防止: adb shell settings put global stay_on_while_plugged_in 3")
    log.info(f"input backend: {INPUT_BACKEND}")

    try:
        sender = get_sender()
        if isinstance(sender, SendeventSender):
            sender.check_available()
    except Exception as e:
        log.error(f"入力バックエンド初期化失敗: {e}")
        notify_windows("monst-bot 入力初期化失敗", str(e))
        return

    # BAN対策: 自動停止上限 (env var で上書き可)
    max_hours = float(os.environ.get("MONST_MAX_HOURS", "0") or 0)
    deadline = time.time() + max_hours * 3600 if max_hours > 0 else None
    if deadline:
        log.info(f"自動停止: {max_hours}h 後 = {datetime.fromtimestamp(deadline):%H:%M}")

    # 人っぽい長休憩: 8-12周ごとに3-7分の break (MONST_NO_BREAK=1 で無効化)
    no_break = os.environ.get("MONST_NO_BREAK", "") == "1"
    BREAK_EVERY = 10**9 if no_break else random.randint(8, 12)
    BREAK_SEC = lambda: random.uniform(180, 420)
    if no_break:
        log.info("長休憩は無効 (ぶっ通し運用)")

    cycle = 0
    consecutive_errors = 0

    while True:
        if deadline and time.time() >= deadline:
            log.info(f"=== 自動停止時刻到達 ({max_hours}h)。停止 ===")
            notify_windows("monst-bot 自動停止", f"{cycle}周完了。{max_hours}h経過で停止しました。")
            break
        try:
            cycle += 1
            log.info(f"\n========== cycle {cycle} ==========")
            ts0 = time.time()
            result, note = run_one_cycle()
            ts1 = time.time()
            append_run_csv(ts0, ts1, result, note)

            if result == "ok":
                consecutive_errors = 0
                # 周回間に短い休憩 (検出回避)
                time.sleep(random.uniform(1.5, 3.0))
                # 一定周ごとに人っぽい長休憩 (連続周回100%は機械臭い)
                if cycle > 0 and cycle % BREAK_EVERY == 0:
                    bsec = BREAK_SEC()
                    log.info(f"== 長休憩 {int(bsec)}秒 ({cycle}周完了時点) ==")
                    time.sleep(bsec)
                    BREAK_EVERY = random.randint(8, 12)  # 次の break タイミングも変える
                continue

            if result == "stamina_out":
                log.info(f"スタミナ切れ -> {STAMINA_WAIT // 60}分待機")
                time.sleep(STAMINA_WAIT)
                consecutive_errors = 0
                continue

            if result in ("timeout", "stuck", "error"):
                consecutive_errors += 1
                log.warning(f"異常終了: {result} ({note})。連続 {consecutive_errors} 回")
                if consecutive_errors >= 3:
                    screen = screencap()
                    if screen is not None:
                        path = save_exception(screen, "consecutive_fail")
                        log.error(f"連続失敗 -> 停止。状態保存: {path.name}")
                        notify_windows("monst-bot 停止",
                                       f"連続 {consecutive_errors} 回失敗。手動確認お願いします。\n"
                                       f"画像: {path.name}")
                    break
                # ホーム復帰試行 (バックキーは押さない: ホームで押すと終了確認が出る)
                log.info("post-battle sweepでホーム復帰試行")
                recover = return_to_home(max_tries=15)
                log.info(f"recover result: {recover}")
                time.sleep(5.0)

        except KeyboardInterrupt:
            log.info(f"停止 (累計 {cycle} cycle)")
            break
        except Exception as e:
            log.exception(f"例外: {e}")
            consecutive_errors += 1
            time.sleep(5.0)
            if consecutive_errors >= 3:
                screen = screencap()
                if screen is not None:
                    path = save_exception(screen, "exception")
                    notify_windows("monst-bot 例外停止", f"{e}\n画像: {path.name}")
                break


# ----------------------------- CLI -----------------------------

def shot() -> None:
    img = screencap()
    if img is None:
        log.error("スクショ失敗。adb devices で接続確認 (unauthorized なら端末で許可)。")
        return
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREEN_DIR / "screen.png"
    cv2.imwrite(str(path), img)
    h, w = img.shape[:2]
    log.info(f"{path} を保存 ({w}x{h})")


def list_templates() -> None:
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\ntemplate dir: {TEMPLATE_DIR}")
    print(f"{'name':<14} {'file':<18} {'thresh':<7} status")
    print("-" * 55)
    for name, (fname, th) in TEMPLATES.items():
        exists = (TEMPLATE_DIR / fname).exists()
        status = "OK" if exists else "MISSING"
        print(f"{name:<14} {fname:<18} {th:<7} {status}")
    print()


def calibrate() -> None:
    img = screencap()
    if img is None:
        log.error("スクショ失敗。adb devices で接続確認。")
        return
    h, w = img.shape[:2]
    print(f"\n端末解像度: {w} x {h}")
    print(f"\n推奨 CONFIG:")
    print(f"  SCREEN_W = {w}")
    print(f"  SCREEN_H = {h}")
    print(f"  FLICK_PIVOT = ({w // 2}, {int(h * 0.62)})")
    print(f"  COORDS['shortcut'] は端末で getevent で実測する: ")
    print(f"    adb shell timeout 30 getevent -lt /dev/input/event2 > tap.txt")


def touch_info() -> None:
    info = detect_touch_device()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    sender = SendeventSender()
    try:
        sender.check_available()
        print("sendevent: OK")
    except Exception as e:
        print(f"sendevent: ERROR: {e}")


def test_swipe() -> None:
    """ホーム等で横ページ送り確認用。現在画面に対して短い右→左スワイプを送る。"""
    log.info("test swipe: right -> left")
    swipe(820, 1410, 260, 1410, 420)


def help_text() -> None:
    print("""
usage: python monst_rank_bot.py [shot|run|templates|calibrate|touch-info|test-swipe|record-touch|replay-touch|help]

  shot        現在画面を screens/screen.png に保存
  run         周回開始 (Ctrl+C で停止)
  templates   テンプレ整備状況を一覧
  calibrate   解像度に応じた CONFIG 推奨値を表示
  touch-info  getevent/wm size から touch device と座標maxを表示し sendevent 権限確認
  test-swipe  現在画面に短い右→左スワイプを送る
  record-touch [seconds] [out.json]
              実機の touch event を記録して JSON 保存
  replay-touch <json>
              record-touch の JSON を sendevent で再生
""")


if __name__ == "__main__":
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "shot":
        shot()
    elif mode == "run":
        run()
    elif mode == "templates":
        list_templates()
    elif mode == "calibrate":
        calibrate()
    elif mode == "touch-info":
        touch_info()
    elif mode == "test-swipe":
        test_swipe()
    elif mode == "record-touch":
        seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
        out = Path(sys.argv[3]) if len(sys.argv) > 3 else None
        print(record_touch(seconds, out))
    elif mode == "replay-touch":
        if len(sys.argv) < 3:
            help_text()
            sys.exit(2)
        replay_touch(Path(sys.argv[2]))
    elif mode in ("help", "-h", "--help"):
        help_text()
    else:
        help_text()
        sys.exit(2)
