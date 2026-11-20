static void clear_pend(void)
{
    LONG i;
    for (i = 0; i < PENDING; i++) {
        pend[i].fd = -1;
        pend[i].screen = FALSE;
    }
}

static void report(void)
{
    printf("drop=%d free=%d zz=%d reset=%d cur=%d frame=%d client=%ld mirror=%d\n",
           dropped, freed, zz_freed, zz.reset ? 1 : 0, zz.cur ? 1 : 0,
           frame_z ? 1 : 0, (long)client, mirror_held);
}

int main(int argc, char **argv)
{
    if (argc != 2)
        return 2;
    client = 4;
    frame_z = (void *)1;
    zz.cur = (void *)1;
    mirror_held = 1;
    clear_pend();
    if (!strcmp(argv[1], "alone"))
        lose_client();
    else if (!strcmp(argv[1], "waiting")) {
        pend[1].fd = 7;
        pend[1].screen = TRUE;
        lose_client();
    } else if (!strcmp(argv[1], "complete")) {
        pend[0].fd = 8;
        pend[0].screen = TRUE;
        command_ok = send_ok = 1;
        handover();
    } else if (!strcmp(argv[1], "fail")) {
        pend[0].fd = 8;
        pend[0].screen = TRUE;
        command_ok = 0;
        handover();
    } else if (!strcmp(argv[1], "two")) {
        pend[0].fd = 8;
        pend[0].screen = TRUE;
        pend[1].fd = 9;
        pend[1].screen = TRUE;
        command_ok = 0;
        handover();
        printf("midfree=%d ", freed);
        handover();
    } else if (!strcmp(argv[1], "zzgiveup")) {
        zz.fails = ZZ_GIVE_UP;
        lose_client();
    } else
        return 2;
    report();
    return 0;
}
