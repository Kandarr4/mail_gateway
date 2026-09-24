"""Генерация значка Mail Gateway: конверт на тёмно-синей плашке.

Запускается один раз, результат (`tray/icon.ico`) хранится в репозитории:

    .venv\\Scripts\\python.exe tray\\generate_icon.py

Палитра совпадает с фирменной у Quick-Queue (навы #2b3c4e, бирюза #1abc9c),
чтобы продукты Somnium выглядели одной линейкой. Это программная заглушка
приемлемого качества: при появлении дизайнерского значка достаточно заменить
файл, ничего не перегенерируя.
"""

from pathlib import Path

from PIL import Image, ImageDraw

NAVY = (43, 60, 78, 255)        # #2b3c4e
TEAL = (26, 188, 156, 255)      # #1abc9c
TEAL_DARK = (22, 160, 133, 255) # #16a085

OUTPUT = Path(__file__).resolve().parent / "icon.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)
CANVAS = 256  # рисуем один раз в максимальном размере, уменьшаем со сглаживанием


def draw_icon() -> Image.Image:
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Скруглённая плашка.
    d.rounded_rectangle((8, 8, CANVAS - 8, CANVAS - 8), radius=52, fill=NAVY)

    # Конверт: корпус.
    left, top, right, bottom = 48, 78, CANVAS - 48, CANVAS - 78
    d.rounded_rectangle((left, top, right, bottom), radius=14, fill=TEAL)

    # Клапан — тёмный треугольник от верхних углов к центру.
    apex_y = top + (bottom - top) * 0.58
    d.polygon(
        [(left + 6, top + 8), (right - 6, top + 8), (CANVAS // 2, apex_y)],
        fill=TEAL_DARK,
    )
    # Линии клапана поверх, цветом плашки — читаются как «конверт» даже в 16px.
    for start in ((left + 4, top + 6), (right - 4, top + 6)):
        d.line([start, (CANVAS // 2, apex_y)], fill=NAVY, width=10)

    return img


def main() -> None:
    base = draw_icon()
    base.save(
        OUTPUT,
        format="ICO",
        sizes=[(s, s) for s in SIZES],
    )
    print(f"Готово: {OUTPUT} ({', '.join(str(s) for s in SIZES)} px)")


if __name__ == "__main__":
    main()
