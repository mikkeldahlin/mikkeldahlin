"""Minimal, safe Ruby Marshal 4.8 reader (pure Python).

Never instantiates or evaluates anything: Ruby objects become plain Python
values.  Used by extract_links.py to read RPG Maker XP .rxdata files, which
are untrusted binary input.

Mapping:
  nil/true/false      -> None/True/False
  Fixnum/Bignum       -> int
  Float               -> float
  String              -> RStr (bytes subclass; .text gives a str)
  Symbol              -> RSym (str subclass)
  Array               -> list
  Hash                -> dict (unhashable keys are skipped)
  Object 'o'          -> RObj(cls, ivars)   ivars keys keep the leading '@'
  Struct 'S'          -> RObj(cls, members)
  user dump 'u'       -> RUser(cls, raw bytes)   (Table/Tone/Color stay opaque)
  user marshal 'U'    -> RObj(cls, {"__data__": value})
  class/module        -> RSym of the name
  regexp              -> RStr of the source
"""

import struct


class RStr(bytes):
    @property
    def text(self):
        return self.decode("utf-8", "replace")


class RSym(str):
    pass


class RObj:
    __slots__ = ("cls", "iv")

    def __init__(self, cls, iv=None):
        self.cls = cls
        self.iv = iv if iv is not None else {}

    def get(self, name, default=None):
        return self.iv.get("@" + name, default)

    def __repr__(self):
        return f"RObj({self.cls}, {list(self.iv)})"


class RUser:
    __slots__ = ("cls", "data")

    def __init__(self, cls, data):
        self.cls = cls
        self.data = data

    def __repr__(self):
        return f"RUser({self.cls}, {len(self.data)} bytes)"


class MarshalError(ValueError):
    pass


class _Reader:
    MAX_DEPTH = 2000

    def __init__(self, data):
        self.d = data
        self.p = 0
        self.syms = []
        self.objs = []
        self.depth = 0

    def byte(self):
        if self.p >= len(self.d):
            raise MarshalError("unexpected end of data")
        b = self.d[self.p]
        self.p += 1
        return b

    def take(self, n):
        if n < 0 or self.p + n > len(self.d):
            raise MarshalError("bad length")
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def long(self):
        c = self.byte()
        if c > 127:
            c -= 256
        if c == 0:
            return 0
        if 5 <= c <= 127:
            return c - 5
        if -128 <= c <= -5:
            return c + 5
        n = abs(c)
        v = int.from_bytes(self.take(n), "little")
        if c < 0:
            v -= 1 << (8 * n)
        return v

    def bytes_(self):
        return self.take(self.long())

    def entry(self, v):
        self.objs.append(v)
        return len(self.objs) - 1

    def symbol(self):
        t = self.byte()
        if t == 0x3A:  # ':'
            s = RSym(self.bytes_().decode("utf-8", "replace"))
            self.syms.append(s)
            return s
        if t == 0x3B:  # ';'
            i = self.long()
            if not 0 <= i < len(self.syms):
                raise MarshalError("bad symlink")
            return self.syms[i]
        if t == 0x49:  # 'I' symbol with encoding ivars
            s = self.symbol()
            for _ in range(self.long()):
                self.symbol()
                self.obj()
            return s
        raise MarshalError(f"expected symbol, got {t:#x}")

    def obj(self):
        self.depth += 1
        if self.depth > self.MAX_DEPTH:
            raise MarshalError("nesting too deep")
        try:
            return self._obj()
        finally:
            self.depth -= 1

    def _obj(self):
        t = chr(self.byte())
        if t == "0":
            return None
        if t == "T":
            return True
        if t == "F":
            return False
        if t == "i":
            return self.long()
        if t in ":;":
            self.p -= 1
            return self.symbol()
        if t == "@":
            i = self.long()
            if not 0 <= i < len(self.objs):
                raise MarshalError("bad object link")
            return self.objs[i]
        if t == "I":
            v = self.obj()
            for _ in range(self.long()):
                self.symbol()
                self.obj()
            return v
        if t == '"':
            v = RStr(self.bytes_())
            self.entry(v)
            return v
        if t == "f":
            s = self.bytes_().split(b"\0", 1)[0].decode("ascii", "replace")
            try:
                v = float({"nan": "nan", "inf": "inf", "-inf": "-inf"}.get(s, s))
            except ValueError:
                v = 0.0
            self.entry(v)
            return v
        if t == "l":
            sign = chr(self.byte())
            n = self.long()
            v = int.from_bytes(self.take(n * 2), "little")
            if sign == "-":
                v = -v
            self.entry(v)
            return v
        if t == "[":
            v = []
            self.entry(v)
            for _ in range(self.long()):
                v.append(self.obj())
            return v
        if t in "{}":
            v = {}
            self.entry(v)
            for _ in range(self.long()):
                k = self.obj()
                val = self.obj()
                try:
                    v[k] = val
                except TypeError:
                    pass
            if t == "}":
                self.obj()  # default value, ignored
            return v
        if t == "o":
            cls = self.symbol()
            v = RObj(cls)
            self.entry(v)
            for _ in range(self.long()):
                k = self.symbol()
                v.iv[k] = self.obj()
            return v
        if t == "S":
            cls = self.symbol()
            v = RObj(cls)
            self.entry(v)
            for _ in range(self.long()):
                k = self.symbol()
                v.iv["@" + k.lstrip("@")] = self.obj()
            return v
        if t == "u":
            cls = self.symbol()
            v = RUser(cls, self.bytes_())
            self.entry(v)
            return v
        if t == "U":
            cls = self.symbol()
            v = RObj(cls)
            self.entry(v)
            v.iv["__data__"] = self.obj()
            return v
        if t in "cmM":
            v = RSym(self.bytes_().decode("utf-8", "replace"))
            self.entry(v)
            return v
        if t == "/":
            v = RStr(self.bytes_())
            self.byte()
            self.entry(v)
            return v
        if t in "eC":
            self.symbol()
            return self.obj()
        if t == "d":
            cls = self.symbol()
            v = RObj(cls)
            self.entry(v)
            v.iv["__data__"] = self.obj()
            return v
        raise MarshalError(f"unsupported type {t!r} at {self.p - 1}")


def loads(data):
    if len(data) < 2 or data[0] != 4 or data[1] != 8:
        raise MarshalError("not Marshal 4.8")
    r = _Reader(data)
    r.p = 2
    return r.obj()


def load_path(path):
    with open(path, "rb") as f:
        return loads(f.read())


def table_dims(u):
    """Dimensions of an RGSS Table user dump: (xsize, ysize, zsize)."""
    if not isinstance(u, RUser) or len(u.data) < 20:
        return None
    _dim, xs, ys, zs, _n = struct.unpack_from("<5i", u.data, 0)
    return xs, ys, zs
