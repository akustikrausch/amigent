/* Just enough of the Amiga for amigentd's deflate and inflate to build on a PC. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
typedef int32_t LONG;
typedef uint32_t ULONG;
typedef int16_t WORD;
typedef uint16_t UWORD;
typedef uint8_t UBYTE;
typedef int BOOL;
typedef void *APTR;
typedef long BPTR;
#define TRUE 1
#define FALSE 0
#define MEMF_ANY 0
#define MEMF_CLEAR 1
#define CHUNK 32768
#define IO_SECONDS 30
#define ERROR_DISK_FULL 221
#define ERROR_NO_FREE_STORE 103
static void *AllocVec(ULONG n, ULONG f) { return f & MEMF_CLEAR ? calloc(1, n) : malloc(n); }
static void FreeVec(void *p) { free(p); }
static void CopyMem(const void *s, void *d, ULONG n) { memmove(d, s, n); }
static void Forbid(void) {}
static void Permit(void) {}
static LONG IoErr(void) { return 0; }
/* the "file" is a growing buffer */
static UBYTE *sink; static size_t sink_len, sink_size;
static LONG Write(BPTR fh, const void *p, LONG n) {
    (void)fh;
    if (sink_len + n > sink_size) { sink_size = (sink_len + n) * 2; sink = realloc(sink, sink_size); }
    memcpy(sink + sink_len, p, n); sink_len += n; return n;
}
/* the "link" reads from a buffer */
struct link { const UBYTE *data; ULONG len, pos; };
static BOOL l_recv(struct link *l, void *d, ULONG n, LONG s) {
    (void)s; if (l->pos + n > l->len) return FALSE; memcpy(d, l->data + l->pos, n); l->pos += n; return TRUE;
}
static BOOL l_skip(struct link *l, ULONG n, LONG s) {
    (void)s; if (l->pos + n > l->len) return FALSE; l->pos += n; return TRUE;
}
