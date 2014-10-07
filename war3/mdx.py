"""This module contains an importer for the MDX format."""


import io
import struct
import sys
from .model import *

__all__ = ["LoadError", "Loader", "load"]


def partition(elements, counts):
    i = 0
    for n in counts:
        li = []
        for j in range(n):
            li.append(elements[i])
            i += 1
        yield li


class LoadError(Exception):
    pass


class _ReadonlyBytesIO:
    def __init__(self, buf, idx=0):
        self.buf = buf
        self.idx = idx

    def read(self, n=-1):
        idx = self.idx
        if n < 0 or len(self.buf) - idx < n:
            self.idx = len(self.buf)
            return self.buf[self.idx:]
        else:
            self.idx += n
            return self.buf[idx:self.idx]


class Loader:
    def __init__(self, infile):
        self.infile = infile
        self.infile_stack = []
        self.model = Model()

    def load(self):
        self.check_magic_number()
        self.load_version()
        self.load_modelinfo()
        self.load_sequences()
        self.load_global_sequences()
        self.load_materials()
        self.load_textures()
        self.load_texture_animations()
        self.load_geosets()
        return self.model

    def check_magic_number(self):
        if self.infile.read(4) != b'MDLX':
            raise LoadError("not a MDX file")

    def check_block_magic(self, magic):
        buf = self.infile.read(4)
        if buf != magic:
            raise LoadError("expected %s, not %s" % (magic, buf))

    def load_version(self):
        self.check_block_magic(b'VERS')
        buf = self.load_block()
        self.model.version, = struct.unpack('<i', buf)

    def load_block(self):
        n, = struct.unpack('<i', self.infile.read(4))
        if n < 0:
            raise LoadError("expected a positive integer")
        return self.infile.read(n)

    def load_modelinfo(self):
        self.check_block_magic(b'MODL')
        buf = self.load_block()

        name, = struct.unpack_from('<80s', buf)
        name = name.rstrip(b'\x00').decode("ascii")
        bounds_radius, = struct.unpack_from('<f', buf, 80)
        min_extent = struct.unpack_from('<3f', buf, 84)
        max_extent = struct.unpack_from('<3f', buf, 96)
        blend_time, = struct.unpack_from('<i', buf, 108)

        self.model.model = ModelInfo(name, bounds_radius,
                                     min_extent, max_extent, blend_time)

    def load_sequences(self):
        self.check_block_magic(b'SEQS')
        buf = self.load_block()
        fmt = '<80s 2i f i f 4x f 3f 3f'

        for i in range(0, len(buf), struct.calcsize(fmt)):
            t = struct.unpack_from(fmt, buf, i)

            name = t[0].rstrip(b'\x00').decode("ascii")
            interval = t[1:3]
            move_speed = t[4]
            non_looping = bool(t[5])
            rarity = t[6]
            bounds_radius = t[7]
            min_extent = t[8:11]
            max_extent = t[11:]

            self.model.sequences.append(
                Animation(name, interval, move_speed, non_looping, rarity,
                          bounds_radius, min_extent, max_extent)
            )

    def load_global_sequences(self):
        self.check_block_magic(b'GLBS')
        buf = self.load_block()
        i, n = 0, len(buf)
        while i < n:
            duration, = struct.unpack_from('<i', buf, i)
            self.model.global_sequences.append(duration)
            i += 4

    def load_materials(self):
        self.check_block_magic(b'MTLS')
        buf = self.load_block()
        i, n = 0, len(buf)

        while i < n:
            t = struct.unpack_from('<i i i', buf, i)
            mat = Material(t[1], bool(t[2] & 0x01),
                           bool(t[2] & 0x10), bool(t[2] & 0x20))

            # HACK: let load_layers() read from existing data
            self.push_infile(_ReadonlyBytesIO(buf, i + 12))
            mat.layers = self.load_layers()
            self.pop_infile()

            self.model.materials.append(mat)
            i += t[0]

    def push_infile(self, infile):
        self.infile_stack.append(self.infile)
        self.infile = infile

    def pop_infile(self):
        infile = self.infile
        self.infile = self.infile_stack.pop()
        return infile

    def load_layers(self):
        self.check_block_magic(b'LAYS')
        nlays, = struct.unpack('<i', self.infile.read(4))
        fmt = '<5i f'
        lays = []

        for i in range(nlays):
            n, = struct.unpack('<i', self.infile.read(4))
            buf = self.infile.read(n - 4)

            t = struct.unpack_from(fmt, buf)
            layer = Layer(t[0], bool(t[1] & 0x01), bool(t[1] & 0x02),
                          bool(t[1] & 0x10), bool(t[1] & 0x20),
                          bool(t[1] & 0x40), bool(t[1] & 0x80),
                          t[2], t[3], t[4], t[5])

            j, n = struct.calcsize(fmt), len(buf)
            while j < n:
                j, anim = self.load_material_keyframe(buf, j)
                layer.animations.append(anim)

            lays.append(layer)

        return lays

    def load_material_keyframe(self, buf, j):
        magic = buf[j:j+4]
        if magic == b'KMTA':
            target = KF.MaterialAlpha
            fmt_val = '<i f'
            fmt_tan = '<2f'
        elif magic == b'KMTF':
            target = KF.MaterialTexture
            fmt_val = fmt_tan = '<2i'
        else:
            raise LoadError("exptected KMT{A,F}, not %s"
                            % magic.decode("ascii"))

        def fn_val(fmt):
            def _fn_val(buf, j):
                frame, value = struct.unpack_from(fmt, buf, j)
                j += 8
                return j, frame, value
            return _fn_val

        def fn_tan(fmt):
            def _fn_tan(buf, j):
                tin, tout = struct.unpack_from(fmt, buf, j)
                j += 8
                return j, tin, tout
            return _fn_tan

        return self.load_keyframe(buf, j, target, fn_val(fmt_val), fn_tan(fmt_tan))

    def load_textures(self):
        self.check_block_magic(b'TEXS')
        buf = self.load_block()
        fmt = '<i 256s 4x i'

        for i in range(0, len(buf), struct.calcsize(fmt)):
            t = struct.unpack_from(fmt, buf, i)
            rid = t[0]
            path = t[1].rstrip(b'\x00').decode("ascii")
            wrap_w = bool(t[2] & 1)
            wrap_h = bool(t[2] & 2)
            self.model.textures.append(Texture(rid, path, wrap_w, wrap_h))

    def load_texture_animations(self):
        magic = self.infile.read(4)
        if magic != b'TXAN':
            self.infile.seek(-4, io.SEEK_CUR)
            return
        buf = self.load_block()

        i, n = 0, len(buf)
        while i < n:
            trans, rot, scal = None, None, None
            j, k = 4, struct.unpack_from('<i', buf, i)[0]
            anims = []
            while j < k:
                j, anim = self.load_texture_keyframe(buf, j)
                anims.append(anim)
            self.model.texture_anims.append(anims)
            i += k

    def load_texture_keyframe(self, buf, j):
        magic = buf[j:j+4]
        if magic == b'KTAT':
            target = KF.TextureAnimTranslation
            fmt1 = '<2f'
        elif magic == b'KTAR':
            target = KF.TextureAnimRotation
        elif magic == b'KTAS':
            target = KF.TextureAnimScaling
        else:
            raise LoadError("exptected KTA{T,R,S}, not %s"
                            % magic.decode("ascii"))

        def fn_val(buf, j):
            frame, *value = struct.unpack_from('<i 3f', buf, j)
            value = tuple(value)
            j += 16
            return j, frame, value

        def fn_tan(buf, j):
            t = struct.unpack_from('<3f 3f', buf, j)
            tin, tout = t[:3], t[3:]
            j += 24
            return j, tin, tout

        return self.load_keyframe(buf, j, target, fn_val, fn_tan)

    def load_keyframe(self, buf, j, target, fn_val, fn_tan):
        t = struct.unpack_from('<3i', buf, j + 4)
        numkeys = t[0]
        linetype = LineType(t[1])
        gsid = t[2]
        j += 16

        anim = KeyframeAnimation(target, linetype, gsid)
        for k in range(numkeys):
            j, frame, value = fn_val(buf, j)

            if linetype in (LineType.Hermite, LineType.Bezier):
                j, tin, tout = fn_tan(buf, j)
            else:
                tin = tout = None

            anim.keyframes.append(Keyframe(frame, value, tin, tout))

        return j, anim

    def load_geosets(self):
        self.check_block_magic(b'GEOS')
        buf = self.load_block()

        i, n = 0, len(buf)
        while i < n:
            i += self.load_geoset(buf, i)

    def load_geoset(self, buf, i):
        m, = struct.unpack_from('<i', buf, i)

        self.push_infile(_ReadonlyBytesIO(buf, i + 4))
        verts = self.load_vectors(b'VRTX')
        norms = self.load_vectors(b'NRMS')
        faces = self.load_faces()
        vgrps = self.load_vectors(b'GNDX', '<B')
        self.pop_infile()

        self.model.geosets.append(Geoset(verts, norms, faces, vgrps))
        return m

    def load_vectors(self, magic, type_='<3f'):
        self.check_block_magic(magic)
        n, = struct.unpack('<i', self.infile.read(4))
        m = struct.calcsize(type_)
        vectors = []

        for i in range(n):
            t = struct.unpack(type_, self.infile.read(m))
            vectors.append(t[0] if len(t) == 1 else t)

        return vectors

    def load_faces(self):
        ptyps = [PrimitiveType(t)
                 for t in self.load_vectors(b'PTYP', '<i')]

        pcnts = self.load_vectors(b'PCNT', '<i')
        assert len(ptyps) == len(pcnts)

        pvtx = self.load_vectors(b'PVTX', '<h')
        assert len(pvtx) == sum(pcnts)

        return [Primitives(*t) for t in zip(ptyps, partition(pvtx, pcnts))]


def load(infile):
    if isinstance(infile, str):
        infile = open(infile, 'rb')
    return Loader(infile).load()
