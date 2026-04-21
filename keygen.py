"""
keygen.py — API key management CLI

Usage:
  python keygen.py                              # list all keys
  python keygen.py --name "Free"  --rate-limit 30
  python keygen.py --name "Pro"   --rate-limit 200
  python keygen.py --name "Enterprise" --rate-limit 1000
  python keygen.py --name "Unlimited"  --rate-limit 0
  python keygen.py --name "Trial" --rate-limit 30 --expires-in 7d
  python keygen.py --name "Scoped" --rate-limit 30 --gateways my-workflow
  python keygen.py --revoke "Free"
"""

import argparse
import sys
from core.auth import create_key, get_all_keys, parse_allowed_gateways, parse_expiration, revoke_key


def list_keys():
    keys = get_all_keys()
    if not keys:
        print("\n  No keys yet. Create one:")
        print('  python keygen.py --name "Free" --rate-limit 30\n')
        return

    print(f"\n  {'NAME':<20} {'RATE LIMIT':<18} {'SCOPE':<24} {'EXPIRES':<24} {'CREATED':<14} {'KEY'}")
    print("  " + "-" * 134)
    for k in keys:
        rpm = k.get("rate_limit_per_minute", 60)
        limit_str = f"{rpm} req/min" if rpm > 0 else "Unlimited"
        scope = ", ".join(k.get("allowed_gateways") or []) or "All"
        expires_at = k.get("expires_at") or "Never"
        print(f"  {k['name']:<20} {limit_str:<18} {scope:<24} {expires_at:<24} {k.get('created_at', '-'):<14} {k['key'][:24]}...")
    print()


def main():
    parser = argparse.ArgumentParser(description="workflow-api key manager")
    parser.add_argument("--name",       type=str, help="Name/tier for this key (e.g. 'Free', 'Pro')")
    parser.add_argument("--rate-limit", type=int, help="Requests per minute (0 = unlimited)")
    parser.add_argument("--expires-in", type=str, help="Relative expiration, e.g. 30d, +30d, 12h")
    parser.add_argument("--expires-at", type=str, help="Absolute expiration, e.g. 2026-12-31")
    parser.add_argument("--gateways", "--scope", type=str, help="Comma-separated gateway names this key can access")
    parser.add_argument("--revoke",     type=str, help="Revoke all keys with this name")
    args = parser.parse_args()

    # List mode
    if not args.name and not args.revoke:
        list_keys()
        return

    # Revoke mode
    if args.revoke:
        if revoke_key(args.revoke):
            print(f"\n  ✅ Revoked all keys named '{args.revoke}'\n")
        else:
            print(f"\n  ⚠️  No keys found with name '{args.revoke}'\n")
        return

    # Create mode
    if not args.name:
        print("  Error: --name is required")
        sys.exit(1)
    if args.rate_limit is None:
        print("  Error: --rate-limit is required")
        sys.exit(1)

    try:
        expires_at = parse_expiration(expires_at=args.expires_at, expires_in=args.expires_in)
        allowed_gateways = parse_allowed_gateways(args.gateways)
    except ValueError as exc:
        print(f"  Error: {exc}")
        sys.exit(1)

    record = create_key(
        name=args.name,
        rate_limit_per_minute=args.rate_limit,
        expires_at=expires_at,
        allowed_gateways=allowed_gateways,
    )

    rpm = record["rate_limit_per_minute"]
    limit_str = f"{rpm} req/min" if rpm > 0 else "Unlimited"

    print(f"""
  ✅ Key created

     Name       : {record['name']}
     Rate limit : {limit_str}
     Scope      : {', '.join(record.get('allowed_gateways') or []) or 'All gateways'}
     Expires    : {record.get('expires_at') or 'Never'}
     Created    : {record['created_at']}
     Key        : {record['key']}

  Share this key with your user. They call:

     curl -X POST http://your-host:8000/run/my-workflow \\
       -H "Authorization: Bearer {record['key']}" \\
       -H "Content-Type: application/json" \\
       -d '{{"input": "hello"}}'
""")


if __name__ == "__main__":
    main()
