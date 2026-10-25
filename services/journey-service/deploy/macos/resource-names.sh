# Sourced by the macOS deploy and teardown scripts:   . "$SCRIPT_DIR/resource-names.sh"
# (the bash twin of ../windows/resource-names.ps1)
#
# The team decided on the resource prefix "careroute" (careroute-routes, careroute-trips, careroute-disruptions, ...).
# The platform stack builds every name from its ResourcePrefix parameter, and its IAM role only allows resources whose
# names start with it. So instead of hardcoding a prefix, our scripts read the one the stack was really built with,
# and warn if it is not "careroute" (the stack's default since main's 55ab5ec).

TEAM_PREFIX="careroute"

# resolve_resource_prefix STACK REGION [OVERRIDE]: prints the prefix to use: OVERRIDE if given, else the stack's
# ResourcePrefix parameter, else the team's. Its messages go to stderr, so  prefix="$(resolve_resource_prefix ...)"  works.
resolve_resource_prefix() {
  local stack="$1" region="$2" prefix="${3:-}" source found
  if [ -n "$prefix" ]; then
    source="the --resource-prefix option"
  else
    found="$(aws cloudformation describe-stacks --stack-name "$stack" --region "$region" \
      --query "Stacks[0].Parameters[?ParameterKey=='ResourcePrefix'].ParameterValue | [0]" --output text 2>/dev/null || true)"
    if [ -n "$found" ] && [ "$found" != "None" ]; then
      prefix="$found"; source="stack $stack"
    else
      prefix="$TEAM_PREFIX"; source="the team default (the stack has no ResourcePrefix parameter, or could not be read)"
    fi
  fi
  printf '    Resource prefix: %s (from %s)\n' "$prefix" "$source" >&2
  if [ "$prefix" != "$TEAM_PREFIX" ]; then
    printf '\033[1;33m    WARNING: the team decided on %s. Stack %s names its tables, queues and buckets with %s,\033[0m\n' \
      "'$TEAM_PREFIX'" "$stack" "'$prefix'" >&2
    printf '\033[1;33m    so this script follows that. The platform owner should set ResourcePrefix=%s (see deploy/README.md).\033[0m\n' \
      "$TEAM_PREFIX" >&2
  fi
  printf '%s\n' "$prefix"
}

# shared_bucket_name PREFIX ACCOUNT_ID [OVERRIDE]: <prefix>-files-<account id> (the platform scripts' naming).
shared_bucket_name() {
  if [ -n "${3:-}" ]; then printf '%s\n' "$3"; else printf '%s-files-%s\n' "$1" "$2"; fi
}
