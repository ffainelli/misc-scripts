#!/usr/bin/perl -w
# email to irc gateway for arpwatch

use IO::Socket;
use Mail::Box::Manager;
use File::stat;
use threads;

my $botnick = $ARGV[0];
my $server = $ARGV[1];

my $channel = "#test-bot";
my $frequency = 10;	# in seconds

my $mailbox = "arpwatch";
my $mgr = Mail::Box::Manager->new;

if(@ARGV < 2) {
	print STDOUT "Usage: $0 [nick] [server].\n";
	exit;
}


$con = IO::Socket::INET->new(PeerAddr=>$server,
			     PeerPort=>'6667', # change this if needed..
			     Proto=>'tcp',
			     Timeout=>'30') or die "Connection to IRC server failed\n";

print $con "USER arpwatch 8 *  : Arpwatch email bot\r\n";
print $con "NICK arpwatch\r\n";
print $con "JOIN $channel\r\n";

sub read_messages() {
	while (1) {
retry:
		# rely on stat to check for valid mailbox file
		$sb = stat($mailbox);
		if (not defined ($sb)) {
			sleep($frequency);
			goto retry;
		}

		$folder = $mgr->open(folder => $mailbox, access => 'rw');

		foreach $msg ($folder->messages) {
			print $con sprintf("PRIVMSG %s :\x02subject\x02: %s\r\n", $channel, $msg->subject);
			sleep(1);
			$msg->delete();
		}

		$mgr->close($folder);

		sleep($frequency);
	}
}

# mailbox reading thread
my $thr = threads->new(\&read_messages);

# main connection thread
while ($answer = <$con>) {
	if($answer =~ m/^PING (.*?)$/gi) {
		print $con "PONG ".$1."\n";

	}
}
