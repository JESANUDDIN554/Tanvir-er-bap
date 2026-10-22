"""Pure-Python Ed25519 (RFC 8032) - sign + verify, no dependencies.

Used by the license system: the app verifies license and integrity
signatures with the embedded public key; the issuer tool signs.
Extended twisted-Edwards coordinates (a = -1) keep verification
fast enough for startup checks. Validated against the RFC 8032
section 7.1 test vectors.
"""

import hashlib

_q = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _q - 2, _q)) % _q
_I = pow(2, (_q - 1) // 4, _q)


def _sha512(data):
    return hashlib.sha512(data).digest()


def _xrecover(y):
    xx = ((y * y - 1) * pow(_D * y * y + 1, _q - 2, _q)) % _q
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_By = (4 * pow(5, _q - 2, _q)) % _q
_Bx = _xrecover(_By)
# base point in extended coordinates (X, Y, Z, T)
_B = (_Bx, _By, 1, (_Bx * _By) % _q)
_IDENTITY = (0, 1, 1, 0)


def _edwards_add(P, Q):
    X1, Y1, Z1, T1 = P
    X2, Y2, Z2, T2 = Q
    A = ((Y1 - X1) * (Y2 - X2)) % _q
    Bv = ((Y1 + X1) * (Y2 + X2)) % _q
    C = (T1 * 2 * _D * T2) % _q
    Dv = (Z1 * 2 * Z2) % _q
    E = (Bv - A) % _q
    F = (Dv - C) % _q
    G = (Dv + C) % _q
    H = (Bv + A) % _q
    return ((E * F) % _q, (G * H) % _q, (F * G) % _q, (E * H) % _q)


def _edwards_double(P):
    X1, Y1, Z1, _T1 = P
    A = (X1 * X1) % _q
    Bv = (Y1 * Y1) % _q
    C = (2 * Z1 * Z1) % _q
    Dv = (-A) % _q
    E = ((X1 + Y1) * (X1 + Y1) - A - Bv) % _q
    G = (Dv + Bv) % _q
    F = (G - C) % _q
    H = (Dv - Bv) % _q
    return ((E * F) % _q, (G * H) % _q, (F * G) % _q, (E * H) % _q)


def _scalarmult(P, e):
    if e == 0:
        return _IDENTITY
    Q = _scalarmult(P, e // 2)
    Q = _edwards_double(Q)
    if e & 1:
        Q = _edwards_add(Q, P)
    return Q


def _encode_point(P):
    X, Y, Z, _T = P
    zinv = pow(Z, _q - 2, _q)
    x = (X * zinv) % _q
    y = (Y * zinv) % _q
    bits = [(y >> i) & 1 for i in range(255)] + [x & 1]
    out = bytearray(32)
    for i in range(32):
        for j in range(8):
            out[i] |= bits[i * 8 + j] << j
    return bytes(out)


def _decode_int(data):
    return int.from_bytes(data, "little")


def _encode_int(y):
    return y.to_bytes(32, "little")


def _decode_point(data):
    if len(data) != 32:
        raise ValueError("bad point length")
    bits = []
    for i in range(32):
        for j in range(8):
            bits.append((data[i] >> j) & 1)
    y = sum(bits[i] * 2 ** i for i in range(255))
    sign = bits[255]
    if y >= _q:
        raise ValueError("point y out of range")
    x = _xrecover(y)
    if x & 1 != sign:
        x = _q - x
    return (x, y, 1, (x * y) % _q)


def _clamp(h):
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= (1 << 254)
    return a


def publickey(seed32):
    """Derive the 32-byte public key from a 32-byte secret seed."""
    if len(seed32) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _sha512(seed32)
    a = _clamp(h)
    return _encode_point(_scalarmult(_B, a))


def sign(seed32, msg):
    """Sign bytes with a 32-byte secret seed. Returns 64-byte sig."""
    if len(seed32) != 32:
        raise ValueError("seed must be 32 bytes")
    h = _sha512(seed32)
    a = _clamp(h)
    prefix = h[32:]
    A = _encode_point(_scalarmult(_B, a))
    r = _decode_int(_sha512(prefix + msg)) % _L
    R = _scalarmult(_B, r)
    Rs = _encode_point(R)
    S = (r + _decode_int(_sha512(Rs + A + msg)) * a) % _L
    return Rs + _encode_int(S)


def verify(pub32, msg, sig64):
    """True iff sig64 is a valid signature of msg under pub32."""
    try:
        if len(pub32) != 32 or len(sig64) != 64:
            return False
        A = _decode_point(pub32)
        R = _decode_point(sig64[:32])
        S = _decode_int(sig64[32:])
        if S >= _L:
            return False
        h = _decode_int(_sha512(sig64[:32] + pub32 + msg))
        # [S]B == R + [H(R||A||M)]A
        left = _encode_point(_scalarmult(_B, S))
        right = _encode_point(_edwards_add(R, _scalarmult(A, h)))
        return left == right
    except Exception:  # noqa: BLE001 - any malformed input is a No
        return False
