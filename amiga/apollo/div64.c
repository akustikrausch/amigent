/* 64-bit arithmetic for the Apollo gcc build. That tree's libgcc.a is empty,
 * and the Apollo link does not pull libnix. Byte counts use unsigned long
 * long. Only 32-bit arithmetic here, so this file does not call itself. */
typedef struct {
    unsigned long hi, lo;
} u64;

static u64 split(unsigned long long v)
{
    union {
        unsigned long long u;
        u64 s;
    } x;
    x.u = v;
    return x.s;
}

static unsigned long long join(u64 s)
{
    union {
        unsigned long long u;
        u64 s;
    } x;
    x.s = s;
    return x.u;
}

static u64 udivmod(u64 n, u64 d, u64 *rem)
{
    u64 q, r;
    int i;

    q.hi = q.lo = r.hi = r.lo = 0;
    if (d.hi == 0 && d.lo == 0) {
        if (rem)
            *rem = n;
        return q;
    }
    for (i = 63; i >= 0; i--) {
        unsigned long bit = i >= 32 ? (n.hi >> (i - 32)) & 1 : (n.lo >> i) & 1;

        r.hi = (r.hi << 1) | (r.lo >> 31);
        r.lo <<= 1;
        r.lo |= bit;
        if (r.hi > d.hi || (r.hi == d.hi && r.lo >= d.lo)) {
            if (r.lo < d.lo)
                r.hi--;
            r.lo -= d.lo;
            r.hi -= d.hi;
            if (i >= 32)
                q.hi |= 1UL << (i - 32);
            else
                q.lo |= 1UL << i;
        }
    }
    if (rem)
        *rem = r;
    return q;
}

unsigned long long __udivdi3(unsigned long long a, unsigned long long b)
{
    return join(udivmod(split(a), split(b), 0));
}

unsigned long long __umoddi3(unsigned long long a, unsigned long long b)
{
    u64 rem;
    udivmod(split(a), split(b), &rem);
    return join(rem);
}

/* 16-bit factors, so gcc emits a 32-bit mul and not __muldi3. */
static unsigned long mul16(unsigned long a, unsigned long b)
{
    return (a & 0xffffUL) * (b & 0xffffUL);
}

static unsigned long long mul32(unsigned long a, unsigned long b)
{
    unsigned long a0 = a & 0xffffUL, a1 = a >> 16;
    unsigned long b0 = b & 0xffffUL, b1 = b >> 16;
    unsigned long p00 = mul16(a0, b0);
    unsigned long p01 = mul16(a0, b1);
    unsigned long p10 = mul16(a1, b0);
    unsigned long p11 = mul16(a1, b1);
    unsigned long mid = (p00 >> 16) + (p01 & 0xffffUL) + (p10 & 0xffffUL);
    unsigned long lo = (p00 & 0xffffUL) | (mid << 16);
    unsigned long hi = (mid >> 16) + (p01 >> 16) + (p10 >> 16) + p11;

    return ((unsigned long long)hi << 32) | lo;
}

unsigned long long __muldi3(unsigned long long a, unsigned long long b)
{
    u64 x = split(a), y = split(b);
    u64 r = split(mul32(x.lo, y.lo));

    r.hi += x.lo * y.hi + x.hi * y.lo;
    return join(r);
}

unsigned long long __ashldi3(unsigned long long v, int n)
{
    u64 x = split(v);

    if (n <= 0)
        return v;
    if (n >= 64)
        return 0;
    if (n >= 32) {
        x.hi = x.lo << (n - 32);
        x.lo = 0;
    } else {
        x.hi = (x.hi << n) | (x.lo >> (32 - n));
        x.lo <<= n;
    }
    return join(x);
}

unsigned long long __lshrdi3(unsigned long long v, int n)
{
    u64 x = split(v);

    if (n <= 0)
        return v;
    if (n >= 64)
        return 0;
    if (n >= 32) {
        x.lo = x.hi >> (n - 32);
        x.hi = 0;
    } else {
        x.lo = (x.lo >> n) | (x.hi << (32 - n));
        x.hi >>= n;
    }
    return join(x);
}
