#!/usr/bin/env perl
use strict;
use warnings;
use POSIX;
use Getopt::Long;

my $GIT = "git";
my $Verbose = 1;
my $Sendemail = 0;

# Global variables
my @branches = (
	"soc",
	"soc-arm64",
	"devicetree",
	"devicetree-arm64",
	"maintainers",
	"defconfig",
);
my $branch_suffix = "next";

my %linux_repo = (
	"url"	=> "http://github.com/Broadcom/stblinux.git",
	"head"	=> "master",
	"base"	=> "1da177e4c3f41524e886b7f1b8a0c1fc7321cac2",
);

my %cclists = (
	"armsoc" => "arm\@kernel.org",
	"base" => ["linux-arm-kernel\@lists.infradead.org",
		   "arnd\@arndb.de",
		   "olof\@lixom.net",
		   "khilman\@kernel.org",
		   "bcm-kernel-feedback-list\@broadcom.com",
	   	  ],
);

sub run($)
{
	my ($cmd) = @_;
	local *F;
	my $ret = "";
	my $err = 0;

	if (!open(F, "$cmd 2>&1 |")) {
		return (-1, "");
	}
	while (<F>) {
		$ret .= $_;
	}
	close(F);

	if (!WIFEXITED($?) || WEXITSTATUS($?)) {
		$err = 1;
	}

	$ret =~ s/[\r\n]+$//;
	return ($err, $ret);
}

sub find_baseline_tag($) {
	my $branch = shift;
	my ($err, $commit, $branch_desc, $tag);

	($err, $branch_desc) = run("$GIT describe $branch");
	($err, $commit) = run("$GIT merge-base $linux_repo{head} $branch");
	return if ($commit eq "");
	($err, $tag) = run("$GIT describe --tags $commit");

	if ($branch_desc eq $tag) {
		return;
	}

	return $tag;
};

sub get_linux_version($) {
	my $tag = shift;
	my ($major, $minor);

	if ($tag =~ /^v([0-9]).([0-9])(.*)$/) {
		$major = $1;
		$minor = $2;
	} else {
		die ("Unrecognized tag format\n");
	}

	# Just assume minor + 1 for now, Linus might change his mind one day
	# though
	$minor += 1;

	print " [+] Determined version $major.$minor based on $tag\n" if $Verbose;

	return "$major.$minor";
};

sub uniq {
	my %seen;
	grep !$seen{$_}++, @_;
};

sub get_authors($$) {
	my ($base, $branch) = @_;
	my ($err, $ret) = run("$GIT log $base..$branch");
	my @person_list;
	my @lines = split("\n", $ret);

	foreach my $line (@lines) {
		# Find the author and people CC'd for this commit
		if ($line =~ /(.*)(Author|CC):\s(.*)$/) {
			my $author = $3;
			push @person_list, $author;
		}

		# Now find the contributors to this commit, identified by
		# standard Linux practices
		if ($line =~ /(.*)(Acked|Reviewed|Reported|Signed-off|Suggested|Tested)-by:\s(.*)$/) {
			my $person = $3;
			push @person_list, $person;
		}
	}

	# Now remove non unique entries, since there could be multiple times
	# the same people present in log
	return uniq(@person_list);
};

sub get_num_branches($$) {
	my ($branch, $suffix) = @_;
	my ($err, $ret);
	my $tag = find_baseline_tag("$branch/$suffix");

	if (!defined($tag)) {
		print "[-] Branch $branch has no changes\n" if $Verbose;
		pop @branches, $branch;
	}
};

my $branch_num = 1;

sub usage() {
	print "Usage ".$ARGV[0]. "\n" .
		"--verbose:     enable verbose mode (default: yes)\n" .
		"--send-email:  send emails while processing (default: no)\n" .
		"--help:        this help\n";
	exit(0);
};

GetOptions("verbose" => \$Verbose,
	   "send-email" => \$Sendemail,
	   "help" => \&usage);

sub format_patch($$$$) {
	my ($branch, $suffix, $version, $tag) = @_;
	my ($err, $ret);
	my @authors = get_authors($tag, "$branch/$suffix");
	my @cclist = @{$cclists{"base"}};
	my $output = "";

	open(my $fh, '>', "$branch.patch") or die("Unable to open $branch.patch for write\n");

	print $fh "Subject: [GIT PULL $branch_num/".scalar(@branches)."] Broadcom $branch changes for $version\n";

	# Append the authors we found in the log
	foreach my $author (@authors) {
		print $fh "CC: $author\n";
	}

	# And the usual suspects
	foreach my $cc (@cclist) {
		print $fh "CC: $cc\n";
	};

	# TODO, if running with patches appended (-p), we could do a first run
	# which also asks scripts/get_maintainer.pl to tell us who to CC
	($err, $ret) = run("$GIT request-pull $tag $linux_repo{url} arm-soc/for-$version/$branch");
	print $fh $ret;
	close($fh);
};

sub send_email($) {
	my $branch = shift;
	my ($err, $ret) = run("$GIT send-email --to ".$cclists{armsoc}. " --confirm=never $branch.patch");
};

sub do_one_branch($$) {
	my ($branch, $suffix) = @_;
	my $tag = find_baseline_tag("$branch/$suffix");

	my $version = get_linux_version($tag);
	die ("unable to get Linux version for $branch") if !defined($version);

	print " [+] Branch $branch is based on $tag, submitting for $version\n" if $Verbose;

	format_patch($branch, $suffix, $version, $tag);

	$branch_num += 1;

	if ($Sendemail) {
		send_email($branch);
	} else {
		print " [+] Not sending email for $branch\n" if $Verbose;
	}

	print "[x] Processed $branch\n" if $Verbose;
};

sub main() {
	my ($err, $ret) = run("$GIT cat-file -e $linux_repo{base}");
	if ($err) {
	        print " [X] Cannot find a Linux git repo in '".getcwd()."'\n";
	        exit(1);
	}

	# Get the number of branches with changes, some might just be empty
	# Modifies @branches if found empty branches (baseline == branch
	# commit)
	foreach my $branch (@branches) {
		get_num_branches("$branch", "$branch_suffix");
	}

	# Now do the actual work of generating the pull request message
	foreach my $branch (@branches) {
		do_one_branch("$branch", "$branch_suffix");
	}
};

main();
