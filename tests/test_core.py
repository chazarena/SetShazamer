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


def _lines(events):
    return [(type(e).__name__, e.entry.display_line if hasattr(e, "entry") else e.uid) for e in events]


def test_builder_dedupes_and_logs_gaps():
    builder = TracklistBuilder(gap_threshold=2)
    builder.add_match(TrackEntry(0, "Fisher", "Losing It"))
    assert _lines(builder.add_match(TrackEntry(90, "FISHER", "Losing It (Extended)"))) == [
        ("TrackUpdatedEvent", "[00:00:00] | Fisher - Losing It")]
    builder.add_match(TrackEntry(180, "Chris Lake", "Turn Off The Lights"))
    assert builder.add_miss(270) == []
    assert _lines(builder.add_miss(360)) == [("TrackEvent", "[00:04:30] | ID - Unidentified")]
    assert builder.add_miss(450) == []
    builder.add_match(TrackEntry(540, "Disclosure", "Latch"))
    assert [e.display_line for e in builder.entries] == [
        "[00:00:00] | Fisher - Losing It",
        "[00:03:00] | Chris Lake - Turn Off The Lights",
        "[00:04:30] | ID - Unidentified",
        "[00:09:00] | Disclosure - Latch",
    ]
    assert [e.hits for e in builder.entries] == [2, 1, 1, 1]
    assert [e.confidence for e in builder.entries] == ["High", "Medium", "", "Medium"]
    assert len({e.uid for e in builder.entries}) == 4
    assert builder.identified_count == 3


def test_version_and_year_variants_merge():
    builder = TracklistBuilder()
    builder.add_match(TrackEntry(90, "Hot'n'Juicy & Mousse T.", "Horny ('98 Radio Edit)"))
    events = builder.add_match(TrackEntry(180, "Mousse T. & Hot 'n' Juicy",
                                          "Horny '98 (Extended Mix) [Mixed] [Mixed]"))
    assert [type(e).__name__ for e in events] == ["TrackUpdatedEvent"]
    assert len(builder.entries) == 1 and builder.entries[0].hits == 2


def test_misfire_between_same_track_is_removed():
    builder = TracklistBuilder()
    builder.add_match(TrackEntry(0, "Bicep", "Glue"))
    misfire = builder.add_match(TrackEntry(90, "Someone", "Wrong Song"))[0].entry
    events = builder.add_match(TrackEntry(180, "Bicep", "Glue (Original Mix)"))
    assert _lines(events) == [("TrackRemovedEvent", misfire.uid),
                              ("TrackUpdatedEvent", "[00:00:00] | Bicep - Glue")]
    assert [e.title for e in builder.entries] == ["Glue"]


def test_confirmed_track_between_is_kept():
    builder = TracklistBuilder()
    builder.add_match(TrackEntry(0, "Bicep", "Glue"))
    b = builder.add_match(TrackEntry(90, "Overmono", "So U Kno"))[0].entry
    builder.verify(b.uid, TrackEntry(120, "Overmono", "So U Kno"))
    builder.add_match(TrackEntry(180, "Bicep", "Glue"))
    assert [e.title for e in builder.entries] == ["Glue", "So U Kno", "Glue"]


def test_verify_outcomes():
    builder = TracklistBuilder()
    a = builder.add_match(TrackEntry(0, "Bicep", "Glue"))[0].entry
    (event,) = builder.verify(a.uid, TrackEntry(30, "BICEP", "Glue"))
    assert event.entry.confidence == "High" and event.entry.verified is True
    b = builder.add_match(TrackEntry(90, "Overmono", "So U Kno"))[0].entry
    (event,) = builder.verify(b.uid, TrackEntry(120, "Peggy Gou", "Nanana"))
    assert event.entry.confidence == "Low" and event.entry.verified is False
    # A later regular slice agreeing overrides the failed double-check.
    builder.add_match(TrackEntry(180, "Overmono", "So U Kno"))
    assert builder.entries[-1].confidence == "High"


def test_clean_title_drops_mixed_tags():
    assert scanner.clean_title("Horny '98 (Extended Mix) [Mixed] [Mixed]") == "Horny '98 (Extended Mix)"
    assert scanner.clean_title("[Mixed]") == "[Mixed]"


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


@pytest.mark.parametrize(
    "text, ok",
    [
        ("https://www.youtube.com/watch?v=aqz-KE-bpKQ", True),
        ("https://soundcloud.com/artist/some-mix", True),
        ("  https://youtu.be/abc  ", True),
        ("youtube.com/watch?v=abc", False),
        ("not a url", False),
        ("/Users/me/mix.mp3", False),
    ],
)
def test_is_url(text, ok):
    from downloader import is_url

    assert is_url(text) is ok


def test_download_rejects_non_links(tmp_path):
    from downloader import DownloadError, download_audio

    with pytest.raises(DownloadError):
        download_audio("not a url", tmp_path)


def _fake_response(artist, title):
    return {"matches": [{}], "track": {"title": title, "subtitle": artist}}


def test_scan_double_checks_new_tracks(monkeypatch):
    """Full scan loop with ffmpeg and Shazam faked out."""
    plan = {  # slice start -> what Shazam "hears"
        0: ("Bicep", "Glue"), 30: ("Bicep", "Glue"),
        90: ("Overmono", "So U Kno"), 120: ("Peggy Gou", "Nanana"),
        180: ("Fisher", "Losing It"), 210: None,
        270: ("Fisher", "Losing It"),
    }
    asked = []

    async def fake_probe(_path):
        return 300.0

    async def fake_extract(_path, start, _length=12):
        return f"{start:.0f}".encode()

    async def fake_recognize(self, data, *args, **kwargs):
        asked.append(int(data))
        heard = plan.get(int(data))
        return _fake_response(*heard) if heard else {"matches": []}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(scanner, "probe_duration", fake_probe)
    monkeypatch.setattr(scanner, "extract_slice", fake_extract)
    monkeypatch.setattr(scanner.Shazam, "recognize", fake_recognize)
    monkeypatch.setattr(scanner.asyncio, "sleep", no_sleep)

    events = []
    config = scanner.ScanConfig("mix.mp3", step_seconds=90)
    entries = asyncio.run(scanner.TracklistScanner(config, events.append).run())

    assert asked == [0, 30, 90, 120, 180, 210, 270]
    assert [(e.title, e.confidence, e.hits) for e in entries] == [
        ("Glue", "High", 2), ("So U Kno", "Low", 1), ("Losing It", "High", 2)]
    assert any(isinstance(e, scanner.TrackUpdatedEvent) for e in events)


def test_scan_without_double_check(monkeypatch):
    async def fake_probe(_path):
        return 200.0

    async def fake_extract(_path, start, _length=12):
        return f"{start:.0f}".encode()

    asked = []

    async def fake_recognize(self, data, *args, **kwargs):
        asked.append(int(data))
        return _fake_response("Bicep", "Glue")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(scanner, "probe_duration", fake_probe)
    monkeypatch.setattr(scanner, "extract_slice", fake_extract)
    monkeypatch.setattr(scanner.Shazam, "recognize", fake_recognize)
    monkeypatch.setattr(scanner.asyncio, "sleep", no_sleep)

    config = scanner.ScanConfig("mix.mp3", step_seconds=90, verify=False)
    entries = asyncio.run(scanner.TracklistScanner(config, lambda e: None).run())
    assert asked == [0, 90, 180]
    assert [(e.title, e.hits) for e in entries] == [("Glue", 3)]


def _run_plan(monkeypatch, plan, duration, **config):
    """Run the scan loop against a fixed {second: (artist, title) | None} plan."""
    asked = []

    async def fake_probe(_path):
        return duration

    async def fake_extract(_path, start, _length=12):
        return f"{start:.0f}".encode()

    async def fake_recognize(self, data, *args, **kwargs):
        asked.append(int(data))
        heard = plan.get(int(data))
        return _fake_response(*heard) if heard else {"matches": []}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(scanner, "probe_duration", fake_probe)
    monkeypatch.setattr(scanner, "extract_slice", fake_extract)
    monkeypatch.setattr(scanner.Shazam, "recognize", fake_recognize)
    monkeypatch.setattr(scanner.asyncio, "sleep", no_sleep)
    cfg = scanner.ScanConfig("mix.mp3", step_seconds=90, **config)
    entries = asyncio.run(scanner.TracklistScanner(cfg, lambda e: None).run())
    return asked, entries


def test_miss_retries_back_then_forward(monkeypatch):
    plan = {
        0: ("Bicep", "Glue"),
        90: None,                       # miss...
        60: ("Bicep", "Glue"),          # ...30 s back only re-hears the outgoing track
        120: ("Overmono", "So U Kno"),  # ...30 s forward finds the new one
        180: ("Overmono", "So U Kno"),
    }
    asked, entries = _run_plan(monkeypatch, plan, 200.0, verify=False)
    assert asked == [0, 90, 60, 120, 180]
    assert [(e.timestamp, e.title, e.hits) for e in entries] == [
        ("00:00:00", "Glue", 1), ("00:02:00", "So U Kno", 2)]


def test_miss_found_going_back_skips_forward(monkeypatch):
    plan = {0: ("Bicep", "Glue"), 90: None, 60: ("Fisher", "Losing It"), 180: ("Fisher", "Losing It")}
    asked, entries = _run_plan(monkeypatch, plan, 200.0, verify=False)
    assert asked == [0, 90, 60, 180]
    assert [(e.timestamp, e.title) for e in entries] == [("00:00:00", "Glue"), ("00:01:00", "Losing It")]


def test_miss_with_nothing_either_side_moves_on(monkeypatch):
    plan = {0: ("Bicep", "Glue")}
    asked, entries = _run_plan(monkeypatch, plan, 290.0, verify=False)
    # 90: back 60, forward 120. 180: back 150, forward 210. 270: back 240 only (no room ahead).
    assert asked == [0, 90, 60, 120, 180, 150, 210, 270, 240]
    assert [e.title for e in entries] == ["Glue", "Unidentified"]


def test_retry_can_be_turned_off(monkeypatch):
    plan = {0: ("Bicep", "Glue")}
    asked, _ = _run_plan(monkeypatch, plan, 200.0, verify=False, retry_misses=False)
    assert asked == [0, 90, 180]


def test_track_found_by_retry_is_double_checked_ahead(monkeypatch):
    plan = {0: ("Bicep", "Glue"), 30: ("Bicep", "Glue"), 90: None,
            60: ("Fisher", "Losing It"), 120: ("Fisher", "Losing It"), 180: ("Fisher", "Losing It")}
    asked, entries = _run_plan(monkeypatch, plan, 200.0)
    # The double-check for a track found 30 s back listens past the missed slice (120, not 90).
    assert asked == [0, 30, 90, 60, 120, 180]
    assert [(e.title, e.confidence) for e in entries] == [("Glue", "High"), ("Losing It", "High")]
