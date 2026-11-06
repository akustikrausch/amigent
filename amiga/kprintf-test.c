/* kprintf-test: one line through exec's RawPutChar, as kprintf would; for the debug
 * check of scripts/regress.py. Build: vc +aos68k -cpu=68000 -o kprintf-test kprintf-test.c */
#include <exec/types.h>
#include <exec/execbase.h>
#include <proto/exec.h>
extern struct ExecBase *SysBase;
void rawput(__reg("a6") struct ExecBase *, __reg("d0") UBYTE c) = "\tjsr\t-516(a6)";
int main(void)
{
    const char *s = "kprintf-test: hello from kprintf\n";
    while (*s) rawput(SysBase, *s++);
    return 0;
}
