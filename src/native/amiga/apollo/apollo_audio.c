/* ARNE mix for the Apollo daemon. CMD_AUDIO is opcode 49; PIP is not implemented here.
 * The offsets are the ones measured from arne.audio 4.22. A later driver
 * that moves them is ignored rather than followed. */
#include <exec/types.h>
#include <exec/memory.h>
#include <exec/libraries.h>
#include <exec/execbase.h>
#include <proto/exec.h>

#include "apollo_audio.h"

#define ARNE_ID "arne.audio 4.22"
#define ARNE_STRING 0x29
#define ARNE_SLOT 0x984
#define ARNE_SEGLIST 42
#define CTRL_MIX 28
#define CTRL_DATA 36
#define CTRL_FRAMES 52
#define PRIV_BACK 64
#define PRIV_INDEX 16
#define PRIV_HALF 24

static BOOL ram(ULONG p)
{
    if (p < 0x1000UL || p >= 0xFFFF0000UL)
        return FALSE;
    return TypeOfMem((APTR)p) != 0;
}

static ULONG peek(const void *p)
{
    return *(volatile ULONG *)p;
}

static BOOL prefix(const UBYTE *s, const char *want)
{
    if (!s)
        return FALSE;
    while (*want) {
        if (*s++ != (UBYTE)*want++)
            return FALSE;
    }
    return TRUE;
}

/* Code hunk of the open arne.audio. */
static ULONG arne_base;

/* The playing half is the one the mixer is not filling. arne.audio keeps the
 * index at +16 as 0 or 16 and fills the half that index selects. The other
 * half still holds the buffer it handed to the DAC. Interrupts stay on:
 * Disable() here deadlocks the machine, because the lookup calls exec while
 * every other task is frozen. */
static ULONG audio_why;

static const UBYTE *arne_mix(ULONG *rate, ULONG *bytes, ULONG *which, ULONG *hold)
{
    struct Library *lib;
    UBYTE *id, *base, *code;
    ULONG bptr, slot, priv, ctrl, freq, frames, index, sel, ptr, gap;

    *rate = 0;
    *bytes = 0;
    if (hold)
        *hold = 0;
    Forbid();
    lib = (struct Library *)FindName(&SysBase->LibList, (STRPTR)"arne.audio");
    if (!lib || lib->lib_OpenCnt == 0) {
        Permit();
        return NULL;
    }
    id = (UBYTE *)lib->lib_IdString;
    bptr = peek((UBYTE *)lib + ARNE_SEGLIST);
    Permit();
    if (!ram((ULONG)id) || id < (UBYTE *)(ARNE_STRING + 16) || !prefix(id, ARNE_ID)) {
        audio_why = 3;
        return NULL;
    }
    base = id - ARNE_STRING;
    code = (UBYTE *)((bptr << 2) + 4);
    if (code != base || !ram((ULONG)base) || !ram((ULONG)(base + ARNE_SLOT))) {
        audio_why = 4;
        return NULL;
    }
    if (base[4] != 0x4A || base[5] != 0xFC || !prefix(base + 0x1e, "arne.audio")) {
        audio_why = 5;
        return NULL;
    }
    slot = peek(base + ARNE_SLOT);
    /* The private block is 504 bytes and the audioctrl fields we use sit
     * within 56 bytes. Both spans have to be RAM before any field is read. */
    if (!ram(slot) || !ram(slot + 68)) {
        audio_why = 6;
        return NULL;
    }
    priv = slot;
    ctrl = peek((UBYTE *)priv + PRIV_BACK);
    if (!ram(ctrl) || !ram(ctrl + 56) || peek((UBYTE *)ctrl + CTRL_DATA) != priv) {
        audio_why = 7;
        return NULL;
    }
    freq = peek((UBYTE *)ctrl + CTRL_MIX);
    frames = peek((UBYTE *)ctrl + CTRL_FRAMES);
    index = peek((UBYTE *)priv + PRIV_INDEX);
    if (index != 0 && index != 16) {
        audio_why = 8;
        return NULL;
    }
    if (freq < 8000 || freq > 96000 || frames < 16 || frames > 65535) {
        audio_why = 9;
        return NULL;
    }
    sel = PRIV_HALF + (index ^ 16);
    ptr = peek((UBYTE *)priv + sel);
    gap = peek((UBYTE *)priv + sel + 4);
    if (gap <= ptr) {
        audio_why = 10;
        return NULL;
    }
    gap -= ptr;
    if (gap < 64 || gap > 256UL * 1024) {
        audio_why = 10;
        return NULL;
    }
    if (frames * 4UL < gap)
        gap = frames * 4UL;
    gap &= ~3UL;
    if (!ram(ptr) || !ram(ptr + gap - 1)) {
        audio_why = 11;
        return NULL;
    }
    *rate = freq;
    *bytes = gap;
    if (which)
        *which = index;
    if (hold)
        *hold = priv;
    arne_base = (ULONG)base;
    audio_why = 0;
    return (const UBYTE *)ptr;
}

static void s16le(UBYTE *d, LONG sample)
{
    d[0] = (UBYTE)sample;
    d[1] = (UBYTE)(sample >> 8);
}

static ULONG copy_mix(UBYTE *dst, ULONG room, const UBYTE *src, ULONG n)
{
    ULONG i;

    if (n > room)
        n = room & ~3UL;
    for (i = 0; i < n; i += 4) {
        s16le(dst + i, (SHORT)((UWORD)src[i] << 8 | src[i + 1]));
        s16le(dst + i + 2, (SHORT)((UWORD)src[i + 2] << 8 | src[i + 3]));
    }
    return n;
}

/* The half the driver is not filling, from a private block arne_mix already
 * checked. Walking the library again on every flip costs long enough to miss
 * the next one. */
static const UBYTE *next_half(ULONG hold, ULONG *index, ULONG *bytes)
{
    ULONG idx, sel, ptr, gap;

    if (!TypeOfMem((APTR)hold))
        return NULL;
    idx = peek((UBYTE *)hold + PRIV_INDEX);
    if (idx != 0 && idx != 16)
        return NULL;
    sel = PRIV_HALF + (idx ^ 16);
    ptr = peek((UBYTE *)hold + sel);
    gap = peek((UBYTE *)hold + sel + 4);
    if (gap <= ptr)
        return NULL;
    gap -= ptr;
    if (gap < 64 || gap > 256UL * 1024)
        return NULL;
    gap &= ~3UL;
    if (!ram(ptr) || !ram(ptr + gap - 1))
        return NULL;
    *index = idx;
    *bytes = gap;
    return (const UBYTE *)ptr;
}

static BOOL hold_live(ULONG hold)
{
    ULONG ctrl;

    if (!hold || !TypeOfMem((APTR)hold))
        return FALSE;
    ctrl = peek((UBYTE *)hold + PRIV_BACK);
    if (!ram(ctrl) || !ram(ctrl + 56))
        return FALSE;
    return peek((UBYTE *)ctrl + CTRL_DATA) == hold;
}

/* One half, for a caller that asks once. A stream uses the FIFO below and
 * does not share this. The driver's interrupt is not hooked: taking over
 * that vector locked the machine. */
static ULONG seen_hold, seen_index = 0xffffffff;

/* About a second and a half at the mix rate. A reader that pauses while a
 * model looks at what it already has does not lose the audio still waiting.
 * Must stay a power of two: the indexes wrap with a mask. */
#define AUDIO_FIFO (256 * 1024)

static struct Task *audio_owner;
static UBYTE audio_fifo[AUDIO_FIFO];
static ULONG fifo_r, fifo_w, stream_hold, stream_index, stream_tag, audio_seq, audio_bytes;
static volatile UBYTE mix_stir;

/* The newest half, in s16le. One repeat covers a flip the poll did not see.
 * It is not played out on a clock: that stretches a short scrap into the tone. */
#define GRAIN_MAX 4096
static UBYTE grain[GRAIN_MAX];
static ULONG grain_n, grain_pos;

static void fifo_write(const UBYTE *src, ULONG n)
{
    ULONG fill, room, drop, at, first;

    if (!n)
        return;
    if (n > AUDIO_FIFO)
        n = AUDIO_FIFO;
    n &= ~3UL;
    fill = fifo_w - fifo_r;
    room = AUDIO_FIFO - fill;
    if (n > room) {
        drop = (n - room + 3UL) & ~3UL;
        if (drop > fill)
            drop = fill & ~3UL;
        fifo_r += drop;
    }
    at = fifo_w & (AUDIO_FIFO - 1);
    first = AUDIO_FIFO - at;
    if (first > n)
        first = n;
    CopyMem((APTR)src, audio_fifo + at, first);
    if (n > first)
        CopyMem((APTR)(src + first), audio_fifo, n - first);
    fifo_w += n;
}

static ULONG half_tag(const UBYTE *src, ULONG n)
{
    ULONG t = n, at[3], k;

    at[0] = 0;
    at[1] = n / 2 & ~3UL;
    at[2] = n >= 4 ? (n - 4) & ~3UL : 0;
    for (k = 0; k < 3; k++) {
        ULONG i = at[k];

        if (i + 4 <= n)
            t = t * 33u + ((ULONG)src[i] << 24 | (ULONG)src[i + 1] << 16 |
                           (ULONG)src[i + 2] << 8 | src[i + 3]);
    }
    return t;
}

/* The half the DAC just finished, queued once. Looping it sounds like a buzz,
 * not like the tone those samples belong to. */
static void remember_half(const UBYTE *src, ULONG n)
{
    if (n > GRAIN_MAX)
        n = GRAIN_MAX;
    n = copy_mix(grain, GRAIN_MAX, src, n);
    grain_n = n;
    grain_pos = 0;
    if (n >= 4)
        fifo_write(grain, n);
    audio_seq++;
}

LONG apollo_audio(UBYTE *dst, LONG cap, ULONG *rate, ULONG ms)
{
    const UBYTE *src;
    ULONG n, index, hold;

    (void)ms;
    *rate = 0;
    if (cap < 4)
        return 0;
    src = arne_mix(rate, &n, &index, &hold);
    if (!src || n < 4)
        return 0;
    if (hold == seen_hold && index == seen_index) {
        *rate = 0;
        return 0;
    }
    seen_hold = hold;
    seen_index = index;
    if (n > (ULONG)cap)
        n = (ULONG)cap & ~3UL;
    return (LONG)copy_mix(dst, n, src, n);
}

BOOL apollo_audio_claim(void)
{
    struct Task *me = FindTask(NULL);

    Forbid();
    if (audio_owner && audio_owner != me) {
        Permit();
        return FALSE;
    }
    audio_owner = me;
    fifo_r = fifo_w = 0;
    stream_hold = 0;
    stream_index = 0xffffffff;
    stream_tag = 0;
    audio_seq = 0;
    audio_bytes = 0;
    grain_n = grain_pos = 0;
    Permit();
    return TRUE;
}

void apollo_audio_release(void)
{
    Forbid();
    if (audio_owner == FindTask(NULL))
        audio_owner = NULL;
    stream_hold = 0;
    stream_index = 0xffffffff;
    stream_tag = 0;
    audio_seq = 0;
    audio_bytes = 0;
    grain_n = grain_pos = 0;
    fifo_r = fifo_w = 0;
    Permit();
}

/* The channel runs at priority 0. Dropping under the screen reader (priority
 * -1) and coming straight back lets that task run a slice. A loop that stayed
 * ready would leave the picture frozen. */
void apollo_audio_breathe(void)
{
    struct Task *me = FindTask(NULL);

    SetTaskPri(me, -1);
    SetTaskPri(me, 0);
}

void apollo_audio_nudge(void)
{
    struct Task *me = FindTask(NULL);

    SetTaskPri(me, me->tc_Node.ln_Pri);
}

/* The two mix buffers live in chip memory and are swapped about every 10 ms.
 * Copy the finished one out in one move before hashing or byte-swapping it.
 * A slow walk of chip memory was still on the buffer when the driver reused it. */
static UBYTE raw_half[GRAIN_MAX];

static void take_half(const UBYTE *src, ULONG n, ULONG index, ULONG *rate, ULONG freq)
{
    ULONG tag;

    if (n > GRAIN_MAX)
        n = GRAIN_MAX;
    n &= ~3UL;
    if (n < 4)
        return;
    CopyMem((APTR)src, raw_half, n);
    tag = half_tag(raw_half, n);
    if (index == stream_index && tag == stream_tag)
        return;
    stream_index = index;
    stream_tag = tag;
    if (freq && rate)
        *rate = freq;
    remember_half(raw_half, n);
}

/* One DMA half. off is 0x18 or 0x28. Three samples say whether it was rewritten.
 * The whole chip buffer is copied only then. */
static BOOL slot_fresh(ULONG hold, ULONG off, ULONG slot_id, ULONG *rate,
                       ULONG *sa, ULONG *sb, ULONG *sc, UBYTE *known)
{
    ULONG p, e, n, a, b, c, mid;

    p = peek((UBYTE *)hold + off);
    e = peek((UBYTE *)hold + off + 4);
    if (e <= p)
        return FALSE;
    n = (e - p) & ~3UL;
    if (n < 64 || n > GRAIN_MAX)
        return FALSE;
    if (!*known) {
        if (!ram(p) || !ram(p + n - 1))
            return FALSE;
        *known = 1;
    }
    mid = (n / 2) & ~3UL;
    a = peek((UBYTE *)p);
    b = peek((UBYTE *)p + mid);
    c = peek((UBYTE *)p + (n - 4));
    if (*known && a == *sa && b == *sb && c == *sc)
        return FALSE;
    *sa = a;
    *sb = b;
    *sc = c;
    take_half((const UBYTE *)p, n, slot_id, rate, 0);
    return TRUE;
}

/* The driver keeps a 32-bit stereo mix, about 20 ms, at private+0xcc.
 * The high half of each long is the sample. The two chip halves are only
 * the piece the DAC is playing, which is why those copies came out short.
 * Returns TRUE when that mix is the source, whether or not it changed. */
static UBYTE snap[8192];
static ULONG mix_ptr[48];
static UWORD mix_ptr_n;

/* Words spread through one pass. The audible half only: the low half is a
 * fraction and does not settle. */
#define SENT 32

static void read_sent(ULONG *s, const UBYTE *buf, ULONG frames)
{
    ULONG step, i, n;

    step = frames / (SENT - 1);
    if (step < 1)
        step = 1;
    for (i = 0; i < SENT - 1; i++) {
        n = i * step;
        if (n >= frames)
            n = frames - 1;
        s[i] = peek(buf + n * 8);
    }
    s[SENT - 1] = peek(buf + (frames - 1) * 8);
}

static BOOL sent_same(const ULONG *a, const ULONG *b)
{
    ULONG i;

    for (i = 0; i < SENT; i++) {
        if ((a[i] ^ b[i]) & 0xffff0000UL)
            return FALSE;
    }
    return TRUE;
}

static BOOL watch_wide(ULONG *rate, ULONG checks)
{
    static ULONG sp, ssize, saved[SENT], cand[SENT], last, sctrl, shold;
    static UBYTE armed, have;
    ULONG ctrl, ptr, size, frames, i, before[SENT], got[SENT];
    UBYTE *d;

    if (!stream_hold)
        return FALSE;
    if (stream_hold != shold) {
        armed = 0;
        have = 0;
        shold = stream_hold;
    }
    ctrl = peek((UBYTE *)stream_hold + PRIV_BACK);
    ptr = peek((UBYTE *)stream_hold + 0xCC);
    if (ctrl != sctrl) {
        if (!ram(ctrl))
            return FALSE;
        sctrl = ctrl;
    }
    /* BuffSamples is one finished pass. BuffSize is that pass plus a few
     * frames still sitting in the allocation. Those extra frames are not the
     * next sample of the sine. */
    frames = peek((UBYTE *)ctrl + 0x34);
    size = peek((UBYTE *)ctrl + 0x40);
    if (size < 256 || size > 8192 || (size & 7))
        return FALSE;
    if (frames < 16 || frames * 8 > size)
        frames = size / 8;
    if (frames * 4 > GRAIN_MAX)
        frames = GRAIN_MAX / 4;
    if (!armed || ptr != sp || size != ssize) {
        if (!ram(ptr) || !ram(ptr + size - 1))
            return FALSE;
        sp = ptr;
        ssize = size;
    }
    /* A pass is kept once it reads the same twice. Two reads in this call
     * always agree, including halfway through a rewrite, and copying every
     * one of those pinned the machine on the test tone. The previous look
     * is the other one, and the caller waits a frame between passes. */
    read_sent(before, (const UBYTE *)ptr, frames);
    if (!have || !sent_same(before, cand)) {
        for (i = 0; i < SENT; i++)
            cand[i] = before[i];
        have = 1;
        mix_stir = 1;
        return TRUE;
    }
    if (armed && sent_same(before, saved))
        return TRUE;
    CopyMem((APTR)ptr, snap, frames * 8);
    read_sent(got, snap, frames);
    if (!sent_same(before, got)) {
        have = 0;
        mix_stir = 1;
        return TRUE;
    }
    for (i = 0; i < SENT; i++)
        saved[i] = got[i];
    armed = 1;
    have = 1;
    last = checks;
    if (mix_ptr_n < 48)
        mix_ptr[mix_ptr_n++] = ptr;
    d = grain;
    for (i = 0; i < frames; i++) {
        ULONG L = peek(snap + i * 8);
        ULONG R = peek(snap + i * 8 + 4);

        s16le(d, (LONG)(SHORT)(L >> 16));
        s16le(d + 2, (LONG)(SHORT)(R >> 16));
        d += 4;
    }
    grain_n = frames * 4;
    (void)rate;
    grain_pos = 0;
    if (grain_n >= 4) {
        fifo_write(grain, grain_n);
        audio_bytes += grain_n;
    }
    audio_seq++;
    return TRUE;
}

/* Kept for the public audio header. No diagnostic file is written here. */
void apollo_mix_ptrs(void)
{
    mix_ptr_n = 0;
}

/* TRUE while a mix is open. A finished half is copied once; the same samples
 * are left alone, so an idle driver does not drone. A mix pass fills both
 * chip halves before the index moves, so watching only the selected one
 * kept the other. The wider mix is preferred when it is there. */
BOOL apollo_audio_pump(ULONG *rate)
{
    const UBYTE *src;
    static ULONG checks, a0, b0, c0, a1, b1, c1;
    static UBYTE k0, k1;
    ULONG n = 0, index = 0, hold = 0, freq = 0;
    BOOL first, second;

    if (!stream_hold) {
        src = arne_mix(&freq, &n, &index, &hold);
        if (!src || n < 4)
            return FALSE;
        stream_hold = hold;
        k0 = k1 = 0;
        if (freq && rate)
            *rate = freq;
        (void)src;
        (void)n;
        (void)index;
        return TRUE;
    }
    if ((++checks & 1023) == 0 && !hold_live(stream_hold)) {
        stream_hold = 0;
        stream_index = 0xffffffff;
        k0 = k1 = 0;
        return FALSE;
    }
    if (watch_wide(rate, checks))
        return TRUE;
    index = peek((UBYTE *)stream_hold + PRIV_INDEX);
    if (index != 0 && index != 16)
        return TRUE;
    /* Index 0 fills the low half, so the high half was handed to the DAC
     * first. Index 16 is the other way round. */
    if (index == 0) {
        first = slot_fresh(stream_hold, 0x28, 16, rate, &a1, &b1, &c1, &k1);
        second = slot_fresh(stream_hold, 0x18, 0, rate, &a0, &b0, &c0, &k0);
    } else {
        first = slot_fresh(stream_hold, 0x18, 0, rate, &a0, &b0, &c0, &k0);
        second = slot_fresh(stream_hold, 0x28, 16, rate, &a1, &b1, &c1, &k1);
    }
    (void)first;
    (void)second;
    return TRUE;
}

UBYTE apollo_mix_stir(void)
{
    UBYTE s = mix_stir;

    mix_stir = 0;
    return s;
}

ULONG apollo_audio_seq(void)
{
    return audio_seq;
}

ULONG apollo_audio_bytes(void)
{
    return audio_bytes;
}

ULONG apollo_audio_span(void)
{
    return grain_n;
}

/* Append n bytes of the newest half. A caller passes one half to cover a
 * single flip the poll missed. */
void apollo_audio_sustain(ULONG n)
{
    n &= ~3UL;
    if (grain_n < 4 || n < 4)
        return;
    while (n) {
        ULONG k = grain_n - grain_pos;

        if (k > n)
            k = n;
        fifo_write(grain + grain_pos, k);
        grain_pos += k;
        if (grain_pos >= grain_n)
            grain_pos = 0;
        n -= k;
    }
}

ULONG apollo_audio_pending(void)
{
    return fifo_w - fifo_r;
}

LONG apollo_audio_take(UBYTE *dst, LONG cap)
{
    ULONG n, at, first;

    n = fifo_w - fifo_r;
    if (n < 4 || cap < 4)
        return 0;
    if (n > (ULONG)cap)
        n = (ULONG)cap;
    n &= ~3UL;
    if (n < 4)
        return 0;
    at = fifo_r & (AUDIO_FIFO - 1);
    first = AUDIO_FIFO - at;
    if (first > n)
        first = n;
    CopyMem(audio_fifo + at, dst, first);
    if (n > first)
        CopyMem(audio_fifo, dst + first, n - first);
    fifo_r += n;
    return (LONG)n;
}
