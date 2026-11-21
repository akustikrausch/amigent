/* ARNE mix, shared by the Apollo daemon. Opcode 49, PIP, is not declared here. */
#ifndef AMIGENT_APOLLO_AUDIO_H
#define AMIGENT_APOLLO_AUDIO_H

LONG apollo_audio(UBYTE *dst, LONG cap, ULONG *rate, ULONG ms);
BOOL apollo_audio_claim(void);
void apollo_audio_release(void);
void apollo_audio_breathe(void);
void apollo_audio_nudge(void);
void apollo_mix_ptrs(void);
BOOL apollo_audio_pump(ULONG *rate);
UBYTE apollo_mix_stir(void);
ULONG apollo_audio_seq(void);
ULONG apollo_audio_bytes(void);
ULONG apollo_audio_span(void);
void apollo_audio_sustain(ULONG n);
ULONG apollo_audio_pending(void);
LONG apollo_audio_take(UBYTE *dst, LONG cap);

#endif
