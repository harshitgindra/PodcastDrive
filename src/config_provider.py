"""Configuration providers for podcast subscriptions.

Supports multiple backends (YAML file, Notion database) through a common
interface. Each provider returns a list of PodcastConfig objects.
"""

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PodcastConfig:
    """Configuration for a single podcast subscription."""

    name: str
    url: str  # Playlist ID, channel handle (@xyz), or full URL
    enabled: bool = True
    max_downloads: int | None = None  # None = use global default
    max_age_days: int | None = None
    sleep_between: int | None = None
    page_id: str | None = None  # Notion page ID (for write-back)
    source: str = "YouTube"  # "YouTube" or "Podcast"


class ConfigProvider(ABC):
    """Abstract base class for podcast configuration providers."""

    @abstractmethod
    def get_podcasts(self) -> list[PodcastConfig]:
        """Return list of podcast configurations."""
        ...

    def update_last_run(self, podcast: PodcastConfig, feed_url: str = "") -> None:
        """Update the last-run timestamp for a podcast. Override in subclasses."""
        pass

    def update_status(self, podcast: PodcastConfig, status: str) -> None:
        """Update the Status field for a podcast. Override in subclasses.

        Args:
            podcast: The podcast to update.
            status: One of ``"Pending"``, ``"Running"``, ``"Done"``, ``"Failed"``.
        """
        pass


class YamlConfigProvider(ConfigProvider):
    """Load podcast config from a local YAML file.

    Expected format::

        defaults:
          max_downloads: 10
          max_age_days: 7
          sleep_between: 5

        podcasts:
          - name: Vantage on Firstpost
            url: PLEVkQGIATCXI1F2qs0slVE2MScaj1cSM0
            enabled: true
          - name: Nitish Rajput
            url: "@NitishRajput"
            max_age_days: 14
    """

    def __init__(self, path: str = "podcasts.yaml") -> None:
        """Initialise the provider.

        Args:
            path: Path to the YAML file (default: ``"podcasts.yaml"``).
        """
        self.path = path

    def get_podcasts(self) -> list[PodcastConfig]:
        """Load and return podcasts from the YAML file.

        Returns:
            List of :class:`PodcastConfig` objects.  Returns an empty list
            if the file does not exist.
        """
        import yaml

        if not os.path.exists(self.path):
            logger.warning("Config file not found: %s", self.path)
            return []

        with open(self.path, "r") as f:
            data = yaml.safe_load(f) or {}

        defaults = data.get("defaults", {})
        podcasts = []

        for entry in data.get("podcasts", []):
            podcasts.append(PodcastConfig(
                name=entry.get("name", entry.get("url", "Unknown")),
                url=entry["url"],
                enabled=entry.get("enabled", True),
                max_downloads=entry.get("max_downloads", defaults.get("max_downloads")),
                max_age_days=entry.get("max_age_days", defaults.get("max_age_days")),
                sleep_between=entry.get("sleep_between", defaults.get("sleep_between")),
            ))

        logger.info("Loaded %d podcasts from %s", len(podcasts), self.path)
        return podcasts


class NotionConfigProvider(ConfigProvider):
    """Load podcast config from a Notion database.

    Requires environment variables:
    - NOTION_API_KEY: Notion integration token
    - NOTION_DATABASE_ID: ID of the Notion database

    Expected database columns:
    - Name (title): Podcast name
    - URL (rich_text): Playlist ID, @handle, or full URL
    - Enabled (checkbox): Subscribe/unsubscribe toggle
    - Max Downloads (number): Per-run download limit (optional)
    - Max Age Days (number): Episode retention in days (optional)
    """

    def __init__(self) -> None:
        """Initialise the provider, reading credentials from environment variables.

        Raises:
            ValueError: If ``NOTION_API_KEY`` or ``NOTION_DATABASE_ID`` is not set.
        """
        self.api_key = os.environ.get("NOTION_API_KEY", "")
        self.database_id = os.environ.get("NOTION_DATABASE_ID", "")

        if not self.api_key or not self.database_id:
            raise ValueError(
                "NOTION_API_KEY and NOTION_DATABASE_ID must be set "
                "when using Notion config provider"
            )

    def get_podcasts(self) -> list[PodcastConfig]:
        """Query the Notion database and return enabled podcast configs.

        Handles Notion pagination automatically.

        Returns:
            List of :class:`PodcastConfig` objects parsed from Notion pages.
            Returns whatever was collected so far if the API call fails mid-way.
        """
        import ssl
        import urllib.request
        import json
        import certifi

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        url = f"https://api.notion.com/v1/databases/{self.database_id}/query"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

        podcasts = []
        has_more = True
        start_cursor = None

        while has_more:
            body = {}
            if start_cursor:
                body["start_cursor"] = start_cursor

            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )

            try:
                with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                logger.error("Failed to query Notion database: %s", exc)
                return podcasts

            for page in data.get("results", []):
                props = page.get("properties", {})
                podcast = self._parse_page(props)
                if podcast:
                    podcast.page_id = page.get("id")
                    podcasts.append(podcast)

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        logger.info("Loaded %d podcasts from Notion", len(podcasts))
        return podcasts

    def _parse_page(self, props: dict) -> PodcastConfig | None:
        """Parse a Notion page's properties dict into a :class:`PodcastConfig`.

        Args:
            props: The ``properties`` dict from a Notion API page object.

        Returns:
            A populated :class:`PodcastConfig`, or ``None`` if the entry
            has no URL or cannot be parsed.
        """
        try:
            # Name (title type)
            name_prop = props.get("Name", {})
            name = ""
            if name_prop.get("type") == "title":
                title_items = name_prop.get("title", [])
                name = title_items[0]["plain_text"] if title_items else ""

            # URL (rich_text or url type)
            url_prop = props.get("URL", {})
            url = ""
            if url_prop.get("type") == "rich_text":
                text_items = url_prop.get("rich_text", [])
                url = text_items[0]["plain_text"] if text_items else ""
            elif url_prop.get("type") == "url":
                url = url_prop.get("url") or ""

            if not url:
                logger.warning("Skipping Notion entry with no URL: %s", name)
                return None

            # Enabled (checkbox type)
            enabled = True
            enabled_prop = props.get("Enabled", {})
            if enabled_prop.get("type") == "checkbox":
                enabled = enabled_prop.get("checkbox", True)

            # Source (select type) — must be "YouTube" to be processed
            source_prop = props.get("Source", {})
            source = ""
            if source_prop.get("type") == "select" and source_prop.get("select"):
                source = source_prop["select"].get("name", "")

            # Filter: skip disabled entries
            if not enabled:
                logger.debug("Skipping Notion entry '%s': disabled", name)
                return None

            # Filter: skip non-YouTube sources (absent Source also excluded)
            if source != "YouTube":
                logger.debug(
                    "Skipping Notion entry '%s': source=%r (expected 'YouTube')",
                    name, source,
                )
                return None

            # Max Downloads (number type)
            max_downloads = None
            md_prop = props.get("Max Downloads", {})
            if md_prop.get("type") == "number" and md_prop.get("number") is not None:
                max_downloads = int(md_prop["number"])

            # Max Age Days (number type)
            max_age_days = None
            ma_prop = props.get("Max Age Days", {})
            if ma_prop.get("type") == "number" and ma_prop.get("number") is not None:
                max_age_days = int(ma_prop["number"])

            return PodcastConfig(
                name=name or url,
                url=url,
                enabled=enabled,
                max_downloads=max_downloads,
                max_age_days=max_age_days,
            )

        except (KeyError, IndexError) as exc:
            logger.warning("Failed to parse Notion page: %s", exc)
            return None

    def update_status(self, podcast: PodcastConfig, status: str) -> None:
        """Update the ``Status`` select field in Notion.

        Args:
            podcast: The podcast whose Notion page should be updated.
                     Must have a ``page_id`` attribute set.
            status: One of ``"Pending"``, ``"Running"``, ``"Done"``, ``"Failed"``.
        """
        if not podcast.page_id:
            logger.warning("No page_id for %s, skipping status update", podcast.name)
            return

        import ssl
        import urllib.request
        import json
        import certifi

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        url = f"https://api.notion.com/v1/pages/{podcast.page_id}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

        body = {
            "properties": {
                "Status": {
                    "select": {
                        "name": status,
                    }
                }
            }
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="PATCH",
        )

        try:
            with urllib.request.urlopen(req, timeout=15, context=ssl_ctx):
                logger.info("Updated Notion status to '%s' for %s", status, podcast.name)
        except Exception as exc:
            logger.warning(
                "Failed to update Notion status for %s: %s", podcast.name, exc
            )

    def update_last_run(self, podcast: PodcastConfig, feed_url: str = "") -> None:
        """Update ``LastUpdated`` (and optionally ``Podcast URL``) in Notion.

        Args:
            podcast: The podcast whose Notion page should be updated.
                     Must have a ``page_id`` attribute set.
            feed_url: Optional CloudFront feed URL to write back to the
                      ``Podcast URL`` property in Notion.
        """
        if not podcast.page_id:
            logger.warning("No page_id for %s, skipping Notion update", podcast.name)
            return

        import ssl
        import urllib.request
        import json
        import certifi
        from datetime import datetime, timezone

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        url = f"https://api.notion.com/v1/pages/{podcast.page_id}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

        now = datetime.now(timezone.utc).isoformat()
        properties = {
            "LastUpdated": {
                "date": {
                    "start": now,
                }
            }
        }

        if feed_url:
            properties["Podcast URL"] = {
                "url": feed_url,
            }

        body = {"properties": properties}

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="PATCH",
        )

        try:
            with urllib.request.urlopen(req, timeout=15, context=ssl_ctx):
                logger.info("Updated Notion for %s", podcast.name)
        except Exception as exc:
            logger.warning("Failed to update Notion for %s: %s", podcast.name, exc)


class NotionPodcastConfigProvider(NotionConfigProvider):
    """Notion config provider that returns ``Source=Podcast`` entries.

    Subclasses :class:`NotionConfigProvider` and overrides ``_parse_page`` to
    filter for RSS podcast feed subscriptions rather than YouTube playlists.

    Additional capability over the base class:
    - :meth:`update_url` — write the resolved RSS feed URL back to the Notion
      ``URL`` property (used after iTunes → RSS URL resolution so the
      Apple Podcasts link is replaced with the real feed URL).
    """

    def _parse_page(self, props: dict) -> PodcastConfig | None:
        """Parse a Notion page into a :class:`PodcastConfig` for RSS podcasts.

        Only returns entries where ``Source == "Podcast"``.

        Args:
            props: The ``properties`` dict from a Notion API page object.

        Returns:
            A populated :class:`PodcastConfig` with ``source="Podcast"``, or
            ``None`` if the entry should be skipped.
        """
        try:
            # Name (title type)
            name_prop = props.get("Name", {})
            name = ""
            if name_prop.get("type") == "title":
                title_items = name_prop.get("title", [])
                name = title_items[0]["plain_text"] if title_items else ""

            # URL (rich_text or url type)
            url_prop = props.get("URL", {})
            url = ""
            if url_prop.get("type") == "rich_text":
                text_items = url_prop.get("rich_text", [])
                url = text_items[0]["plain_text"] if text_items else ""
            elif url_prop.get("type") == "url":
                url = url_prop.get("url") or ""

            # Enabled (checkbox type)
            enabled = True
            enabled_prop = props.get("Enabled", {})
            if enabled_prop.get("type") == "checkbox":
                enabled = enabled_prop.get("checkbox", True)

            # Source (select type) — must be "Podcast"
            source_prop = props.get("Source", {})
            source = ""
            if source_prop.get("type") == "select" and source_prop.get("select"):
                source = source_prop["select"].get("name", "")

            if not enabled:
                logger.debug("Skipping Notion entry '%s': disabled", name)
                return None

            if source != "Podcast":
                logger.debug(
                    "Skipping Notion entry '%s': source=%r (expected 'Podcast')",
                    name, source,
                )
                return None

            # URL may be empty — process_podcast_feed will discover it via iTunes Search
            if not url:
                logger.info(
                    "Notion entry '%s' has no URL — will search iTunes by name", name
                )

            # Max Age Days (number type) — controls how far back to fetch episodes
            max_age_days = None
            ma_prop = props.get("Max Age Days", {})
            if ma_prop.get("type") == "number" and ma_prop.get("number") is not None:
                max_age_days = int(ma_prop["number"])

            # Max Downloads (number type) — max episodes to process per run
            max_downloads = None
            md_prop = props.get("Max Downloads", {})
            if md_prop.get("type") == "number" and md_prop.get("number") is not None:
                max_downloads = int(md_prop["number"])

            return PodcastConfig(
                name=name or url,
                url=url,
                enabled=enabled,
                max_downloads=max_downloads,
                max_age_days=max_age_days,
                source="Podcast",
            )

        except (KeyError, IndexError) as exc:
            logger.warning("Failed to parse Notion podcast page: %s", exc)
            return None

    def update_url(self, podcast: PodcastConfig, new_url: str) -> None:
        """Write a resolved RSS feed URL back to the Notion ``URL`` property.

        Called after an Apple Podcasts link is resolved to its real RSS feed
        URL so subsequent runs skip the iTunes API lookup.

        Args:
            podcast: The podcast whose Notion page should be updated.
                     Must have a ``page_id`` attribute set.
            new_url: The resolved RSS feed URL to persist.
        """
        if not podcast.page_id:
            logger.warning("No page_id for %s, skipping URL update", podcast.name)
            return

        import ssl
        import urllib.request
        import json
        import certifi

        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        url = f"https://api.notion.com/v1/pages/{podcast.page_id}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

        body = {
            "properties": {
                "URL": {
                    "url": new_url,
                }
            }
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="PATCH",
        )

        try:
            with urllib.request.urlopen(req, timeout=15, context=ssl_ctx):
                logger.info(
                    "Updated Notion URL for '%s' → %s", podcast.name, new_url
                )
        except Exception as exc:
            logger.warning(
                "Failed to update Notion URL for %s: %s", podcast.name, exc
            )


def get_config_provider() -> ConfigProvider:
    """Factory: return the appropriate config provider based on CONFIG_PROVIDER env var.

    - "yaml" (default): reads from podcasts.yaml
    - "notion": reads from a Notion database
    """
    provider_type = os.environ.get("CONFIG_PROVIDER", "yaml").lower()

    if provider_type == "notion":
        return NotionConfigProvider()
    else:
        yaml_path = os.environ.get("PODCASTS_YAML", "podcasts.yaml")
        return YamlConfigProvider(path=yaml_path)


def get_podcast_config_provider() -> "NotionPodcastConfigProvider | ConfigProvider":
    """Factory: return the RSS-podcast config provider.

    Currently only Notion is supported for RSS podcast subscriptions.
    Falls back gracefully when ``CONFIG_PROVIDER`` is not ``"notion"``.

    Returns:
        A :class:`NotionPodcastConfigProvider` when ``CONFIG_PROVIDER=notion``,
        otherwise a :class:`YamlConfigProvider` (which returns no RSS podcast
        entries by default — callers should check for an empty list).
    """
    provider_type = os.environ.get("CONFIG_PROVIDER", "yaml").lower()

    if provider_type == "notion":
        return NotionPodcastConfigProvider()
    else:
        yaml_path = os.environ.get("PODCASTS_YAML", "podcasts.yaml")
        return YamlConfigProvider(path=yaml_path)
