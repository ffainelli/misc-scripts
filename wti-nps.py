#!/usr/bin/env python3

import argparse
import telnetlib
import sys

PROMPT = b"NPS> "

def range_check_relay(relay):
    if (int(relay) <= 0 or int(relay) > 8):
        print("Invalid relay value range: [1..8]")
        sys.exit(1)

def send_cmd(tn, args):
    cmd = "/"
    if args.on:
        cmd += "On "
        relay = args.on
    elif args.off:
        cmd += "Off "
        relay = args.off
    elif args.reboot:
        cmd += "Boot "
        relay = args.reboot

    range_check_relay(relay)

    cmd += relay + "\r\n"

    tn.write(cmd.encode())
    resp = tn.read_until(PROMPT)

def get_status(tn, relay):
    range_check_relay(relay)

    cmd = "/S\r\n"
    tn.write(cmd.encode())
    buf = ""

    tn.read_until(PROMPT + cmd.encode())
    while True:
        resp = tn.read_until(PROMPT)
        if resp is not None:
            break

    buf = resp.decode()

    for line in buf.splitlines():
        if line == "":
            continue
        words = line.split()
        if words[0] == relay:
            print(words[4])


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", help="WTI relay host")
    parser.add_argument("--password", help="WTI relay password")
    parser.add_argument("--on", help="Turn relay on")
    parser.add_argument("--off", help="Turn relay off")
    parser.add_argument("--reboot", help="Reboot relay")
    parser.add_argument("--status", help="Get relay status")
    args = parser.parse_args()

    tn = telnetlib.Telnet(args.host)
    if args.password:
        tn.write((args.password + "\r\n").encode())
        resp = tn.read_until(PROMPT)

    if args.status:
        get_status(tn, args.status)
    else:
        send_cmd(tn, args)
    tn.close()


if __name__ == "__main__":
    main()
