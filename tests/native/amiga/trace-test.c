/* A test program for tracepoints: calls tick() for a while and says where tick() is.
 * Needs no NDK: vc +aos68k -cpu=68000 -O1 -o trace-test trace-test.c */
#include <stdio.h>
#include <stdlib.h>


long ticks;

long tick(long n)
{
    ticks += n;
    return ticks;
}

int main(int argc, char **argv)
{
    long i, loops = argc > 1 ? atol(argv[1]) : 100000;  /* more on a fast CPU */
    volatile long k;

    printf("tick at %p\n", (void *)tick);
    fflush(stdout);
    for (i = 1; i <= 60; i++) {
        tick(i);
        for (k = 0; k < loops; k++)  /* a while on a 68000; no clock() needed */
            ;
    }
    printf("ticks %ld\n", ticks);
    return 0;
}
