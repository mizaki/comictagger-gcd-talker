"""
Grand Comics Database™ (https://www.comics.org) information source
"""
# Copyright comictagger team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sqlite3
from typing import Any, Callable
from urllib.parse import urljoin

import requests
import settngs
from bs4 import BeautifulSoup
from comicapi import utils
from comicapi.genericmetadata import ComicSeries, GenericMetadata, TagOrigin
from comicapi.issuestring import IssueString
from comictalker.comiccacher import ComicCacher
from comictalker.comiccacher import Issue as CCIssue
from comictalker.comiccacher import Series as CCSeries
from comictalker.comictalker import ComicTalker, TalkerDataError, TalkerNetworkError
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


class GCDSeries(TypedDict, total=False):
    count_of_issues: int | None
    notes: str
    id: int
    name: str
    sort_name: str | None
    publisher_name: str | None
    format: str
    year_began: int | None
    year_ended: int | None
    image: str | None
    cover_downloaded: bool  # Store for option switching. If option was False = False etc. Means can mark as complete


class GCDIssue(TypedDict, total=False):
    id: int
    key_date: str
    number: str
    issue_title: str
    series_id: int
    issue_notes: str
    volume: int
    maturity_rating: str
    country: str
    country_iso: str
    story_ids: list[str]  # CSV int - Used to gather credits from gcd_story_credit
    characters: list[GCDCredit]
    language: str
    language_iso: str
    story_titles: list[str]  # combined gcd_story title_inferred and type_id for display title
    genres: list[str]  # gcd_story semicolon separated
    synopses: list[str]  # combined gcd_story synopsis
    image: str
    alt_image_urls: list[str]  # generated via variant_of_id
    credits: list[
        GCDCredit
    ]  # gcd_issue_credit and gcd_story_credit (using story_id) and gcd_credit_type and gcd_creator
    covers_downloaded: bool  # Store for option switching. If option was False = False etc. Means can mark as complete


class GCDCredit(TypedDict):
    name: str
    gcd_role: str


class GCDTalkerExt(ComicTalker):
    name: str = "Grand Comics Database"
    id: str = "gcd"
    website: str = "https://www.comics.org/"
    logo_url: str = "https://files1.comics.org/static/img/gcd_logo.aaf0e64616e2.png"
    attribution: str = f"Data from <a href='{website}'>{name}</a> (<a href='http://creativecommons.org/licenses/by/3.0/'>CCA license</a>)"

    def __init__(self, version: str, cache_folder: pathlib.Path):
        super().__init__(version, cache_folder)
        # Default settings
        self.db_file: str = ""
        self.use_series_start_as_volume: bool = False
        self.download_gui_covers: bool = False
        self.download_tag_covers: bool = False

        self.has_issue_id_type_id_index: bool = False

    def register_settings(self, parser: settngs.Manager) -> None:
        parser.add_setting(
            "--gcd-use-series-start-as-volume",
            default=False,
            action=argparse.BooleanOptionalAction,
            display_name="Use series start as volume",
            help="Use the series start year as the volume number",
        )
        parser.add_setting(
            "--gcd-gui-covers",
            default=False,
            action=argparse.BooleanOptionalAction,
            display_name="Attempt to download covers for the GUI",
            help="Attempt to download covers for use in series and issue list windows",
        )
        parser.add_setting(
            "--gcd-tag-covers",
            default=False,
            action=argparse.BooleanOptionalAction,
            display_name="Attempt to download covers for auto-tagging",
            help="Attempt to download covers for use with auto-tagging",
        )
        parser.add_setting(f"--{self.id}-key", file=False, cmdline=False)
        parser.add_setting(
            f"--{self.id}-url",
            display_name="SQLite DB location URI",
            help="The path and filename of the GCD SQLite file",
        )

    def parse_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        settings = super().parse_settings(settings)

        self.use_series_start_as_volume = settings["gcd_use_series_start_as_volume"]
        self.download_gui_covers = settings["gcd_gui_covers"]
        self.download_tag_covers = settings["gcd_tag_covers"]
        self.db_file = settings["gcd_url"]
        return settings

    def check_status(self, settings: dict[str, Any]) -> tuple[str, bool]:
        try:
            with sqlite3.connect(settings["gcd_url"]) as con:
                con.row_factory = sqlite3.Row
                con.text_factory = str
                cur = con.cursor()
                cur.execute("SELECT * FROM gcd_credit_type")

                cur.fetchone()

            return "The DB access test was successful", True

        except sqlite3.Error:
            return "DB access failed", False

    def check_create_index(self) -> None:
        # Without this index the current issue list query is VERY slow
        if not self.has_issue_id_type_id_index:
            try:
                with sqlite3.connect(self.db_file) as con:
                    con.row_factory = sqlite3.Row
                    con.text_factory = str
                    cur = con.cursor()

                    cur.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'issue_id_on_type_id';")

                    if cur.fetchone():
                        self.has_issue_id_type_id_index = True
                    else:
                        # Create the index
                        cur.execute("CREATE INDEX issue_id_on_type_id ON gcd_story (type_id, issue_id);")
                        self.has_issue_id_type_id_index = True

            except sqlite3.DataError as e:
                logger.debug(f"DB data error: {e}")
                raise TalkerDataError(self.name, 1, str(e))
            except sqlite3.Error as e:
                logger.debug(f"DB error: {e}")
                raise TalkerDataError(self.name, 0, str(e))

    def check_db_filename_not_empty(self):
        if not self.db_file:
            raise TalkerDataError(self.name, 3, "Database path is empty, specify a path and filename!")

    def search_for_series(
        self,
        series_name: str,
        callback: Callable[[int, int], None] | None = None,
        refresh_cache: bool = False,
        literal: bool = False,
        series_match_thresh: int = 90,
    ) -> list[ComicSeries]:
        search_series_name = utils.sanitize_title(series_name, literal)
        logger.info(f"{self.name} searching: {search_series_name}")

        cvc = ComicCacher(self.cache_folder, self.version)
        if not refresh_cache and not literal:
            cached_search_results = cvc.get_search_results(self.id, series_name)

            if len(cached_search_results) > 0:
                return self._format_search_results([json.loads(x[0].data) for x in cached_search_results])

        results = []

        self.check_db_filename_not_empty()
        try:
            with sqlite3.connect(self.db_file) as con:
                con.row_factory = sqlite3.Row
                con.text_factory = str
                cur = con.cursor()

                cur.execute(
                    "SELECT gcd_series.id AS 'id', gcd_series.name AS 'series_name', "
                    "gcd_series.sort_name AS 'sort_name', gcd_series.notes AS 'notes', "
                    "gcd_series.year_began AS 'year_began', gcd_series.year_ended AS 'year_ended', "
                    "gcd_series.issue_count AS 'issue_count', gcd_publisher.name AS 'publisher_name' "
                    "FROM gcd_publisher "
                    "LEFT JOIN gcd_series ON gcd_series.publisher_id=gcd_publisher.id "
                    "WHERE gcd_series.name LIKE ?",
                    [series_name],
                )
                rows = cur.fetchall()

                # now process the results
                for record in rows:
                    result = GCDSeries(
                        id=record["id"],
                        name=record["series_name"],
                        sort_name=record["sort_name"],
                        notes=record["notes"],
                        year_began=record["year_began"],
                        year_ended=record["year_ended"],
                        count_of_issues=record["issue_count"],
                        publisher_name=record["publisher_name"],
                        format="",
                        image="",
                    )

                    results.append(result)

        except sqlite3.DataError as e:
            logger.debug(f"DB data error: {e}")
            raise TalkerDataError(self.name, 1, str(e))
        except sqlite3.Error as e:
            logger.debug(f"DB error: {e}")
            raise TalkerDataError(self.name, 0, str(e))

        # Cache because fetching the cache will require all records
        cvc.add_search_results(
            self.id,
            series_name,
            [CCSeries(id=str(x["id"]), data=json.dumps(x).encode("utf-8")) for x in results],
            False,
        )

        # Format result to ComicIssue
        formatted_search_results = self._format_search_results(results)

        return formatted_search_results

    def fetch_comic_data(
        self, issue_id: str | None = None, series_id: str | None = None, issue_number: str = ""
    ) -> GenericMetadata:
        self.check_db_filename_not_empty()

        comic_data = GenericMetadata()
        if issue_id:
            comic_data = self._fetch_issue_data_by_issue_id(int(issue_id))
        elif issue_number and series_id:
            comic_data = self._fetch_issue_data(int(series_id), issue_number)

        return comic_data

    def fetch_issues_in_series(self, series_id: str) -> list[GenericMetadata]:
        cvc = ComicCacher(self.cache_folder, self.version)
        cached_series_issues_result = cvc.get_series_issues_info(series_id, self.id)

        series = self._fetch_series_data(int(series_id))

        # Is this a sane check? Could count_of_issues be entered before all issues?
        if len(cached_series_issues_result) == series["count_of_issues"]:
            return [
                (self._map_comic_issue_to_metadata(json.loads(x[0].data), series)) for x in cached_series_issues_result
            ]

        results: list[GCDIssue] = []

        self.check_create_index()

        try:
            with sqlite3.connect(self.db_file) as con:
                con.row_factory = sqlite3.Row
                con.text_factory = str
                cur = con.cursor()
                # TODO "... AND gcd_story.type_id=19" makes the query very slow
                cur.execute(
                    "SELECT gcd_issue.id AS 'id', gcd_issue.key_date AS 'key_date', gcd_issue.number AS 'number', "
                    "gcd_issue.title AS 'issue_title', gcd_issue.series_id AS 'series_id', "
                    "GROUP_CONCAT(CASE WHEN gcd_story.title IS NOT NULL AND gcd_story.title != '' THEN "
                    "gcd_story.title END, '\n') AS 'story_titles' "
                    "FROM gcd_issue "
                    "LEFT JOIN gcd_story ON gcd_story.issue_id=gcd_issue.id "
                    "WHERE gcd_issue.series_id=? AND gcd_story.type_id=19 "
                    "GROUP BY gcd_issue.id",
                    [int(series_id)],
                )
                rows = cur.fetchall()

                # Compare issue count to rows? Is it possible for a mix of issues with stories and not?
                if rows:
                    # now process the results
                    for record in rows:
                        results.append(self._format_gcd_issue(record))

                # It's possible an issue doesn't have "stories" so the above will be empty
                else:
                    # Select only issue data without "stories"
                    cur.execute(
                        "SELECT gcd_issue.id AS 'id', gcd_issue.key_date AS 'key_date', gcd_issue.number AS 'number', "
                        "gcd_issue.title AS 'issue_title', gcd_issue.series_id AS 'series_id' "
                        "FROM gcd_issue "
                        "WHERE gcd_issue.series_id=?",
                        [int(series_id)],
                    )

                    rows = cur.fetchall()

                    # now process the results
                    for record in rows:
                        results.append(self._format_gcd_issue(record))

        except sqlite3.DataError as e:
            logger.debug(f"DB data error: {e}")
            raise TalkerDataError(self.name, 1, str(e))
        except sqlite3.Error as e:
            logger.debug(f"DB error: {e}")
            raise TalkerDataError(self.name, 0, str(e))

        series = self._fetch_series_data(int(series_id))

        cvc.add_issues_info(
            self.id,
            [CCIssue(id=str(x["id"]), series_id=series_id, data=json.dumps(x).encode("utf-8")) for x in results],
            False,
        )

        # Format to expected output
        formatted_series_issues_result = [self._map_comic_issue_to_metadata(x, series) for x in results]

        return formatted_series_issues_result

    def fetch_issues_by_series_issue_num_and_year(
        self, series_id_list: list[str], issue_number: str, year: int | None
    ) -> list[GenericMetadata]:
        # Example CLI result: Batman (1940) #55 [DC Comics] (10/1949) - The Case of the 48 Jokers
        results: list[GenericMetadata] = []
        year_search = "%"

        if year:
            year_search = str(year) + "%"

        self.check_create_index()

        try:
            with sqlite3.connect(self.db_file) as con:
                con.row_factory = sqlite3.Row
                con.text_factory = str
                cur = con.cursor()

                for vid in series_id_list:
                    series = self._fetch_series_data(int(vid))

                    # TODO Use cache if possible? if row == cache else? only if download covers?
                    # Use cache too for image url
                    """cvc = ComicCacher(self.cache_folder, self.version)
                    cached_series_issues_result = cvc.get_series_issues_info(series_id, self.id)

                    series = self._fetch_series_data(int(series_id))[0]

                    if len(cached_series_issues_result) == series.count_of_issues:
                        return [
                            (self._map_comic_issue_to_metadata(json.loads(x[0].data), series), x[1])
                            for x in cached_series_issues_result
                        ]"""

                    cur.execute(
                        "SELECT gcd_issue.id AS 'id', gcd_issue.key_date AS 'key_date', gcd_issue.number AS 'number', "
                        "gcd_issue.title AS 'issue_title', gcd_issue.series_id AS 'series_id', "
                        "GROUP_CONCAT(CASE WHEN gcd_story.title IS NOT NULL AND gcd_story.title != '' THEN "
                        "gcd_story.title END, '\n') AS 'story_titles' "
                        "FROM gcd_issue "
                        "LEFT JOIN gcd_story ON gcd_story.issue_id=gcd_issue.id "
                        "WHERE gcd_issue.series_id=? AND gcd_story.type_id=19 "
                        "AND gcd_issue.number=? AND gcd_issue.key_date LIKE ? "
                        "GROUP BY gcd_issue.id",
                        [vid, issue_number, year_search],
                    )

                    rows = cur.fetchall()

                    if rows:
                        # TODO add all issues found in a series to a list so it can be added to cache?
                        for record in rows:
                            issue = self._format_gcd_issue(record)

                            # Download covers for matching
                            if self.download_tag_covers:
                                image, variants = self._find_issue_images(issue["id"])
                                issue["image"] = image
                                issue["alt_image_urls"] = variants

                            results.append(self._map_comic_issue_to_metadata(issue, series))

                    # It's possible an issue doesn't have "stories" so the above will be empty
                    else:
                        # Select only issue data without "stories"
                        cur.execute(
                            "SELECT gcd_issue.id AS 'id', gcd_issue.key_date AS 'key_date', "
                            "gcd_issue.number AS 'number', gcd_issue.title AS 'issue_title', "
                            "gcd_issue.series_id AS 'series_id' "
                            "FROM gcd_issue "
                            "WHERE gcd_issue.series_id=? AND gcd_issue.number=? AND gcd_issue.key_date LIKE ? ",
                            [vid, issue_number, str(year) + "%"],
                        )
                        rows = cur.fetchall()

                        # now process the results
                        for record in rows:
                            issue = self._format_gcd_issue(record)

                            # Download covers for matching
                            if self.download_tag_covers:
                                image, variants = self._find_issue_images(issue["id"])
                                issue["image"] = image
                                issue["alt_image_urls"] = variants

                            results.append(self._map_comic_issue_to_metadata(issue, series))

        except sqlite3.DataError as e:
            logger.debug(f"DB data error: {e}")
            raise TalkerDataError(self.name, 1, str(e))
        except sqlite3.Error as e:
            logger.debug(f"DB error: {e}")
            raise TalkerDataError(self.name, 0, str(e))

        return results

    def _find_series_image(self, series_id: int) -> str:
        """Find the id of the first issue and get the image url"""
        issue_id = None
        cover = ""
        try:
            with sqlite3.connect(self.db_file) as con:
                con.row_factory = sqlite3.Row
                con.text_factory = str
                cur = con.cursor()

                cur.execute(
                    "SELECT gcd_series.first_issue_id " "FROM gcd_series " "WHERE gcd_series.id=?",
                    [series_id],
                )
                issue_id = cur.fetchone()[0]

        except sqlite3.DataError as e:
            logger.debug(f"DB data error: {e}")
            raise TalkerDataError(self.name, 1, str(e))
        except sqlite3.Error as e:
            logger.debug(f"DB error: {e}")
            raise TalkerDataError(self.name, 0, str(e))

        if issue_id:
            cover, _ = self._find_issue_images(issue_id)

        return cover

    def _find_issue_images(self, issue_id: int) -> tuple[str, list[str]]:
        """Fetch images for the issue id"""
        cover = ""
        variants = []
        # start_time = time.perf_counter()
        try:
            covers_html = requests.get(f"{self.website}/issue/{issue_id}/cover/4").text
        except requests.exceptions.Timeout:
            logger.debug(f"Connection to {self.website} timed out.")
            raise TalkerNetworkError(self.website, 4)
        except requests.exceptions.RequestException as e:
            logger.debug(f"Request exception: {e}")
            raise TalkerNetworkError(self.website, 0, str(e)) from e

        # end_time = time.perf_counter()
        # print(f"html:  {end_time - start_time}")

        # start_time = time.perf_counter()
        covers_page = BeautifulSoup(covers_html, "html.parser")

        img_list = covers_page.findAll("img", "cover_img")

        for i, image in enumerate(img_list):
            # Strip arbitrary number from end for cache
            src = image.get("src").split("?")[0]
            if i == 0:
                cover = src
            else:
                variants.append(src)
        # end_time = time.perf_counter()
        # print(f"parse:  {end_time - start_time}")
        return cover, variants

    def _find_issue_credits(self, issue_id: int, story_id_list: list[str]) -> list[GCDCredit]:
        credit_results = []
        # First get the issue credits
        try:
            with sqlite3.connect(self.db_file) as con:
                con.row_factory = sqlite3.Row
                con.text_factory = str
                cur = con.cursor()
                cur.execute(
                    "SELECT gcd_issue_credit.credit_name AS 'role', gcd_creator_name_detail.name "
                    "FROM gcd_issue_credit "
                    "INNER JOIN gcd_creator_name_detail ON gcd_issue_credit.creator_id=gcd_creator_name_detail.id "
                    "WHERE gcd_issue_credit.issue_id=?",
                    [issue_id],
                )
                rows = cur.fetchall()

                # now process the results
                for record in rows:
                    result = GCDCredit(
                        name=record[1],
                        gcd_role=record[0],
                    )

                    credit_results.append(result)

                # Get story credits
                for story_id in story_id_list:
                    cur.execute(
                        "SELECT gcd_creator_name_detail.name, gcd_credit_type.name "
                        "FROM gcd_story_credit "
                        "INNER JOIN gcd_credit_type ON gcd_credit_type.id=gcd_story_credit.credit_type_id "
                        "INNER JOIN gcd_creator_name_detail ON gcd_creator_name_detail.id=gcd_story_credit.creator_id "
                        "WHERE gcd_story_credit.story_id=?",
                        [int(story_id)],
                    )
                    rows = cur.fetchall()

                    for record in rows:
                        result = GCDCredit(
                            name=record[0],
                            gcd_role=record[1],
                        )

                        credit_results.append(result)

        except sqlite3.DataError as e:
            logger.debug(f"DB data error: {e}")
            raise TalkerDataError(self.name, 1, str(e))
        except sqlite3.Error as e:
            logger.debug(f"DB error: {e}")
            raise TalkerDataError(self.name, 0, str(e))

        return credit_results

    # Search results and full series data
    def _format_search_results(self, search_results: list[GCDSeries]) -> list[ComicSeries]:
        formatted_results = []
        for record in search_results:
            # TODO Add genres and volume when fields have been added to ComicSeries

            # Option to use sort name?

            formatted_results.append(
                ComicSeries(
                    aliases=[],
                    count_of_issues=record.get("count_of_issues"),
                    count_of_volumes=None,
                    description=record.get("notes"),
                    id=str(record["id"]),
                    image_url=record.get("image", ""),
                    name=record["name"],
                    publisher=record["publisher_name"],
                    genres=[],
                    format=None,
                    start_year=record["year_began"],
                )
            )

        return formatted_results

    def _format_gcd_issue(self, row: sqlite3.Row, complete: bool = False) -> GCDIssue:
        # Convert for attribute access
        row_dict = dict(row)

        if complete:
            return GCDIssue(
                id=row_dict["id"],
                key_date=row_dict["key_date"],
                number=row_dict["number"],
                issue_title=row_dict["issue_title"],
                series_id=row_dict["series_id"],
                issue_notes=row_dict["issue_notes"],
                volume=row_dict["volume"],
                maturity_rating=row_dict["maturity_rating"],
                characters=row_dict["characters"].split("; ") if "characters" in row_dict else "",
                country=row_dict["country"],
                country_iso=row_dict["country_iso"],
                story_ids=row_dict["story_ids"].split("\n")
                if "story_ids" in row_dict and row_dict["story_ids"]
                else [],
                language=row_dict["language"],
                language_iso=row_dict["language_iso"],
                story_titles=row_dict["story_titles"].split("\n")
                if "story_titles" in row_dict and row_dict["story_titles"]
                else [],
                genres=row_dict["genres"].split("\n") if "genres" in row_dict and row_dict["genres"] else [],
                synopses=row_dict["synopses"].split("\n\n") if "synopses" in row_dict and row_dict["synopses"] else [],
                alt_image_urls=[],
                credits=[],
                image="",
            )

        return GCDIssue(
            id=row_dict["id"],
            key_date=row_dict["key_date"],
            number=row_dict["number"],
            issue_title=row_dict["issue_title"],
            series_id=row_dict["series_id"],
            story_titles=row_dict["story_titles"].split("\n")
            if "story_titles" in row_dict and row_dict["story_titles"] is not None
            else [],
            synopses=row_dict["synopses"].split("\n")
            if "synopses" in row_dict and row_dict["synopses"] is not None
            else [],
            image="",
            alt_image_urls=[],
        )

    def fetch_series(self, series_id: str) -> ComicSeries:
        return self._format_search_results([self._fetch_series_data(int(series_id))])[0]

    def _fetch_series_data(self, series_id: int) -> GCDSeries:
        cvc = ComicCacher(self.cache_folder, self.version)
        cached_series = cvc.get_series_info(str(series_id), self.id)

        if cached_series is not None and cached_series[1]:
            # TODO Check cover_downloaded
            return json.loads(cached_series[0].data)

        try:
            with sqlite3.connect(self.db_file) as con:
                con.row_factory = sqlite3.Row
                con.text_factory = str
                cur = con.cursor()

                cur.execute(
                    "SELECT gcd_series.id AS 'id', gcd_series.name AS 'series_name', "
                    "gcd_series.sort_name AS 'sort_name', gcd_series.notes AS 'notes', "
                    "gcd_series.year_began AS 'year_began', gcd_series.year_ended AS 'year_ended', "
                    "gcd_series.issue_count AS 'issue_count', gcd_publisher.name AS 'publisher_name', "
                    "gcd_series.country_id AS 'country_id', gcd_series.language_id AS 'lang_id', "
                    "gcd_series.publishing_format AS 'format', gcd_series.is_current AS 'is_current' "
                    "FROM gcd_publisher "
                    "LEFT JOIN gcd_series ON gcd_series.publisher_id=gcd_publisher.id "
                    "WHERE gcd_series.id=?",
                    [series_id],
                )
                row = cur.fetchone()

                image = ""
                cover_download = False
                if self.download_gui_covers:
                    image = self._find_series_image(series_id)
                    cover_download = True

                # now process the results
                result = GCDSeries(
                    id=row["id"],
                    name=row["series_name"],
                    sort_name=row["sort_name"],
                    notes=row["notes"],
                    year_began=row["year_began"],
                    year_ended=row["year_ended"],
                    count_of_issues=row["issue_count"],
                    publisher_name=row["publisher_name"],
                    format=row["format"],
                    image=image,
                    cover_downloaded=cover_download,
                )

        except sqlite3.DataError as e:
            logger.debug(f"DB data error: {e}")
            raise TalkerDataError(self.name, 1, str(e))
        except sqlite3.Error as e:
            logger.debug(f"DB error: {e}")
            raise TalkerDataError(self.name, 0, str(e))

        if result:
            cvc.add_series_info(self.id, CCSeries(id=str(result["id"]), data=json.dumps(result).encode("utf-8")), True)

        return result

    def _fetch_issue_data(self, series_id: int, issue_number: str) -> GenericMetadata:
        # Find the id of the issue and pass it along
        try:
            with sqlite3.connect(self.db_file) as con:
                con.row_factory = sqlite3.Row
                con.text_factory = str
                cur = con.cursor()

                cur.execute(
                    "SELECT gcd_issue.id AS 'id' "
                    "FROM gcd_issue "
                    "WHERE gcd_issue.series_id=? AND gcd_issue.number=?",
                    [series_id, issue_number],
                )
                row = cur.fetchone()

                if row["id"]:
                    return self._fetch_issue_data_by_issue_id(row["id"])

        except sqlite3.DataError as e:
            logger.debug(f"DB data error: {e}")
            raise TalkerDataError(self.name, 1, str(e))
        except sqlite3.Error as e:
            logger.debug(f"DB error: {e}")
            raise TalkerDataError(self.name, 0, str(e))

        return GenericMetadata()

    def _fetch_issue_data_by_issue_id(self, issue_id: int) -> GenericMetadata:
        issue = self._fetch_issue_by_issue_id(issue_id)
        series = self._fetch_series_data(issue["series_id"])

        return self._map_comic_issue_to_metadata(issue, series)

    def _fetch_issue_by_issue_id(self, issue_id: int) -> GCDIssue:
        cvc = ComicCacher(self.cache_folder, self.version)
        cached_issue = cvc.get_issue_info(issue_id, self.id)

        if cached_issue and cached_issue[1]:
            # TODO covers_downloaded
            return json.loads(cached_issue[0].data)

        # Need this one?
        self.check_create_index()

        try:
            with sqlite3.connect(self.db_file) as con:
                con.row_factory = sqlite3.Row
                con.text_factory = str
                cur = con.cursor()

                # TODO break genres into it's own query to use distinct?
                # TODO break format out to make something sensible from it?
                cur.execute(
                    "SELECT gcd_issue.id AS 'id', gcd_issue.key_date AS 'key_date', gcd_issue.number AS 'number', "
                    "gcd_issue.title AS 'issue_title', gcd_issue.series_id AS 'series_id', "
                    "gcd_issue.notes AS 'issue_notes', gcd_issue.volume AS 'volume', "
                    "gcd_issue.rating AS 'maturity_rating', gcd_story.characters AS 'characters', "
                    "stddata_country.name AS 'country', stddata_country.code AS 'country_iso', "
                    "stddata_language.name AS 'language', stddata_language.code AS 'language_iso', "
                    "GROUP_CONCAT(CASE WHEN gcd_story.title IS NOT NULL AND gcd_story.title != '' THEN "
                    "gcd_story.title END, '\n') AS 'story_titles',"
                    "GROUP_CONCAT(CASE WHEN gcd_story.genre IS NOT NULL AND gcd_story.genre != '' THEN "
                    "gcd_story.genre END, '\n') AS 'genres',"
                    "GROUP_CONCAT(CASE WHEN gcd_story.synopsis IS NOT NULL AND gcd_story.synopsis != '' THEN "
                    "gcd_story.synopsis END,'\n\n') AS 'synopses', "
                    "GROUP_CONCAT(CASE WHEN gcd_story.id IS NOT NULL AND gcd_story.id != '' THEN "
                    "gcd_story.id END, '\n') AS 'story_ids' "
                    "FROM gcd_issue "
                    "LEFT JOIN gcd_story ON gcd_story.issue_id=gcd_issue.id "
                    "LEFT JOIN gcd_indicia_publisher ON gcd_issue.indicia_publisher_id=gcd_indicia_publisher.id "
                    "LEFT JOIN gcd_series ON gcd_issue.series_id=gcd_series.id "
                    "LEFT JOIN stddata_country ON gcd_indicia_publisher.country_id=stddata_country.id "
                    "LEFT JOIN stddata_language ON gcd_series.language_id=stddata_language.id "
                    "WHERE gcd_issue.id=? AND gcd_story.type_id=19 "
                    "GROUP BY gcd_issue.id",
                    [issue_id],
                )
                row = cur.fetchone()

                if row:
                    issue_result = self._format_gcd_issue(row, True)
                    # It's possible an issue doesn't have "stories" so the above will be empty
                else:
                    # Select only issue data without "stories"
                    cur.execute(
                        "SELECT gcd_issue.id AS 'id', gcd_issue.key_date AS 'key_date', gcd_issue.number AS 'number', "
                        "gcd_issue.title AS 'issue_title', gcd_issue.series_id AS 'series_id', "
                        "gcd_issue.notes AS 'issue_notes', gcd_issue.volume AS 'volume', "
                        "gcd_issue.rating AS 'maturity_rating', "
                        "stddata_country.name AS 'country', stddata_country.code AS 'country_iso', "
                        "stddata_language.name AS 'language', stddata_language.code AS 'language_iso' "
                        "FROM gcd_issue "
                        "LEFT JOIN gcd_indicia_publisher ON gcd_issue.indicia_publisher_id=gcd_indicia_publisher.id "
                        "LEFT JOIN gcd_series ON gcd_issue.series_id=gcd_series.id "
                        "LEFT JOIN stddata_country ON gcd_indicia_publisher.country_id=stddata_country.id "
                        "LEFT JOIN stddata_language ON gcd_series.language_id=stddata_language.id "
                        "WHERE gcd_issue.id=?",
                        [issue_id],
                    )

                    row = cur.fetchone()

                    # Still may not have an issue?
                    if row:
                        issue_result = self._format_gcd_issue(row, True)

        except sqlite3.DataError as e:
            logger.debug(f"DB data error: {e}")
            raise TalkerDataError(self.name, 1, str(e))
        except sqlite3.Error as e:
            logger.debug(f"DB error: {e}")
            raise TalkerDataError(self.name, 0, str(e))

        # Add credits
        issue_result["credits"] = self._find_issue_credits(issue_id, issue_result["story_ids"])

        # Add variant covers
        if self.download_gui_covers or self.download_tag_covers:
            image, variants = self._find_issue_images(issue_result["id"])
            issue_result["image"] = image
            issue_result["alt_image_urls"] = variants

        # How to handle covers downloaded or not? There could be no cover so "" doesn't mean it wasn't tried. Add flag?
        cvc.add_issues_info(
            self.id,
            [
                CCIssue(
                    id=str(issue_result["id"]),
                    series_id=str(issue_result["series_id"]),
                    data=json.dumps(issue_result).encode("utf-8"),
                )
            ],
            True,
        )

        return issue_result

    def _map_comic_issue_to_metadata(self, issue: GCDIssue, series: GCDSeries) -> GenericMetadata:
        md = GenericMetadata(
            tag_origin=TagOrigin(self.id, self.name),
            issue_id=utils.xlate(issue["id"]),
            series_id=utils.xlate(series["id"]),
            publisher=utils.xlate(series.get("publisher_name")),
            issue=utils.xlate(IssueString(issue.get("number")).as_string()),
            series=utils.xlate(series["name"]),
        )

        md.cover_image = issue.get("image")
        md.alternate_images = issue.get("alt_image_urls")

        if issue.get("characters"):
            # Logan [disambiguation: Wolverine] - (name) James Howlett
            md.characters = issue["characters"]

        # TODO story_arcs can be taken from story_titles?
        # story_list: list = []

        if issue.get("credits"):
            for person in issue["credits"]:
                md.add_credit(person["name"], person["gcd_role"])

        # TODO series and title aliases? Not so much aliases but different languages
        title = ""
        if issue.get("issue_title"):
            md.title = issue["issue_title"]
        elif not title and issue.get("story_titles"):
            md.title = "; ".join(issue["story_titles"])

        md.genres = issue.get("genres")

        # TODO price?

        # TODO Figure out number of issues is valid? Use cancelled/ended?
        md.issue_count = utils.xlate_int(series["count_of_issues"])

        # TODO Merge if notes and synopses, option?
        md.description = issue.get("issue_notes")

        if len(issue["synopses"]) == len(issue["story_titles"]):
            # Init as string for concat
            md.description = ""
            # Will presume titles go with synopsis
            for i, title in enumerate(issue["story_titles"]):
                if title and issue["synopses"][i]:
                    md.description += f"{title}: {issue['synopses'][i]}\n\n"
        else:
            md.description = "\n\n".join(issue["synopses"])

        md.web_link = urljoin(self.website, f"issue/{issue['id']}")

        md.volume = utils.xlate_int(issue.get("volume"))
        if self.use_series_start_as_volume:
            md.volume = series["year_began"]

        if issue.get("key_date"):
            md.day, md.month, md.year = utils.parse_date_str(issue.get("key_date"))
        elif series["year_began"]:
            md.year = utils.xlate_int(series["year_began"])

        md.language = issue.get("language_iso")
        md.country = issue.get("country")

        # The publishing_format field is a free-text mess
        md.format = series.get("format")

        md.maturity_rating = issue.get("maturity_rating")

        return md
