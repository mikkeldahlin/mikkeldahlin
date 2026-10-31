"""Merge app/data/area_images.json (from build_maps.py) into app/data/areas.json `image` fields.
Run after extract_world.py and build_maps.py:  python3 -I scripts/merge_area_images.py app/data
"""
import json, sys, os
d = sys.argv[1]
areas = json.load(open(os.path.join(d, 'areas.json'), encoding='utf-8'))
imgs = json.load(open(os.path.join(d, 'area_images.json'), encoding='utf-8'))
n = 0
for a in areas:
    a['image'] = imgs.get(a['id'])
    n += a['image'] is not None
json.dump(areas, open(os.path.join(d, 'areas.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print(f'areas: {len(areas)}, with image: {n}')
