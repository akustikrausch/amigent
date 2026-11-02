/* Inserts an entry into the AmiStart menu file, sorted by name.
   Args: SECTION|Name|Program[|Stack]. AmiStart shows it after its next start.
   The file is handled as one string because READLN splits lines over 1000 chars. */
parse arg section '|' name '|' file '|' stack
if stack = '' then stack = 32768
prefs = 'SYS:Utilities/Expansion/AmiStart/sm.prefs'
temp = 'T:sm.prefs.new'
nl = '0a'x
item = 'ITEM ICON="'file'" NAME="'name'" METHOD="0" CMDMODE="0" RGB="0x0" TEXTRGB="0x0" FILE="'file'" EXECMODE="0" STACK="'stack'"'

if ~open('in', prefs, 'R') then exit 10
data = readch('in', 65535)
call close('in')
if length(data) > 65000 then exit 10

start = index(data, nl'NEWDIR NAME="'section'"')
if start = 0 then exit 10
stop = index(data, nl'ENDDIR', start)
if stop = 0 then exit 10

insert = stop + 1
p = index(data, nl, start + 1) + 1
do while p < stop
  e = index(data, nl, p)
  line = substr(data, p, e - p)
  if left(line, 5) == 'ITEM ' & index(line, '" GROUP ') = 0 then do
    parse var line 'NAME="' current '"'
    if upper(current) > upper(name) then do
      insert = p
      leave
    end
  end
  p = e + 1
end

if ~open('out', temp, 'W') then exit 10
call writech('out', left(data, insert - 1) || item || nl || substr(data, insert))
call close('out')

do try = 1 to 10
  address command 'Copy' temp prefs 'QUIET'
  if rc = 0 then exit 0
  address command 'Wait 1'
end
exit 10
