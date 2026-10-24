#!/usr/bin/env python3
"""Compose existing screenshots; never edits the source captures."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import argparse
import json
import math

parser = argparse.ArgumentParser()
parser.add_argument('directory', type=Path)
parser.add_argument('--label', default='Before')
args = parser.parse_args()
manifest = json.loads((args.directory / 'manifest.json').read_text())
font_path = str(Path(__file__).resolve().parents[1] / 'fonts' / 'Manrope.ttf')
font = ImageFont.truetype(font_path, 18)
small = ImageFont.truetype(font_path, 13)
for device in ('desktop', 'phone'):
    entries = [item for item in manifest['results'] if item['device'] == device]
    width = 480 if device == 'desktop' else 234
    viewport = entries[0]['viewport']
    height = round(viewport['height'] * width / viewport['width'])
    gutter, label, top = 16, 40, 66
    sheet = Image.new('RGB', (3 * width + 4 * gutter, top + math.ceil(len(entries) / 3) * (height + label + gutter)), '#edf1e9')
    draw = ImageDraw.Draw(sheet)
    draw.text((gutter, 10), f'{args.label} · {device} · first viewport', font=font, fill='#173f35')
    draw.text((gutter, 36), 'Synthetic test fixtures. No analysis performed. Full-page captures are separate.', font=small, fill='#425e51')
    for index, entry in enumerate(entries):
        x = gutter + index % 3 * (width + gutter)
        y = top + index // 3 * (height + label + gutter)
        source = Image.open(args.directory / entry['screenshot']).convert('RGB')
        crop = source.crop((0, 0, viewport['width'], viewport['height']))
        crop = crop.resize((width, height), Image.Resampling.LANCZOS)
        sheet.paste(crop, (x, y + label))
        draw.text((x, y + 8), entry['state'], font=font, fill='#173f35')
    sheet.save(args.directory / f'contact-{device}.png')
