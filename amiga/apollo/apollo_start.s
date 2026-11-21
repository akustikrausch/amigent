| Entry for amigentd.apollo. The shell calls a program with the command
| line in a0 and its length in d0. a2 is preserved by the C calling
| convention, so the shell's stack pointer survives the call. No libnix
| crt, so nothing opens "*". Assembled by gcc so this is the first code.

	.text
	.globl _start
	.extern _apollo_enter
	.extern _SysBase

_start:
	move.l %sp,%a2
	move.l 4.w,%a6
	move.l %a6,_SysBase
	move.l %d0,-(%sp)
	move.l %a0,-(%sp)
	jsr _apollo_enter
	move.l %a2,%sp
	rts
