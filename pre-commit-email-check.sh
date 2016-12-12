#/bin/sh

EMAIL_PUB="notsoscret@hooli.com"
EMAIL_WORK="supersecret@evilcorp.com"

# Find the base branch this is coming from
if [ -n "$1" ]; then
	current_branch=$1
else
	current_branch=`git rev-parse --abbrev-ref HEAD`
fi

base_branch=""
echo "Current branch: $current_branch"

for branch in $(git branch -l | cut -d' ' -f2,3 | sed 's/^ //g' | grep -v $current_branch)
do
	# Does branch have an upstream?
	git rev-parse --symbolic-full-name $branch@{u}
	ret=$?
	if [ $ret -eq 0 ]; then
		base_branch=$branch
		fork_point=$(git merge-base --fork-point $branch $current_branch)
		if [ -n "$fork_point" ]; then
			break;
		fi
	fi
done

if [ -e $base_branch ]; then
	echo "Could not find a fork-point, rebase?"
	exit 1
fi

echo "Fork point is: $fork_point on $base_ranch"

echo "Base branch is: $base_branch"

# Now get the remote associated with this branch has to be in local config since we track it
remote=$(git config branch."$base_branch".remote)

echo "Remote is: $remote"

# And now verify this remote URL against our list

url=$(git config remote."$remote".url)

echo "Remote URL is: $url"

echo $url | grep -q "evilcorp.com"
ret=$?

echo "Ret: $ret"

if [ "$ret" -eq "0" ]; then
	email="$EMAIL_WORK"
else
	email="$EMAIL_PUB"
fi

echo "Using email: $email"
git config user.email $email
