#!/usr/bin/env python3
from demoterm import __version__
import curses
import fcntl
import os
import pty
import re
import select
import shutil
import struct
import sys
import termios
import time
import tty

HISTORY_MARGIN = 2.5
MULTICHAR_KEYCODES = {
        "\x1bOA": "[\u2191]",   # up (vim variant)
        "\x1bOB": "[\u2193]",   # down (vim variant)
        "\x1bOC": "[\u2192]",   # right (vim variant)
        "\x1bOD": "[\u2190]",   # left (vim variant)
        "\x1b[A": "[\u2191]",   # up
        "\x1b[B": "[\u2193]",   # down
        "\x1b[C": "[\u2192]",   # right
        "\x1b[D": "[\u2190]",   # left
        "\x1b[3~": "[\u2717]",  # suppr
        "\x1b[5~": "[\u21DE]",  # page up
        "\x1b[6~": "[\u21DF]",  # page down
}
SINGLECHAR_KEYCODES = {
        "\x7f": "[\u232B]",     # backspace
        "\t": "[\u21E5]",       # tab
        "\r": "[\u2936]",       # enter
        " ": "\u2423",          # space
}

# Esc key is special, it corresponds to a single "\x1b" byte,
# and since it is the startup char of the escape sequences,
# we will have to process it specifically (i.e., only recognize
# an esc key when it is detected as a single input char).
ESC_KEY = "[\u241B]"

RE_ANSI_ESCAPE = re.compile(
    r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\a]*\a|P[^\\]*\\)')
# Detect \e[{row};{col}H and \e[{row}H
RE_ESC_SET_CURSOR_POS = re.compile(rb"\x1b\[(\d+)(;\d+)?H")

#debug = open("/tmp/debug", 'a', 1)

def get_replace_filter_function(replacements):
    pattern = re.compile("|".join(
                re.escape(k) for k in replacements.keys()))
    return (lambda data:
            pattern.sub(lambda m: replacements[m.group(0)], data))

def get_input_filter_functions():
    # add ctrl-a to ctrl-z single char keycodes
    singlechar_keycodes = SINGLECHAR_KEYCODES.copy()
    for i in range(26):
        ch = chr(i+1)
        letter = chr(ord('A')+i)
        if ch not in singlechar_keycodes:
            singlechar_keycodes[ch] = f"[^{letter}]"
    # return filter functions
    return (
        get_replace_filter_function(MULTICHAR_KEYCODES),
        (lambda data: RE_ANSI_ESCAPE.sub('', data)),
        get_replace_filter_function(singlechar_keycodes),
    )

def apply_filter_functions(filter_functions, data):
    for f in filter_functions:
        data = f(data)
    return data

def get_input_pprint_function():
    filter_functions = get_input_filter_functions()
    return (lambda data: apply_filter_functions(filter_functions, data))

def set_tty_size(width, height):
    packed = struct.pack("HHHH", height, width, 0, 0)
    fcntl.ioctl(1, termios.TIOCSWINSZ, packed)

def write_status_zone(esc, width, height, input_line):
    # set cursor to penultimate row and write separator line
    esc.set_cursor_position(height-2, 0)
    os.write(1, ('\u2500'*width).encode())
    # set cursor to last row and update it
    esc.set_cursor_position(height-1, 0)
    esc.clear_to_eol()
    if input_line == "":
        input_line = "-- idle --"
    else:
        input_line = f"-- typing -- {input_line}"
    input_line = f"[demoterm {__version__}] {input_line}"
    input_line = input_line[-width:]
    os.write(1, input_line.encode())

def invalidate_old_history(input_history):
    if len(input_history) == 0:
        return input_history
    history_bound = time.time() - HISTORY_MARGIN
    if input_history[-1][0] < history_bound:
        return []
    else:
        return input_history

def next_invalidate_timeout(input_history):
    if len(input_history) > 0:
        return max(0.,
                   input_history[-1][0] + HISTORY_MARGIN - time.time())
    else:
        return None

class EscCodeGenerator:
    def __init__(self):
        self._esc_codes = {}
        for terminfo_code, method_name in (
                ("clear", "clear"),
                ("csr", "set_scroll_region"),
                ("sc", "save_cursor_position"),
                ("rc", "restore_cursor_position"),
                ("civis", "hide_cursor"),
                ("cnorm", "show_cursor"),
                ("el", "clear_to_eol"),
                ("cup", "set_cursor_position")):
            esc_code = curses.tigetstr(terminfo_code)
            if esc_code == "":
                sys.exit("Incompatible terminal, sorry.")
            self._esc_codes[method_name] = esc_code
    def __getattr__(self, method_name):
        esc_code = self._esc_codes[method_name]
        return lambda *args: self._write_ansi_escape(esc_code, *args)
    def _write_ansi_escape(self, esc_code, *args):
        if len(args) > 0:
            esc_code = curses.tparm(esc_code, *args)
        os.write(1, esc_code)

def select_shell():
    if len(sys.argv) > 1:
        shell = shutil.which(sys.argv[1])
        if shell is None:
            sys.exit("Sorry cannot run specified argument.")
    else:
        shell = os.environ.get("SHELL")
        if shell is None:
            shell = shutil.which("bash")
            if shell is None:
                shell = shutil.which("sh")
            if shell is None:
                sys.exit("Sorry could not find a shell to start.")
    return shell

def read_available(fd):
    data = b''
    while True:
        chunk = os.read(fd, 4096)
        if len(chunk) == 0:
            # empty read, stop
            return data
        data += chunk
        # if no more data is available, break
        rlist, _, _ = select.select([fd], [], [], 0)
        if len(rlist) == 0:
            return data

# If the child process sends escape codes for positionning the cursor
# below the scroll window, we have to replace them otherwise we will
# overwrite the two status lines.
# Without a scroll window, the effect of these codes is to scroll
# the rows upward to create the missing rows.
# We have to emulate the same thing:
# 1. position the cursor to the last row of the scroll window
# 2. send one or more "\n" to scroll and create the missing rows
# 3. possibly send one more escape code if the column parameter
#    was not 1.
def get_cursor_placement_filter(max_row):
    def replace_match(match):
        row = int(match.group(1))
        col = 1  # default for short form \e[<row>H
        if match.group(2):
            col = int(match.group(2)[1:])
        if row > max_row:
            scroll_lines = row - max_row
            return (f"\x1b[{max_row}H" +
                    "\n" * scroll_lines +
                    f"\x1b[{max_row};{col}H").encode()
        else:
            return match.group(0)  # nothing changed
    return (lambda data:
            RE_ESC_SET_CURSOR_POS.sub(replace_match, data))

def main():
    # Check which shell we should start
    shell_cmd = select_shell()

    # Save terminal settings
    old_settings = termios.tcgetattr(1)

    # Get terminal attributes and prepare esc code generator
    curses.setupterm()
    height = curses.tigetnum("lines")
    width = curses.tigetnum("cols")
    if height < 1 or width < 1:
        sys.exit("Could not retrieve terminal size, exiting.")
    esc = EscCodeGenerator()

    # Clear the screen and set cursor position to top left corner
    esc.clear()
    esc.set_cursor_position(0, 0)

    # Set a scroll region (last two terminal lines excluded)
    esc.set_scroll_region(0, height-3)

    # Set terminal in raw mode
    tty.setraw(1)

    # Start a shell in a pseudo-terminal
    pid, master_fd = pty.fork()
    if pid == 0:  # if child
        # set terminal size to that of the scroll region
        set_tty_size(width, height-2)
        # execute the shell
        os.execl(shell_cmd, shell_cmd)
        sys.exit(0)

    input_pprint = get_input_pprint_function()
    cursor_placement_filter = get_cursor_placement_filter(height-2)
    input_history = []

    try:
        while True:
            # invalidate old input history
            # (older than HISTORY_MARGIN seconds in the past)
            input_history = invalidate_old_history(input_history)

            # Filter, format and print the input line on the last
            # screen row.
            # Note: advanced terminal programs such as vim send
            # various escape sequences to the terminal and some of
            # them cause the terminal to send a response.
            # For instance a request '\x1b[>c' will cause the terminal
            # to respond '\x1b[>61;7600;1c' in my case (this request
            # allows to identify the terminal type).
            # See https://invisible-island.net/xterm/ctlseqs/ctlseqs.html
            # Since the terminal is also our stdin, this kind of response
            # will be intermixed with keyboard input when reading stdin.
            # Here we just want to display keyboard input, so we first
            # look for the key-codes we can recognize (input_pprint())
            # and replace them with human-readable text, and then
            # if we still find something looking like an escape sequence,
            # (or rather a response to an escape sequence), we use
            # RE_ANSI_ESCAPE to discard it.
            input_line = b''.join(elem[1] for elem in input_history)
            input_line = input_line.decode()
            input_line = input_pprint(input_line)

            # -- update the status line --

            # save and hide cursor
            esc.save_cursor_position()
            esc.hide_cursor()

            # write status zone
            #debug.write(f"display: {repr(input_line)}\n")
            write_status_zone(esc, width, height, input_line)

            # in case the user would have typed "reset" in the shell,
            # ensure we re-create the scroll region before waiting
            esc.set_scroll_region(0, height-3)

            # restore and show cursor
            esc.restore_cursor_position()
            esc.show_cursor()

            # -- wait for next event --
            # (or timeout if we reach HISTORY_MARGIN seconds)
            timeout = next_invalidate_timeout(input_history)
            rlist, _, _ = select.select(
                    [master_fd, 0], [], [], timeout)

            # Read shell output and print it
            if master_fd in rlist:
                data = read_available(master_fd)
                # intercept the escape sequence querying the terminal size
                if b"\x1b[18t" in data:
                    data = data.replace(b"\x1b[18t", b"")
                    response = f"\x1b[8;{height-2};{width}t".encode()
                    os.write(master_fd, response)
                    if len(data) == 0:
                        continue
                data = cursor_placement_filter(data)
                #debug.write(f"output: {repr(data)}\n")
                if not data:
                    break
                os.write(1, data)

            # Read keyboard input and save it into input_history
            # with a timestamp
            if 0 in rlist:
                data = read_available(0)
                #debug.write(f"input: {repr(data)}\n")
                # If a single escape char was received, this is the
                # ESC key. Replace it right away to avoid conflicting
                # with other Escape sequences starting with \x1b.
                if data == b'\x1b':
                    mod_data = ESC_KEY.encode()
                else:
                    mod_data = data
                input_history.append((time.time(), mod_data))
                # also send keyboard input to the child process
                #debug.write(f"send: {repr(data)}\n")
                os.write(master_fd, data)
    except OSError:
        pass
    finally:
        # Restore scroll region to full height
        esc.set_scroll_region(0, height-1)
        # Clear the screen again
        esc.clear()
        # Restore initial terminal parameters
        termios.tcsetattr(1, termios.TCSADRAIN, old_settings)

if __name__ == "__main__":
    main()
