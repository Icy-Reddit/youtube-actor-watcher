import csv
import json
import os
import re
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from dotenv import load_dotenv

CHANNELS_FILE = "channels.csv"
ACTORS_FILE = "actors.csv"
STATE_FILE = "state.json"


def load_channels(path: str):
    channels = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Channel Name") or "").strip()
            handle = (row.get("Handle") or "").strip()
            channel_id = (row.get("Channel ID") or "").strip()

            if not name or not channel_id:
                continue

            channels.append({
                "name": name,
                "handle": handle,
                "channel_id": channel_id,
            })
    return channels


def load_actors(path: str):
    actors = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            english_name = (row.get("English Name") or "").strip()
            chinese_name = (row.get("Chinese Name") or "").strip()

            if not english_name or not chinese_name:
                continue

            actors.append({
                "english_name": english_name,
                "chinese_name": chinese_name,
            })
    return actors


def normalize_webhook_url(raw_value: str | None) -> str:
    if raw_value is None:
        raise ValueError("Missing DISCORD_WEBHOOK_URL in environment.")

    webhook_url = raw_value.strip()

    # Jeśli ktoś wkleił całą linię z .env zamiast samego URL
    if webhook_url.startswith("DISCORD_WEBHOOK_URL="):
        webhook_url = webhook_url.split("=", 1)[1].strip()

    # Usuń wszystkie whitespace, także ukryte newline/tab w środku URL
    webhook_url = re.sub(r"\s+", "", webhook_url)

    # Usuń otaczające cudzysłowy
    if (
        (webhook_url.startswith('"') and webhook_url.endswith('"'))
        or (webhook_url.startswith("'") and webhook_url.endswith("'"))
    ):
        webhook_url = webhook_url[1:-1]

    # Usuń otaczające < >
    if webhook_url.startswith("<") and webhook_url.endswith(">"):
        webhook_url = webhook_url[1:-1]

    webhook_url = webhook_url.strip()

    if not webhook_url:
        raise ValueError("DISCORD_WEBHOOK_URL is empty.")

    parsed = urlparse(webhook_url)

    if parsed.scheme not in ("https", "http"):
        raise ValueError(
            "DISCORD_WEBHOOK_URL has invalid scheme. It should start with https://"
        )

    if not parsed.netloc:
        raise ValueError("DISCORD_WEBHOOK_URL has no hostname.")

    valid_hosts = {
        "discord.com",
        "discordapp.com",
        "ptb.discord.com",
        "canary.discord.com",
    }

    if parsed.netloc not in valid_hosts:
        raise ValueError(
            f"DISCORD_WEBHOOK_URL has unexpected host: {parsed.netloc}"
        )

    if "/api/webhooks/" not in parsed.path:
        raise ValueError(
            "DISCORD_WEBHOOK_URL does not look like a Discord webhook URL."
        )

    return webhook_url


def fetch_feed(channel_id: str):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.text


def parse_feed(xml_text: str):
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }

    root = ET.fromstring(xml_text)
    entries = []

    for entry in root.findall("atom:entry", ns):
        video_id = entry.findtext("yt:videoId", default="", namespaces=ns).strip()
        title = entry.findtext("atom:title", default="", namespaces=ns).strip()
        published = entry.findtext("atom:published", default="", namespaces=ns).strip()

        link_el = entry.find("atom:link", ns)
        link = link_el.attrib.get("href", "").strip() if link_el is not None else ""

        entries.append({
            "video_id": video_id,
            "title": title,
            "published": published,
            "link": link,
        })

    return entries


def find_matched_actors(title: str, actors: list[dict]):
    matches = []

    for actor in actors:
        chinese_name = actor["chinese_name"]
        if chinese_name and chinese_name in title:
            matches.append(actor)

    return matches


def format_actor_list(matches: list[dict]) -> str:
    return ", ".join(
        f"{actor['english_name']} ({actor['chinese_name']})"
        for actor in matches
    )


def format_matched_text(matches: list[dict]) -> str:
    return ", ".join(actor["chinese_name"] for actor in matches)


def load_state(path: str):
    if not os.path.exists(path):
        return {
            "initialized": False,
            "alerted_video_ids": {}
        }

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, state: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_discord_alert(webhook_url: str, channel_name: str, item: dict, matches: list[dict]):
    actors_text = format_actor_list(matches)
    matched_text = format_matched_text(matches)

    if len(matches) == 1:
        title = f"YouTube match - {actors_text}"
    else:
        title = f"YouTube match - {len(matches)} actors found"

    payload = {
        "embeds": [
            {
                "title": title,
                "url": item["link"],
                "description": item["title"],
                "fields": [
                    {
                        "name": "Actors",
                        "value": actors_text,
                        "inline": False
                    },
                    {
                        "name": "Channel",
                        "value": channel_name,
                        "inline": True
                    },
                    {
                        "name": "Published",
                        "value": item["published"],
                        "inline": True
                    },
                    {
                        "name": "Matched text",
                        "value": matched_text,
                        "inline": False
                    },
                    {
                        "name": "Link",
                        "value": item["link"],
                        "inline": False
                    },
                ]
            }
        ]
    }

    response = requests.post(webhook_url, json=payload, timeout=20)

    if not (200 <= response.status_code < 300):
        raise RuntimeError(f"Discord returned {response.status_code}: {response.text}")


def main():
    load_dotenv()

    webhook_url = normalize_webhook_url(os.getenv("DISCORD_WEBHOOK_URL"))

    channels = load_channels(CHANNELS_FILE)
    actors = load_actors(ACTORS_FILE)
    state = load_state(STATE_FILE)

    if not channels:
        print("No channels found in channels.csv")
        return

    if not actors:
        print("No actors found in actors.csv")
        return

    print(f"Loaded channels: {len(channels)}")
    print(f"Loaded actors:   {len(actors)}")
    print(f"Initialized:     {state['initialized']}")
    print("Webhook URL validated.")
    print()

    total_matches_found = 0
    total_alerts_sent = 0
    total_seeded = 0

    for channel in channels:
        print("=" * 80)
        print(f"Channel: {channel['name']}")
        print(f"Handle: @{channel['handle']}")
        print(f"Channel ID: {channel['channel_id']}")
        print("-" * 80)

        try:
            xml_text = fetch_feed(channel["channel_id"])
            entries = parse_feed(xml_text)

            if not entries:
                print("No entries found in feed.")
                print()
                continue

            for item in entries:
                matches = find_matched_actors(item["title"], actors)

                if not matches:
                    continue

                total_matches_found += 1
                video_id = item["video_id"]

                if video_id in state["alerted_video_ids"]:
                    print(f"SKIP already alerted: {video_id} | {item['title']}")
                    continue

                if not state["initialized"]:
                    state["alerted_video_ids"][video_id] = {
                        "channel": channel["name"],
                        "title": item["title"],
                        "published": item["published"],
                        "matched_actors": format_actor_list(matches),
                        "link": item["link"],
                    }
                    total_seeded += 1
                    print(f"SEED first run: {video_id} | {format_actor_list(matches)}")
                    continue

                send_discord_alert(
                    webhook_url=webhook_url,
                    channel_name=channel["name"],
                    item=item,
                    matches=matches,
                )

                state["alerted_video_ids"][video_id] = {
                    "channel": channel["name"],
                    "title": item["title"],
                    "published": item["published"],
                    "matched_actors": format_actor_list(matches),
                    "link": item["link"],
                }

                total_alerts_sent += 1
                print(f"ALERT sent: {video_id} | {format_actor_list(matches)}")

            print()

        except Exception as e:
            print(f"Error: {e}")
            print()

    if not state["initialized"]:
        state["initialized"] = True
        print("First run complete - state initialized, no Discord alerts sent.")
    else:
        print(f"Run complete - Discord alerts sent: {total_alerts_sent}")

    print(f"Matched entries found this run: {total_matches_found}")
    print(f"Seeded on first run:           {total_seeded}")

    save_state(STATE_FILE, state)


if __name__ == "__main__":
    main()
