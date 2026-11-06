; The stub in front of each patched system function, see "hooks" in amigentd.c.
; amigentd copies these bytes into memory of their own, so the stub outlives
; amigentd when another program patched the same function later and the
; vector can't go back. Assembled with: vasmm68k_mot -Fbin -o hookstub.bin hookstub.s
;
; All registers are saved, the hook (if set) gets a pointer to them, d0-d7
; then a0-a6, and the original function runs with the registers as they came.

start:
	movem.l	d0-d7/a0-a6,-(sp)
	lea	data(pc),a5
	addq.l	#1,4(a5)		; busy: amigentd waits for 0 before it goes
	move.l	(a5),d7			; the hook, 0 while amigentd is away
	beq.s	skip
	move.l	d7,a4
	move.l	sp,-(sp)
	jsr	(a4)
	addq.l	#4,sp
	lea	data(pc),a5
skip:
	subq.l	#1,4(a5)
	movem.l	(sp)+,d0-d7/a0-a6
	move.l	data+8(pc),-(sp)	; on to the original, registers as they came
	rts
	cnop	0,4
data:
	dc.l	0			; hook
	dc.l	0			; busy
	dc.l	0			; original
	dc.l	$414d4854		; 'AMHT'
