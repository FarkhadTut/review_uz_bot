#!/usr/bin/env python3
"""Independently recompute a Review.uz giveaway draw. Standard library only.

    python verify_draw.py draw-12.txt --hash <posted hash> --round <posted round>

draw-12.txt is the file the bot sends for /verify 12 (entry codes, one per line).
The randomness is fetched from the public drand quicknet beacon unless --randomness is given.
"""

import argparse
import hashlib
import json
import sys
import urllib.request

QUICKNET = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"


def fetch_randomness(round_no: int, relay: str = "https://api.drand.sh") -> str:
    with urllib.request.urlopen(f"{relay}/{QUICKNET}/public/{round_no}", timeout=15) as resp:
        data = json.load(resp)
    if hashlib.sha256(bytes.fromhex(data["signature"])).hexdigest() != data["randomness"]:
        sys.exit("beacon response is inconsistent: randomness != sha256(signature)")
    return data["randomness"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("codes_file")
    p.add_argument("--hash", required=True, help="snapshot hash from the channel post")
    p.add_argument("--round", type=int, required=True, help="drand round from the channel post")
    p.add_argument("--randomness", help="skip fetching; use this randomness hex")
    args = p.parse_args(argv)

    with open(args.codes_file, encoding="utf-8") as f:
        codes = [line.strip() for line in f if line.strip()]
    content = "\n".join(sorted(codes))
    computed = hashlib.sha256(content.encode("utf-8")).hexdigest()
    print(f"participants: {len(codes)}")
    print(f"snapshot hash: {computed}")
    if computed != args.hash.lower():
        print(f"MISMATCH: posted hash is {args.hash}")
        return 1

    randomness = args.randomness or fetch_randomness(args.round)
    digest = hashlib.sha256(bytes.fromhex(randomness) + bytes.fromhex(computed)).digest()
    index = int.from_bytes(digest, "big") % len(codes)
    print(f"randomness (round {args.round}): {randomness}")
    print(f"winner index: {index} -> entry code {sorted(codes)[index]}")
    print("(if the post lists skipped indexes, the winner is the next index not skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
