#!/usr/bin/env python3

import argparse
import telnetlib

PROMPT = b"NPS> "

def send_cmd(tn, args):
	cmd = "/"
	if args.on:
		cmd += "On " + args.on
	elif args.off:
		cmd += "Off " + args.off
	elif args.reboot:
		cmd += "Boot " + args.reboot
	cmd += "\r\n"

	tn.write(cmd.encode())
	resp = tn.read_until(PROMPT)

def get_status(tn, relay):
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


if __name__ == "__main__":
	main()

