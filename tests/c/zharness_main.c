/* zharness pack LEVEL < raw > deflate     zharness unpack < deflate > raw
 * pack writes one raw deflate stream; blocks of up to 32 KB, as amigentd sends them. */
int main(int argc, char **argv)
{
    static UBYTE in[64 << 20];
    size_t n = fread(in, 1, sizeof(in), stdin), at = 0;
    if (argc > 2 && !strcmp(argv[1], "pack")) {
        struct zpack *z = z_new(atoi(argv[2]), CHUNK + CHUNK / 8 + 1024);
        if (!z) return 2;
        z_start(z);
        do {
            size_t k = n - at > CHUNK ? CHUNK : n - at;
            z_block(z, in + at, k, at + k == n);
            fwrite(z->out, 1, z->olen, stdout);
            z->olen = 0;
            at += k;
        } while (at < n);
        z_free(z);
        return 0;
    }
    if (argc > 1 && !strcmp(argv[1], "tile")) {  /* one whole stream at once, as for a tile */
        struct zpack *z = z_new(atoi(argv[2]), n + n / 8 + 256);
        LONG k = z_pack(z, in, n);
        fwrite(z->out, 1, k, stdout);
        return 0;
    }
    if (argc > 1 && !strcmp(argv[1], "unpack")) {
        struct link l = {in, n, 0};
        struct zin *z = zi_new();
        BOOL alive;
        z->l = &l;
        z->left = n;
        z->fh = 1;
        alive = zi_run(z);
        if (!alive || z->bad) return 3;
        fwrite(sink, 1, sink_len, stdout);
        return 0;
    }
    return 1;
}
