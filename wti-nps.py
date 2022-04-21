#!/usr/bin/env python3

import argparse
import telnetlib
import sys
import re

PROMPT = [br'NPS> ', br'IPS> ', br'NBB> ']
STATUS = '([0-9])([0-9a-z\s\(\)\|])+\|\s+(ON|OFF)'

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
    resp = tn.expect(PROMPT, 10)

def get_status(tn, relay):
    range_check_relay(relay)

    cmd = "/S\r\n"
    tn.write(cmd.encode())
    buf = ""

    while True:
        resp = tn.expect(PROMPT, 10)
        if resp[2] is not None:
            break

    buf = resp[2].decode()

    for line in buf.splitlines():
        if line == "":
            continue

        m = re.search(STATUS, line)
        if m is not None and m.group(1) == relay:
            print(m.group(3))


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", help="WTI relay host")
    parser.add_argument("--password", help="WTI relay password")
    parser.add_argument("--on", help="Turn relay on")
    parser.add_argument("--off", help="Turn relay off")
    parser.add_argument("--reboot", help="Reboot relay")
    parser.add_argument("--status", help="Get relay status")
    parser.add_argument("--debug", help="Enable debug")
    args = parser.parse_args()

    tn = telnetlib.Telnet(args.host)
    if args.debug:
        tn.set_debuglevel(255)

    if args.password:
        tn.write((args.password + "\r\n").encode())

    """ Flush out whathever was sent until we get a prompt """
    tn.expect(PROMPT, 10)

    if args.status:
        get_status(tn, args.status)
    else:
        send_cmd(tn, args)
    tn.close()


if __name__ == "__main__":
    main()
