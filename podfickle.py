import argparse
import logging
import os
import re
from dataclasses import dataclass
from time import sleep
from typing import Callable, Iterable, List, Optional, Type, TypeVar
from urllib.parse import urljoin, urlparse, urlunparse

import jinja2
from dotenv import load_dotenv
from pydantic import BaseModel
from selenium.common.exceptions import (
    ElementNotInteractableException,
    NoSuchElementException,
)
from selenium.webdriver import Chrome, ChromeOptions
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.wait import WebDriverWait

_log = logging.getLogger(__file__)

_T = TypeVar("_T")


_RX_PART_N_OF_THE = re.compile(r"Part (\d+) of the")


class Urls(BaseModel):
    # Anchor FM episode url, as found via the UI
    anchor_fm: str
    # Google Drive sharing URL
    google_drive: str
    # Mediafire sharing URL
    mediafire: str
    # Spotify episode url, as found by 'Share > Copy Episode Link'
    spotify: str

    def clean_urls(self) -> "Urls":
        """Clean any optional params from the given urls."""
        return Urls(
            spotify=_clean_url_path(self.spotify),
            anchor_fm=_clean_url_path(self.anchor_fm),
            google_drive=_clean_url_path(self.google_drive),
            mediafire=_clean_url_path(self.mediafire),
        )

    def spotify_embed(self) -> str:
        return self.spotify.replace("episode", "embed-podcast/episode")


class ParentConfig(BaseModel):
    # Parent work id
    work_id: str
    # Whether parent work is considered 'explicit'
    explicit: bool
    # Parent author tumblr name.
    tumblr: Optional[str]

    def tumblr_url(self) -> str:
        assert self.tumblr is not None, ".parent.tumblr is not set or null"
        return _tumblr_url(self.tumblr)


class Config(BaseModel):
    # AO3 username
    ao3_username: str
    # Podfic tumblr name
    tumblr: str
    # Links back to the parent work
    parent: ParentConfig
    # Urls to podfic audio hosted content
    urls: Optional[Urls]


def retry_on_error(
    error_type: Type[BaseException],
    action: Callable[[], _T],
    message: str = "Expected error ocurred, retrying",
    attempts: int = 5,
    interval: int = 1,
) -> _T:
    final_error = BaseException()
    while attempts > 0:
        try:
            return action()
        except error_type as error:
            final_error = error
            _log.warning(message)
            attempts -= 1
            sleep(interval)

    raise final_error


_ADDITIONAL_PODFIC_TAGS = (
    "Podfic",
    "Podfic & Podficced Works",
)


def get_element_by_id(driver: Chrome, id_: str) -> WebElement:
    return WebDriverWait(driver, 10).until(lambda x: x.find_element_by_id(id_))


def element_text(element: WebElement) -> str:
    return element.text


def _clean_url_path(url: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts[0:3] + ("", "", ""))


def _tumblr_url(name: str) -> str:
    return f"https://{name}.tumblr.com/"


@dataclass(frozen=True)
class SeriesPart:
    series: str
    part: int


@dataclass(frozen=True)
class Work:
    """All the information about a work from AO3."""

    id: str
    url: str
    title: str
    author: str
    summary: str
    rating: List[str]
    warning: List[str]
    category: List[str]
    fandom: List[str]
    relationship: List[str]
    character: List[str]
    freeform: List[str]
    series_part: Optional[SeriesPart]
    author_url: str


@dataclass(frozen=True)
class ParentWork:
    """Information about an AO3 work, plus metadata."""

    work: Work
    config: ParentConfig


@dataclass(frozen=True)
class PodficWork:
    author: str
    tumblr: str
    parent: ParentWork
    template_post: jinja2.Template
    template_notes: jinja2.Template
    # TODO runtime tagging
    # runtime: int
    urls: Urls

    def tumblr_url(self) -> str:
        return _tumblr_url(self.tumblr)

    def content(self) -> str:
        return self.template_post.render(podfic_work=self)

    def notes(self) -> str:
        return self.template_notes.render(podfic_work=self)


@dataclass(frozen=True)
class PodficEpisode:
    author: str
    tumblr: str
    parent: ParentWork
    template: jinja2.Template

    def tumblr_url(self) -> str:
        return _tumblr_url(self.tumblr)

    def description(self) -> str:
        return self.template.render(podfic_episode=self)


def _fill_tag(element: WebElement, tag: str) -> None:
    element.send_keys(tag)
    element.send_keys(Keys.ENTER)


def _fill_tags(element: WebElement, tags: Iterable[str]) -> None:
    for tag in tags:
        element.send_keys(tag)
        element.send_keys(Keys.ENTER)


@dataclass
class AO3:
    driver: Chrome
    base_url: str
    username: str

    def quit(self) -> None:
        self.driver.quit()

    def _element_by_id(self, id_: str) -> WebElement:
        return get_element_by_id(self.driver, id_)

    def _url(self, path) -> str:
        return urljoin(self.base_url, path)

    def _get(self, path) -> str:
        url = self._url(path)
        _log.info("Navigating to '%s'", url)
        self.driver.get(url)
        return url

    def home(self) -> "AO3":
        self._get("/")
        return self

    def accept_tos(self) -> "AO3":
        tos_agree = self._element_by_id("tos_agree")
        accept_tos = self._element_by_id("accept_tos")
        actions = ActionChains(self.driver)
        actions.click(tos_agree).click(accept_tos)

        retry_on_error(
            ElementNotInteractableException,
            lambda: actions.perform(),
            "Error accepting TOS, retrying",
        )
        sleep(1)
        return self

    def login(self) -> "AO3":
        login_dropdown = self._element_by_id("login-dropdown")
        actions = ActionChains(self.driver)
        actions.click(login_dropdown)

        retry_on_error(
            ElementNotInteractableException,
            lambda: actions.perform(),
            "Error opening login form, retrying",
        )

        username = self._element_by_id("user_session_login_small")
        username.send_keys(self.username)
        password = self._element_by_id("user_session_password_small")
        password.send_keys(os.environ["AO3_PASSWORD"])
        password.submit()
        return self

    def _fill_warnings(self, warnings: Iterable[str]) -> None:
        for warning in warnings:
            if warning == "Creator Chose Not To Use Archive Warnings":
                self._click_checkbox_value("Choose Not To Use Archive Warnings")
        else:
            self._click_checkbox_value(warning)

    def new_podfic(self, podfic: PodficWork) -> "AO3":
        work = podfic.parent.work

        _log.info("Creating new podfic work based on '%s'", work.id)
        self._get("/works/new")

        # Rating, just the one
        [work_rating] = work.rating
        self._select_value_text("work_rating_string", work_rating)

        # Fill in other tags and data
        self._fill_warnings(work.warning)
        self._fill_field_tags("work_fandom_autocomplete", work.fandom)
        for category in work.category:
            self._click_checkbox_value(category)
        self._fill_field_tags("work_relationship_autocomplete", work.relationship)
        self._fill_field_tags("work_character_autocomplete", work.character)

        self._fill_field_tags("work_freeform_autocomplete", work.freeform)
        self._fill_field_tags("work_freeform_autocomplete", _ADDITIONAL_PODFIC_TAGS)

        title = f"[podfic] {work.title}"
        self._element_by_id("work_title").send_keys(title)

        self._element_by_id("work_summary").send_keys(work.summary)
        self._element_by_id("end-notes-options-show").click()
        self._element_by_id("work_endnotes").send_keys(podfic.notes())

        self._element_by_id("parent-options-show").click()
        self._element_by_id("work_parent_attributes_url").send_keys(work.url)

        if work.series_part is not None:
            self._element_by_id("series-options-show").click()
            series_name = f"[podfic] {work.series_part.series}"
            if work.series_part.part == 1:
                self._element_by_id("work_series_attributes_title").send_keys(
                    series_name
                )
            else:
                self._select_value_text("work_series_attributes_id", series_name)

        self._select_value_text("work_language_id", "English")

        self._element_by_id("content").send_keys(podfic.content())

        return self

    def load_work_data(self, work_id: str) -> Work:
        _log.info("Loading work data for '%s'", work_id)
        url = self._get(f"/works/{work_id}")

        title = self.driver.find_element_by_css_selector(
            ".preface.group .title.heading"
        ).text
        author_element = self.driver.find_element_by_css_selector(
            ".preface.group .byline.heading a"
        )
        author = author_element.text
        author_url = self._url(author_element.get_attribute("href"))
        summary = self.driver.find_element_by_css_selector(
            ".summary .userstuff"
        ).get_attribute("innerHTML")

        # A work can be part of more than one series on AO3
        # Here, we just care about the first one as the 'canonical series'
        series_part = None
        try:
            series_element = self.driver.find_element_by_css_selector(
                "span.series span.position"
            )
            series = series_element.find_element_by_css_selector("a").text
            match = _RX_PART_N_OF_THE.match(series_element.text)
            assert match is not None, "No series part number"
            part = int(match.group(1))
            series_part = SeriesPart(series=series, part=part)
        except NoSuchElementException:
            pass

        return Work(
            id=work_id,
            url=url,
            title=title,
            author=author,
            summary=summary,
            rating=self._load_tags("rating"),
            warning=self._load_tags("warning"),
            category=self._load_tags("category"),
            fandom=self._load_tags("fandom"),
            relationship=self._load_tags("relationship"),
            character=self._load_tags("character"),
            freeform=self._load_tags("freeform"),
            series_part=series_part,
            author_url=author_url,
        )

    def _load_tags(self, category: str) -> List[str]:
        return list(
            map(
                element_text,
                self.driver.find_elements_by_css_selector(f".{category}.tags .tag"),
            )
        )

    def _click_checkbox_value(self, value: str) -> None:
        self.driver.find_element_by_xpath(f"//input[@value='{value}']").click()

    def _fill_field_tags(self, id_: str, values: Iterable[str]) -> None:
        _fill_tags(self._element_by_id(id_), values)

    def _select_value_text(self, id_: str, text: str) -> None:
        select = self._element_by_id(id_)
        Select(select).select_by_visible_text(text)


def _get_jinja2_template(template: str) -> jinja2.Template:
    loader = jinja2.FileSystemLoader(template)
    env = jinja2.Environment(loader=loader, autoescape=True)
    return env.get_template("")


def run_describe(args: argparse.Namespace) -> None:
    config = Config.parse_file(args.config)

    driver = Chrome()
    ao3 = AO3(driver=driver, base_url=args.base_url, username=config.ao3_username)
    ao3.home().accept_tos().login()
    work = ao3.home().load_work_data(config.parent.work_id)
    ao3.quit()

    episode = PodficEpisode(
        author=config.ao3_username,
        tumblr=config.tumblr,
        parent=ParentWork(work=work, config=config.parent),
        template=_get_jinja2_template(args.template),
    )
    print(episode.description())


def run_post(args: argparse.Namespace) -> None:
    template_post = _get_jinja2_template(args.template_post)
    template_notes = _get_jinja2_template(args.template_notes)
    config = Config.parse_file(args.config)

    if config.urls is None:
        _log.error("To post a podficced work, you must provide urls.")
        return None

    options = ChromeOptions()
    options.add_experimental_option("detach", True)
    driver = Chrome(options=options)

    ao3 = AO3(driver=driver, base_url=args.base_url, username=config.ao3_username)
    ao3.home().accept_tos().login()
    work = ao3.home().load_work_data(config.parent.work_id)

    podfic = PodficWork(
        author=config.ao3_username,
        tumblr=config.tumblr,
        parent=ParentWork(work=work, config=config.parent),
        template_post=template_post,
        template_notes=template_notes,
        urls=config.urls.clean_urls(),
    )

    ao3.home().new_podfic(podfic=podfic)


def main_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="podfickle - podfic creation toolkit")
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Config file path.",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        help="Base url of AO3 instance.",
        default="https://archiveofourown.org",
    )

    subparsers = parser.add_subparsers(required=True, dest="subcommand")
    describe = subparsers.add_parser("describe")
    describe.set_defaults(execute=run_describe)
    describe.add_argument(
        "--template",
        type=str,
        help="Jinja2 template file for work content.",
        default="./describe.jinja2",
    )

    post = subparsers.add_parser("post")
    post.set_defaults(execute=run_post)
    post.add_argument(
        "--template-post",
        type=str,
        help="Jinja2 template file for work content.",
        default="./post.jinja2",
    )
    post.add_argument(
        "--template-notes",
        type=str,
        help="Jinja2 template file for work notes.",
        default="./notes.jinja2",
    )

    return parser


def main() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s|%(name)s|%(levelname)s|%(message)s")
    )
    root_logger.handlers = [stream_handler]

    load_dotenv()

    parser = main_parser()
    args = parser.parse_args()
    args.execute(args)


if __name__ == "__main__":
    main()
