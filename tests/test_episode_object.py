"""Naming rules for episode audio objects.

Every revision of an episode must get its own object name, and every reader of
a key must be able to get back to the one episode id the audio belongs to.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from episode_object import (  # noqa: E402
    REVISION_FIELD,
    episode_filename,
    episode_key,
    published_filename,
    published_key,
    published_revision,
    split_episode_filename,
)


class TestEpisodeFilename(unittest.TestCase):
    def test_revision_zero_keeps_the_published_name(self):
        # Nothing already published may move, so revision 0 is the plain name.
        self.assertEqual(episode_filename("abc123"), "abc123.mp3")
        self.assertEqual(episode_filename("abc123", 0), "abc123.mp3")

    def test_negative_revision_is_treated_as_the_plain_name(self):
        self.assertEqual(episode_filename("abc123", -1), "abc123.mp3")

    def test_each_revision_gets_its_own_name(self):
        self.assertEqual(episode_filename("abc123", 1), "abc123.r1.mp3")
        self.assertEqual(episode_filename("abc123", 12), "abc123.r12.mp3")

    def test_an_id_containing_dots_is_left_alone(self):
        self.assertEqual(episode_filename("9to5mac.com-e451", 2), "9to5mac.com-e451.r2.mp3")

    def test_key_is_prefixed_by_the_playlist(self):
        self.assertEqual(episode_key("slug", "abc", 0), "slug/episodes/abc.mp3")
        self.assertEqual(episode_key("slug", "abc", 3), "slug/episodes/abc.r3.mp3")


class TestSplitEpisodeFilename(unittest.TestCase):
    def test_plain_name_is_revision_zero(self):
        self.assertEqual(split_episode_filename("abc123.mp3"), ("abc123", 0))

    def test_revision_marker_is_stripped_back_to_the_id(self):
        self.assertEqual(split_episode_filename("abc123.r4.mp3"), ("abc123", 4))

    def test_dotted_id_survives_a_round_trip(self):
        for revision in (0, 1, 7):
            name = episode_filename("9to5mac.com-e451", revision)
            self.assertEqual(split_episode_filename(name), ("9to5mac.com-e451", revision))

    def test_non_audio_objects_are_not_episodes(self):
        for name in ("feed.xml", "manifest.json", "", ".mp3"):
            self.assertIsNone(split_episode_filename(name), name)

    def test_a_dotted_id_is_not_mistaken_for_a_revision(self):
        # Only ".r<digits>" is a marker: an id that merely ends in something
        # dot-prefixed keeps its full name.
        self.assertEqual(split_episode_filename("ep.release.mp3"), ("ep.release", 0))
        self.assertEqual(split_episode_filename("ep.r0.mp3"), ("ep.r0", 0))
        self.assertEqual(split_episode_filename("ep.r01.mp3"), ("ep.r01", 0))


class TestPublishedRevision(unittest.TestCase):
    def test_absent_manifest_means_the_plain_name(self):
        self.assertEqual(published_revision(None, "abc"), 0)
        self.assertEqual(published_revision({}, "abc"), 0)

    def test_missing_or_unusable_field_means_the_plain_name(self):
        self.assertEqual(published_revision({"abc": {}}, "abc"), 0)
        self.assertEqual(published_revision({"abc": {REVISION_FIELD: "2"}}, "abc"), 0)
        self.assertEqual(published_revision({"abc": {REVISION_FIELD: 0}}, "abc"), 0)
        self.assertEqual(published_revision({"abc": {REVISION_FIELD: -3}}, "abc"), 0)
        self.assertEqual(published_revision({"abc": "not-a-dict"}, "abc"), 0)

    def test_recorded_revision_is_returned(self):
        self.assertEqual(published_revision({"abc": {REVISION_FIELD: 5}}, "abc"), 5)

    def test_published_key_and_filename_follow_the_manifest(self):
        manifest = {"abc": {REVISION_FIELD: 3}}
        self.assertEqual(published_key("slug", "abc", manifest), "slug/episodes/abc.r3.mp3")
        self.assertEqual(published_filename("abc", manifest), "abc.r3.mp3")
        self.assertEqual(published_key("slug", "new", manifest), "slug/episodes/new.mp3")
        self.assertEqual(published_filename("new", None), "new.mp3")


if __name__ == "__main__":
    unittest.main()
