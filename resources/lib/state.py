# -*- coding: utf-8 -*-
# GNU General Public License v2.0 (see COPYING or https://www.gnu.org/licenses/gpl-2.0.txt)

from __future__ import absolute_import, division, unicode_literals

import api
import constants
import upnext
import utils
from settings import SETTINGS


class UpNextState(object):  # pylint: disable=too-many-public-methods
    """Class encapsulating all state variables and methods"""

    __slots__ = (
        # Addon data
        'data',
        'encoding',
        # Current video details
        'current_item',
        'filename',
        'total_time',
        # Popup state variables
        'next_item',
        'popup_time',
        'popup_cue',
        'detect_time',
        'shuffle_on',
        # Tracking player state variables
        'starting',
        'tracking',
        'played_in_a_row',
        'queued',
        'playing_next',
        'keep_playing',
    )

    def __init__(self, reset=False, test=False):
        self.log('Reset' if reset else 'Init')

        # Plugin data
        self.data = None
        self.encoding = 'base64'
        # Current video details
        self.current_item = utils.create_item_details(item=None, reset=True)
        self.filename = None
        self.total_time = 0
        # Popup state variables
        self.next_item = None
        self.popup_time = 0
        self.popup_cue = False
        self.detect_time = 0
        self.shuffle_on = False
        # Tracking player state variables
        self.starting = 0
        self.tracking = False
        self.played_in_a_row = 1
        self.queued = False
        self.playing_next = False
        self.keep_playing = False

        if test:
            api.DISABLE_RETRY = True

    @classmethod
    def log(cls, msg, level=utils.LOGDEBUG):
        utils.log(msg, name=cls.__name__, level=level)

    def reset(self):
        self.__init__(reset=True)  # pylint: disable=unnecessary-dunder-call

    def reset_item(self):
        if self.next_item:
            self.current_item = self.next_item
        else:
            self.current_item = utils.create_item_details(
                item=self.current_item,
                reset=True,
            )
        self.next_item = None

    def get_tracked_file(self):
        return self.filename

    def is_tracking(self):
        return self.tracking

    def start_tracking(self, filename):
        self.tracking = True
        self.filename = filename
        self.log('Tracking enabled: {0}'.format(filename), utils.LOGINFO)

    def stop_tracking(self):
        self.tracking = False
        self.log('Tracking stopped')

    def reset_tracking(self):
        self.tracking = False
        self.filename = None
        self.log('Tracking reset')

    def reset_queue(self, on_start=False):
        if self.queued:
            if on_start:
                playcount = self.played_in_a_row
                self.played_in_a_row = playcount + 1
                self.log('Increment group playcount for queued item: {0} to {1}'
                         .format(playcount, self.played_in_a_row))
            self.queued = api.reset_queue()

    def get_next(self):
        """Get next video to play, based on current video source"""

        next_position, _ = api.get_playlist_position(offset=1)
        plugin_type = self.get_plugin_type(next_position)

        # Next episode from plugin data
        if plugin_type:
            next_video = self.data.get('next_video')
            source = constants.PLUGIN_TYPES[plugin_type]

            if (SETTINGS.unwatched_only
                    and utils.get_int(next_video, 'playcount') > 0):
                next_video = None
            self.log('Plugin next_video: {0}'.format(next_video))

        # Next item from non-plugin playlist
        elif next_position and not self.shuffle_on:
            next_video = api.get_from_playlist(
                position=next_position,
                properties=(api.EPISODE_PROPERTIES | api.MOVIE_PROPERTIES),
                unwatched_only=SETTINGS.unwatched_only
            )
            source = 'playlist'

        # Next video from Kodi library
        else:
            next_video = api.get_next_from_library(
                item=self.current_item,
                next_season=SETTINGS.next_season,
                unwatched_only=SETTINGS.unwatched_only,
                random=self.shuffle_on
            )
            source = 'library'

            # Show Still Watching? popup if next episode is from next season or
            # next item is a movie
            if (next_video and (
                    next_video.get('type') == 'movie'
                    or (not self.shuffle_on
                        and len({constants.SPECIALS,
                                 next_video.get('season'),
                                 self.current_item['details']['season']}) == 3)
            )):
                self.played_in_a_row = SETTINGS.played_limit

        if next_video:
            self.next_item = utils.create_item_details(next_video, source,
                                                       next_position)
        return self.next_item

    def get_detect_time(self):
        return self.detect_time

    def _set_detect_time(self):
        # Don't use detection time period if a plugin cue point was provided,
        # or end credits detection is disabled
        if self.popup_cue or not SETTINGS.detect_enabled:
            self.detect_time = None
            return

        # Detection time period starts before normal popup time
        self.detect_time = max(
            0,
            self.popup_time - (SETTINGS.detect_period * self.total_time / 3600)
        )

    def get_popup_time(self):
        return self.popup_time

    def set_detected_popup_time(self, detected_time):
        popup_time = 0

        # Detected popup time overrides plugin data and settings
        if detected_time:
            # Force popup time to specified play time
            popup_time = detected_time

            # Enable cue point unless forced off in sim mode
            self.popup_cue = SETTINGS.sim_cue != constants.SETTING_OFF

        self.popup_time = popup_time
        self._set_detect_time()

        self.log('Popup: due at {0}s of {1}s (cue: {2})'.format(
            self.popup_time, self.total_time, self.popup_cue
        ), utils.LOGINFO)

    @staticmethod
    def get_final_chapter_time(total_time, threshold=80):
        """Get the start time of the final chapter if it's likely credits.
        Returns the time in seconds, or None if no suitable chapter found.
        
        Detection logic (in priority order):
        1. Method 2 (high confidence): If exactly ONE chapter exists in the
           last 30% of the video AND it starts in the last 10%, it's credits.
        2. Method 1 (fallback): If the last chapter starts at or after the
           threshold percentage, use it as credits.
        
        Note: Chapters starting less than 10 seconds from the end are ignored
        as they represent end-of-video markers, not credits."""
        try:
            import xbmc
            chapters_str = xbmc.getInfoLabel('Player.Chapters')
            if not chapters_str:
                return None
            # Chapters are comma-separated percentages
            chapters = [float(c.strip()) for c in chapters_str.split(',') if c.strip()]
            if len(chapters) < 2:
                # Need at least 2 chapters (content + credits)
                return None
            
            # Filter out chapters that start less than 10 seconds from the end
            # (these are end-of-video markers, not credits)
            min_threshold = ((total_time - 10) / total_time) * 100 if total_time > 10 else 0
            valid_chapters = [c for c in chapters if c < min_threshold]
            if len(valid_chapters) < 2:
                return None
            
            # Method 2 (priority): If exactly ONE chapter in last 30% AND in last 10%
            chapters_in_last_30 = [c for c in valid_chapters if c >= 70]
            if len(chapters_in_last_30) == 1 and chapters_in_last_30[0] >= 90:
                final_chapter = chapters_in_last_30[0]
                return int((final_chapter / 100) * total_time)
            
            # Method 1 (fallback): Use last valid chapter if it's after threshold
            final_chapter = valid_chapters[-1]
            if final_chapter >= threshold:
                return int((final_chapter / 100) * total_time)
                
        except (ValueError, TypeError, AttributeError):
            pass
        return None

    def set_popup_time(self, total_time):
        popup_time = 0

        # Use 1s offset from total_time to try and avoid race condition with
        # internal Kodi playlist handling
        self.total_time = total_time
        if SETTINGS.enable_queue:
            total_time -= 1

        # Try chapter detection first if enabled
        if SETTINGS.detect_chapters:
            chapter_time = self.get_final_chapter_time(
                total_time, SETTINGS.detect_chapters_threshold
            )
            if chapter_time:
                popup_time = chapter_time
                # Disable cue point to keep popup until video ends (like fallback)
                self.popup_cue = SETTINGS.sim_cue == constants.SETTING_ON
                self.log('Popup: using chapter detection at {0}s'.format(popup_time))

        # Alway use plugin data, when available
        if not popup_time and self.get_plugin_type():
            # Some plugins send the time from video end
            popup_duration = utils.get_int(self.data, 'notification_time', 0)
            # Some plugins send the time from video start (e.g. Netflix)
            popup_time = utils.get_int(self.data, 'notification_offset', 0)

            # Ensure popup duration is not too short
            if constants.POPUP_MIN_DURATION <= popup_duration < total_time:
                popup_time = total_time - popup_duration

            # Ensure popup time is not too close to end of playback
            if 0 < popup_time <= total_time - constants.POPUP_MIN_DURATION:
                # Enable cue point unless forced off in sim mode
                self.popup_cue = SETTINGS.sim_cue != constants.SETTING_OFF
            # Otherwise ignore popup time from plugin data
            else:
                popup_time = 0

        # Use addon settings as fallback option
        if not popup_time:
            # Time from video end
            popup_duration = SETTINGS.popup_durations[max(0, 0, *[
                duration for duration in SETTINGS.popup_durations
                if total_time > duration
            ])]

            # Ensure popup duration is not too short
            if constants.POPUP_MIN_DURATION <= popup_duration < total_time:
                popup_time = total_time - popup_duration
            # Otherwise set default popup time
            else:
                popup_time = total_time - constants.POPUP_MIN_DURATION

            # Disable cue point unless forced on in sim mode
            self.popup_cue = SETTINGS.sim_cue == constants.SETTING_ON

        self.popup_time = popup_time
        self._set_detect_time()

        self.log('Popup: due at {0}s of {1}s (cue: {2})'.format(
            self.popup_time, self.total_time, self.popup_cue
        ), utils.LOGINFO)

    def process_now_playing(self, playlist_position, plugin_type, play_info):
        if plugin_type:
            new_video = self._get_plugin_now_playing(play_info)
            source = constants.PLUGIN_TYPES[plugin_type]

        elif playlist_position:
            new_video = api.get_from_playlist(
                position=playlist_position,
                properties=api.get_json_properties(play_info)
            )
            source = 'playlist'

        else:
            new_video = self._get_library_now_playing(play_info)
            source = 'library' if new_video else None

        if new_video and source:
            new_item = utils.create_item_details(new_video, source,
                                                 playlist_position)

            # Reset played in a row count if new tvshow or set is playing,
            # unless playing from a playlist
            new_group = new_item['group_name']
            current_group = self.current_item['group_name']
            if new_group != current_group:
                self.played_in_a_row = 1
                self.log('Reset playcount: group change - {0} to {1}'.format(
                    current_group, new_group
                ))

            self.current_item = new_item
        return self.current_item

    def _get_plugin_now_playing(self, play_info):
        if self.data:
            # Fallback to now playing info if plugin does not provide current
            # episode details
            current_video = self.data.get('current_video')
            if not current_video:
                current_video = api.get_now_playing(
                    properties=api.get_json_properties(play_info),
                    retry=SETTINGS.api_retry_attempts,
                )
        else:
            current_video = None

        self.log('Plugin current_video: {0}'.format(current_video))
        if not current_video:
            return None

        return current_video

    @classmethod
    # pylint: disable-next=too-many-branches,too-many-return-statements,too-many-locals
    def _get_library_now_playing(cls, play_info):
        if 'id' in play_info['item']:
            current_video = api.get_from_library(item=play_info['item'])
        else:
            current_video = api.get_now_playing(
                properties=api.get_json_properties(play_info, {'mediapath'}),
                retry=SETTINGS.api_retry_attempts
            )
        if not current_video:
            return None

        video_type = current_video['type']
        plugin_url = current_video.get('mediapath') or current_video.get('file', '')
        tmdb_id = None
        tmdb_type = None

        if plugin_url.startswith('plugin://'):
            _, _, addon_args = utils.parse_url(plugin_url)
            tmdb_id = addon_args.get('tmdb_id')
            tmdb_type = addon_args.get('tmdb_type')
            if video_type in ('unknown', '') and tmdb_type:
                video_type = 'movie' if tmdb_type == 'movie' else 'episode'

        if video_type == 'movie':
            if (current_video['set']
                    and utils.get_int(current_video, 'setid') > 0):
                return current_video
            if SETTINGS.enable_tmdbhelper_fallback:
                # Try to get TMDB ID from plugin URL or by searching
                if not tmdb_id:
                    from tmdb_helper import TMDb
                    title = current_video.get('title', '')
                    year = utils.get_int(current_video, 'year')
                    if title:
                        tmdb_id = TMDb().get_tmdb_id(
                            tmdb_type='movie', query=title, year=year if year else None
                        )
                if tmdb_id:
                    return cls._get_tmdb_movie_now_playing(current_video, tmdb_id)
            return None

        # Previously resolved listitems may lose infotags that are set when the
        # listitem is resolved. Fallback to Player notification data.
        values_to_ignore = {constants.UNDEFINED, constants.UNKNOWN, ''}
        for info, value in play_info['item'].items():
            if current_video.get(info, '') in values_to_ignore:
                current_video[info] = value

        tvshowid = current_video.get('tvshowid', constants.UNDEFINED)
        title = current_video.get('showtitle')
        season = utils.get_int(current_video, 'season')
        episode = utils.get_int(current_video, 'episode')

        # Fallback to play_info item data if empty (for plugins)
        if not title or constants.UNDEFINED in {season, episode}:
            play_item = play_info.get('item', {})
            title = title or play_item.get('showtitle')
            if season == constants.UNDEFINED:
                season = utils.get_int(play_item, 'season', season)
            if episode == constants.UNDEFINED:
                episode = utils.get_int(play_item, 'episode', episode)
        
        if not title or constants.UNDEFINED in {season, episode}:
            return None

        plugin_url = None
        addon_id = None
        supported_addons = {constants.ADDON_ID, constants.TMDBH_ADDON_ID}
        params_to_replace = ('player', 'tmdb_id', 'season', 'episode')
        for plugin_url_type in ('mediapath', 'file'):
            _plugin_url = current_video.get(plugin_url_type, '')
            if (_plugin_url == plugin_url
                    or not _plugin_url.startswith('plugin://')):
                continue
            plugin_url = _plugin_url
            addon_id, _, addon_args = utils.parse_url(plugin_url)
            if addon_id in supported_addons:
                addon_id = None
            else:
                break
            for param in params_to_replace:
                value = addon_args.get(param, '')
                if value in values_to_ignore:
                    continue
                current_video[param] = value

        if tvshowid == constants.UNDEFINED or plugin_url:
            # Video plugins can provide a plugin specific tvshowid. Search Kodi
            # library for tvshow title instead.
            tvshowid = api.get_tvshowid(title)
        # Now playing show not found in Kodi library
        if tvshowid == constants.UNDEFINED:
            if SETTINGS.enable_tmdbhelper_fallback:
                return cls._get_tmdb_now_playing(
                    current_video, title, season, episode, addon_id
                )
            return None
        # Use found tvshowid for library integrated plugins e.g. Emby,
        # Jellyfin, Plex, etc.
        current_video['tvshowid'] = tvshowid

        # Get current episode id or search in library if detail missing
        episodeid = (utils.get_int(current_video, 'episodeid', None)
                     or utils.get_int(current_video, 'id'))
        if episodeid == constants.UNDEFINED:
            details = api.get_episode_info(tvshowid, season, episode)
            # Now playing episode not found in library
            if not details:
                return None
            current_video = dict(current_video, **details)
        else:
            current_video['episodeid'] = episodeid

        return current_video

    @staticmethod
    def _get_tmdb_now_playing(current_video, title, season, episode, addon_id):
        if not SETTINGS.import_tmdbhelper:
            return None

        from tmdb_helper import TMDb, get_item_details, get_next_episodes

        tmdb_id = current_video.get('tmdb_id')
        if not tmdb_id:
            tmdb_id = TMDb().get_tmdb_id(
                tmdb_type='tv', query=title, season=season, episode=episode
            )

        current_details = get_item_details('tv', tmdb_id, season, episode)
        episodes = get_next_episodes(tmdb_id, season, episode)
        
        # Return None if no episodes found
        if not episodes:
            return None
        
        player_name = current_video.get('player', addon_id)
        current_infolabels = getattr(current_details, 'infolabels', {}) or {}
        current_art = getattr(current_details, 'art', {}) or {}
        next_infolabels = getattr(episodes[0], 'infolabels', {}) or {}
        next_art = getattr(episodes[0], 'art', {}) or {}
        
        upnext.send_signal(
            sender='UpNext.TMDBHelper',
            upnext_info={
                'current_video': dict(
                    current_infolabels,
                    tmdb_id=tmdb_id,
                    art=current_art,
                    showtitle=title,
                ),
                'next_video': dict(
                    next_infolabels,
                    tmdb_id=tmdb_id,
                    art=next_art,
                    showtitle=title,
                ),
                'play_info': {},
                'player': player_name,
            }
        )
        return None

    @staticmethod
    def _get_tmdb_movie_now_playing(current_video, tmdb_id):
        if not SETTINGS.import_tmdbhelper:
            return None

        from tmdb_helper import get_item_details, get_next_movie

        current_details = get_item_details('movie', tmdb_id)
        if not current_details:
            return None

        next_movie = get_next_movie(tmdb_id)
        if not next_movie:
            return None

        current_infolabels = getattr(current_details, 'infolabels', {}) or {}
        current_art = getattr(current_details, 'art', {}) or {}
        next_infolabels = getattr(next_movie, 'infolabels', {}) or {}
        next_art = getattr(next_movie, 'art', {}) or {}
        next_tmdb_id = getattr(next_movie, 'unique_ids', {}).get('tmdb', '')

        play_url = 'plugin://{0}/?info=play&tmdb_type=movie&tmdb_id={1}'.format(
            constants.TMDBH_ADDON_ID, next_tmdb_id
        )

        upnext.send_signal(
            sender='UpNext.TMDBHelper',
            upnext_info={
                'current_video': dict(
                    current_infolabels,
                    tmdb_id=tmdb_id,
                    art=current_art,
                    mediatype='movie',
                ),
                'next_video': dict(
                    next_infolabels,
                    tmdb_id=next_tmdb_id,
                    art=next_art,
                    mediatype='movie',
                ),
                'play_url': play_url,
            }
        )

        return dict(
            current_video,
            type='movie',
            title=current_infolabels.get('title', current_video.get('label', '')),
            set=current_infolabels.get('set', ''),
            setid=current_video.get('setid', -1),
            tmdb_id=tmdb_id,
        )

    def get_plugin_type(self, playlist_next=None):
        if self.data:
            _get = self.data.get
            plugin_type = constants.PLUGIN_DATA_ERROR
            if _get('play_direct'):
                plugin_type += constants.PLUGIN_DIRECT
            elif playlist_next:
                plugin_type += constants.PLUGIN_PLAYLIST
            if _get('play_url'):
                plugin_type += constants.PLUGIN_PLAY_URL
            elif _get('play_info'):
                plugin_type += constants.PLUGIN_PLAY_INFO
            return plugin_type
        return None

    def set_plugin_data(self, plugin_data):
        if not plugin_data:
            self.data = None
            self.encoding = None
            return

        data, encoding = plugin_data

        # Map to new data structure
        if 'current_episode' in data:
            data['current_video'] = data.pop('current_episode')
        if 'next_video' in data:
            self.log('next video : {0}'.format(data['next_video']))
            data['next_video'] = data.pop('next_video')

        self.data = data
        self.encoding = encoding or 'base64'
