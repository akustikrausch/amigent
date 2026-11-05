/* amigent-update.rexx [HOME]: swap the running amigentd for C:amigentd.new.
   HOME is the folder with the copy for amigent-repair, by default the one in
   ENVARC:amigent-home, else SYS:amigent.
   Started by Amigent-Update on the Amiga and by amigent-setup --update on the
   PC. The arguments of amigentd's line in S:User-Startup (TLSONLY and the
   like) stay. Should the new amigentd not come up, the old one starts again.
   The outcome goes to T:amigent-update.done: new, old, busy, reboot or missing. */
parse arg home
home = strip(home)
if home = '' & open('h', 'ENVARC:amigent-home', 'R') then do  /* left there by amigent-setup */
  home = strip(readln('h'))
  call close('h')
end
if home = '' then home = 'SYS:amigent'
if ~exists('C:amigentd.new') then call finish 'missing', 'C:amigentd.new is missing, so nothing was changed.'
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
do 15 until show('P', 'AMIGENT'); address command 'Wait 1'; end
if ~show('P', 'AMIGENT') then do
  address command 'Copy C:amigentd.old C:amigentd QUIET'
  address command line
  call finish 'old', 'The new amigentd did not start, so the old one runs again.'
end
if exists(home'/amigentd') then address command 'Copy C:amigentd' home'/ QUIET'
address command 'Delete >NIL: C:amigentd.old QUIET'
options results
address AMIGENT 'VERSION'
call finish 'new', 'Done:' result 'is running.'

swap:
  address command 'Copy C:amigentd C:amigentd.old QUIET'
  address command 'Copy C:amigentd.new C:amigentd QUIET'
  address command 'Protect C:amigentd rwed'
  address command 'Delete >NIL: C:amigentd.new QUIET'
  return

finish:
  parse arg outcome, text
  say text
  if open('d', 'T:amigent-update.done', 'W') then do
    call writeln('d', outcome)
    call close('d')
  end
  exit
