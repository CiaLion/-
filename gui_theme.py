# -*- coding: utf-8 -*-
"""gui_theme.py -- Design System v2。

设计口径：
  * 字体：`Microsoft YaHei UI` 10pt（抗锯齿，PreferAntialias）；字号四级
    appTitle 20px / sectionTitle 16px / stepTitle 15px / 正文 13px。
  * 控件：普通按钮 36px、主按钮 40px、演奏键 44px；输入框统一 34px。
  * 主题：Studio Dark（#0B0F19 / #111827）与 Modern Light（#F1F5F9 / #FFFFFF）。
  * 颜色只用冷灰 + 一块蓝色（#2563EB）与一块祖母绿（#059669），不用彩色 Emoji。

对外接口（与旧版兼容）：
  `setup_app_font(app)` / `build_stylesheet(mode)` / `apply_theme(app, mode=None)`
  / `load_mode()` / `save_mode(mode)`；旧名 `DARK_QSS` / `LIGHT_QSS` 仍保留。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / "gui_settings.json"

UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"

# ── 设计令牌 ────────────────────────────────────────────────────────────────
DARK = {
    "bg": "#0B0F19", "card": "#111827", "inner": "#0F1626", "line": "#1F2937",
    "text": "#E6EDF3", "text_dim": "#94A3B8", "text_mute": "#64748B",
    "input": "#1E293B", "input_line": "#334155", "hover": "#334155",
    "blue": "#2563EB", "blue_hover": "#3B82F6", "cyan": "#22D3EE",
    "green": "#059669", "green_hover": "#10B981", "green_text": "#34D399",
    "guide_bg": "#172554", "guide_line": "#1E40AF", "guide_text": "#DBEAFE",
    "danger": "#F87171",
    "danger_line": "#7F1D1D", "ghost_bg": "#0F1626", "ghost_text": "#CBD5E1",
}
LIGHT = {
    "bg": "#F1F5F9", "card": "#FFFFFF", "inner": "#F8FAFC", "line": "#E2E8F0",
    "text": "#0F172A", "text_dim": "#475569", "text_mute": "#94A3B8",
    "input": "#F8FAFC", "input_line": "#CBD5E1", "hover": "#E2E8F0",
    "blue": "#2563EB", "blue_hover": "#1D4ED8", "cyan": "#0E7490",
    "green": "#059669", "green_hover": "#047857", "green_text": "#047857",
    "guide_bg": "#EFF6FF", "guide_line": "#BFDBFE", "guide_text": "#1E3A8A",
    "danger": "#B91C1C",
    "danger_line": "#FCA5A5", "ghost_bg": "#FFFFFF", "ghost_text": "#334155",
}


def setup_app_font(app):
    """把应用字体强制设为 10pt Microsoft YaHei UI，并开启抗锯齿。"""
    from PyQt5.QtGui import QFont
    f = QFont(UI_FONT, 10)
    try:
        f.setStyleStrategy(QFont.PreferAntialias)
    except Exception:
        pass
    app.setFont(f)
    return f


def build_stylesheet(mode="dark"):
    """按主题生成完整 QSS。

    兼容两种调用：``build_stylesheet(True/False)`` 或
    ``build_stylesheet("dark"/"light")``。运行日志框（QPlainTextEdit）的
    底色/字色/边框跟随主题切换，浅色主题为 #F8FAFC 白底深字。
    """
    if isinstance(mode, str):
        dark = str(mode).lower() != "light"
    else:
        dark = bool(mode)
    c = DARK if dark else LIGHT
    if dark:
        log_bg, log_fg, log_bd = "#090D16", "#E2E8F0", "#334155"
        stage_chunk = "#2563EB"
    else:
        log_bg, log_fg, log_bd = "#F8FAFC", "#0F172A", "#CBD5E1"
        stage_chunk = "#60A5FA"
    return """
    QWidget {{
        background: {bg}; color: {text};
        font-family: "{font}"; font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {bg}; }}
    QLabel {{ background: transparent; color: {text}; }}
    QLabel#appTitle {{ color: {title}; font-size: 20px; font-weight: 800; }}
    QLabel#title {{ color: {title}; font-size: 20px; font-weight: 800; }}
    QLabel#sectionTitle {{ color: {title}; font-size: 16px; font-weight: 700; }}
    QLabel#cardtitle {{ color: {title}; font-size: 16px; font-weight: 700; }}
    QLabel#stepTitle {{ color: {cyan}; font-size: 15px; font-weight: 700; }}
    QLabel#section {{ color: {text_dim}; font-size: 13px; font-weight: 700; }}
    QLabel#subtitle, QLabel#cardsub, QLabel#sub, QLabel#fieldname {{
        color: {text_dim}; font-size: 12px; }}
    QLabel#roleDesc {{ color: {text_dim}; font-size: 12px; }}
    QLabel#dev {{ color: {cyan}; }}
    QLabel#status {{ color: {green_text}; font-size: 13px; }}
    QLabel#statusBadge {{
        background: {badge_bg}; color: {green_text};
        border: 1px solid {badge_line}; border-radius: 9px;
        padding: 6px 12px; min-height: 26px; line-height: 1.4;
        font-size: 12px; font-weight: 600; }}
    QLabel#guide_text {{
        color: {guide_text}; background: {guide_bg};
        border: 1px solid {guide_line}; border-radius: 10px;
        padding: 10px 12px; font-size: 14px; }}
    QLabel#badge {{
        background: {blue}; color: #FFFFFF; border-radius: 17px;
        font-weight: 700; font-size: 16px; }}

    QFrame#card, QFrame#hero, QFrame#guide, QGroupBox {{
        background: {card}; border: 1px solid {line}; border-radius: 12px; }}
    QFrame#card_inner, QFrame#subcard {{
        background: {inner}; border: 1px solid {line}; border-radius: 10px; }}
    QGroupBox {{ margin-top: 12px; padding: 14px 12px 10px 12px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 4px;
                        color: {text_dim}; font-weight: 600; }}

    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        background: {input}; color: {text}; border: 1px solid {input_line};
        border-radius: 7px; padding: 4px 8px; min-height: 34px; font-size: 14px; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border: 1px solid {blue}; }}
    QComboBox QAbstractItemView {{
        background: {input}; color: {text}; border: 1px solid {input_line};
        selection-background-color: {blue}; selection-color: #FFFFFF; }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
        color: {text_mute}; }}

    QPushButton {{
        background: {input}; color: {text}; border: 1px solid {input_line};
        border-radius: 8px; padding: 4px 18px; min-height: 36px;
        font-size: 14px; font-weight: 600; }}
    QPushButton:hover {{ background: {hover}; border-color: {blue}; }}
    QPushButton:pressed {{ background: {inner}; }}
    QPushButton:disabled {{ color: {text_mute}; background: {inner};
                            border-color: {line}; }}
    QPushButton#primary {{
        background: {blue}; border-color: {blue}; color: #FFFFFF;
        min-height: 40px; font-size: 15px; font-weight: 700; }}
    QPushButton#primary:hover {{ background: {blue_hover}; }}
    QPushButton#playBtn, QPushButton#play {{
        background: {green}; border-color: {green}; color: #FFFFFF;
        min-height: 44px; min-width: 170px; font-size: 16px; font-weight: 800; }}
    QPushButton#playBtn:hover, QPushButton#play:hover {{ background: {green_hover}; }}
    QPushButton#accent {{
        background: {cyan}; border-color: {cyan}; color: #052E2B;
        font-weight: 700; }}
    QPushButton#ghost {{ background: {ghost_bg}; color: {ghost_text}; }}
    QPushButton#danger {{ color: {danger}; border-color: {danger_line}; }}

    QCheckBox, QRadioButton {{
        background: transparent; color: {text}; spacing: 8px;
        font-size: 14px; font-weight: 600; padding: 2px 0; }}
    QRadioButton#roleRadio {{ font-size: 15px; font-weight: 700; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
    QCheckBox::indicator:unchecked, QRadioButton::indicator:unchecked {{
        border: 1px solid {input_line}; background: {input}; }}
    QCheckBox::indicator:unchecked {{ border-radius: 4px; }}
    QRadioButton::indicator:unchecked {{ border-radius: 8px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        border: 1px solid {blue}; background: {blue}; }}
    QCheckBox::indicator:checked {{ border-radius: 4px; }}
    QRadioButton::indicator:checked {{ border-radius: 8px; }}

    QTabWidget::pane {{ border: 1px solid {line}; background: {bg};
                        border-radius: 10px; top: -1px; }}
    QTabBar::tab {{ background: {card}; color: {text_dim};
                    padding: 10px 20px; margin-right: 4px;
                    border: 1px solid {line}; border-top-left-radius: 9px;
                    border-top-right-radius: 9px; font-size: 13px;
                    font-weight: 600; }}
    QTabBar::tab:selected {{ background: {bg}; color: {cyan};
                             border-bottom: 2px solid {blue}; }}
    QTabBar::tab:hover {{ color: {text}; }}

    QProgressBar {{ background: {input}; border: 1px solid {input_line};
                   border-radius: 6px; text-align: center; color: {text};
                   min-height: 10px; }}
    QProgressBar::chunk {{ background: {blue}; border-radius: 5px; }}
    QProgressBar#stageBar {{
        background: {input}; border: 1px solid {input_line};
        border-radius: 8px; text-align: center; color: {title};
        min-height: 26px; max-height: 26px;
        font-size: 13px; font-weight: 700; }}
    QProgressBar#stageBar::chunk {{
        background: {stage_chunk}; border-radius: 7px; }}

    QSlider::groove:horizontal {{
        height: 6px; background: #334155; border-radius: 3px; }}
    QSlider::sub-page:horizontal {{
        background: #3B82F6; border-radius: 3px; }}
    QSlider::add-page:horizontal {{
        background: #334155; border-radius: 3px; }}
    QSlider::handle:horizontal {{
        background: #38BDF8; border: 2px solid #FFFFFF;
        width: 16px; height: 16px; margin: -6px 0; border-radius: 9px; }}
    QSlider::handle:horizontal:hover {{ background: #60A5FA; }}
    QSlider::groove:vertical {{
        width: 6px; background: #334155; border-radius: 3px; }}
    QSlider::handle:vertical {{
        background: #38BDF8; border: 2px solid #FFFFFF;
        width: 16px; height: 16px; margin: 0 -6px; border-radius: 9px; }}

    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QWidget#adv_inner {{ background: transparent; }}

    QPlainTextEdit {{
        background-color: {log_bg};
        color: {log_fg};
        font-family: "{mono}", "{font}", monospace;
        font-size: 13px;
        border: 1px solid {log_bd};
        border-radius: 8px;
        padding: 8px;
        selection-background-color: {blue};
        selection-color: #FFFFFF; }}
    QPlainTextEdit#log {{
        background-color: {log_bg};
        color: {log_fg};
        border: 1px solid {log_bd};
        border-radius: 8px;
        font-family: "{mono}", "{font}", monospace;
        font-size: 13px;
        padding: 8px;
        selection-background-color: {blue};
        selection-color: #FFFFFF; }}
    QTextEdit {{
        background: {input}; color: {text}; border: 1px solid {input_line};
        border-radius: 8px; }}

    QMenu, QMessageBox, QFileDialog, QDialog {{ background: {card}; color: {text}; }}
    QToolTip {{ background: {input}; color: {text};
               border: 1px solid {input_line}; padding: 4px 6px; }}

    QScrollBar:vertical {{ background: {bg}; width: 10px; border-radius: 5px;
                          margin: 0; }}
    QScrollBar::handle:vertical {{ background: {input_line}; min-height: 28px;
                                  border-radius: 5px; }}
    QScrollBar::handle:vertical:hover {{ background: {text_mute}; }}
    QScrollBar:horizontal {{ background: {bg}; height: 10px; border-radius: 5px; }}
    QScrollBar::handle:horizontal {{ background: {input_line}; min-width: 28px;
                                    border-radius: 5px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """.format(
        font=UI_FONT, mono=MONO_FONT,
        log_bg=log_bg, log_fg=log_fg, log_bd=log_bd,
        stage_chunk=stage_chunk,
        title="#FFFFFF" if c is DARK else "#0F172A",
        badge_bg="#0B2A20" if c is DARK else "#ECFDF5",
        badge_line="#14532D" if c is DARK else "#A7F3D0",
        **c
    )


DARK_QSS = build_stylesheet("dark")
LIGHT_QSS = build_stylesheet("light")


def load_mode():
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        mode = str(data.get("theme") or "").lower()
        if mode in ("dark", "light"):
            return mode
    except Exception:
        pass
    return None


def save_mode(mode):
    mode = "light" if str(mode).lower() == "light" else "dark"
    try:
        data = {}
        if SETTINGS.is_file():
            try:
                data = json.loads(SETTINGS.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["theme"] = mode
        SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    except Exception:
        pass
    return mode


def _is_os_dark():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return int(val) == 0
    except Exception:
        return True


def apply_theme(app, mode=None):
    """应用主题：Fusion 调色板 + 10pt 微软雅黑 UI + 全套 QSS。"""
    from PyQt5.QtGui import QColor, QPalette
    mode = (mode or load_mode() or ("dark" if _is_os_dark() else "light")).lower()
    mode = "light" if mode == "light" else "dark"
    c = LIGHT if mode == "light" else DARK
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(c["bg"]))
    pal.setColor(QPalette.WindowText, QColor(c["text"]))
    pal.setColor(QPalette.Base, QColor(c["input"]))
    pal.setColor(QPalette.AlternateBase, QColor(c["inner"]))
    pal.setColor(QPalette.Text, QColor(c["text"]))
    pal.setColor(QPalette.Button, QColor(c["input"]))
    pal.setColor(QPalette.ButtonText, QColor(c["text"]))
    pal.setColor(QPalette.Highlight, QColor(c["blue"]))
    pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(QPalette.ToolTipBase, QColor(c["input"]))
    pal.setColor(QPalette.ToolTipText, QColor(c["text"]))
    app.setPalette(pal)
    setup_app_font(app)
    app.setStyleSheet(build_stylesheet(mode))
    return mode