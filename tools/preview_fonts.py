"""Render the eight built fonts with FreeType (Pillow); no OS font installation."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build' / 'fonts'
fonts = sorted(OUT.glob('*.otf'))
assert len(fonts) == 8
im = Image.new('RGB', (1600, 8 * 255 + 80), '#171b22')
draw = ImageDraw.Draw(im)
label = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf', 21)
draw.text((30, 20), 'AVOWED · Українські шрифти · Оригінальна латинка + адаптована кирилиця Dante eText', font=label, fill='#e1c28a')
for i, path in enumerate(fonts):
    y = 80 + i * 255
    draw.text((30, y), path.stem, font=label, fill='#d9bb85')
    font = ImageFont.truetype(str(path), 36)
    for j, text in enumerate([
        'Avowed — Living Lands · Живі Землі — Ґалаґан, Єдність, Історія, Їжак',
        'АБВГҐДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯ',
        'абвгґдеєжзиіїйклмнопрстуфхцчшщьюя · 0123456789',
    ]):
        assert draw.textbbox((30, y + 38 + j * 46), text, font=font)[2] < 1570
        draw.text((30, y + 38 + j * 46), text, font=font, fill='#f1ede4')
    small = ImageFont.truetype(str(path), 22)
    draw.text((30, y + 185), 'Зберегти гру · Налаштування · Повернутися · мандрівник · здоров’я · ґрунт · її історія', font=small, fill='#c1c8d4')
    draw.line((30, y + 239, 1570, y + 239), fill='#343c48')
im.save(OUT / 'font_preview.png')
print(OUT / 'font_preview.png')
