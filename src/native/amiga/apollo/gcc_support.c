/* cybergraphics.library for the Apollo gcc build. vbcc uses its own inlines.
 * Offsets match the vbcc stubs and the DevPac LVOs: GetCyberMapAttr -96,
 * ReadPixelArray -120, LockBitMapTagList -168, UnLockBitMap -174. */
#include <exec/types.h>
#include <graphics/gfx.h>
#include <graphics/rastport.h>
#include <utility/tagitem.h>
#include <proto/exec.h>
#include <inline/macros.h>

extern struct Library *CyberGfxBase;

ULONG GetCyberMapAttr(struct BitMap *bm, ULONG attr)
{
    return LP2(0x60, ULONG, GetCyberMapAttr, struct BitMap *, bm, a0, ULONG, attr, d0,
               , CyberGfxBase);
}

void ReadPixelArray(APTR dest, ULONG destx, ULONG desty, ULONG destmod, struct RastPort *rp,
                    ULONG srcx, ULONG srcy, ULONG sizex, ULONG sizey, ULONG format)
{
    LP10NR(0x78, ReadPixelArray, APTR, dest, a0, ULONG, destx, d0, ULONG, desty, d1,
           ULONG, destmod, d2, struct RastPort *, rp, a1, ULONG, srcx, d3, ULONG, srcy, d4,
           ULONG, sizex, d5, ULONG, sizey, d6, ULONG, format, d7, , CyberGfxBase);
}

/* A0 bitmap, A1 tag list, result in D0. CyberGfxBase stays in A6. */
APTR LockBitMapTagList(APTR bitmap, struct TagItem *tags)
{
    return LP2(168, APTR, LockBitMapTagList, APTR, bitmap, a0, struct TagItem *, tags, a1,
               , CyberGfxBase);
}

/* A0 handle, no result. The caller in zz_fit() unlocks the same handle. */
void UnLockBitMap(APTR handle)
{
    LP1NR(174, UnLockBitMap, APTR, handle, a0, , CyberGfxBase);
}
