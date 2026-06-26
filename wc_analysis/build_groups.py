"""Extract WC 2026 group composition from Wikipedia and build standings/motivation.

Run on local to populate wc_analysis/data/groups_2026.json:
    python build_groups.py --url "https://en.wikipedia.org/wiki/2026_FIFA_World_Cup" --out data/groups_2026.json
"""
import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = None
    if url.startswith("https"):
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.urlopen(req, timeout=30, context=ctx).read().decode("utf-8", errors="replace")


def extract_groups(html: str) -> dict:
    """Extract groups A-L with their 4 teams each from Wikipedia HTML."""
    groups = {l: [] for l in "ABCDEFGHIJKL"}
    # Find each Group section by anchor (h3 headers)
    # Pattern: <h3 id="Group_A">Group A</h3>
    sections = re.split(r'<h3 id="Group_([A-L])">', html)
    for i in range(1, len(sections), 2):
        letter = sections[i]
        body = sections[i+1] if i+1 < len(sections) else ""
        # Find next Group letter or end (look for h3 with id)
        next_letter_match = re.search(r'<h3 id="Group_[A-L]">', body)
        if next_letter_match:
            body = body[:next_letter_match.start()]
        # Look for "Teams" or "Seeds" or "Group [X] consists of..."
        # Find all team links
        # Pattern: title="[Team] national football team">[Team]
        teams = re.findall(
            r'title="([^"]+ national (?:football|soccer) team)"[^>]*>([^<]+)</a>',
            body
        )
        # Deduplicate by team name
        seen = set()
        for _, name in teams:
            if name in seen:
                continue
            seen.add(name)
            # Normalize name
            if name == "DR Congo":
                normalized = "DR Congo"
            elif name == "Ivory Coast":
                normalized = "Ivory Coast"
            elif name == "Cape Verde":
                normalized = "Cape Verde"
            elif name == "Curaçao":
                normalized = "Curaçao"
            elif name == "Czech Republic":
                normalized = "Czech Republic"
            elif name == "South Korea":
                normalized = "South Korea"
            elif name == "New Zealand":
                normalized = "New Zealand"
            elif name == "Saudi Arabia":
                normalized = "Saudi Arabia"
            elif name == "Bosnia and Herzegovina":
                normalized = "Bosnia and Herzegovina"
            elif name == "Republic of Ireland":
                normalized = "Republic of Ireland"
            else:
                normalized = name
            if len(groups[letter]) < 4:
                groups[letter].append(normalized)
    return groups


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="https://en.wikipedia.org/wiki/2026_FIFA_World_Cup")
    p.add_argument("--out", default="data/groups_2026.json")
    args = p.parse_args()
    print(f"Fetching {args.url}...")
    html = fetch(args.url)
    print(f"  Got {len(html)} bytes")
    groups = extract_groups(html)
    print("\nGroups found:")
    for letter, teams in groups.items():
        if teams:
            print(f"  {letter}: {teams}")
    out_path = Path(args.out)
    out_path.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
