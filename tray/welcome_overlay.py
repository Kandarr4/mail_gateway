"""Заставка запуска Mail Gateway — ракета, дым и прогресс, как в Quick-Queue.

Показывается GUI-режимом при старте и гаснет, когда программа впервые сняла
реальное состояние службы (см. tray/app.py). Прогресс здесь — анимация
ожидания, а не измерение: фразы намеренно шуточные, это фирменный стиль
заставок Somnium (ui/welcome_overlay.py в Quick-Queue V1).
"""

import math
import random

from PySide6.QtCore import (
    QByteArray,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .tray_icon import icon_path

LOADING_TIME_MS = 8000
REFRESH_RATE_MS = 60
ANIMATION_FPS = 60

COLORS = {
    "bg": "#2d3b48",
    "teal": "#1ABB9B",
    "progress_bg": "#455a64",
    "text_white": "#ffffff",
    "text_grey": "#b0bec5",
}

LOADING_PHRASES = [
    "Инициализация нейроморфных ядер...",
    "Прогрев SMTP-слушателя...",
    "Рекалибровка векторов прерываний...",
    "Синтез асинхронных потоков данных...",
    "Развёртывание ORM-сущностей...",
    "Триангуляция MX-записей...",
    "Генерация энтропии для DKIM-подписей...",
    "Валидация транзакционных логов...",
    "Оптимизация индексов базы данных...",
    "Расшифровка защищённых шаблонов...",
    "Прогрев кеша второго уровня...",
    "Опрос диспетчера служб Windows...",
    "Финализация сборки мусора (GC)...",
]

ROCKET_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve" width="100%" height="100%" version="1.0" style="shape-rendering:geometricPrecision; text-rendering:geometricPrecision; image-rendering:optimizeQuality; fill-rule:evenodd; clip-rule:evenodd"
viewBox="0 0 1028904 1028045">
 <defs>
  <style type="text/css">
   <![CDATA[
    .fil0 {fill:#1ABB9B}
    .fil1 {fill:#ffffff}
   ]]>
  </style>
 </defs>
 <g>
  <g>
   <path class="fil0" d="M398388 258797c-261503,3250 -231130,-25758 -359706,188452 -30715,51188 -50745,71218 -30488,127565l245899 9345c-39440,86177 -31335,98709 20827,153108 81107,84559 48027,61291 170496,36216 0,263198 -39137,338652 250565,148771 136088,-89187 25000,-245178 96913,-309378 58307,-52060 126086,-83800 182382,-203259 32334,-68626 87733,-309239 25139,-376411 -67273,-72192 -463018,-39301 -602027,225591z"/>
   <path class="fil1" d="M754541 183470c-108825,26998 -78704,177059 37139,151009 92930,-20903 59572,-174997 -37139,-151009z"/>
  </g>
 </g>
</svg>
"""

STYLESHEET = f"""
    QFrame#MainFrame {{
        background-color: {COLORS['bg']};
        border-radius: 10px;
    }}
    QLabel#TitleLabel {{
        color: {COLORS['text_white']};
        font-family: 'Segoe UI', 'Arial', sans-serif;
        font-size: 24px;
        font-weight: bold;
    }}
    QLabel#StatusLabel {{
        color: {COLORS['text_grey']};
        font-family: 'Segoe UI', 'Arial', sans-serif;
        font-size: 14px;
        min-height: 20px;
    }}
    QLabel#WaitLabel {{
        color: {COLORS['text_grey']};
        font-family: 'Segoe UI', 'Arial', sans-serif;
        font-size: 12px;
        font-style: italic;
    }}
"""


class SmokeParticle:
    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y
        self.size = random.uniform(5, 12)
        self.speed_x = random.uniform(-2.5, -0.5)
        self.speed_y = random.uniform(0.5, 6.0)
        self.initial_alpha = 200
        self.alpha = self.initial_alpha
        self.decay = random.uniform(4, 7)
        self.rotation = random.uniform(0, 360)
        self.rotation_speed = random.uniform(-20, 20)
        self.color = QColor(255, 255, 255)

    def update(self) -> bool:
        self.y += self.speed_y
        self.x += self.speed_x
        self.size += 0.3
        self.alpha -= self.decay
        self.rotation += self.rotation_speed
        # Белый дым остывает в оранжевый выхлоп по мере жизни частицы.
        life = max(0.0, self.alpha / self.initial_alpha)
        self.color = QColor(255, int(255 - (255 - 107) * (1 - life)), int(255 - (255 - 53) * (1 - life)))
        return self.alpha > 0


class AnimatedRocketWidget(QWidget):
    def __init__(self, svg_data: str, rocket_w: int = 100, parent=None) -> None:
        super().__init__(parent)
        self.rocket_width = rocket_w
        self.setFixedSize(300, 350)
        self.renderer = QSvgRenderer(QByteArray(svg_data.encode("utf-8")))
        self.particles: list[SmokeParticle] = []
        self.frame_count = 0

        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self._tick)
        self.anim_timer.start(1000 // ANIMATION_FPS)

    def _rocket_pos(self) -> tuple[float, float]:
        center_x = (self.width() - self.rocket_width) / 2
        offset = math.sin(self.frame_count * 0.12) * 4
        return center_x + 20 + offset, 20 - offset

    def _tick(self) -> None:
        self.frame_count += 1
        x, y = self._rocket_pos()
        for _ in range(3):
            self.particles.append(
                SmokeParticle(x + self.rocket_width * 0.25, y + self.rocket_width * 0.8)
            )
        self.particles = [p for p in self.particles if p.update()]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 — контракт Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        painter.setPen(Qt.NoPen)
        for p in self.particles:
            color = QColor(p.color)
            color.setAlpha(int(max(0, p.alpha)))
            painter.setBrush(QBrush(color))
            painter.save()
            painter.translate(p.x, p.y)
            painter.rotate(p.rotation)
            painter.drawEllipse(QPointF(0, 0), p.size / 2, p.size / 2)
            painter.restore()

        x, y = self._rocket_pos()
        self.renderer.render(painter, QRectF(x, y, self.rocket_width, self.rocket_width))


class RoundedProgressBar(QWidget):
    """Тонкая полоса-«пилюля», нарисованная вручную.

    QProgressBar на Windows рисует заполнение сегментами (нативный стиль) или
    с отрывом скругления (QSS) — отсюда «полоса из двух частей» и «кривая».
    Своя отрисовка убирает зависимость от стиля: жёлоб и заполнение — два
    скруглённых прямоугольника с антиалиасингом, заполнение всегда цельное.
    """

    def __init__(self, track_color: str, fill_color: str, parent=None) -> None:
        super().__init__(parent)
        self._min = 0
        self._max = 100
        self._value = 0
        self._track = QColor(track_color)
        self._fill = QColor(fill_color)
        self.setFixedHeight(8)

    def setRange(self, low: int, high: int) -> None:  # noqa: N802 — как у QProgressBar
        self._min, self._max = low, high
        self.update()

    def setValue(self, value: int) -> None:  # noqa: N802 — как у QProgressBar
        self._value = value
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 — контракт Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        width = self.width()
        height = self.height()
        radius = height / 2

        track = QPainterPath()
        track.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
        painter.fillPath(track, self._track)

        span = max(1, self._max - self._min)
        fraction = max(0.0, min(1.0, (self._value - self._min) / span))
        fill_width = width * fraction
        if fill_width > 0:
            fill = QPainterPath()
            fill.addRoundedRect(QRectF(0, 0, fill_width, height), radius, radius)
            painter.fillPath(fill, self._fill)


class WelcomeOverlay(QWidget):
    animation_finished = Signal()

    def __init__(self, version: str) -> None:
        super().__init__()
        self.version = version
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(600, 480)

        self._progress = 0.0
        self._fading = False
        self.total_range = 1000
        self.increment = self.total_range / (LOADING_TIME_MS / REFRESH_RATE_MS)

        self._init_ui()
        self._center_on_screen()
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._update_progress)
        self.timer.start(REFRESH_RATE_MS)

    def _init_ui(self) -> None:
        frame = QFrame(self)
        frame.setObjectName("MainFrame")
        frame.setGeometry(0, 0, self.width(), self.height())
        frame.setStyleSheet(STYLESHEET)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(25)
        shadow.setYOffset(8)
        shadow.setColor(QColor(0, 0, 0, 90))
        frame.setGraphicsEffect(shadow)

        layout = QVBoxLayout(frame)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(10)
        layout.setContentsMargins(40, 20, 40, 40)

        self.rocket_widget = AnimatedRocketWidget(ROCKET_SVG, rocket_w=100)
        layout.addWidget(self.rocket_widget, alignment=Qt.AlignCenter)

        title = QLabel("Почтовый шлюз Mail Gateway")
        title.setObjectName("TitleLabel")
        title.setAlignment(Qt.AlignCenter)
        title.setWordWrap(True)
        layout.addWidget(title)

        layout.addSpacing(20)

        self.status_label = QLabel(LOADING_PHRASES[0])
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

        self.progress_bar = RoundedProgressBar(COLORS["progress_bg"], COLORS["teal"])
        self.progress_bar.setRange(0, self.total_range)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        wait_label = QLabel("Пожалуйста, ждите...")
        wait_label.setObjectName("WaitLabel")
        wait_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(wait_label)

        layout.addStretch()

        developer = QLabel(f"Разработано ИП Somnium\nwww.somnium.kz\nВерсия: v{self.version}")
        developer.setAlignment(Qt.AlignCenter)
        developer.setStyleSheet(
            f"color: {COLORS['text_grey']}; font-family: 'Segoe UI', sans-serif; "
            "font-size: 8pt; margin-top: 10px;"
        )
        layout.addWidget(developer)

        # Значок продукта в углу — вместо гифки Quick-Queue.
        logo = QPixmap(icon_path())
        if not logo.isNull():
            logo = logo.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            logo_label = QLabel(frame)
            logo_label.setPixmap(logo)
            logo_label.setStyleSheet("background: transparent;")
            logo_label.resize(logo.size())
            logo_label.move(self.width() - logo.width() - 30, 20)

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen().geometry()
        self.move((screen.width() - self.width()) // 2, (screen.height() - self.height()) // 2)

    def _update_progress(self) -> None:
        if self._progress < self.total_range:
            self._progress += self.increment

        pct = min(self._progress / self.total_range, 1.0)
        index = min(int(pct * len(LOADING_PHRASES)), len(LOADING_PHRASES) - 1)
        self.status_label.setText(LOADING_PHRASES[index])
        self.progress_bar.setValue(int(self._progress))

        if pct >= 1.0:
            self.timer.stop()
            self.status_label.setText("Готовность 100%. Ожидание ядра...")
            self.progress_bar.setValue(self.total_range)

    def start_fade_out(self, duration_ms: int = 500) -> None:
        if self._fading:
            return
        self._fading = True
        self.progress_bar.setValue(self.total_range)
        self.status_label.setText("Успешный запуск.")
        self.timer.stop()
        self.rocket_widget.anim_timer.stop()

        self.animation = QPropertyAnimation(self, b"windowOpacity")
        self.animation.setDuration(duration_ms)
        self.animation.setStartValue(1.0)
        self.animation.setEndValue(0.0)
        self.animation.setEasingCurve(QEasingCurve.InOutQuad)
        self.animation.finished.connect(self.animation_finished.emit)
        self.animation.finished.connect(self.close)
        self.animation.start()
