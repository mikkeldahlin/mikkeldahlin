"""Post-process: merge area_images.json into areas.json `image` fields; sort uncategorized trainers last.
Run after extract_world.py and build_maps.py:  python3 -I scripts/postprocess.py app/data
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

# Uncategorized trainers (the "Test Train" secret-room battles) sort last in story order.
tp = os.path.join(d, 'trainers.json')
trainers = json.load(open(tp, encoding='utf-8'))
moved = [t['id'] for t in trainers if t.get('category') is None and t.get('order') is not None]
for t in trainers:
    if t['id'] in moved:
        t['order'] = None
json.dump(trainers, open(tp, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print(f'trainers: order cleared for {moved}')
