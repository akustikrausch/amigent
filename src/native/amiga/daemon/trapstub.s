; The task trap handler for tracepoints, see "trace" in amigentd.c. amigentd
; copies these bytes into memory of their own, one copy per traced task, and
; sets the copy as that task's tc_TrapCode. Assembled with:
; vasmm68k_mot -Fbin -m68020 -o trapstub.bin trapstub.s
;
; Exec calls tc_TrapCode in supervisor mode with the trap number on the stack
; above the exception frame. TRAP #15 (vector 47) goes to the hook in slots,
; with a pointer to the saved registers (d0-d7, a0-a6, then the trap number
; and the frame: SR, PC) and the user stack pointer. When the hook returns
; non-zero it has handled the trap and moved the PC back; anything else goes
; on to the task's earlier trap code.

start:
	cmp.l	#47,(sp)
	bne.s	chain
	movem.l	d0-d7/a0-a6,-(sp)
	move.l	usp,a0
	move.l	a0,-(sp)
	pea	4(sp)
	move.l	slots(pc),d7
	beq.s	nothing
	move.l	d7,a4
	jsr	(a4)
	tst.l	d0
	beq.s	nothing
	addq.l	#8,sp
	movem.l	(sp)+,d0-d7/a0-a6
	addq.l	#4,sp
	rte
nothing:
	addq.l	#8,sp
	movem.l	(sp)+,d0-d7/a0-a6
chain:
	move.l	slots+4(pc),-(sp)
	rts
	cnop	0,4
slots:
	dc.l	0			; hook
	dc.l	0			; the task's earlier tc_TrapCode
	dc.l	$414d5452		; 'AMTR'
