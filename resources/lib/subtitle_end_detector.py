from __future__ import absolute_import, division, unicode_literals

import struct

import utils
import xbmc
import xbmcvfs
import time
from settings import SETTINGS

# EBML element IDs
_EBML_HEADER = 0x1A45DFA3
_SEGMENT = 0x18538067
_SEEK_HEAD = 0x114D9B74
_SEEK = 0x4DBB
_SEEK_ID = 0x53AB
_SEEK_POSITION = 0x53AC
_INFO = 0x1549A966
_TIMECODE_SCALE = 0x2AD7B1
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_TRACK_TYPE = 0x83
_TRACK_NAME = 0x536E
_TRACK_FLAG_FORCED = 0x55AA
_CODEC_ID = 0x86
_CUES = 0x1C53BB6B
_CUE_POINT = 0xBB
_CUE_TIME = 0xB3
_CUE_TRACK_POSITIONS = 0xB7
_CUE_TRACK = 0xF7
_CUE_CLUSTER_POSITION = 0xF1
_CLUSTER = 0x1F43B675
_CLUSTER_TIMESTAMP = 0xE7
_SIMPLE_BLOCK = 0xA3
_BLOCK_GROUP = 0xA0
_BLOCK = 0xA1
_BLOCK_DURATION = 0x9B

_TRACK_TYPE_SUBTITLE = 17
_SUPPORTED_CODECS = frozenset({
    'S_TEXT/UTF8', 'S_TEXT/ASS', 'S_TEXT/SSA', 'S_TEXT/WEBVTT',
})

# Number of last clusters to examine when looking for the final subtitle
_LAST_N_CLUSTERS = 10


# ── EBML utility functions ────────────────────────────────────────────────────

def _read_vint(reader):
    first = reader.read(1)
    if not first:
        return None, 0
    b = first[0]
    if b == 0:
        return None, 0
    length = 1
    mask = 0x80
    while length <= 8:
        if b & mask:
            break
        mask >>= 1
        length += 1
    if length > 8:
        return None, 0
    value = b & (mask - 1)
    if length > 1:
        rest = reader.read(length - 1)
        if len(rest) < length - 1:
            return None, 0
        for byte in rest:
            value = (value << 8) | byte
    return value, length


def _read_element_id(reader):
    first = reader.read(1)
    if not first:
        return None, 0
    b = first[0]
    if b == 0:
        return None, 0
    length = 1
    mask = 0x80
    while length <= 4:
        if b & mask:
            break
        mask >>= 1
        length += 1
    if length > 4:
        return None, 0
    value = b
    if length > 1:
        rest = reader.read(length - 1)
        if len(rest) < length - 1:
            return None, 0
        for byte in rest:
            value = (value << 8) | byte
    return value, length


def _read_uint(data):
    value = 0
    for b in data:
        value = (value << 8) | b
    return value


def _peek_block_track(reader, block_size):
    if block_size < 4:
        return None
    pos = reader.tell()
    peek = reader.read(min(4, block_size))
    reader.seek(pos)
    if not peek:
        return None
    b = peek[0]
    if b & 0x80:
        return b & 0x7F
    if len(peek) >= 2 and (b & 0x40):
        return ((b & 0x3F) << 8) | peek[1]
    if len(peek) >= 3 and (b & 0x20):
        return ((b & 0x1F) << 16) | (peek[1] << 8) | peek[2]
    if len(peek) >= 4 and (b & 0x10):
        return ((b & 0x0F) << 24) | (peek[1] << 16) | (peek[2] << 8) | peek[3]
    return None


def _read_block_header(data):
    if len(data) < 4:
        return None, 0, 0, 0
    b = data[0]
    length = 1
    mask = 0x80
    while length <= 4:
        if b & mask:
            break
        mask >>= 1
        length += 1
    if length > 4 or len(data) < length + 3:
        return None, 0, 0, 0
    track_num = b & (mask - 1)
    for i in range(1, length):
        track_num = (track_num << 8) | data[i]
    ts_offset = struct.unpack('>h', data[length:length + 2])[0]
    flags = data[length + 2]
    return track_num, ts_offset, flags, length + 3


# ── Buffered reader ───────────────────────────────────────────────────────────

class _BufferedReader(object):
    def __init__(self, file_obj, buffer_size=65536):
        self._file = file_obj
        self._buffer = b''
        self._buffer_pos = 0
        self._file_pos = 0
        self._buffer_start = 0
        self._buffer_size = buffer_size
        self._eof = False

    def tell(self):
        return self._file_pos

    def seek(self, offset):
        if self._buffer_start <= offset < self._buffer_start + len(self._buffer):
            self._buffer_pos = offset - self._buffer_start
            self._file_pos = offset
            return True
        self._buffer = b''
        self._buffer_pos = 0
        self._buffer_start = offset
        self._file_pos = offset
        self._eof = False
        try:
            self._file.seek(offset, 0)
            return True
        except Exception:
            return False

    def read(self, size):
        result = b''
        remaining = size
        while remaining > 0:
            available = len(self._buffer) - self._buffer_pos
            if available > 0:
                chunk_size = min(available, remaining)
                result += self._buffer[self._buffer_pos:self._buffer_pos + chunk_size]
                self._buffer_pos += chunk_size
                self._file_pos += chunk_size
                remaining -= chunk_size
            if remaining > 0:
                if self._eof:
                    break
                read_size = max(self._buffer_size, remaining)
                data = self._file.readBytes(read_size)
                if not data:
                    self._eof = True
                    break
                self._buffer = bytes(data)
                self._buffer_start = self._file_pos
                self._buffer_pos = 0
        return result


# ── MKV end parser ────────────────────────────────────────────────────────────

class MKVEndParser(object):
    """
    Parse an MKV file to find the timestamp of the last subtitle entry.

    Uses Cues for efficient random access; when Cues are absent returns None
    to avoid scanning the entire file over the network.
    """

    def __init__(self):
        self._timecode_scale = 1000000  # default: 1 ns/unit → 1 ms per unit
        self._segment_start = 0
        self._tracks = []  # list of (track_number, codec_id, track_name, forced_flag)
        self._cues = []    # list of (cue_time, cue_track_or_None, cluster_pos)

    def _get_track_priority(self, track_info):
        """Return a priority tuple for sorting: (type_priority, track_number).
        Lower values = higher priority. SDH (0) > FULL (1) > FORCED (2)."""
        track_number, _codec_id, track_name, forced_flag = track_info
        is_sdh = 'sdh' in (track_name or '').lower()
        if is_sdh:
            type_priority = 0
        elif forced_flag:
            type_priority = 2
        else:
            type_priority = 1
        return (type_priority, track_number)

    def _log(self, msg, level=utils.LOGINFO):
        utils.log(msg, name='MKVEndParser', level=level)

    def _get_max_end_percent(self):
        """Retrieve max END percent threshold from settings (90-99, default 95)."""
        max_pct = SETTINGS.get_int('detectSubtitlesMaxPercent', default=95) or 95
        return max(90, min(99, max_pct)) / 100.0

    def _format_percentage(self, timestamp_sec, duration_sec):
        """Format percentage string for logging. Returns '(pct%)' or empty string."""
        if not duration_sec or duration_sec <= 0:
            return ''
        pct = (timestamp_sec / duration_sec) * 100.0
        pct = min(100.0, pct)
        return ' ({:.1f}%)'.format(pct)

    def _should_reject_timestamp_by_threshold(self, timestamp_sec, duration_sec, max_percent):
        """Check if timestamp exceeds max percent threshold. Returns (rejected, percentage)."""
        if not duration_sec or duration_sec <= 0:
            return False, None
        pct = (timestamp_sec / duration_sec) * 100.0
        pct = min(100.0, pct)
        rejected = timestamp_sec > duration_sec * max_percent
        return rejected, pct

    def get_last_subtitle_timestamp(self, file_path):
        """
        Return the end timestamp in seconds of the last subtitle block, or None.

        Only works when the MKV file contains a Cues element (which most
        modern encoders produce).  For files without Cues, returns None so
        that the caller can fall back to other detection methods.

        Args:
            file_path:    Path accepted by xbmcvfs.File (local, smb://, …).
            stream_index: 0-based index of the subtitle stream to use.

        Returns:
            float (seconds) or None.
        """
        file_obj = None
        try:
            file_obj = xbmcvfs.File(file_path)
            reader = _BufferedReader(file_obj)

            if not self._read_ebml_header(reader):
                self._log('Not a valid EBML/MKV file', utils.LOGWARNING)
                return None
            self._log('EBML header OK (pos={0})'.format(reader.tell()))

            seg_start, seg_size = self._find_segment(reader)
            if seg_start is None:
                self._log('Segment element not found', utils.LOGWARNING)
                return None
            self._segment_start = seg_start

            seek_positions = {}
            self._parse_segment_headers(reader, seg_start, seg_size, seek_positions)
            self._log('SeekHead entries: {0}'.format(list(hex(k) for k in seek_positions)))

            if not self._tracks:
                tracks_pos = seek_positions.get(_TRACKS)
                if tracks_pos is not None:
                    self._log('Tracks not inline, seeking via SeekHead pos={0}'.format(tracks_pos))
                    reader.seek(self._segment_start + tracks_pos)
                    elem_id, _ = _read_element_id(reader)
                    elem_size, _ = _read_vint(reader)
                    if elem_id == _TRACKS and elem_size is not None:
                        self._parse_tracks(reader, elem_size)

            if not self._tracks:
                self._log('No subtitle tracks found', utils.LOGWARNING)
                return None

            self._log('All tracks: {0}'.format(
                [(num, name or 'unnamed', 'sdh' if 'sdh' in (name or '').lower() else ('forced' if forced else 'full')) 
                 for num, codec, name, forced in self._tracks]
            ))

            # Sort tracks by priority: SDH (0) > FULL (1) > FORCED (2)
            sorted_tracks = sorted(self._tracks, key=self._get_track_priority)
            best_tracks = sorted_tracks[:2]  # Take top 2 priority tracks
            best_track_numbers = [num for num, _, _, _ in best_tracks]
            self._log('Selected best {0} tracks by priority (SDH>FULL>FORCED): {1}'.format(
                len(best_track_numbers), best_track_numbers
            ), utils.LOGINFO)

            subtitle_track_numbers = frozenset(best_track_numbers)

            cues_pos = seek_positions.get(_CUES)
            if cues_pos is None:
                self._log('No Cues element; skipping subtitle end detection', utils.LOGWARNING)
                return None

            reader.seek(self._segment_start + cues_pos)
            elem_id, _ = _read_element_id(reader)
            elem_size, _ = _read_vint(reader)
            if elem_id != _CUES or elem_size is None:
                self._log('Cues element unreadable', utils.LOGWARNING)
                return None

            self._parse_cues(reader, elem_size)

            if not self._cues:
                self._log('Cues list empty after parse', utils.LOGWARNING)
                return None

            # Calculate duration from cues for percentage checks
            cue_ms_times = [(c[0] * self._timecode_scale) // 1000000 for c in self._cues]
            duration = (max(cue_ms_times) / 1000.0) if cue_ms_times else None

            # Filter cluster positions to only those of subtitle tracks
            subtitle_cluster_offsets = sorted(set(
                pos for _, track, pos in self._cues
                if track in subtitle_track_numbers
            ))
            if not subtitle_cluster_offsets:
                subtitle_cluster_offsets = sorted(set(pos for _, _, pos in self._cues))

            if not subtitle_cluster_offsets:
                return None

            last_clusters = subtitle_cluster_offsets[-_LAST_N_CLUSTERS:]
            self._log('Examining last {0} cluster(s) (offsets: {1})'.format(
                len(last_clusters), last_clusters
            ))

            # Collect subtitle blocks from clusters
            all_blocks = []
            for cluster_pos in last_clusters:
                abs_offset = self._segment_start + cluster_pos
                reader.seek(abs_offset)
                elem_id, _ = _read_element_id(reader)
                if elem_id != _CLUSTER:
                    self._log('Expected CLUSTER at {0}, got {1}'.format(
                        abs_offset, hex(elem_id) if elem_id else 'None'
                    ), utils.LOGWARNING)
                    continue
                elem_size, _ = _read_vint(reader)
                if elem_size is None:
                    continue
                blocks = self._parse_cluster_any_subtitle(reader, elem_size, subtitle_track_numbers)
                all_blocks.extend(blocks)

            if not all_blocks:
                self._log('No subtitle blocks found in last {0} clusters'.format(
                    len(last_clusters)
                ), utils.LOGWARNING)
                return None

            # Build track metadata: (type_name, is_text_codec)
            track_meta = {}
            for num, codec, name, forced in self._tracks:
                if num in subtitle_track_numbers:
                    is_text = (codec or '').upper() in _SUPPORTED_CODECS
                    if 'sdh' in (name or '').lower():
                        track_meta[num] = ('SDH', is_text)
                    elif forced:
                        track_meta[num] = ('FORCED', is_text)
                    else:
                        track_meta[num] = ('FULL', is_text)


            # Keywords for end-of-episode detection
            END_KEYWORDS = [
                'subtitles', 'sous-titres', 'перевод субтитров', 'subtitles by', 'traduction',
                'subtitling', 'adaptation', 'captioning', 'caption', 'captioned by', 'subtitulado', 'traducción'
            ]

            # Find END-marked subtitles in last blocks of each track
            end_blocks = []
            logged_blocks = set()
            for num in subtitle_track_numbers:
                track_type, is_text = track_meta.get(num, ('UNK', False))
                last_blocks = [b for b in all_blocks if b[0] == num][-5:]
                
                # Search for END keyword in last blocks
                found_end = False
                for ts_ms, end_ms, text in [(b[1], b[2], b[3]) for b in last_blocks]:
                    if text and any(kw in text.lower() for kw in END_KEYWORDS):
                        block_id = (num, ts_ms, end_ms, text)
                        if block_id not in logged_blocks:
                            pct_str = self._format_percentage(end_ms / 1000.0, duration)
                            if is_text:
                                display_text = (text or '').replace('\n', ' ').replace('\x00', ' ')
                                self._log('Track {0} ({1}) END: {2:.1f}s - {3:.1f}s | "{4}"{5}'.format(
                                    num, track_type, ts_ms / 1000.0, end_ms / 1000.0, display_text, pct_str
                                ), utils.LOGINFO)
                            else:
                                self._log('Track {0} ({1}) END: {2:.1f}s - {3:.1f}s{4}'.format(
                                    num, track_type, ts_ms / 1000.0, end_ms / 1000.0, pct_str
                                ), utils.LOGINFO)
                            logged_blocks.add(block_id)
                            end_blocks.append((num, ts_ms, end_ms, text))
                            found_end = True
                            break
                
                # Log last subtitle if no END keyword found
                if not found_end and last_blocks:
                    ts_ms, end_ms, text = last_blocks[-1][1], last_blocks[-1][2], last_blocks[-1][3]
                    block_id = (num, ts_ms, end_ms, text)
                    if block_id not in logged_blocks:
                        pct_str = self._format_percentage(end_ms / 1000.0, duration)
                        if is_text:
                            display_text = (text or '').replace('\n', ' ').replace('\x00', ' ')
                            self._log('Track {0} ({1}): {2:.1f}s - {3:.1f}s | "{4}"{5}'.format(
                                num, track_type, ts_ms / 1000.0, end_ms / 1000.0, display_text, pct_str
                            ), utils.LOGINFO)
                        else:
                            self._log('Track {0} ({1}): {2:.1f}s - {3:.1f}s{4}'.format(
                                num, track_type, ts_ms / 1000.0, end_ms / 1000.0, pct_str
                            ), utils.LOGINFO)
                        logged_blocks.add(block_id)

            # Apply max percent threshold check to both END and fallback detection
            max_percent = self._get_max_end_percent()
            
            if end_blocks:
                # Use first END-marked block
                end_seconds = end_blocks[0][2] / 1000.0
            else:
                # Fallback to last subtitle of any track
                last_block = max(all_blocks, key=lambda b: b[1])
                end_seconds = last_block[2] / 1000.0

            # Validate timestamp against max percent threshold
            if duration:
                rejected, pct = self._should_reject_timestamp_by_threshold(end_seconds, duration, max_percent)
                if rejected and pct is not None:
                    msg = 'Subtitle detected at {:.1f}% (max={:.0f}%) - {:.1f}s > {:.1f}s. Fallback to default detection'.format(
                        pct, max_percent * 100, end_seconds, duration * max_percent
                    )
                    self._log(msg, utils.LOGINFO)
                    return None

            return end_seconds

        except Exception as exc:
            self._log('Error: {0}'.format(exc), utils.LOGERROR)
            import traceback
            self._log(traceback.format_exc(), utils.LOGERROR)
            return None
        finally:
            if file_obj:
                try:
                    file_obj.close()
                except Exception:
                    pass

    # ── EBML header & Segment ─────────────────────────────────────────────────

    def _read_ebml_header(self, reader):
        elem_id, _ = _read_element_id(reader)
        if elem_id != _EBML_HEADER:
            return False
        elem_size, _ = _read_vint(reader)
        if elem_size is None:
            return False
        reader.read(elem_size)
        return True

    def _find_segment(self, reader):
        elem_id, _ = _read_element_id(reader)
        if elem_id != _SEGMENT:
            return None, None
        elem_size, _ = _read_vint(reader)
        if elem_size is None:
            return None, None
        return reader.tell(), elem_size

    # ── Segment children ──────────────────────────────────────────────────────

    def _parse_segment_headers(self, reader, seg_start, seg_size, seek_positions):
        end_pos = seg_start + seg_size
        max_scan = min(end_pos, seg_start + 100 * 1024 * 1024)
        reader.seek(seg_start)
        found_tracks = False
        found_seekhead = False

        while reader.tell() < max_scan:
            elem_start = reader.tell()
            elem_id, _ = _read_element_id(reader)
            if elem_id is None:
                break
            elem_size, _ = _read_vint(reader)
            if elem_size is None:
                break
            data_start = reader.tell()

            if elem_id == _SEEK_HEAD:
                self._parse_seekhead(reader, elem_size, seek_positions)
                found_seekhead = True
            elif elem_id == _INFO:
                self._parse_info(reader, elem_size)
            elif elem_id == _TRACKS:
                self._parse_tracks(reader, elem_size)
                found_tracks = True
            elif elem_id == _CLUSTER:
                reader.seek(elem_start)
                break

            reader.seek(data_start + elem_size)
            if found_tracks and found_seekhead:
                break

    def _parse_seekhead(self, reader, size, seek_positions):
        end_pos = reader.tell() + size
        while reader.tell() < end_pos:
            elem_id, _ = _read_element_id(reader)
            if elem_id is None:
                break
            elem_size, _ = _read_vint(reader)
            if elem_size is None:
                break
            data_start = reader.tell()
            if elem_id == _SEEK:
                self._parse_seek_entry(reader, elem_size, seek_positions)
            reader.seek(data_start + elem_size)

    def _parse_seek_entry(self, reader, size, seek_positions):
        end_pos = reader.tell() + size
        seek_id = None
        seek_pos = None
        while reader.tell() < end_pos:
            elem_id, _ = _read_element_id(reader)
            if elem_id is None:
                break
            elem_size, _ = _read_vint(reader)
            if elem_size is None:
                break
            data_start = reader.tell()
            if elem_id == _SEEK_ID:
                seek_id = _read_uint(reader.read(elem_size))
            elif elem_id == _SEEK_POSITION:
                seek_pos = _read_uint(reader.read(elem_size))
            reader.seek(data_start + elem_size)
        if seek_id is not None and seek_pos is not None:
            seek_positions[seek_id] = seek_pos

    def _parse_info(self, reader, size):
        end_pos = reader.tell() + size
        while reader.tell() < end_pos:
            elem_id, _ = _read_element_id(reader)
            if elem_id is None:
                break
            elem_size, _ = _read_vint(reader)
            if elem_size is None:
                break
            data_start = reader.tell()
            if elem_id == _TIMECODE_SCALE:
                self._timecode_scale = _read_uint(reader.read(elem_size))
            reader.seek(data_start + elem_size)

    # ── Tracks ────────────────────────────────────────────────────────────────

    def _parse_tracks(self, reader, size):
        end_pos = reader.tell() + size
        while reader.tell() < end_pos:
            elem_id, _ = _read_element_id(reader)
            if elem_id is None:
                break
            elem_size, _ = _read_vint(reader)
            if elem_size is None:
                break
            data_start = reader.tell()
            if elem_id == _TRACK_ENTRY:
                track = self._parse_track_entry(reader, elem_size)
                if track is not None:
                    self._tracks.append(track)
            reader.seek(data_start + elem_size)

    def _parse_track_entry(self, reader, size):
        """Return (track_number, codec_id, track_name, forced_flag) for subtitle tracks, else None."""
        end_pos = reader.tell() + size
        track_number = 0
        track_type = 0
        codec_id = ''
        track_name = ''
        forced_flag = False

        while reader.tell() < end_pos:
            elem_id, _ = _read_element_id(reader)
            if elem_id is None:
                break
            elem_size, _ = _read_vint(reader)
            if elem_size is None:
                break
            data_start = reader.tell()
            if elem_id == _TRACK_NUMBER:
                track_number = _read_uint(reader.read(elem_size))
            elif elem_id == _TRACK_TYPE:
                track_type = _read_uint(reader.read(elem_size))
            elif elem_id == _TRACK_NAME:
                track_name = reader.read(elem_size).decode('utf-8', errors='replace')
            elif elem_id == _TRACK_FLAG_FORCED:
                forced_flag = bool(_read_uint(reader.read(elem_size)))
            elif elem_id == _CODEC_ID:
                codec_id = reader.read(elem_size).decode('ascii', errors='replace')
            reader.seek(data_start + elem_size)

        if track_type != _TRACK_TYPE_SUBTITLE:
            return None
        # Accept subtitle tracks regardless of codec so we can at least use
        # their block timestamps for end detection (PGS/image subtitles
        # don't contain text but still provide timing).  If the codec is a
        # known text codec we keep it as-is; otherwise log an informational
        # message and return the track for timestamp-only use.
        codec_up = (codec_id or '').upper()
        if codec_up in _SUPPORTED_CODECS:
            return (track_number, codec_id, track_name, forced_flag)
        return (track_number, codec_id, track_name, forced_flag)

    # ── Cues ──────────────────────────────────────────────────────────────────

    def _parse_cues(self, reader, size):
        end_pos = reader.tell() + size
        while reader.tell() < end_pos:
            elem_id, _ = _read_element_id(reader)
            if elem_id is None:
                break
            elem_size, _ = _read_vint(reader)
            if elem_size is None:
                break
            data_start = reader.tell()
            if elem_id == _CUE_POINT:
                self._parse_cue_point(reader, elem_size)
            reader.seek(data_start + elem_size)

    def _parse_cue_point(self, reader, size):
        end_pos = reader.tell() + size
        cue_time = 0
        while reader.tell() < end_pos:
            elem_id, _ = _read_element_id(reader)
            if elem_id is None:
                break
            elem_size, _ = _read_vint(reader)
            if elem_size is None:
                break
            data_start = reader.tell()
            if elem_id == _CUE_TIME:
                cue_time = _read_uint(reader.read(elem_size))
            elif elem_id == _CUE_TRACK_POSITIONS:
                cue_track, cluster_pos = self._parse_cue_track_positions(reader, elem_size)
                if cluster_pos is not None:
                    self._cues.append((cue_time, cue_track, cluster_pos))
            reader.seek(data_start + elem_size)

    def _parse_cue_track_positions(self, reader, size):
        end_pos = reader.tell() + size
        cue_track = None
        cluster_pos = None
        while reader.tell() < end_pos:
            elem_id, _ = _read_element_id(reader)
            if elem_id is None:
                break
            elem_size, _ = _read_vint(reader)
            if elem_size is None:
                break
            data_start = reader.tell()
            if elem_id == _CUE_TRACK:
                cue_track = _read_uint(reader.read(elem_size))
            elif elem_id == _CUE_CLUSTER_POSITION:
                cluster_pos = _read_uint(reader.read(elem_size))
            reader.seek(data_start + elem_size)
        return cue_track, cluster_pos

    # ── Cluster ───────────────────────────────────────────────────────────────

    def _parse_cluster_any_subtitle(self, reader, size, target_track_nums):
        """Parse a Cluster and return list of (track_num, timestamp_ms, end_ms, text) for any of the target tracks."""
        end_pos = reader.tell() + size
        cluster_timestamp = 0
        blocks = []

        while reader.tell() < end_pos:
            elem_id, _ = _read_element_id(reader)
            if elem_id is None:
                break
            elem_size, _ = _read_vint(reader)
            if elem_size is None:
                break
            data_start = reader.tell()

            if elem_id == _CLUSTER_TIMESTAMP:
                cluster_timestamp = _read_uint(reader.read(elem_size))
            elif elem_id == _SIMPLE_BLOCK:
                track_num = _peek_block_track(reader, elem_size)
                if track_num in target_track_nums:
                    data = reader.read(elem_size)
                    result = self._block_timestamps(data, cluster_timestamp, 0)
                    if result is not None:
                        ts_ms, end_ms, text = result if len(result) == 3 else (result[0], result[1], None)
                        blocks.append((track_num, ts_ms, end_ms, text))
            elif elem_id == _BLOCK_GROUP:
                result = self._parse_block_group_any_subtitle(
                    reader, elem_size, cluster_timestamp, target_track_nums
                )
                if result is not None:
                    track_num, ts_ms, end_ms, text = result
                    blocks.append((track_num, ts_ms, end_ms, text))

            reader.seek(data_start + elem_size)

        return blocks

    def _parse_block_group_any_subtitle(self, reader, size, cluster_timestamp, target_track_nums):
        end_pos = reader.tell() + size
        block_data = None
        block_duration = 0
        track_num = None

        while reader.tell() < end_pos:
            elem_id, _ = _read_element_id(reader)
            if elem_id is None:
                break
            elem_size, _ = _read_vint(reader)
            if elem_size is None:
                break
            data_start = reader.tell()
            if elem_id == _BLOCK:
                track_num = _peek_block_track(reader, elem_size)
                if track_num in target_track_nums:
                    block_data = reader.read(elem_size)
                else:
                    # Block is for a non-subtitle track; skip the rest
                    reader.seek(data_start + elem_size)
                    break
            elif elem_id == _BLOCK_DURATION:
                block_duration = _read_uint(reader.read(elem_size))
            reader.seek(data_start + elem_size)

        if block_data is not None and track_num is not None:
            result = self._block_timestamps(block_data, cluster_timestamp, block_duration)
            if result is not None:
                ts_ms, end_ms, text = result if len(result) == 3 else (result[0], result[1], None)
                return (track_num, ts_ms, end_ms, text)
        return None

    def _block_timestamps(self, data, cluster_timestamp, duration):
        """Return (timestamp_ms, end_ms, text) from raw block data, or None."""
        track_num, ts_offset, flags, header_size = _read_block_header(data)
        if track_num is None:
            return None
        # Skip laced blocks (unusual for subtitles)
        if (flags >> 1) & 0x03:
            return None
        abs_timestamp = cluster_timestamp + ts_offset
        timestamp_ms = abs_timestamp * self._timecode_scale // 1000000
        duration_ms = duration * self._timecode_scale // 1000000 if duration else 0
        end_ms = timestamp_ms + duration_ms if duration_ms > 0 else timestamp_ms
        # Extract subtitle text (for S_TEXT/UTF8, S_TEXT/ASS, etc.)
        text = None
        # The subtitle payload starts after the header
        if len(data) > header_size:
            try:
                text = data[header_size:].decode('utf-8', errors='replace').strip()
            except Exception:
                text = None
        return (timestamp_ms, end_ms, text)


# ── Public API ────────────────────────────────────────────────────────────────

class SubtitleEndDetector(object):
    """
    Detects end-of-content time using the last subtitle timestamp in an MKV.

    Integrates with UpNext's state/player: on success calls
    ``state.set_detected_popup_time()`` and fires the
    ``upnext_credits_detected`` event.
    """

    @classmethod
    def log(cls, msg, level=utils.LOGDEBUG):
        utils.log(msg, name=cls.__name__, level=level)

    def __init__(self, player, state):
        self.player = player
        self.state = state

    def detect(self, file_path):
        """
        Try to determine end-of-content from the last subtitle in the file.

        Returns True when a timestamp was found and applied, False otherwise.
        """
        if not file_path:
            self.log('detect() called with empty file_path', utils.LOGWARNING)
            return False

        file_lower = file_path.lower()
        # If the playing item is not a direct MKV path (eg. plugin://), try to
        # resolve the underlying stream URL from the player a few times. This
        # helps when inputstream/input add-ons proxy/resolve a real HTTP MKV
        # after playback starts.
        if not file_lower.endswith('.mkv'):
            self.log(
                'File not a direct MKV ({0}), attempting to resolve stream URL'.format(
                    file_path
                ),
                utils.LOGINFO
            )
            resolved = None
            for _ in range(3):
                try:
                    resolved = self.player.getPlayingFile(use_info=False)
                except Exception:
                    resolved = None
                if not resolved:
                    try:
                        resolved = xbmc.getInfoLabel('Player.FilenameAndPath')
                    except Exception:
                        resolved = None
                if resolved:
                    resolved = resolved.strip()
                    if not resolved.lower().startswith('plugin://'):
                        self.log('Resolved playing file to: {0}'.format(resolved), utils.LOGINFO)
                        file_path = resolved
                        break
                time.sleep(1)
            else:
                self.log(
                    'Not an MKV file ({0}), skipping subtitle end detection'.format(
                        file_path
                    ),
                    utils.LOGINFO
                )
                return False

        timestamp = MKVEndParser().get_last_subtitle_timestamp(file_path)
        if timestamp is None:
            self.log(
                'Subtitle end detection: no usable timestamp found',
                utils.LOGWARNING
            )
            return False

        # Sanity check: reject timestamps before the configured detection
        # threshold to avoid setting an early popup from sparse subtitle tracks.
        total_time = getattr(self.state, 'total_time', 0) if self.state else 0
        threshold_pct = getattr(SETTINGS, 'detect_threshold', 80)
        if total_time > 0:
            min_time = total_time * threshold_pct / 100.0
            if timestamp < min_time:
                self.log(
                    'Detected timestamp {0:.1f}s is before {1}%% of total {2:.1f}s '
                    '(min={3:.1f}s) - ignoring'.format(
                        timestamp, threshold_pct, total_time, min_time
                    ),
                    utils.LOGWARNING
                )
                return False

        self.log(
            'Subtitle end detected at {0:.1f}s - applying popup time'.format(timestamp),
            utils.LOGINFO
        )
        if self.state:
            self.log(
                'Previous popup_time={0} popup_cue={1}'.format(
                    getattr(self.state, 'popup_time', 'N/A'),
                    getattr(self.state, 'popup_cue', 'N/A')
                ),
                utils.LOGINFO
            )
            self.state.set_detected_popup_time(timestamp)
            utils.event('upnext_credits_detected', internal=True)
            self.log(
                'New popup_time={0}'.format(getattr(self.state, 'popup_time', 'N/A')),
                utils.LOGINFO
            )
        return True
