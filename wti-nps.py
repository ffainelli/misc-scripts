#!/usr/bin/env python3

import argparse
import sys
import re

try:
    from telnetlib3 import Telnet
except ImportError:
    sys.exit("Install Telnet support: python3 -m pip install 'telnetlib3>=4,<5'")

PROMPT = [br'NPS> ', br'IPS> ', br'NBB> ']
STATUS = r'([0-9])([0-9a-z\s\(\)\|])+\|\s+(ON|OFF)'

def expect_prompt(tn):
    index, _, response = tn.expect(PROMPT, 10)
    if index < 0:
        raise TimeoutError("Timed out waiting for WTI prompt")
    return response

def range_check_relay(relay):
    if (int(relay) <= 0 or int(relay) > 8):
        print("Invalid relay value range: [1..8]")
        sys.exit(1)

def __send_cmd(tn, args, relay):
    cmd = "/"
    if args.on:
        cmd += "On"
    elif args.off:
        cmd += "Off"
    elif args.reboot:
        cmd += "Boot"

    cmd += " " + str(relay) + "\r\n"
    tn.write(cmd.encode())
    expect_prompt(tn)

def send_cmd(tn, args):
    relay = None
    if args.on:
        relay = args.on
    elif args.off:
        relay = args.off
    elif args.reboot:
        relay = args.reboot

    if relay == "all":
        for r in range(1, 9):
            __send_cmd(tn, args, r)
    else:
        range_check_relay(relay)
        __send_cmd(tn, args, relay)

def __get_status(tn, relay):
    cmd = "/S\r\n"
    tn.write(cmd.encode())
    buf = expect_prompt(tn).decode(errors="replace")

    for line in buf.splitlines():
        if line == "":
            continue

        m = re.search(STATUS, line)
        if m is not None and m.group(1) == relay:
            print(m.group(3))
            return
    raise ValueError("No status found for relay " + relay)

def get_status(tn, relay):
    if relay == "all":
        for r in range(1, 9):
            __get_status(tn, str(r))
    else:
        range_check_relay(relay)
        __get_status(tn, relay)

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, help="WTI relay host")
    parser.add_argument("--password", help="WTI relay password")
    actions = parser.add_mutually_exclusive_group(required=True)
    choices = [str(r) for r in range(1, 9)] + ["all"]
    actions.add_argument("--on", choices=choices, help="Turn relay on")
    actions.add_argument("--off", choices=choices, help="Turn relay off")
    actions.add_argument("--reboot", choices=choices, help="Reboot relay")
    actions.add_argument("--status", choices=choices, help="Get relay status")
    parser.add_argument("--debug", nargs="?", const=True, help="Enable debug")
    args = parser.parse_args()

    try:
        with Telnet(args.host, timeout=10) as tn:
            if args.debug:
                tn.set_debuglevel(255)
            if args.password:
                tn.write((args.password + "\r\n").encode())
            expect_prompt(tn)
            if args.status:
                get_status(tn, args.status)
            else:
                send_cmd(tn, args)
    except (OSError, EOFError, ValueError) as exc:
        print("WTI command failed: " + str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
