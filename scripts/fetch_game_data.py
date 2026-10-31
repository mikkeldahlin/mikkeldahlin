# Fetch selected files from the official Reborn download (rebornevo.com) via HTTP Range requests.
# Usage: python3 -I scripts/fetch_game_data.py URL list [regex] | get OUTDIR regex
# Map data used by extract_links.py: core-lnx.zip then patch.zip from https://www.rebornevo.com/downloads/rebornremote/, regex "(^|/)Data/(Map\d+|MapInfos)\.rxdata$", patch extracted over core.
import sys, io, re, os, time, zipfile, urllib.request
UA = "RebornFieldGuide/0.1 (personal fan companion tool; https://github.com/mikkeldahlin/mikkeldahlin)"
class RangeFile(io.RawIOBase):
    def __init__(s, url):
        s.url = url; s.pos = 0; s.size = int(s._get(0, 0, head=True))
    def _get(s, a, b, head=False):
        for i in range(6):
            try:
                r = urllib.request.urlopen(urllib.request.Request(s.url, headers={'User-Agent': UA, 'Range': f'bytes={a}-{b}'}), timeout=180)
                if head: return r.headers['Content-Range'].split('/')[1]
                return r.read()
            except Exception as e:
                print('retry', i, e, file=sys.stderr); time.sleep(3 * (i + 1))
        raise SystemExit('failed')
    def seekable(s): return True
    def readable(s): return True
    def tell(s): return s.pos
    def seek(s, o, w=0):
        s.pos = o if w == 0 else s.pos + o if w == 1 else s.size + o; return s.pos
    def read(s, n=-1):
        if n < 0: n = s.size - s.pos
        if n == 0 or s.pos >= s.size: return b''
        d = s._get(s.pos, min(s.pos + n, s.size) - 1); s.pos += len(d); return d
    def readinto(s, b):
        d = s.read(len(b)); b[:len(d)] = d; return len(d)
url, cmd = sys.argv[1], sys.argv[2]
z = zipfile.ZipFile(io.BufferedReader(RangeFile(url), buffer_size=1 << 20))
if cmd == 'list':
    pat = re.compile(sys.argv[3] if len(sys.argv) > 3 else '.')
    infos = [i for i in z.infolist() if pat.search(i.filename)]
    for i in infos[:60]: print(i.filename, i.file_size, i.compress_size)
    print('matched', len(infos), 'total uncompressed', sum(i.file_size for i in infos), 'compressed', sum(i.compress_size for i in infos))
else:
    out, pat = sys.argv[3], re.compile(sys.argv[4])
    for i in z.infolist():
        if pat.search(i.filename) and not i.is_dir():
            dest = os.path.join(out, i.filename)
            if os.path.exists(dest) and os.path.getsize(dest) == i.file_size: continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(i) as src, open(dest, 'wb') as f: f.write(src.read())
    print('done')
