#!/usr/bin/env perl
use strict;
use warnings;
use POSIX;
use Getopt::Long;

my $GIT = "git";
my $AIAIAI = "aiaiai-test-patchset";
my $AIAIAI_KTARGETS = "all dtbs dtbs_install dtbs_check";
my $AIAIAI_KMAKE_OPTS = "INSTALL_DTBS_PATH=\"\$PWD/dtbs_install\"";
my $NUM_CPUS = do { local @ARGV='/proc/cpuinfo'; grep /^processor\s+:/, <>; };
my $AIAIAI_OPTS = "-j $NUM_CPUS --bisectability --sparse --smatch --cppcheck --coccinelle --checkpatch --targets \"$AIAIAI_KTARGETS\" -M \"$AIAIAI_KMAKE_OPTS\" --logdir=\"\$PWD/logs/\" -p";
my $Fetch = 0;
my $Push = 0;
my $Verbose = 1;
my $Sendemail = 0;
my $Force = 0;
my $Build = 0;
my $Branch_suffix = "next";

# Global variables
my %branches = (
	"soc" => [ "arm" ],
	"soc-arm64" => [ "arm64" ],
	"devicetree" => [ "arm" ],
	"devicetree-arm64" => [ "arm64" ],
	"maintainers" => [],
	"maintainers-arm64" => [],
	"defconfig" => [ "arm" ],
	"defconfig-arm64" => [ "arm64" ],
	"drivers" => [ "arm", "arm64", "mips" ], # Shared drivers
);
my @gen_branches;
my $armsoc_tag_pattern = '^arm-soc\/for-([0-9]).([0-9]+)(.*)$';
my $linus_tag_pattern = '^v([0-9]).([0-9]+)(.*)$';

my %linux_repo = (
	"url"	=> "https://github.com/Broadcom/stblinux.git",
	"head"	=> "master",
	"base"	=> "1da177e4c3f41524e886b7f1b8a0c1fc7321cac2",
);

my %cclists = (
	"soc" => "soc\@kernel.org",
	"base" => ["linux-arm-kernel\@lists.infradead.org",
		   "arnd\@arndb.de",
		   "olof\@lixom.net",
		   "khilman\@kernel.org",
		   "bcm-kernel-feedback-list\@broadcom.com",
	   	  ],
);

my %cross_configs = (
	"arm" => "arm-linux-",
	"arm64" => "aarch64-linux-",
	"mips" => "mipsel-linux-",
);

my %build_configs = (
	"arm" => [ "multi_v7_defconfig", "bcm2835_defconfig" ],
	"arm64" => [ "defconfig" ],
	"mips" => [ "bmips_stb_defconfig", "bcm63xx_defconfig" ],
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

sub branch_exists($) {
	my $branch_name = shift;
	my ($err, $branch_desc);

	# Check that the branch exists
	($err, $branch_desc) = run("$GIT rev-parse --verify --quiet $branch_name");
	if ($err ne 0) {
		print "[!] No such branch $branch_name, create?\n";
		return;
	}

	return $branch_desc;
}

sub find_merge_base($) {
	my $branch_name = shift;
	my $head = $linux_repo{head};
	my ($err, $commit, $tag);

	($err, $commit) = run("$GIT merge-base $head $branch_name");
	return if ($commit eq "");
	($err, $tag) = run("$GIT describe --tags --exact-match $commit");
	return if ($err ne 0);

	return ($commit, $tag);
}

sub find_baseline_tag($$) {
	my ($branch, $branch_suffix) = @_;
	my ($err, $commit, $branch_desc, $tag, $end_tag, $commits);
	my $branch_name = $branch . "/" . $branch_suffix;

	$branch_desc = branch_exists($branch_name);
	return if !defined($branch_desc);

	($err, $branch_desc) = run("$GIT describe --tags --exact-match $branch_name");
	($commit, $tag) = find_merge_base($branch_name);
	return if !defined($commit) or !defined($tag);

	($err, $commits) = run("$GIT log --format=%H $commit~1..$branch_name~1");
	return if ($err ne 0);

	# Walk the list of commits from newest to oldest, and match our own
	# tags created with $tag_pattern
	foreach $commit (split "\n", $commits) {
		($err, $tag) = run("$GIT describe --tags --exact-match $commit");
		next if ($err ne 0);

		last if ($tag =~ /$armsoc_tag_pattern/);
		last if ($tag =~ /$linus_tag_pattern/);
	}

	return if ($err ne 0);

	if ($branch_desc eq $tag) {
		return;
	}

	return ($tag, $branch_desc);
};

sub get_linux_version($$) {
	my ($tag, $branch_suffix) = @_;
	my ($major, $minor);
	my $base_tag;

	return if !defined($tag) or $tag eq "";

	if ($tag =~ /$linus_tag_pattern/) {
		$major = $1;
		$minor = $2;
		# Just assume minor + 1 for now, Linus might change his mind one day
		# though
		$minor += 1 if ($branch_suffix eq "next");
	} elsif ($tag =~ /$armsoc_tag_pattern/) {
		$major = $1;
		$minor = $2;
	} else {
		return undef;
	}

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
		if ($line =~ /(.*)(Acked|Reviewed|Reported|Signed-off|Suggested|Tested|Co-developed)-by:\s(.*)$/) {
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
	my $tag = find_baseline_tag($branch, $suffix);

	if (!defined($tag)) {
		print "[-] Branch $branch has no changes\n" if $Verbose;
	} else {
		push @gen_branches, $branch;
	}
};

my $branch_num = 1;

sub usage() {
	print "Usage ".$0. " [options]\n" .
		"--fetch:       fetch branches from repo (default: no)\n" .
		"--push:        push branches to repo (default: no)\n" .
		"--verbose:     enable verbose mode (default: yes)\n" .
		"--send-email:  send emails while processing (default: no)\n" .
		"--force:       force actions (default: no)\n" .
		"--build:       build branches (default: no)\n" .
		"--branch:	specify the branch to use (default: next)\n" .
		"--help:        this help\n";
	exit(0);
};

GetOptions("fetch" => \$Fetch,
	   "push" => \$Push,
	   "verbose" => \$Verbose,
	   "send-email" => \$Sendemail,
	   "force" => \$Force,
	   "build" => \$Build,
	   "branch=s" => \$Branch_suffix,
	   "help" => \&usage);

sub get_patch_filename($$)
{
	my ($branch_num, $branch) = @_;

	return sprintf("%02x-%s.patch", $branch_num, $branch);
}

sub format_patch($$$$$) {
	my ($branch, $suffix, $version, $start_tag, $end_tag) = @_;
	my ($err, $ret);
	my @authors = get_authors($start_tag, "$branch/$suffix");
	my @cclist = @{$cclists{"base"}};
	my $output = "";
	my $filename = get_patch_filename($branch_num, $branch);
	my $kind;
	my $cmd;

	if ($suffix eq "next") {
		$kind = "changes";
	} else {
		$kind = "fixes";
	}

	open(my $fh, '>', $filename) or die("Unable to open $filename for write\n");

	print $fh "Subject: [GIT PULL $branch_num/".scalar(@gen_branches)."] Broadcom $branch $kind for $version\n";

	# Append the authors we found in the log
	foreach my $author (@authors) {
		print $fh "CC: $author\n";
	}

	# And the usual suspects
	foreach my $cc (@cclist) {
		print $fh "CC: $cc\n";
	};

	print $fh "\n";

	# TODO, if running with patches appended (-p), we could do a first run
	# which also asks scripts/get_maintainer.pl to tell us who to CC
	$cmd = "$GIT request-pull $start_tag $linux_repo{url} $end_tag";
	($err, $ret) = run($cmd);
	if ($err ne 0) {
		print ("Unable to run pull request!: $cmd\n");
		close($fh);
		unlink($filename);
	} else {
		print $fh $ret;
		close($fh);
	}
};

sub send_email($$) {
	my ($branch, $branch_num) = @_;
	my $filename = get_patch_filename($branch_num, $branch);
	my ($err, $ret) = run("$GIT send-email --to ".$cclists{soc}. " --confirm=never $filename");
};

sub do_one_branch($$) {
	my ($branch, $suffix) = @_;
	my ($start_tag, $end_tag) = find_baseline_tag($branch, $suffix);
	my $branch_name = "$branch/$suffix";

	my $version = get_linux_version($start_tag, $suffix);
	die ("unable to get Linux version for $branch_name") if !defined($version);

	print " [+] Branch $branch_name is based on $start_tag, submitting for $version\n" if $Verbose;

	format_patch($branch, $suffix, $version, $start_tag, $end_tag);

	if ($Sendemail) {
		send_email($branch, $branch_num);
	} else {
		print " [+] Not sending email for $branch_name\n" if $Verbose;
	}
	$branch_num += 1;

	print "[x] Processed $branch_name\n" if $Verbose;
};

sub build_one_branch($$) {
	my ($branch, $suffix) = @_;
	my ($start_tag, $start_commit, $end_tag, $branch_desc, $err, $ret);
	my $branch_name = "$branch/$suffix";
	my $filename = "$branch.patch";
	my $linux_dir;
	($err, $linux_dir) = run("$GIT rev-parse --show-toplevel");

	die ("Unable to obtain top level directory!?") if ($err ne 0);

	$branch_desc = branch_exists($branch_name);
	die if !defined($branch_desc);

	($start_commit, $start_tag) = find_merge_base($branch_name);

	return if !defined($start_tag) or !defined($start_commit);

	print "[X] Branch is based on $start_tag\n";

	($err, $ret) = run("$GIT format-patch $start_tag..$branch_name --stdout > $filename");
	if ($err ne 0) {
		print ("Unable to run format-patch!\n");
		unlink($filename);
		exit($err);
	}

	my $aiaiai_targets = "";
	foreach my $arch (@{$branches{"$branch"}}) {
		foreach my $defconfig (@{$build_configs{"$arch"}}) {
			my $cross = $cross_configs{$arch};
			$aiaiai_targets .= "$defconfig,$arch,$cross ";
		}
	}

	if ($aiaiai_targets eq "") {
		print "[X] No build architectures for $branch";
		exit(0);
	}

	my $cmd = "$AIAIAI $AIAIAI_OPTS -c $start_tag $linux_dir $aiaiai_targets < $filename";
	print  "[X] Building with $cmd\n";
	($err, $ret) = run($cmd);
	if ($err ne 0) {
		print ("Build failure: $ret\n");
		exit($err);
	}
	print "[X] aiaiai build results:\n";
	print $ret;

	unlink($filename);
}

sub update($) {
	my $cmd = shift;
	my ($err, $ret);
	foreach (sort keys %branches) {
		my $branch_name = "$_/$Branch_suffix";
		print " [+] Update branch $branch_name\n" if $Verbose;
		my $git_cmd = "$GIT $cmd broadcom-github " . ($Force eq 1 ? "+" : "") .
			      "$branch_name:$branch_name";
		($err, $ret) = run("$git_cmd");
		print " [X] $branch_name: $ret\n" if ($err ne 0);
	}

	exit($err);
};

sub main() {
	my ($err, $ret) = run("$GIT cat-file -e $linux_repo{base}");
	my ($branch);
	if ($err) {
	        print " [X] Cannot find a Linux git repo in '".getcwd()."'\n";
	        exit(1);
	}

	# Get the number of branches with changes, some might just be empty
	# Modifies @branches if found empty branches (baseline == branch
	# commit)
	foreach (sort keys %branches) {
		get_num_branches("$_", "$Branch_suffix");
	}

	# Now do the actual work of generating the pull request message
	foreach $branch (@gen_branches) {
		if ($Build eq 1) {
			build_one_branch("$branch", "$Branch_suffix");
		} else {
			do_one_branch("$branch", "$Branch_suffix");
		}
	}
};

if ($Fetch eq 1) {
	update("fetch");
} elsif ($Push eq 1) {
	update("push");
} else {
	main();
}
