/* pointer-test MODEID W H TOP SECS: opens a screen in that mode, TOP pixels down,
 * and writes the pointer position Intuition reports on it to RAM:pointer-test.pos. */
#include <exec/types.h>
#include <intuition/screens.h>
#include <graphics/modeid.h>
#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/intuition.h>
#include <stdio.h>
#include <stdlib.h>

struct IntuitionBase *IntuitionBase;

int main(int argc, char **argv)
{
    struct Screen *scr;
    ULONG id;
    LONG w, h, top, secs, i;
    FILE *f;

    if (argc < 6) { printf("pointer-test MODEID W H TOP SECS\n"); return 10; }
    id = strtoul(argv[1], NULL, 16);
    w = atol(argv[2]); h = atol(argv[3]); top = atol(argv[4]); secs = atol(argv[5]);
    IntuitionBase = (struct IntuitionBase *)OpenLibrary("intuition.library", 39);
    if (!IntuitionBase) return 20;
    scr = OpenScreenTags(NULL, SA_DisplayID, id, SA_Width, w, SA_Height, h, SA_Depth, 2,
                         SA_Top, top, SA_Title, (ULONG)"pointer-test", SA_ShowTitle, TRUE,
                         SA_Draggable, TRUE, TAG_END);
    if (!scr) { printf("no screen for %08lx\n", id); CloseLibrary((struct Library *)IntuitionBase); return 10; }
    printf("screen %ldx%ld at top %ld\n", (LONG)scr->Width, (LONG)scr->Height, (LONG)scr->TopEdge);
    for (i = 0; i < secs * 10; i++) {
        if (SetSignal(0, SIGBREAKF_CTRL_C) & SIGBREAKF_CTRL_C) break;
        if ((f = fopen("RAM:pointer-test.pos", "w"))) {
            fprintf(f, "%ld %ld %ld\n", (LONG)scr->MouseX, (LONG)scr->MouseY, (LONG)scr->TopEdge);
            fclose(f);
        }
        Delay(5);
    }
    CloseScreen(scr);
    CloseLibrary((struct Library *)IntuitionBase);
    return 0;
}
