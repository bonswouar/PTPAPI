#!/usr/bin/env python
import argparse
import logging

from urllib.parse import parse_qs, urlparse

import requests

import ptpapi

from ptpapi.config import config


def main():
    parser = argparse.ArgumentParser(
        description="Attempt to find torrents to reseed on PTP from other sites"
    )
    parser.add_argument("-i", "--id", help="Only full PTP links for now", nargs="*")
    parser.add_argument(
        "--debug",
        help="Print lots of debugging statements",
        action="store_const",
        dest="loglevel",
        const=logging.DEBUG,
        default=logging.WARNING,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        help="Be verbose",
        action="store_const",
        dest="loglevel",
        const=logging.INFO,
    )
    parser.add_argument(
        "-l",
        "--limit",
        help="Limit need-for-seed results to N movies",
        default=100,
        type=int,
    )
    parser.add_argument(
        "-s", "--search", help="Allow filtering the need-for-seed results", default=None
    )
    parser.add_argument(
        "-r",
        "--required-remote-seeds",
        help="The number of seeds required on the remote site",
        default=1,
        type=int,
    )
    parser.add_argument(
        "-m",
        "--min-ptp-seeds",
        help="Set the minimum number of seeds before a reseed will happen",
        default=0,
        type=int,
    )
    args = parser.parse_args()

    logging.basicConfig(level=args.loglevel)
    logger = logging.getLogger("reseed-machine")

    logger.info("Logging into PTP")
    ptp = ptpapi.login()

    if args.id:
        movies = args.id
    else:
        filters = {}
        if args.search:
            for arg in args.search.split(","):
                filters[arg.split("=")[0]] = arg.split("=")[1]
        movies = [t["Link"] for t in ptp.need_for_seed(filters)][: args.limit]

    for i in movies:
        ptp_movie = None
        if "://passthepopcorn.me" in i:
            parsed_url = parse_qs(urlparse(i).query)
            ptp_movie = ptpapi.Movie(ID=parsed_url["id"][0])
            torrent_id = int(parsed_url.get("torrentid", ["0"])[0])

        if ptp_movie is None:
            logger.error("Could not figure out ID '{0}'".format(i))
        else:
            try:
                ptp_movie["ImdbId"]
            except KeyError:
                logger.warning("ImdbId not found from '{0}', skipping".format(i))
                continue
            if ptp_movie["ImdbId"]:
                find_match(ptp_movie, torrent_id)


def match_results(ptp_result: dict, other_result: dict) -> dict:
    logger = logging.getLogger(__name__)
    percent_diff = 1
    if other_result["protocol"] == "torrent":
        size_diff = round(
            abs(((other_result["size"] / ptp_result["size"]) - 1) * 100), 2
        )
        if (
            other_result["indexer"] != "PassThePopcorn"
            and other_result["seeders"] > 0
            and 0 <= size_diff < percent_diff
        ):
            logger.info(
                "torrent match: %s (%s), with %.2f%% size diff",
                other_result["indexer"],
                other_result["size"],
                size_diff,
            )
            return other_result
        elif other_result["seeders"] > 0:
            logger.debug(
                "torrent size mismatch: %s (%s), with %.2f%% size diff",
                other_result["indexer"],
                other_result["size"],
                size_diff,
            )
    elif other_result["protocol"] == "usenet":
        # Usenet sizes vary wildly based on PAR2 levels,
        # etc, so size comparisons aren't as useful
        size_diff = 0
        # Check for a couple trivial changes
        # TODO: Replace with "edit distance > 1" check from difflib?
        sortTitles = [
            ptp_result["sortTitle"],
            ptp_result["sortTitle"].replace("blu ray", "bluray"),
        ]
        if other_result["sortTitle"] in sortTitles:
            logger.info(
                "usenet match: %s (%s)", other_result["indexer"], other_result["title"]
            )
            return other_result
        else:
            logger.debug(
                "usenet title mismatch: %s",
                other_result["title"],
            )
    return {}

def bytes_to_human(b: int):
    for count in ['B','KiB','MiB','GiB']:
        if b < 1024.0:
           return "%3.1f %s" % (b, count)
        b /= 1024.0

def find_match(ptp_movie, torrent_id=0):
    logger = logging.getLogger(__name__)
    session = requests.Session()
    session.headers.update({"X-Api-Key": config.get("Prowlarr", "api_key")})
    resp = session.get(
        config.get("Prowlarr", "url") + "api/v1/search",
        params={
            "query": "{ImdbId:" + ptp_movie["ImdbId"] + "}",
            "categories": "2000",
            "type": "movie",
        },
    ).json()

    for result in resp:
        if result["indexer"] == "PassThePopcorn" and result["seeders"] == 0:
            if torrent_id and f"torrentid={torrent_id}" not in result.get(
                "infoUrl", ""
            ):
                continue
            logger.debug(
                "Working dead torrent %s (size %s (%s), sortTitle '%s')",
                result["title"],
                result["size"],
                bytes_to_human(int(result["size"])),
                result["sortTitle"],
            )
            download = {}
            for other_result in resp:
                download = match_results(result, other_result)
                if download:
                    break
            # If no match found, search again by release title
            if not download:
                release_title_resp = session.get(
                    config.get("Prowlarr", "url") + "api/v1/search",
                    params={
                        "query": result["title"],
                        "type": "search",
                        "limit": 100,
                    },
                ).json()
                for release_result in release_title_resp:
                    download = match_results(result, release_result)
                    if download:
                        break

            if download:
                logger.info(
                    "Downloading %s (%s) from %s",
                    download["title"],
                    download["guid"],
                    download["indexer"],
                )
                r = session.post(
                    config.get("Prowlarr", "url") + "api/v1/search",
                    json={
                        "guid": download["guid"],
                        "indexerId": download["indexerId"],
                    },
                )
                r.raise_for_status()


if __name__ == "__main__":
    main()
