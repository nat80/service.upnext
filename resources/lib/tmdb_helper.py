# -*- coding: utf-8 -*-
# GNU General Public License v2.0 (see COPYING or https://www.gnu.org/licenses/gpl-2.0.txt)

from __future__ import absolute_import, division, unicode_literals

import sys
from importlib import import_module


try:
    from urllib.parse import urlencode
except ImportError:
    from urllib import urlencode

import constants
import utils
from settings import SETTINGS


def log(msg, level=utils.LOGDEBUG):
    """Log wrapper"""

    utils.log(msg, name=__name__, level=level)


TMDB_API_KEY = 'b5004196f5004839a7b0a89e623d3bd2'


def _apply_custom_api_key():
    """Apply custom TMDB API key"""
    try:
        # Remove only cached TMDB Helper lib modules, not the plugin itself
        modules_to_remove = [m for m in sys.modules.keys() if 'tmdbhelper.lib' in m]
        for module_name in modules_to_remove:
            del sys.modules[module_name]
        
        # Import and set the API key
        import tmdbhelper.lib.api.api_keys.tmdb as tmdb_keys
        tmdb_keys.API_KEY = TMDB_API_KEY
    except Exception as e:
        log(f'Failed to apply API key')


class Import(object):  # pylint: disable=too-few-public-methods
    def __new__(cls, name, mod_attrs=None):
        try:
            try:
                module = sys.modules[name]
            except KeyError:
                module = import_module(name)
            if mod_attrs is not None:
                module.__dict__.update(mod_attrs)
        except ImportError:
            from traceback import format_exc

            log('ImportError: {0}'.format(format_exc()))
            module = None
        return module


class ObjectImport(Import):  # pylint: disable=too-few-public-methods
    # pylint: disable-next=arguments-differ
    def __new__(cls, name, obj_name, obj_attrs=None, **kwargs):
        module = super(ObjectImport, cls).__new__(cls, name, **kwargs)
        try:
            imported_obj = getattr(module, obj_name)
            if obj_attrs is not None:
                imported_obj.__dict__.update(obj_attrs)
        except AttributeError:
            from traceback import format_exc

            log('ImportError: {0}'.format(format_exc()))
            imported_obj = None
        return imported_obj


class ClassImport(ObjectImport):  # pylint: disable=too-few-public-methods
    def __new__(cls, name, obj_name, obj_attrs=None, **kwargs):
        def substitute(cls, func=None, default_return=None):
            def wrapper(*_args, **_kwargs):
                return default_return

            def decorator(func):
                if cls._initialised:
                    return func
                from functools import wraps

                return wraps(func)(wrapper)

            if func:
                return decorator(func)
            return decorator

        def is_initialised(cls):
            return cls._initialised

        if 'obj' in kwargs:  # pylint: disable=consider-using-get
            imported_obj = kwargs['obj']
        else:
            imported_obj = super(ClassImport, cls).__new__(
                cls, name, obj_name, **kwargs
            )
        if imported_obj:
            initialised = True
        else:
            imported_obj = object
            initialised = False

        _dict = {
            '_initialised': initialised,
            '_substitute': classmethod(substitute),
            'is_initialised': classmethod(is_initialised),
        }
        if obj_attrs is not None:
            _dict.update(obj_attrs)
        return type(obj_name, (imported_obj,), _dict)


# pylint: disable=invalid-name
_TMDb = ClassImport(
    'tmdbhelper_lib.api.tmdb.api',
    'TMDb',
)

class TMDb(_TMDb):  # pylint: disable=inherit-non-class,too-few-public-methods
    def __init__(self, *args, **kwargs):
        kwargs['api_key'] = TMDB_API_KEY
        super(TMDb, self).__init__(*args, **kwargs)
        try:
            self.tmdb_database.tmdb_api = self
        except AttributeError:
            self.tmdb_database = None

    def get_tmdb_id(self, *args, **kwargs):
        if self.tmdb_database:
            return self.tmdb_database.get_tmdb_id(*args, **kwargs)
        return super(TMDb, self).get_tmdb_id(*args, **kwargs)


_Player = ObjectImport(
    'tmdbhelper_lib.player.dialog.player',
    'Player',
)

_PlayerDetails = ClassImport(
    'tmdbhelper_lib.player.dialog.details',
    'PlayerDetails',
)

_PlayerNextEpisodes = ClassImport(
    'tmdbhelper_lib.player.action.episodes',
    'PlayerNextEpisodes',
)


def get_item_details(tmdb_type, tmdb_id, season=None, episode=None):
    try:
        _apply_custom_api_key()
        details = _PlayerDetails(tmdb_type, tmdb_id, season, episode)
        if details and hasattr(details, 'details'):
            return details.details
        return None
    except Exception as e:
        log(f"Failed to get item details: {e}")
        return None


def get_next_episodes(tmdb_id, season, episode, player=None):
    try:
        _apply_custom_api_key()
        log(f"Fetching next episodes for TMDb ID: {tmdb_id}, Season: {season}, Episode: {episode}")
        player_next = _PlayerNextEpisodes(tmdb_id, season, episode, player)
        items = player_next.items

        if not items:
            log("No next episodes found in current season, checking next season.")
            player_next = _PlayerNextEpisodes(tmdb_id, season + 1, 0, player)
            items = player_next.items

        if not items:
            return None

        if hasattr(items[0], 'is_unaired') and items[0].is_unaired:
            return None

        if SETTINGS.unwatched_only:
            items = [
                item for idx, item in enumerate(items)
                if not idx or item.infolabels.get(
                    'playcount', constants.UNDEFINED
                ) < 1
            ]

        return items
    except Exception as e:
        log(f"Failed to get next episodes : {e}")
        return None


def Players(**kwargs):
    """Factory function to create appropriate Player instance"""
    return _Player(**kwargs)


def generate_player_data(upnext_data, player=None, play_url=False):
    video_details = upnext_data.get('next_video')
    if video_details:
        offset = 0
        if play_url:
            play_url = ''.join((
                'plugin://',
                constants.ADDON_ID,
                '/play_plugin?{0}',
            ))
    else:
        video_details = upnext_data.get('current_video')
        offset = 1
        if play_url:
            play_url = ''.join((
                'plugin://',
                constants.TMDBH_ADDON_ID,
                '/?{0}',
            ))

    tmdb_id = video_details.get('tmdb_id', '')
    media_type = video_details.get('mediatype', '')

    if media_type == 'movie':
        data = {
            'info': 'play',
            'tmdb_type': 'movie',
            'tmdb_id': tmdb_id,
            'ignore_default': False,
            'islocal': False,
            'player': player,
            'mode': 'play',
        }
    else:
        title = video_details.get('showtitle', '')
        season = utils.get_int(video_details, 'season')
        episode = utils.get_int(video_details, 'episode') + offset

        data = {
            'info': 'play',
            'query': title,
            'tmdb_type': 'tv',
            'tmdb_id': tmdb_id,
            'season': season,
            'episode': episode,
            'ignore_default': False,
            'islocal': False,
            'player': player,
            'mode': 'play',
        }

    if play_url:
        return play_url.format(urlencode(data))
    return data


def get_next_movie(tmdb_id):
    _apply_custom_api_key()

    IMAGEPATH_ORIGINAL = 'https://image.tmdb.org/t/p/original'
    thumb_base = 'https://image.tmdb.org/t/p/w500'

    # Helper class to create movie details from TMDb API data
    class MovieDetails:
        def __init__(self, data):
            log(f"Creating MovieDetails from API data for TMDb ID: {data.get('id', '')}")
            self.infolabels = {
                'title': data.get('title', ''),
                'plot': data.get('overview', ''),
                'year': int(data.get('release_date', '0000')[:4]) if data.get('release_date') else 0,
                'mediatype': 'movie',
            }
            backdrop = '{0}{1}'.format(IMAGEPATH_ORIGINAL, data.get('backdrop_path', '')) if data.get('backdrop_path') else ''
            self.art = {
                'poster': '{0}{1}'.format(IMAGEPATH_ORIGINAL, data.get('poster_path', '')) if data.get('poster_path') else '',
                'fanart': backdrop,
                'landscape': backdrop,
                'thumb': '{0}{1}'.format(thumb_base, data.get('backdrop_path', '')) if data.get('backdrop_path') else '',
            }
            self.unique_ids = {'tmdb': str(data.get('id', ''))}

    def get_movie_details_or_fallback(next_id, api_data):
        """Get movie details from API or create from fallback data"""
        details = get_item_details('movie', next_id)
        if details:
            log(f"Using movie details for TMDb ID: {next_id}")
            return details
        return MovieDetails(api_data)

    try:
        tmdb = TMDb()
        movie_details = tmdb.get_response_json('movie', tmdb_id)
        if not movie_details:
            log(f"No movie details found for TMDb ID: {tmdb_id}")
            return None

        belongs_to = movie_details.get('belongs_to_collection')
        if belongs_to:
            collection_id = belongs_to.get('id')
            if collection_id:
                collection = tmdb.get_response_json('collection', collection_id)
                if collection:
                    parts = collection.get('parts', [])
                    if parts:
                        def get_year(movie):
                            date = movie.get('release_date', '')
                            if date and len(date) >= 4:
                                try:
                                    return int(date[:4])
                                except (ValueError, TypeError):
                                    pass
                            return 9999
                        parts = sorted(parts, key=get_year)
                        found = False
                        for movie in parts:
                            if str(movie.get('id')) == str(tmdb_id):
                                found = True
                                continue
                            if found:
                                return get_movie_details_or_fallback(movie.get('id'), movie)

        recommendations = tmdb.get_response_json('movie/{0}/recommendations'.format(tmdb_id))
        if recommendations:
            results = recommendations.get('results', [])
            if results:
                next_movie = results[0]
                return get_movie_details_or_fallback(next_movie.get('id'), next_movie)

        return None
    except Exception:
        log(f"Failed to get next movie for TMDb ID: {tmdb_id}")
        return None
