# -*- coding: utf-8 -*-
"""
音乐管家 — 本地音乐管理播放器

功能：
1. 设置里可自定义程序首页名字，设置保存在程序同目录的 cangku 文件夹中
2. 歌曲统一存放在 cangku/歌曲库 文件夹下
3. 窗口内按流派分类管理歌曲，可导入 MP3
4. 调用大模型 API（OpenAI 兼容接口）提取歌词并设计封面，
   结果保存到 cangku/歌词、cangku/封面，下次播放不再重复调用，节省 token
5. 播放时展示封面与歌词，点击歌词任意一行可跳转到对应时间播放
6. 支持顺序播放 / 随机播放 / 单曲循环三种模式
7. 设置里可填写 AI 大模型 API，支持测试连接；断网时提示网络出错
"""

import os
import sys
import json
import re
import random
import time
import queue
import threading
import traceback

import requests
import pygame

from PIL import Image, ImageDraw, ImageFont, ImageTk

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:  # 高分屏清晰显示
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


# ====================== 路径与持久化 ======================

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


CANGKU = os.path.join(app_dir(), "cangku")
SONG_DIR = os.path.join(CANGKU, "歌曲库")
LYRIC_DIR = os.path.join(CANGKU, "歌词")
COVER_DIR = os.path.join(CANGKU, "封面")
CONFIG_PATH = os.path.join(CANGKU, "config.json")
LIBRARY_PATH = os.path.join(CANGKU, "library.json")

DEFAULT_CONFIG = {
    "app_name": "音乐管家",
    "api_base": "",
    "api_key": "",
    "model": "",
    "play_mode": "order",   # order / shuffle / loop
    "volume": 80,
}


def ensure_dirs():
    for d in (CANGKU, SONG_DIR, LYRIC_DIR, COVER_DIR):
        os.makedirs(d, exist_ok=True)


def load_config():
    ensure_dirs()
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_library():
    try:
        with open(LIBRARY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_library(lib):
    ensure_dirs()
    with open(LIBRARY_PATH, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=2)


def lrc_path(sid):
    return os.path.join(LYRIC_DIR, sid + ".lrc")


def cover_path(sid):
    return os.path.join(COVER_DIR, sid + ".jpg")


def mp3_path(fn):
    return os.path.join(SONG_DIR, fn)


def probe_duration(mp3):
    try:
        return int(pygame.mixer.Sound(mp3).get_length())
    except Exception:
        return 0


# ====================== LRC 歌词解析 ======================

def parse_lrc(text):
    """把 LRC 文本解析成 [(时间秒, 歌词), ...]，按时间排序。"""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        for tag in re.findall(r"\[([0-9:.]+)\]", line):
            parts = [float(x) for x in tag.split(":")]
            t = 0.0
            for p in parts:
                t = t * 60 + p
            content = re.sub(r"\[[^\]]*\]", "", line).strip()
            lines.append((round(t, 2), content))
    lines.sort(key=lambda x: x[0])
    return lines


# ====================== AI 接口（OpenAI 兼容） ======================

class ApiError(Exception):
    def __init__(self, msg, kind="generic"):
        super().__init__(msg)
        self.kind = kind  # config / network / http / parse


def api_call(cfg, messages, timeout=90):
    base = (cfg.get("api_base") or "").strip().rstrip("/")
    key = (cfg.get("api_key") or "").strip()
    model = (cfg.get("model") or "").strip()
    if not base:
        raise ApiError("尚未填写API地址，请先在设置中配置", "config")
    if not key:
        raise ApiError("尚未填写API Key，请先在设置中配置", "config")
    if not model:
        raise ApiError("尚未填写模型名称，请先在设置中配置", "config")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "temperature": 0.7, "stream": False}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        raise ApiError("请求超时，请检查网络连接或API地址是否正确", "network")
    except requests.exceptions.SSLError:
        raise ApiError("网络出错（SSL），请检查网络连接", "network")
    except requests.exceptions.ConnectionError:
        raise ApiError("网络出错，请检查网络连接", "network")
    except requests.exceptions.RequestException as e:
        raise ApiError("网络出错：" + str(e), "network")
    if resp.status_code != 200:
        raise ApiError("接口返回错误（HTTP %d）：%s" % (resp.status_code, resp.text[:200]), "http")
    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise ApiError("接口返回格式异常，请检查API地址与模型是否正确", "parse")


def extract_json(text):
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ApiError("模型未返回有效JSON，请重试或更换模型", "parse")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        raise ApiError("模型返回的JSON解析失败，请重试或更换模型", "parse")


AI_SYSTEM = (
    "你是一位专业的音乐信息处理助手。用户会提供一首歌曲的文件名（通常包含歌名，可能还包含歌手名），请完成以下任务：\n"
    "1. 提取歌曲名和歌手；判断歌曲风格（例如：流行、摇滚、民谣、电子、说唱、R&B、古风、爵士、纯音乐、其他等）。\n"
    "2. 提供歌词，格式为LRC：每行形如 [分钟:秒.百分秒]歌词内容，例如 [00:03.50]第一句歌词，时间戳要覆盖整首歌节奏，一句一行。"
    "如果你熟悉这首歌，请给出真实完整歌词；如果不熟悉，请根据歌名创作风格贴合、意境合理的歌词，按约3分半的时长排布，每句间隔3-6秒。\n"
    "3. 提供封面设计参数：一句话画面描述（不超过50字）、两个主色调（十六进制颜色值）、一个最贴切主题的emoji。\n"
    "4. 如果判断为纯音乐，lrc 字段填 \"[00:00.00]纯音乐\"。\n"
    "只输出一个JSON对象，不要输出任何其他文字、解释或代码块标记，JSON结构为：\n"
    "{\"title\":\"歌曲名\",\"artist\":\"歌手\",\"genre\":\"风格\",\"lrc\":\"[00:00.00]第一句歌词\\n[00:04.00]第二句歌词\",\"cover_desc\":\"画面描述\",\"color1\":\"#AA0000\",\"color2\":\"#0033AA\",\"emoji\":\"🎵\"}\n"
    "注意：lrc 字段内多行歌词之间用 \\n 转义符分隔。"
)


# ====================== 封面生成（本地绘制） ======================

def hex_to_rgb(h, default):
    h = (h or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return default


def _load_font(size):
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/Deng.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def draw_note(d, cx, cy, s, fill):
    """用基本形状画一个简单的音符。"""
    d.ellipse([cx - s, cy - s * 0.65, cx - s * 0.32, cy + s * 0.12], fill=fill)
    d.ellipse([cx + s * 0.32, cy - s * 0.65, cx + s, cy + s * 0.12], fill=fill)
    d.rectangle([cx - s * 0.80, cy - s * 2.0, cx - s * 0.64, cy - s * 0.65], fill=fill)
    d.rectangle([cx + s * 0.36, cy - s * 2.0, cx + s * 0.52, cy - s * 0.65], fill=fill)
    d.rectangle([cx - s * 0.88, cy - s * 2.0, cx + s * 0.62, cy - s * 1.78], fill=fill)


def make_cover(title, artist, cover_desc, c1, c2, out_path, size=600):
    """根据 AI 提供的颜色/描述生成一张方形封面。"""
    W = H = size
    top = hex_to_rgb(c1, (24, 30, 66))
    bot = hex_to_rgb(c2, (110, 44, 130))
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        d.line([(0, y), (W, y)], fill=(r, g, b))
    # 装饰层（半透明圆形 + 音符）
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.ellipse([W * 0.52, -H * 0.28, W * 1.32, H * 0.52], fill=(255, 255, 255, 26))
    od.ellipse([-W * 0.30, H * 0.58, W * 0.42, H * 1.30], fill=(255, 255, 255, 20))
    od.ellipse([W * 0.58, H * 0.60, W * 1.02, H * 1.04], fill=(255, 255, 255, 14))
    draw_note(od, W * 0.5, H * 0.32, W * 0.13, (255, 255, 255, 90))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    d = ImageDraw.Draw(img)
    # 标题 / 歌手
    def draw_center_text(text, cy, size, color, shadow=True):
        font = _load_font(size)
        while size > 20:
            try:
                w = d.textlength(text, font=font)
                if w <= W * 0.86:
                    break
            except Exception:
                pass
            size -= 2
            font = _load_font(size)
        try:
            w = d.textlength(text, font=font)
        except Exception:
            w = len(text) * size
        x = (W - w) / 2
        if shadow:
            d.text((x + 3, cy + 3), text, font=font, fill=(0, 0, 0, 120))
        d.text((x, cy), text, font=font, fill=color)
        return size
    title = (title or "").strip() or "未命名歌曲"
    artist = (artist or "").strip()
    ts = draw_center_text(title, H * 0.60, 88, (255, 255, 255))
    if artist:
        draw_center_text(artist, H * 0.60 + ts * 1.6, max(int(ts * 0.42), 24), (230, 230, 240))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path, "JPEG", quality=88)
    return out_path


def make_placeholder_cover(size=600):
    """未处理歌曲的默认封面。"""
    img = Image.new("RGB", (size, size), (30, 32, 44))
    d = ImageDraw.Draw(img)
    for y in range(size):
        d.line([(0, y), (size, y)], fill=(34 + int(26 * y / size), 36 + int(28 * y / size), 52 + int(30 * y / size)))
    d = ImageDraw.Draw(img)
    draw_note(d, size * 0.5, size * 0.36, size * 0.14, (255, 255, 255, 70))
    font = _load_font(int(size * 0.075))
    d.text((size * 0.5, size * 0.62), "等待AI处理", font=font, fill=(170, 174, 196), anchor="mm")
    return img


# ====================== 界面主题 ======================

COL_BG = "#17171f"
COL_PANEL = "#20202c"
COL_PANEL2 = "#292936"
COL_TEXT = "#e8e8f2"
COL_SUB = "#9a9ab0"
COL_ACCENT = "#ff8c42"
COL_ACCENT2 = "#ffb066"
COL_ERR = "#ef5350"
COL_OK = "#4caf50"


# ====================== 主程序 ======================

class MusicApp:

    def __init__(self):
        ensure_dirs()
        self.cfg = load_config()
        self.library = self.scan_library()
        self.audio_ok = self._init_audio()

        self.root = tk.Tk()
        self.root.title(self.cfg.get("app_name", "音乐管家"))
        self.root.geometry("1040x680")
        self.root.minsize(920, 600)
        self.root.configure(bg=COL_BG)
        try:
            self.root.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass

        self.evq = queue.Queue()          # 工作线程 -> 主线程消息
        self.cancel_ai = False

        # 播放状态
        self.current_id = None
        self.dur = 0
        self._base_ms = 0          # 本次 play() 起点（毫秒）
        self._paused_ms = 0
        self.paused = False
        self._started = False
        self.dragging = False
        self.volume = int(self.cfg.get("volume", 80))
        self.play_mode = self.cfg.get("play_mode", "order")

        # 歌词状态
        self.lyric_times = []
        self.lyric_idx = -1

        # 封面
        self.cover_photo = None
        self._placeholder = None

        self._settings_dialog = None
        self._ai_running = False

        self._build_styles()
        self._build_ui()
        self.refresh_genres()
        self.refresh_rows()
        self.update_mode_button()
        self.tick()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 基础 ----------

    def _init_audio(self):
        try:
            pygame.mixer.pre_init(44100, -16, 2, 4096)
            pygame.mixer.init()
            return True
        except Exception as e:
            print("音频初始化失败:", e)
            return False

    def _build_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=COL_BG)
        style.configure("Music.Treeview",
                        background=COL_PANEL, fieldbackground=COL_PANEL,
                        foreground=COL_TEXT, borderwidth=0, rowheight=28,
                        font=("Microsoft YaHei UI", 10))
        style.map("Music.Treeview",
                  background=[("selected", COL_ACCENT)],
                  foreground=[("selected", "#141414")])
        style.configure("Music.Treeview.Heading",
                        background=COL_PANEL2, foreground=COL_SUB,
                        relief="flat", font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Music.Treeview.Heading", background=[("active", COL_PANEL2)])
        style.configure("Music.TCombobox",
                        background=COL_PANEL2, fieldbackground=COL_PANEL2,
                        foreground=COL_TEXT, arrowcolor=COL_SUB,
                        bordercolor=COL_PANEL2, lightcolor=COL_PANEL2, darkcolor=COL_PANEL2)
        style.map("Music.TCombobox", fieldbackground=[("readonly", COL_PANEL2)])
        style.configure("Horizontal.TScale", background=COL_BG, troughcolor=COL_PANEL2,
                        bordercolor=COL_PANEL2, lightcolor=COL_ACCENT, darkcolor=COL_ACCENT)

    # ---------- 界面构建 ----------

    def _build_ui(self):
        root = self.root
        # 顶栏
        header = tk.Frame(root, bg=COL_BG)
        header.pack(fill="x", padx=14, pady=(12, 6))
        self.app_title = tk.Label(header, text=self.cfg.get("app_name", "音乐管家"),
                                  font=("Microsoft YaHei UI", 17, "bold"),
                                  fg=COL_TEXT, bg=COL_BG)
        self.app_title.pack(side="left")
        tk.Button(header, text="⚙ 设置", command=self.open_settings,
                  bg=COL_PANEL2, fg=COL_TEXT, activebackground=COL_ACCENT,
                  activeforeground="#141414", relief="flat", bd=0,
                  font=("Microsoft YaHei UI", 10), padx=12, pady=4,
                  cursor="hand2").pack(side="right")

        # 主体：左列表 / 右播放器
        paned = ttk.Panedwindow(root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=14, pady=6)

        left = tk.Frame(paned, bg=COL_PANEL)
        right = tk.Frame(paned, bg=COL_PANEL, width=430)
        paned.add(left, weight=3)
        paned.add(right, weight=2)
        right.pack_propagate(False)

        self._build_library_panel(left)
        self._build_player_panel(right)

        # 状态栏
        status = tk.Frame(root, bg=COL_BG)
        status.pack(fill="x", padx=14, pady=(0, 10))
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(status, textvariable=self.status_var, fg=COL_SUB, bg=COL_BG,
                 font=("Microsoft YaHei UI", 9), anchor="w").pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(status, orient="horizontal", mode="determinate",
                                        length=180, maximum=100)
        self.cancel_btn = tk.Button(status, text="取消AI处理", command=self.cancel_ai_process,
                                    bg=COL_PANEL2, fg=COL_TEXT, relief="flat", bd=0,
                                    font=("Microsoft YaHei UI", 9), padx=10, cursor="hand2")
        self.cancel_btn.pack(side="right")
        self.cancel_btn.pack_forget()
        if not self.audio_ok:
            self.set_status("警告：音频设备初始化失败，无法播放，但仍可管理歌曲")

    # ----- 左侧：曲库 -----

    def _build_library_panel(self, parent):
        bar = tk.Frame(parent, bg=COL_PANEL)
        bar.pack(fill="x", padx=10, pady=(10, 6))
        tk.Button(bar, text="导入音乐", command=self.import_music,
                  bg=COL_ACCENT, fg="#141414", activebackground=COL_ACCENT2,
                  relief="flat", bd=0, font=("Microsoft YaHei UI", 10, "bold"),
                  padx=10, pady=4, cursor="hand2").pack(side="left")
        tk.Button(bar, text="AI处理", command=self.ai_process,
                  bg=COL_PANEL2, fg=COL_TEXT, activebackground=COL_ACCENT,
                  activeforeground="#141414", relief="flat", bd=0,
                  font=("Microsoft YaHei UI", 10), padx=10, pady=4,
                  cursor="hand2").pack(side="left", padx=(8, 0))
        tk.Button(bar, text="删除", command=self.delete_selected,
                  bg=COL_PANEL2, fg=COL_TEXT, activebackground="#d9534f",
                  relief="flat", bd=0, font=("Microsoft YaHei UI", 10),
                  padx=10, pady=4, cursor="hand2").pack(side="left", padx=(8, 0))
        tk.Button(bar, text="打开存储文件夹", command=self.open_cangku,
                  bg=COL_PANEL2, fg=COL_TEXT, activebackground=COL_ACCENT,
                  activeforeground="#141414", relief="flat", bd=0,
                  font=("Microsoft YaHei UI", 9), padx=8, pady=4,
                  cursor="hand2").pack(side="right")

        filt = tk.Frame(parent, bg=COL_PANEL)
        filt.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(filt, text="流派：", bg=COL_PANEL, fg=COL_SUB,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.genre_var = tk.StringVar(value="全部流派")
        self.genre_cb = ttk.Combobox(filt, textvariable=self.genre_var, state="readonly",
                                     style="Music.TCombobox", width=12,
                                     font=("Microsoft YaHei UI", 9))
        self.genre_cb.pack(side="left", padx=(0, 10))
        self.genre_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_rows())
        tk.Label(filt, text="搜索：", bg=COL_PANEL, fg=COL_SUB,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.refresh_rows())
        tk.Entry(filt, textvariable=self.search_var, bg=COL_PANEL2, fg=COL_TEXT,
                 insertbackground=COL_TEXT, relief="flat", bd=0,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", fill="x", expand=True, ipady=3)

        tree_frame = tk.Frame(parent, bg=COL_PANEL)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        cols = ("title", "artist", "genre", "dur")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                 style="Music.Treeview", selectmode="extended")
        self.tree.heading("title", text="歌曲")
        self.tree.heading("artist", text="歌手")
        self.tree.heading("genre", text="流派")
        self.tree.heading("dur", text="时长")
        self.tree.column("title", width=240, anchor="w")
        self.tree.column("artist", width=120, anchor="w")
        self.tree.column("genre", width=90, anchor="center")
        self.tree.column("dur", width=70, anchor="center")
        vs = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.play_selected())
        self.tree.bind("<Button-3>", self.show_context_menu)

        self.context_menu = tk.Menu(self.root, tearoff=0, bg=COL_PANEL2, fg=COL_TEXT,
                                    activebackground=COL_ACCENT, activeforeground="#141414")
        self.context_menu.add_command(label="播放", command=self.play_selected)
        self.context_menu.add_command(label="AI处理（提取歌词+封面）", command=self.ai_process)
        self.context_menu.add_command(label="重新AI处理", command=self.ai_process)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="修改流派...", command=self.edit_genre)
        self.context_menu.add_command(label="打开所在文件夹", command=self.open_song_folder)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="从歌曲库删除（含文件）", command=self.delete_selected)

    # ----- 右侧：播放器 -----

    def _build_player_panel(self, parent):
        inner = tk.Frame(parent, bg=COL_PANEL)
        inner.pack(fill="both", expand=True, padx=12, pady=10)
        self.cover_canvas = tk.Canvas(inner, width=280, height=280, bg=COL_PANEL,
                                      highlightthickness=0)
        self.cover_canvas.pack(pady=(6, 8))
        self.title_var = tk.StringVar(value="未在播放")
        self.artist_var = tk.StringVar(value="请在左侧选择歌曲开始播放")
        tk.Label(inner, textvariable=self.title_var, bg=COL_PANEL, fg=COL_TEXT,
                 font=("Microsoft YaHei UI", 13, "bold"), wraplength=380).pack()
        tk.Label(inner, textvariable=self.artist_var, bg=COL_PANEL, fg=COL_SUB,
                 font=("Microsoft YaHei UI", 10)).pack(pady=(2, 8))

        # 进度条
        prog = tk.Frame(inner, bg=COL_PANEL)
        prog.pack(fill="x", padx=4)
        self.pos_var = tk.StringVar(value="00:00")
        self.total_var = tk.StringVar(value="--:--")
        tk.Label(prog, textvariable=self.pos_var, bg=COL_PANEL, fg=COL_SUB,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.slider = ttk.Scale(prog, from_=0, to=1, orient="horizontal",
                                command=lambda v: None)
        self.slider.pack(side="left", fill="x", expand=True, padx=6)
        tk.Label(prog, textvariable=self.total_var, bg=COL_PANEL, fg=COL_SUB,
                 font=("Microsoft YaHei UI", 9)).pack(side="right")
        self.slider.bind("<ButtonPress-1>", self._slider_press)
        self.slider.bind("<ButtonRelease-1>", self._slider_release)

        # 控制区
        ctrl = tk.Frame(inner, bg=COL_PANEL)
        ctrl.pack(fill="x", pady=(8, 6))
        self.mode_btn = tk.Button(ctrl, text="顺序播放", command=self.cycle_mode,
                                  bg=COL_PANEL2, fg=COL_SUB, relief="flat", bd=0,
                                  font=("Microsoft YaHei UI", 9), padx=8, pady=3,
                                  cursor="hand2")
        self.mode_btn.pack(side="left")
        tk.Button(ctrl, text="⏮", command=self.prev_song, bg=COL_PANEL2, fg=COL_TEXT,
                  relief="flat", bd=0, font=("Microsoft YaHei UI", 12),
                  width=4, pady=4, cursor="hand2").pack(side="left", padx=(10, 4))
        self.play_btn = tk.Button(ctrl, text="▶", command=self.toggle_play,
                                  bg=COL_ACCENT, fg="#141414", relief="flat", bd=0,
                                  font=("Microsoft YaHei UI", 13, "bold"),
                                  width=5, pady=5, cursor="hand2")
        self.play_btn.pack(side="left", padx=4)
        tk.Button(ctrl, text="⏭", command=self.next_song, bg=COL_PANEL2, fg=COL_TEXT,
                  relief="flat", bd=0, font=("Microsoft YaHei UI", 12),
                  width=4, pady=4, cursor="hand2").pack(side="left", padx=4)
        tk.Label(ctrl, text="音量", bg=COL_PANEL, fg=COL_SUB,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(14, 4))
        vol = ttk.Scale(ctrl, from_=0, to=100, orient="horizontal",
                        command=self._on_volume)
        vol.set(self.volume)
        vol.pack(side="left", fill="x", expand=True)

        # 歌词区
        lyr_head = tk.Frame(inner, bg=COL_PANEL)
        lyr_head.pack(fill="x", pady=(4, 2))
        tk.Label(lyr_head, text="歌词（点击任意一行跳转播放）", bg=COL_PANEL, fg=COL_SUB,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.lyric_list = tk.Listbox(inner, bg=COL_PANEL, fg=COL_SUB,
                                     selectbackground=COL_PANEL2, selectforeground=COL_TEXT,
                                     bd=0, highlightthickness=0, relief="flat",
                                     font=("Microsoft YaHei UI", 10), height=9,
                                     activestyle="none")
        lvs = ttk.Scrollbar(inner, orient="vertical", command=self.lyric_list.yview)
        self.lyric_list.configure(yscrollcommand=lvs.set)
        lvs.pack(side="right", fill="y", pady=(0, 6))
        self.lyric_list.pack(fill="both", expand=True, pady=(0, 6))
        self.lyric_list.bind("<Button-1>", self._lyric_click)

    # ---------- 曲库操作 ----------

    def scan_library(self):
        ensure_dirs()
        old = {s["id"]: s for s in load_library()}
        entries = []
        if os.path.isdir(SONG_DIR):
            for fn in sorted(os.listdir(SONG_DIR)):
                if not fn.lower().endswith(".mp3"):
                    continue
                sid = os.path.splitext(fn)[0]
                if sid in old:
                    e = old[sid]
                    e["file"] = fn
                else:
                    e = {"id": sid, "file": fn, "title": sid, "artist": "",
                         "genre": "未分类", "dur": 0,
                         "has_lrc": False, "has_cover": False}
                e["has_lrc"] = os.path.exists(lrc_path(sid))
                e["has_cover"] = os.path.exists(cover_path(sid))
                if not e.get("dur"):
                    e["dur"] = probe_duration(mp3_path(fn))
                entries.append(e)
        save_library(entries)
        return entries

    def song_by_id(self, sid):
        for s in self.library:
            if s["id"] == sid:
                return s
        return None

    def import_music(self):
        files = filedialog.askopenfilenames(
            title="选择要导入的MP3文件",
            filetypes=[("MP3 音乐文件", "*.mp3"), ("所有文件", "*.*")])
        if not files:
            return
        added = 0
        existed = 0
        for src in files:
            name = os.path.basename(src)
            dst = os.path.join(SONG_DIR, name)
            if os.path.abspath(src).lower() == os.path.abspath(dst).lower():
                continue
            if os.path.exists(dst):
                existed += 1
                continue
            try:
                import shutil
                shutil.copy2(src, dst)
                added += 1
            except Exception as e:
                messagebox.showerror("导入失败", "导入 %s 失败：%s" % (name, e))
        if added:
            self.library = self.scan_library()
            self.refresh_genres()
            self.refresh_rows()
        self.set_status("导入完成：新增 %d 首，%d 首已存在跳过" % (added, existed))

    def delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选择要删除的歌曲")
            return
        if not messagebox.askyesno("确认删除", "将从 cangku/歌曲库 中删除选中的 %d 首歌曲文件（含歌词与封面），确定？" % len(sel)):
            return
        for sid in sel:
            song = self.song_by_id(sid)
            if song:
                for p in (mp3_path(song["file"]), lrc_path(sid), cover_path(sid)):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except Exception:
                        pass
            self.library = [s for s in self.library if s["id"] != sid]
            if self.current_id == sid:
                self._stop_playback()
        save_library(self.library)
        self.refresh_genres()
        self.refresh_rows()
        self.set_status("已删除 %d 首歌曲" % len(sel))

    def edit_genre(self):
        sel = self.tree.selection()
        if not sel:
            return
        sid = sel[0]
        song = self.song_by_id(sid)
        if not song:
            return
        newg = simpledialog.askstring("修改流派", "为《%s》设置流派：" % song["title"],
                                      initialvalue=song.get("genre", "未分类"),
                                      parent=self.root)
        if newg is None:
            return
        newg = newg.strip() or "未分类"
        song["genre"] = newg
        save_library(self.library)
        self.refresh_genres()
        self.refresh_rows()
        self.set_status("已设置流派：%s" % newg)

    def open_cangku(self):
        try:
            os.startfile(CANGKU)
        except Exception as e:
            messagebox.showerror("错误", "打开文件夹失败：%s" % e)

    def open_song_folder(self):
        sel = self.tree.selection()
        if not sel:
            return
        song = self.song_by_id(sel[0])
        if song:
            try:
                os.startfile(SONG_DIR)
            except Exception:
                pass

    def refresh_genres(self):
        genres = sorted({s.get("genre", "未分类") for s in self.library})
        vals = ["全部流派"] + genres
        self.genre_cb["values"] = vals
        if self.genre_var.get() not in vals:
            self.genre_var.set("全部流派")

    def refresh_rows(self):
        self.tree.delete(*self.tree.get_children())
        genre = self.genre_var.get()
        kw = self.search_var.get().strip().lower()
        self.visible_list = []
        for s in self.library:
            if genre != "全部流派" and s.get("genre", "未分类") != genre:
                continue
            if kw and kw not in (s.get("title", "") + " " + s.get("artist", "")).lower():
                continue
            self.visible_list.append(s)
        for s in self.visible_list:
            dur = s.get("dur", 0)
            dtext = ("%02d:%02d" % (dur // 60, dur % 60)) if dur else "--:--"
            self.tree.insert("", "end", iid=s["id"],
                             values=(s.get("title", ""), s.get("artist", ""),
                                     s.get("genre", "未分类"), dtext))

    def show_context_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.context_menu.tk_popup(event.x_root, event.y_root)
        else:
            self.context_menu.tk_popup(event.x_root, event.y_root)

    # ---------- 播放控制 ----------

    def _stop_playback(self):
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
        self.current_id = None
        self.paused = False
        self._started = False
        self.dur = 0
        self.pos_var.set("00:00")
        self.total_var.set("--:--")
        self.slider.config(to=1)
        self.slider.set(0)
        self.title_var.set("未在播放")
        self.artist_var.set("请在左侧选择歌曲开始播放")
        self.play_btn.config(text="▶")
        self.lyric_list.delete(0, "end")
        self.lyric_times = []
        self.lyric_idx = -1
        self.update_cover(None)

    def play_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        self.play_song(sel[0])

    def play_song(self, sid, start=0.0):
        if not self.audio_ok:
            messagebox.showerror("无法播放", "音频设备初始化失败，无法播放音乐")
            return
        song = self.song_by_id(sid)
        if not song:
            return
        path = mp3_path(song["file"])
        if not os.path.exists(path):
            messagebox.showerror("文件不存在", "歌曲文件不存在：\n%s" % path)
            return
        try:
            pygame.mixer.music.stop()
            pygame.mixer.music.load(path)
            pygame.mixer.music.set_volume(max(0.0, min(1.0, self.volume / 100.0)))
            pygame.mixer.music.play(start=start)
        except Exception as e:
            messagebox.showerror("播放失败", str(e))
            return
        self.current_id = sid
        self._base_ms = int(start * 1000)
        self._paused_ms = 0
        self.paused = False
        self._started = True
        self.play_btn.config(text="⏸")
        self.title_var.set(song.get("title") or sid)
        self.artist_var.set(song.get("artist") or "")
        self.update_cover(sid)
        self.load_lyrics(sid)
        if not song.get("dur"):
            song["dur"] = probe_duration(path)
            save_library(self.library)
        self.dur = song["dur"] or 0
        self.slider.config(to=max(self.dur * 1000, 1))
        self.total_var.set(("%02d:%02d" % (self.dur // 60, self.dur % 60)) if self.dur else "--:--")
        self.root.title((self.cfg.get("app_name") or "音乐管家") + " - " + (song.get("title") or sid))
        try:
            self.tree.selection_set(sid)
            self.tree.see(sid)
        except Exception:
            pass

    def toggle_play(self):
        if not self.current_id:
            if self.visible_list:
                self.play_song(self.visible_list[0]["id"])
            return
        if self.paused:
            sec = self._paused_ms / 1000.0
            self._base_ms = self._paused_ms
            try:
                pygame.mixer.music.play(start=sec)
            except Exception as e:
                messagebox.showerror("播放失败", str(e))
                return
            self.paused = False
            self._started = True
            self.play_btn.config(text="⏸")
        else:
            self._paused_ms = self.get_abs_pos_ms()
            try:
                pygame.mixer.music.pause()
            except Exception:
                pass
            self.paused = True
            self.play_btn.config(text="▶")

    def get_abs_pos_ms(self):
        if self.paused:
            return self._paused_ms
        try:
            return self._base_ms + pygame.mixer.music.get_pos()
        except Exception:
            return self._base_ms

    def seek(self, sec):
        if not self.current_id:
            return
        if self.dur:
            sec = max(0.0, min(sec, float(self.dur)))
        self._base_ms = int(sec * 1000)
        try:
            pygame.mixer.music.play(start=sec)
        except Exception as e:
            messagebox.showerror("播放失败", str(e))
            return
        self.paused = False
        self._started = True
        self.play_btn.config(text="⏸")

    def next_song(self):
        if not self.visible_list:
            return
        if not self.current_id:
            self.play_song(self.visible_list[0]["id"])
            return
        idx = self._current_visible_index()
        nxt = self.visible_list[(idx + 1) % len(self.visible_list)]
        self.play_song(nxt["id"])

    def prev_song(self):
        if not self.visible_list:
            return
        if not self.current_id:
            self.play_song(self.visible_list[0]["id"])
            return
        idx = self._current_visible_index()
        prev = self.visible_list[(idx - 1) % len(self.visible_list)]
        self.play_song(prev["id"])

    def _current_visible_index(self):
        for i, s in enumerate(self.visible_list):
            if s["id"] == self.current_id:
                return i
        return 0

    def _on_track_end(self):
        if not self.visible_list:
            self._stop_playback()
            return
        if self.play_mode == "loop":
            if self.current_id:
                self.play_song(self.current_id)
            return
        if self.play_mode == "shuffle":
            nxt = random.choice(self.visible_list)
        else:
            idx = self._current_visible_index()
            nxt = self.visible_list[(idx + 1) % len(self.visible_list)]
        self.play_song(nxt["id"])

    def cycle_mode(self):
        order = ["order", "shuffle", "loop"]
        self.play_mode = order[(order.index(self.play_mode) + 1) % 3]
        self.cfg["play_mode"] = self.play_mode
        save_config(self.cfg)
        self.update_mode_button()
        self.set_status("播放模式：%s" % {"order": "顺序播放", "shuffle": "随机播放", "loop": "单曲循环"}[self.play_mode])

    def update_mode_button(self):
        text = {"order": "顺序播放", "shuffle": "随机播放", "loop": "单曲循环"}.get(self.play_mode, "顺序播放")
        self.mode_btn.config(text=text)

    def _on_volume(self, val):
        self.volume = int(float(val))
        try:
            pygame.mixer.music.set_volume(max(0.0, min(1.0, self.volume / 100.0)))
        except Exception:
            pass
        self.cfg["volume"] = self.volume
        save_config(self.cfg)

    def _slider_press(self, _e):
        self.dragging = True

    def _slider_release(self, _e):
        self.dragging = False
        self.seek(int(self.slider.get()) / 1000.0)

    # ---------- 封面与歌词 ----------

    def update_cover(self, sid):
        photo = None
        if sid:
            cp = cover_path(sid)
            if os.path.exists(cp):
                try:
                    img = Image.open(cp).convert("RGB")
                    img.thumbnail((280, 280))
                    photo = ImageTk.PhotoImage(img)
                except Exception:
                    photo = None
        if photo is None:
            if self._placeholder is None:
                self._placeholder = ImageTk.PhotoImage(make_placeholder_cover(280))
            photo = self._placeholder
        self.cover_photo = photo
        self.cover_canvas.delete("all")
        self.cover_canvas.create_image(140, 140, image=photo)

    def load_lyrics(self, sid):
        self.lyric_list.delete(0, "end")
        self.lyric_times = []
        self.lyric_idx = -1
        lp = lrc_path(sid)
        if os.path.exists(lp):
            try:
                with open(lp, "r", encoding="utf-8") as f:
                    parsed = parse_lrc(f.read())
                for t, text in parsed:
                    self.lyric_list.insert("end", text)
                self.lyric_times = [t for t, _ in parsed]
            except Exception:
                self.lyric_list.insert("end", "歌词解析失败")
        else:
            self.lyric_list.insert("end", "（暂无歌词：在歌曲列表选中歌曲，点击「AI处理」提取）")
            self.lyric_list.itemconfig(0, foreground=COL_SUB)

    def _lyric_click(self, event):
        idx = self.lyric_list.nearest(event.y)
        if 0 <= idx < len(self.lyric_times):
            self.seek(self.lyric_times[idx])
            self.lyric_idx = idx - 1  # 让高亮立即跳到该行

    def _update_lyrics(self, pos_ms):
        if not self.lyric_times:
            return
        sec = pos_ms / 1000.0
        idx = self.lyric_idx
        times = self.lyric_times
        if idx < 0 or (idx + 1 < len(times) and sec < times[idx]):
            idx = 0
        while idx + 1 < len(times) and sec >= times[idx + 1]:
            idx += 1
        if idx != self.lyric_idx:
            if 0 <= self.lyric_idx < self.lyric_list.size():
                self.lyric_list.itemconfig(self.lyric_idx, foreground=COL_SUB)
            self.lyric_idx = idx
            self.lyric_list.itemconfig(idx, foreground=COL_ACCENT2,
                                       font=("Microsoft YaHei UI", 10, "bold"))
            if idx > 0:
                self.lyric_list.see(idx - 3)

    # ---------- 设置 ----------

    def open_settings(self):
        if self._settings_dialog and self._settings_dialog.winfo_exists():
            self._settings_dialog.lift()
            return
        cfg = load_config()
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.geometry("620x620")
        win.minsize(560, 560)
        win.configure(bg=COL_BG)
        win.resizable(True, True)
        win.transient(self.root)
        win.grab_set()
        self._settings_dialog = win

        # 按钮栏先打包并固定在窗口底部，避免高DPI缩放下被挤出窗口看不见
        btn_bar = tk.Frame(win, bg=COL_BG)
        btn_bar.pack(side="bottom", fill="x", padx=20, pady=(0, 16))

        body = tk.Frame(win, bg=COL_BG)
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.columnconfigure(1, weight=1)

        def row(y, text):
            tk.Label(body, text=text, bg=COL_BG, fg=COL_TEXT,
                     font=("Microsoft YaHei UI", 10)).grid(row=y, column=0, sticky="w", pady=7)
        row(0, "程序首页名字：")
        name_var = tk.StringVar(value=cfg.get("app_name", "音乐管家"))
        tk.Entry(body, textvariable=name_var, bg=COL_PANEL2, fg=COL_TEXT,
                 insertbackground=COL_TEXT, relief="flat", bd=0,
                 font=("Microsoft YaHei UI", 10)).grid(row=0, column=1, sticky="ew", ipady=4, pady=7)

        row(1, "API 地址：")
        base_var = tk.StringVar(value=cfg.get("api_base", ""))
        tk.Entry(body, textvariable=base_var, bg=COL_PANEL2, fg=COL_TEXT,
                 insertbackground=COL_TEXT, relief="flat", bd=0,
                 font=("Microsoft YaHei UI", 10)).grid(row=1, column=1, sticky="ew", ipady=4, pady=7)
        tk.Label(body, text="支持 OpenAI 兼容接口，例如 https://api.deepseek.com/v1 或 https://dashscope.aliyuncs.com/compatible-mode/v1",
                 bg=COL_BG, fg=COL_SUB, font=("Microsoft YaHei UI", 9),
                 wraplength=420, justify="left").grid(row=2, column=1, sticky="w")

        row(3, "API Key：")
        key_var = tk.StringVar(value=cfg.get("api_key", ""))
        key_entry = tk.Entry(body, textvariable=key_var, bg=COL_PANEL2, fg=COL_TEXT,
                             insertbackground=COL_TEXT, relief="flat", bd=0, show="*",
                             font=("Microsoft YaHei UI", 10))
        key_entry.grid(row=3, column=1, sticky="ew", ipady=4, pady=7)

        row(4, "模型名称：")
        model_var = tk.StringVar(value=cfg.get("model", ""))
        tk.Entry(body, textvariable=model_var, bg=COL_PANEL2, fg=COL_TEXT,
                 insertbackground=COL_TEXT, relief="flat", bd=0,
                 font=("Microsoft YaHei UI", 10)).grid(row=4, column=1, sticky="ew", ipady=4, pady=7)
        tk.Label(body, text="例如 deepseek-chat、qwen-plus、glm-4-flash 等（填写你的模型名）",
                 bg=COL_BG, fg=COL_SUB, font=("Microsoft YaHei UI", 9)).grid(row=5, column=1, sticky="w")

        show_key = tk.BooleanVar(value=False)

        def toggle_key():
            key_entry.config(show="" if show_key.get() else "*")
        tk.Checkbutton(body, text="显示Key", variable=show_key, command=toggle_key,
                       bg=COL_BG, fg=COL_SUB, selectcolor=COL_PANEL2,
                       activebackground=COL_BG, activeforeground=COL_TEXT,
                       font=("Microsoft YaHei UI", 9)).grid(row=6, column=1, sticky="w")

        # 测试结果（注意：标签绑定了 textvariable，更新必须用变量.set，text= 会被忽略）
        test_var = tk.StringVar(value="")
        test_lbl = tk.Label(body, textvariable=test_var, bg=COL_BG, fg=COL_OK,
                            font=("Microsoft YaHei UI", 10), justify="left", wraplength=430)
        test_lbl.grid(row=7, column=0, columnspan=2, sticky="w", pady=6)
        win._test_lbl = test_lbl
        win._test_var = test_var

        def do_test():
            test_var.set("正在测试连接...（最长等待15秒，请稍候）")
            test_lbl.config(fg=COL_SUB)
            test_btn.config(state="disabled", text="测试中...")
            tmp = {"api_base": base_var.get(), "api_key": key_var.get(), "model": model_var.get()}
            threading.Thread(target=self._test_worker, args=(tmp,), daemon=True).start()

        def do_save():
            cfg["app_name"] = (name_var.get().strip() or "音乐管家")
            cfg["api_base"] = base_var.get().strip()
            cfg["api_key"] = key_var.get().strip()
            cfg["model"] = model_var.get().strip()
            save_config(cfg)
            self.cfg = cfg
            self.app_title.config(text=cfg["app_name"])
            self.root.title(cfg["app_name"])
            if self._settings_dialog and self._settings_dialog.winfo_exists():
                self._settings_dialog.destroy()
            self._settings_dialog = None
            self.set_status("设置已保存")

        test_btn = tk.Button(btn_bar, text="测试连接", command=do_test, bg=COL_PANEL2, fg=COL_TEXT,
                             activebackground=COL_ACCENT, activeforeground="#141414", relief="flat", bd=0,
                             font=("Microsoft YaHei UI", 10), padx=16, pady=5, cursor="hand2")
        test_btn.pack(side="left")
        win._test_btn = test_btn
        tk.Button(btn_bar, text="保存设置", command=do_save, bg=COL_ACCENT, fg="#141414",
                  relief="flat", bd=0, font=("Microsoft YaHei UI", 10, "bold"),
                  padx=22, pady=5, cursor="hand2").pack(side="right", padx=(8, 0))
        tk.Button(btn_bar, text="取消", command=lambda: (win.destroy(),
                                                        setattr(self, "_settings_dialog", None)),
                  bg=COL_PANEL2, fg=COL_TEXT, relief="flat", bd=0,
                  font=("Microsoft YaHei UI", 10), padx=16, pady=5,
                  cursor="hand2").pack(side="right")

        win.protocol("WM_DELETE_WINDOW", lambda: (win.destroy(),
                                                  setattr(self, "_settings_dialog", None)))

    def _test_worker(self, tmp):
        try:
            content = api_call(tmp, [{"role": "user", "content": "请只回复四个字：连接成功"}], timeout=15)
            ok = ("连接成功" in content) or bool(content.strip())
            self.evq.put(("test", ok, content.strip()[:100]))
        except ApiError as e:
            self.evq.put(("test", False, str(e)))
        except Exception as e:
            self.evq.put(("test", False, "测试出错：" + str(e)))

    # ---------- AI 处理 ----------

    def ai_process(self):
        if self._ai_running:
            self.set_status("AI处理正在进行中，请稍候...")
            return
        sel = self.tree.selection()
        if sel:
            ids = list(sel)
        else:
            ids = [s["id"] for s in self.library
                   if not s.get("has_lrc") or not s.get("has_cover")]
        if not ids:
            messagebox.showinfo("提示", "没有需要处理的歌曲。请先在左侧选中歌曲，或先导入歌曲。")
            return
        cfg = load_config()
        if not (cfg.get("api_base") and cfg.get("api_key") and cfg.get("model")):
            messagebox.showwarning("未配置AI接口", "请先点击右上角「设置」，填写 API 地址、Key 和模型名称，并点击「测试连接」确认成功后再使用AI处理。")
            return
        if not messagebox.askyesno("AI处理", "将对 %d 首歌曲调用大模型提取歌词并生成封面（会消耗API额度）。\n\n处理结果会保存在 cangku/歌词 与 cangku/封面，之后播放不会再重复调用。\n\n确定开始？" % len(ids)):
            return
        self.cancel_ai = False
        self._ai_running = True
        self.cancel_btn.pack(side="right")
        self.progress.config(maximum=len(ids), value=0)
        self.progress.pack(side="left", padx=(10, 0))
        self.set_status("AI处理开始：共 %d 首" % len(ids))
        threading.Thread(target=self._ai_worker, args=(ids,), daemon=True).start()

    def cancel_ai_process(self):
        if self._ai_running:
            self.cancel_ai = True
            self.set_status("正在取消AI处理...")

    def _ai_worker(self, ids):
        cfg = load_config()
        ok_n = 0
        fail_n = 0
        fail_msgs = []
        total = len(ids)
        for i, sid in enumerate(ids, 1):
            if self.cancel_ai:
                self.evq.put(("ai_abort", "已取消，完成 %d/%d 首" % (ok_n + fail_n, total)))
                break
            song = self.song_by_id(sid)
            if not song:
                continue
            self.evq.put(("ai_progress", (i, total, song.get("title", sid))))
            try:
                filename = song["file"]
                content = api_call(cfg, [
                    {"role": "system", "content": AI_SYSTEM},
                    {"role": "user", "content": "歌曲文件名：" + filename + "\n请按系统要求输出JSON。"},
                ], timeout=120)
                data = extract_json(content)
                lrc = data.get("lrc") or ""
                title = (data.get("title") or "").strip() or song.get("title") or sid
                artist = (data.get("artist") or "").strip()
                genre = (data.get("genre") or "").strip() or "未分类"
                # 保存歌词
                with open(lrc_path(sid), "w", encoding="utf-8") as f:
                    f.write(lrc if lrc.endswith("\n") else lrc + "\n")
                # 生成封面
                make_cover(title, artist, data.get("cover_desc", ""),
                           data.get("color1", ""), data.get("color2", ""),
                           cover_path(sid))
                song["title"] = title
                song["artist"] = artist
                song["genre"] = genre
                song["has_lrc"] = True
                song["has_cover"] = True
                save_library(self.library)
                ok_n += 1
                self.evq.put(("ai_done", sid, True, (title, genre)))
            except ApiError as e:
                fail_n += 1
                fail_msgs.append("%s：%s" % (song.get("title", sid), e))
                self.evq.put(("ai_done", sid, False, str(e)))
                if e.kind == "network":
                    self.evq.put(("ai_abort", "网络出错，请检查网络连接（已完成 %d/%d 首）" % (ok_n + fail_n, total)))
                    break
            except Exception as e:
                fail_n += 1
                fail_msgs.append("%s：%s" % (song.get("title", sid), e))
                self.evq.put(("ai_done", sid, False, str(e)))
            time.sleep(0.3)
        self.evq.put(("ai_finish", (ok_n, fail_n, fail_msgs[:5])))

    # ---------- 主循环消息 ----------

    def _poll_queue(self):
        try:
            while True:
                ev = self.evq.get_nowait()
                kind = ev[0]
                if kind == "test":
                    ok, msg = ev[1], ev[2]
                    win = self._settings_dialog
                    if win and win.winfo_exists():
                        var = getattr(win, "_test_var", None)
                        if var is not None:
                            var.set("连接成功！API配置有效，可以正常调用" if ok else msg)
                        lbl = getattr(win, "_test_lbl", None)
                        if lbl and lbl.winfo_exists():
                            lbl.config(fg=COL_OK if ok else COL_ERR)
                        btn = getattr(win, "_test_btn", None)
                        if btn and btn.winfo_exists():
                            btn.config(state="normal", text="测试连接")
                elif kind == "ai_progress":
                    i, total, title = ev[1]
                    self.progress.config(maximum=total, value=i)
                    self.set_status("AI处理中（%d/%d）：%s ..." % (i, total, title))
                elif kind == "ai_done":
                    sid, ok, info = ev[1], ev[2], ev[3]
                    if ok:
                        title, genre = info
                        self.set_status("已完成：《%s》（%s）" % (title, genre))
                    else:
                        self.set_status("处理失败：%s" % info)
                elif kind == "ai_abort":
                    self._ai_running = False
                    self.cancel_btn.pack_forget()
                    self.progress.pack_forget()
                    self.set_status(ev[1])
                elif kind == "ai_finish":
                    ok_n, fail_n, msgs = ev[1]
                    self._ai_running = False
                    self.cancel_btn.pack_forget()
                    self.progress.pack_forget()
                    self.refresh_genres()
                    self.refresh_rows()
                    if self.current_id:
                        song = self.song_by_id(self.current_id)
                        if song:
                            self.load_lyrics(self.current_id)
                            self.update_cover(self.current_id)
                    self.set_status("AI处理结束：成功 %d 首，失败 %d 首" % (ok_n, fail_n))
                    if fail_n:
                        messagebox.showwarning("部分处理失败", "以下歌曲处理失败：\n" + "\n".join(msgs))
        except queue.Empty:
            pass

    def _set_test_label(self, win, ok, msg):
        # 找到测试结果标签并更新（必须通过变量更新，text= 会被 textvariable 覆盖）
        try:
            var = getattr(win, "_test_var", None)
            if var is not None:
                var.set("连接成功！API配置有效，可以正常调用" if ok else msg)
            lbl = getattr(win, "_test_lbl", None)
            if lbl and lbl.winfo_exists():
                lbl.config(fg=COL_OK if ok else COL_ERR)
        except Exception:
            pass

    # ---------- 定时刷新 ----------

    def tick(self):
        try:
            self._poll_queue()
            if self.current_id and not self.paused and self._started:
                if not self.dragging:
                    pos = self.get_abs_pos_ms()
                    self.slider.set(pos)
                    self.pos_var.set("%02d:%02d" % ((pos // 60000), (pos // 1000) % 60))
                self._update_lyrics(self.get_abs_pos_ms())
                if not pygame.mixer.music.get_busy():
                    self._on_track_end()
        except Exception:
            pass
        self.root.after(200, self.tick)

    # ---------- 其他 ----------

    def set_status(self, text):
        self.status_var.set(text)

    def on_close(self):
        try:
            pygame.mixer.music.stop()
            pygame.mixer.quit()
        except Exception:
            pass
        self.root.destroy()


# ====================== 自测模式 ======================

def selftest():
    log = []
    try:
        ensure_dirs()
        cfg = load_config()
        log.append("OK dirs+config, app_name=" + cfg["app_name"])
        pygame.mixer.pre_init(44100, -16, 2, 4096)
        pygame.mixer.init()
    except Exception as e:
        log.append("FAIL init: " + repr(e) + "\n" + traceback.format_exc())
        _write_selftest(log)
        os._exit(1)

    try:
        songs = MusicApp.scan_library(object())
        log.append("OK library scan: %d songs" % len(songs))
        mp3s = [s for s in songs]
        if mp3s:
            f = mp3_path(mp3s[0]["file"])
            pygame.mixer.music.load(f)
            pygame.mixer.music.play()
            time.sleep(1.2)
            pygame.mixer.music.play(start=5)
            time.sleep(0.5)
            pos = pygame.mixer.music.get_pos()
            log.append("OK playback+seek pos_ms=%d" % pos)
            pygame.mixer.music.stop()
        else:
            log.append("WARN no mp3 in 歌曲库")
    except Exception as e:
        log.append("FAIL play: " + repr(e) + "\n" + traceback.format_exc())

    try:
        out = cover_path("_selftest_cover")
        make_cover("测试歌曲", "测试歌手", "desc", "#112244", "#66aa44", out)
        log.append("OK cover gen: " + str(os.path.exists(out)))
        lrc = "[00:00.00]第一句\n[00:04.20]第二句\n[01:02.50]第三句"
        parsed = parse_lrc(lrc)
        log.append("OK lrc parse: %s" % parsed)
    except Exception as e:
        log.append("FAIL cover/lrc: " + repr(e) + "\n" + traceback.format_exc())

    log.append("SELFTEST_OK" if "FAIL" not in "\n".join(log) else "SELFTEST_HAS_FAIL")
    _write_selftest(log)
    os._exit(0 if "SELFTEST_OK" in log[-1] else 2)


def _write_selftest(log):
    try:
        with open(os.path.join(app_dir(), "selftest.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(log))
    except Exception:
        pass


# ====================== 入口 ======================

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--guismoke" in sys.argv:
        try:
            app = MusicApp()
            app.root.after(4000, app.root.destroy)
            app.root.mainloop()
            result = "GUISMOKE_OK"
        except Exception as e:
            result = "GUISMOKE_FAIL\n" + traceback.format_exc()
        try:
            with open(os.path.join(app_dir(), "guismoke.log"), "w", encoding="utf-8") as f:
                f.write(result)
        except Exception:
            pass
        os._exit(0 if result == "GUISMOKE_OK" else 2)
    else:
        app = MusicApp()
        app.root.mainloop()
