/* Host types for the screen-handover slice of amiga/amigentd.c.
 * Nothing here resets an Amiga or talks to a ZZ9000. */
#include <stdio.h>
#include <string.h>
#include <stdint.h>
typedef int32_t LONG;
typedef uint32_t ULONG;
typedef uint8_t UBYTE;
typedef int BOOL;
#define TRUE 1
#define FALSE 0
#define PENDING 4
#define ZZ_GIVE_UP 3

static struct newcomer {
    LONG fd, have;
    UBYTE cmd[10];
    BOOL authed, screen;
    ULONG since, addr;
} pend[PENDING];

static LONG client = -1;
static ULONG client_addr, client_since, client_id, conn_serial, heard;
static BOOL authed;
static int picture;
static ULONG owner, powner, cconn, pconn;
static void *frame_z;
static struct {
    UBYTE fails;
    BOOL reset;
    void *cur;
} zz;

static int dropped, freed, zz_freed, mirror_held;

static void ObtainSemaphore(int *s) { (void)s; }
static void ReleaseSemaphore(int *s) { (void)s; }
static void free_frame(void) { freed++; }
static void free_planar(void) {}
static void z_free(void *p) { (void)p; }
static void zz_free(void) { zz_freed++; mirror_held = 0; }
static void drop_client(void)
{
    dropped++;
    if (client >= 0)
        client = -1;
}
static ULONG now_ms(void) { return 1; }
static void mon_say(int kind, const char *what, const char *arg)
{
    (void)kind; (void)what; (void)arg;
}
static int command_ok = 1, send_ok = 1;
static BOOL command(UBYTE *cmd) { (void)cmd; return command_ok; }
static BOOL send_acks(void) { return send_ok; }
