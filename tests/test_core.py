"""Offline tests: no Shazam or Spotify requests are made."""

import asyncio
import shutil
import struct
import subprocess

import pytest

import scanner
from scanner import TrackEntry, TrackKey, TracklistBuilder, parse_track, slice_offsets
from spotify import SettingsStore, SpotifyClient, SpotifyTrack, _shazam_search_query, match_score


@pytest.mark.parametrize(
    "a, b, same",
    [
        (("Fisher", "Losing It"), ("FISHER", "Losing It (Original Mix)"), True),
        (("Disclosure feat. Sam Smith", "Latch"), ("Disclosure", "Latch"), True),
        (("Calvin Harris & Dua Lipa", "One Kiss"), ("Calvin Harris", "One Kiss - Extended Mix"), True),
        (("Fred again..", "Delilah (pull me out of this)"), ("Fred again..", "Delilah"), True),
        (("Beyoncé", "Cuff It"), ("Beyonce", "CUFF IT"), True),
        (("Fisher", "Losing It"), ("Chris Lake", "Turn Off The Lights"), False),
        (("Artist A", "Levels"), ("Artist B", "Levels"), False),
    ],
)
def test_track_key_same_track(a, b, same):
    assert TrackKey.of(*a).same_track(TrackKey.of(*b)) is same


def test_builder_dedupes_and_logs_gaps():
    builder = TracklistBuilder(gap_threshold=2)
    results = [
        builder.add_match(TrackEntry(0, "Fisher", "Losing It")),
        builder.add_match(TrackEntry(90, "FISHER", "Losing It (Extended)")),
        builder.add_match(TrackEntry(180, "Chris Lake", "Turn Off The Lights")),
        builder.add_miss(270),
        builder.add_miss(360),
        builder.add_miss(450),
        builder.add_match(TrackEntry(540, "Disclosure", "Latch")),
    ]
    logged = [r.display_line if r else None for r in results]
    assert logged == [
        "[00:00:00] | Fisher - Losing It",
        None,
        "[00:03:00] | Chris Lake - Turn Off The Lights",
        None,
        "[00:04:30] | ID - Unidentified",
        None,
        "[00:09:00] | Disclosure - Latch",
    ]
    assert builder.identified_count == 3


def test_single_miss_is_not_a_gap():
    builder = TracklistBuilder(gap_threshold=2)
    assert builder.add_miss(0) is None
    builder.add_match(TrackEntry(90, "A", "B"))
    assert builder.add_miss(180) is None


SHAZAM_RESPONSE = {
    "matches": [{}],
    "track": {
        "title": "Latch",
        "subtitle": "Disclosure feat. Sam Smith",
        "isrc": "GBUM71205458",
        "url": "https://www.shazam.com/track/1",
        "sections": [{"type": "SONG", "metadata": [
            {"title": "Album", "text": "Settle"},
            {"title": "Label", "text": "PMR Records"},
        ]}],
        "hub": {
            "options": [{"actions": [{"uri": "https://music.apple.com/gb/album/latch/123"}]}],
            "providers": [{"type": "SPOTIFY", "actions": [{"uri": "spotify:search:Latch%20Disclosure"}]}],
        },
    },
}


def test_parse_track_extracts_metadata():
    entry = parse_track(SHAZAM_RESPONSE, 3723)
    assert entry.display_line == "[01:02:03] | Disclosure feat. Sam Smith - Latch (PMR Records)"
    assert entry.album == "Settle"
    assert entry.isrc == "GBUM71205458"
    assert entry.apple_music_url.startswith("https://music.apple.com/")
    assert entry.spotify_uri == "spotify:search:Latch%20Disclosure"
    assert _shazam_search_query(entry.spotify_uri) == "Latch Disclosure"


def test_parse_track_no_match():
    assert parse_track({"matches": []}, 0) is None
    assert parse_track({}, 0) is None


def test_label_falls_back_to_na():
    response = {"matches": [{}], "track": {"title": "T", "subtitle": "A"}}
    entry = parse_track(response, 0)
    assert entry.label == "N/A"
    assert entry.display_line == "[00:00:00] | A - T"


def test_slice_offsets():
    assert slice_offsets(200, 60) == [0.0, 60.0, 120.0, 180.0]
    assert slice_offsets(10, 90) == [0.0]
    assert slice_offsets(182, 60) == [0.0, 60.0, 120.0]  # 2 s tail is too short
    assert len(slice_offsets(9000, 90)) == 100


def test_patch_wav_sizes():
    pcm = b"\x01\x00" * 1000
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
    header = b"RIFF" + b"\xff\xff\xff\xff" + b"WAVE" + fmt + b"data" + b"\xff\xff\xff\xff"
    fixed = scanner._patch_wav_sizes(bytearray(header + pcm))
    assert struct.unpack_from("<I", fixed, 4)[0] == len(fixed) - 8
    assert struct.unpack_from("<I", fixed, 40)[0] == len(pcm)


def test_match_score_prefers_right_track():
    entry = parse_track(SHAZAM_RESPONSE, 0)
    right = match_score(entry, SpotifyTrack("u", "Latch", "Disclosure, Sam Smith", ""))
    acoustic = match_score(entry, SpotifyTrack("u", "Latch - Acoustic", "Sam Smith", ""))
    wrong = match_score(entry, SpotifyTrack("u", "Help Me Lose My Mind", "Disclosure", ""))
    assert right > acoustic > wrong
    assert right >= 0.95
    assert wrong < 0.75


def test_settings_store_is_private_and_client_id_change_drops_token(tmp_path):
    store = SettingsStore(tmp_path / "spotify.json")
    client = SpotifyClient(store)
    client.set_client_id("abc")
    store.update(token={"refresh_token": "x"})
    assert client.is_connected
    client.set_client_id("def")
    assert not client.is_connected
    assert (tmp_path / "spotify.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_ffmpeg_pipeline(tmp_path):
    media = tmp_path / "tone.mp3"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
         str(media)],
        check=True,
    )
    duration = asyncio.run(scanner.probe_duration(str(media)))
    assert 39 < duration < 41
    audio = asyncio.run(scanner.extract_slice(str(media), 20))
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    # 12 s of 16 kHz mono 16-bit PCM, plus the header.
    assert abs(len(audio) - (44 + 12 * 16000 * 2)) < 2000
