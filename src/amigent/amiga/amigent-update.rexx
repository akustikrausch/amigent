/* amigent-update.rexx [HOME]: swap the running amigentd for C:amigentd.new.
   HOME is the folder with the copy for amigent-repair, by default the one in
   ENVARC:amigent-home, else SYS:amigent.
   Started by Amigent-Update on the Amiga and by amigent-setup --update on the
   PC. The arguments of amigentd's line in S:User-Startup (TLSONLY and the
   like) stay. Should the new amigentd not come up, the old one starts again.
   The outcome goes to T:amigent-update.done: new, old, busy, reboot or missing. */
parse arg home
token = ''
candidate = 'C:amigentd.new'
saved = 'C:amigentd.old'
record = 'T:amigent-update'
if word(home, 1) == 'VERIFY' then do
  parse var home . token home
  if length(token) ~= 32 | verify(token, '0123456789abcdef') ~= 0 then exit 20
  prefix = 'ag-'left(token, 16)
  record = 'T:'prefix
  candidate = 'C:'prefix'.new'
  saved = 'C:'prefix'.old'
end
home = strip(home)
if home = '' & open('h', 'ENVARC:amigent-home', 'R') then do  /* left there by amigent-setup */
  home = strip(readln('h'))
  call close('h')
end
if home = '' then home = 'SYS:amigent'
if ~exists(candidate) then call finish 'missing', 'C:amigentd.new is missing, so nothing was changed.'
args = ''
if open('f', 'S:User-Startup', 'R') then do
  do until eof('f')
    l = strip(readln('f'))
    p = pos('C:AMIGENTD', upper(l))
    if p > 0 & left(l, 1) ~= ';' & upper(word(l, 1)) ~= 'IF' then args = substr(l, p + 10)
  end
  call close('f')
end
line = 'Run <NIL: >NIL: C:amigentd' args
call pragma('D', 'SYS:')  /* amigentd inherits it: no lock on the drawer this came from */
address command 'Wait 2'  /* lets the PC that started this hang up first */
if ~show('P', 'AMIGENT') then do
  /* older than the ARexx port: it cannot be stopped from here */
  call swap
  call finish 'reboot', 'This amigentd is too old to be stopped from here. The new one is in',
    'C:amigentd now and starts with the next boot: restart the Amiga.'
end
say 'Stopping the running amigentd...'
address AMIGENT 'QUIT'
do 15 while show('P', 'AMIGENT'); address command 'Wait 1'; end
if show('P', 'AMIGENT') then call finish 'busy', 'The running amigentd did not stop, so nothing was changed.'
call swap
say 'Starting the new amigentd...'
address command line
/* the old one's port can stay taken for a minute; the new one tries for 90 s */
do 100 until show('P', 'AMIGENT'); address command 'Wait 1'; end
if ~show('P', 'AMIGENT') then signal rollback
if token ~= '' then do
  call report 'verify'
  do 60 until exists(record'.ack'); address command 'Wait 1'; end
  ok = 0
  if open('ack', record'.ack', 'R') then do
    ok = readch('ack', 32) == token
    call close('ack')
    address command 'Delete >NIL:' record'.ack QUIET'
  end
  if ~ok then do
    address AMIGENT 'QUIT'
    do 15 while show('P', 'AMIGENT'); address command 'Wait 1'; end
    if show('P', 'AMIGENT') then call finish 'busy', 'Recovery copy retained in' saved
    signal rollback
  end
end
if exists(home'/amigentd') then address command 'Copy C:amigentd' home'/ QUIET'
address command 'Delete >NIL:' saved 'QUIET'
options results
address AMIGENT 'VERSION'
call finish 'new', 'Done:' result 'is running.'

rollback:
  address command 'Copy' saved 'C:amigentd QUIET'
  if rc ~= 0 then call finish 'restore', 'Restore the recovery copy from' saved
  address command line
  call finish 'old', 'The previous executable has been restored.'

swap:
  if exists('C:amigentd') then do
    address command 'Copy C:amigentd' saved 'QUIET'
    if rc ~= 0 then do
      if ~show('P', 'AMIGENT') then address command line
      call finish 'backup', 'Could not save the previous executable.'
    end
  end
  address command 'Copy' candidate 'C:amigentd QUIET'
  if rc ~= 0 then signal rollback
  address command 'Protect C:amigentd rwed'
  address command 'Delete >NIL:' candidate 'QUIET'
  return

finish:
  parse arg outcome, text
  say text
  call report outcome
  exit

report:
  parse arg outcome
  if open('d', record'.done', 'W') then do
    call writeln('d', outcome)
    call close('d')
  end
  return
