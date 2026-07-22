#!/usr/bin/env python3
"""
Persistent connection manager for tbaMUD (CircleMUD-family) game servers.

Claude Code runs each Bash tool call as a separate short-lived process, so a
plain `nc` invocation can't hold a login session open between commands. This
script works around that by running the actual socket connection in a small
background daemon process. The daemon logs in once and then just relays text:
commands come in through a FIFO, game output gets appended to a log file.
Every other subcommand (`send`, `read`, `status`, `stop`) is a short-lived
process that talks to the daemon through those files.

Subcommands:
  start   - launch the daemon, log in, print the initial game output
  send    - send one command line, wait for the reply, print new output
  read    - flush any unread/unsolicited output (tells, combat, etc.)
  status  - report whether the daemon is alive and show recent output
  stop    - send `quit`, then tear down the daemon and session files
"""
import argparse
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 4000
DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / "session"

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[a-zA-Z]")

GAME_PROMPT_RE = re.compile(r"\d+H \d+M \d+V.*>\s*$")
MENU_RE = "Make your choice"
PRESS_RETURN_RE = "PRESS RETURN"
NAME_PROMPT_RE = "By what name do you wish to be known?"
PASSWORD_PROMPT_RE = "Password:"


def state_paths(state_dir: Path):
    return {
        "dir": state_dir,
        "log": state_dir / "mud.log",
        "fifo": state_dir / "mud.cmdfifo",
        "pid": state_dir / "mud.pid",
        "ready": state_dir / "mud.ready",
        "error": state_dir / "mud.error",
        "cursor": state_dir / "mud.cursor",
    }


def filter_telnet(sock: socket.socket, data: bytes) -> bytes:
    """Strip telnet IAC control sequences, auto-declining any option negotiation."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break
        cmd = data[i + 1]
        if cmd in (WILL, WONT, DO, DONT):
            if i + 2 >= n:
                break
            opt = data[i + 2]
            reply = None
            if cmd == WILL:
                reply = bytes([IAC, DONT, opt])
            elif cmd == DO:
                reply = bytes([IAC, WONT, opt])
            if reply:
                try:
                    sock.sendall(reply)
                except OSError:
                    pass
            i += 3
        elif cmd == SB:
            j = i + 2
            while j + 1 < n and not (data[j] == IAC and data[j + 1] == SE):
                j += 1
            i = j + 2
        elif cmd == IAC:
            out.append(IAC)
            i += 2
        else:
            i += 2
    return bytes(out)


def clean_text(raw: bytes) -> str:
    return ANSI_RE.sub(b"", raw).decode("utf-8", errors="replace")


def wait_for_output(log_path: Path, start_offset: int, quiet=0.6, timeout=8.0):
    """Wait until output past start_offset goes quiet, then return (text, new_offset)."""
    start = time.time()
    last_size = start_offset
    last_change = start
    got_any = False
    while True:
        try:
            size = log_path.stat().st_size
        except FileNotFoundError:
            size = start_offset
        if size > last_size:
            got_any = True
            last_size = size
            last_change = time.time()
        now = time.time()
        if got_any and (now - last_change) >= quiet:
            break
        if (now - start) >= timeout:
            break
        time.sleep(0.1)
    with open(log_path, "rb") as f:
        f.seek(start_offset)
        data = f.read(last_size - start_offset)
    return clean_text(data), last_size


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

def daemon_main(host, port, user, password, state_dir: Path):
    p = state_paths(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    for f in (p["ready"], p["error"]):
        f.unlink(missing_ok=True)
    p["pid"].write_text(str(os.getpid()))

    try:
        sock = socket.create_connection((host, port), timeout=10)
    except OSError as e:
        p["error"].write_text(f"could not connect to {host}:{port}: {e}")
        return
    # create_connection() leaves its connect-timeout as the socket's ongoing
    # timeout, so idle gaps between player commands (e.g. the human thinking)
    # would make recv() raise and look like a dropped connection. The
    # daemon should block indefinitely between game output, not time out.
    sock.settimeout(None)

    log_f = open(p["log"], "ab", buffering=0)

    def connection_lost(reason: str):
        # The socket is dead (or about to be). Make that visible to the CLI
        # commands immediately instead of letting `send` hang or silently
        # no-op against a connection that will never reply again.
        p["ready"].unlink(missing_ok=True)
        p["error"].write_text(reason)
        try:
            sock.close()
        except OSError:
            pass

    def reader_loop():
        try:
            while True:
                try:
                    chunk = sock.recv(4096)
                except OSError as e:
                    connection_lost(f"connection lost while reading: {e}")
                    return
                if not chunk:
                    connection_lost("the MUD server closed the connection")
                    return
                filtered = filter_telnet(sock, chunk)
                if filtered:
                    log_f.write(filtered)
        except Exception:
            import traceback
            traceback.print_exc()
            sys.stderr.flush()

    reader_thread = threading.Thread(target=reader_loop, daemon=True)
    reader_thread.start()

    def send_line(text: str):
        sock.sendall(text.encode() + b"\r\n")

    # --- login handshake -------------------------------------------------
    offset = 0
    deadline = time.time() + 30
    entered_game = False
    sent_name = sent_password = False
    while time.time() < deadline and not entered_game:
        text, offset = wait_for_output(p["log"], offset, quiet=0.5, timeout=3)
        if not text:
            continue
        if NAME_PROMPT_RE in text and not sent_name:
            send_line(user)
            sent_name = True
        elif PASSWORD_PROMPT_RE in text and not sent_password:
            send_line(password)
            sent_password = True
        elif PRESS_RETURN_RE in text:
            send_line("")
        elif MENU_RE in text:
            send_line("1")
        elif GAME_PROMPT_RE.search(text):
            entered_game = True

    if not entered_game:
        p["error"].write_text(
            "login handshake did not reach the game prompt within 30s; "
            "see mud.log for what the server actually sent"
        )
        sock.close()
        p["pid"].unlink(missing_ok=True)
        return

    p["ready"].touch()
    p["cursor"].write_text(str(offset))

    # --- command relay loop -----------------------------------------------
    p["fifo"].unlink(missing_ok=True)
    os.mkfifo(p["fifo"])
    try:
        while True:
            with open(p["fifo"], "r") as fifo:
                for line in fifo:
                    cmd = line.rstrip("\n")
                    if cmd == "__STOP__":
                        sock.close()
                        return
                    try:
                        send_line(cmd)
                    except OSError as e:
                        connection_lost(f"connection lost while sending: {e}")
                        return
    finally:
        for f in (p["ready"], p["pid"], p["fifo"]):
            f.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def not_connected_message(p) -> str:
    if p["error"].exists():
        return f"Not connected ({p['error'].read_text()}). Run `start` to reconnect."
    return "Not connected. Run `start` first."


def cmd_start(args):
    p = state_paths(args.state_dir)
    if p["pid"].exists() and pid_alive(int(p["pid"].read_text())) and p["ready"].exists():
        print("Already connected. Use `status` to see recent output, or `stop` first to reconnect.")
        return
    args.state_dir.mkdir(parents=True, exist_ok=True)
    p["log"].write_bytes(b"")

    import subprocess
    log_out = open(p["dir"] / "daemon.stderr.log", "ab")
    subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).resolve()),
         "--host", args.host, "--port", str(args.port),
         "--user", args.user, "--password", args.password,
         "--state-dir", str(args.state_dir), "_daemon"],
        stdin=subprocess.DEVNULL, stdout=log_out, stderr=log_out,
        start_new_session=True,
    )

    deadline = time.time() + 30
    while time.time() < deadline:
        if p["error"].exists():
            print(f"Connection failed: {p['error'].read_text()}", file=sys.stderr)
            sys.exit(1)
        if p["ready"].exists():
            break
        time.sleep(0.2)
    else:
        print("Timed out waiting for login to complete.", file=sys.stderr)
        sys.exit(1)

    text, offset = wait_for_output(p["log"], 0, quiet=0.5, timeout=2)
    p["cursor"].write_text(str(offset))
    print(text.strip())


def cmd_send(args):
    p = state_paths(args.state_dir)
    if not p["ready"].exists():
        print(not_connected_message(p), file=sys.stderr)
        sys.exit(1)
    offset = int(p["cursor"].read_text()) if p["cursor"].exists() else 0
    with open(p["fifo"], "w") as fifo:
        fifo.write(" ".join(args.command) + "\n")
    text, new_offset = wait_for_output(p["log"], offset, quiet=args.quiet, timeout=args.timeout)
    p["cursor"].write_text(str(new_offset))
    if not p["ready"].exists():
        print(not_connected_message(p), file=sys.stderr)
        sys.exit(1)
    print(text.strip())


def cmd_read(args):
    p = state_paths(args.state_dir)
    if not p["ready"].exists():
        print(not_connected_message(p), file=sys.stderr)
        sys.exit(1)
    offset = int(p["cursor"].read_text()) if p["cursor"].exists() else 0
    text, new_offset = wait_for_output(p["log"], offset, quiet=0.3, timeout=1.0)
    p["cursor"].write_text(str(new_offset))
    print(text.strip() if text.strip() else "(no new output)")


def cmd_status(args):
    p = state_paths(args.state_dir)
    alive = p["pid"].exists() and pid_alive(int(p["pid"].read_text()))
    ready = p["ready"].exists()
    print(f"daemon running: {alive}")
    print(f"logged in:      {ready}")
    if not ready and p["error"].exists():
        print(f"last error:     {p['error'].read_text()}")
    if p["log"].exists():
        tail = p["log"].read_bytes()[-2000:]
        print("--- recent output ---")
        print(clean_text(tail).strip())


def cmd_stop(args):
    p = state_paths(args.state_dir)
    if p["ready"].exists():
        try:
            with open(p["fifo"], "w") as fifo:
                fifo.write("quit\n")
            time.sleep(0.5)
        except OSError:
            pass
    if p["fifo"].exists():
        try:
            with open(p["fifo"], "w") as fifo:
                fifo.write("__STOP__\n")
        except OSError:
            pass
    time.sleep(0.3)
    if p["pid"].exists():
        try:
            pid = int(p["pid"].read_text())
            if pid_alive(pid):
                os.kill(pid, 15)
        except (OSError, ValueError):
            pass
    for f in (p["pid"], p["ready"], p["fifo"], p["error"], p["cursor"]):
        f.unlink(missing_ok=True)
    print("Disconnected.")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--user", default=os.environ.get("MUD_USER", "dummy"))
    parser.add_argument("--password", default=os.environ.get("MUD_PASSWORD", "helloworld"))
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)

    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("start", help="connect and log in").set_defaults(func=cmd_start)

    p_send = sub.add_parser("send", help="send a command and print the reply")
    p_send.add_argument("command", nargs="+", help="the command to send, e.g. look, north, say hi")
    p_send.add_argument("--quiet", type=float, default=0.6, help="seconds of silence before treating the reply as complete")
    p_send.add_argument("--timeout", type=float, default=8.0, help="max seconds to wait for a reply")
    p_send.set_defaults(func=cmd_send)

    sub.add_parser("read", help="flush any unread output since the last send/read").set_defaults(func=cmd_read)
    sub.add_parser("status", help="show connection status and recent output").set_defaults(func=cmd_status)
    sub.add_parser("stop", help="quit and close the connection").set_defaults(func=cmd_stop)

    p_daemon = sub.add_parser("_daemon", help=argparse.SUPPRESS)
    p_daemon.set_defaults(func=None)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.subcommand == "_daemon":
        daemon_main(args.host, args.port, args.user, args.password, args.state_dir)
        return
    args.func(args)


if __name__ == "__main__":
    main()
